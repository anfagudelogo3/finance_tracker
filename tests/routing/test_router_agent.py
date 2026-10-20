from unittest.mock import MagicMock, patch

import pytest

from finance_tracker.config import (
    MSG_ERROR,
    MSG_OUT_OF_SCOPE,
    MSG_PARTIAL_TIMEOUT,
    MSG_ROUTER_EMPTY,
)
from finance_tracker.core.agent_types import AgentRequest, AgentResponse
from finance_tracker.routing import router_agent


def _request(text="almuerzo 20k y me pagaron 2M", profile=None, conversation=None):
    return AgentRequest(
        user_id=7,
        message_id=42,
        phone="+573001234567",
        text=text,
        media=[],
        message_type="text",
        now="2026-10-18T10:00:00-05:00",
        conversation=conversation or [],
        **({"profile": profile} if profile else {}),
    )


def _tool_use(name, tool_input, block_id="tu_1"):
    block = MagicMock(type="tool_use", id=block_id, input=tool_input)
    block.name = name
    return block


def _response(stop_reason, *blocks):
    return MagicMock(stop_reason=stop_reason, content=list(blocks))


def _stub_handlers(seen):
    def make(agent):
        def handle(request):
            seen.append((agent, request))
            return AgentResponse(ok=True, reply_text=f"✅ {agent}: {request.text}")

        return handle

    return {a: make(a) for a in ("expenses", "incomes", "reporting", "users")}


@pytest.fixture
def claude():
    with patch("finance_tracker.routing.router_agent.client") as mock:
        yield mock


class TestDelegation:
    def test_multi_intent_delegates_to_each_agent_and_sends_their_confirmations(
        self, claude
    ):
        claude.messages.create.side_effect = [
            _response(
                "tool_use",
                _tool_use("delegate_to_expenses", {"task": "almuerzo 20k"}, "tu_1"),
                _tool_use("delegate_to_incomes", {"task": "me pagaron 2M"}, "tu_2"),
            ),
            _response("end_turn"),
        ]
        seen = []

        result = router_agent.route(_request(), _stub_handlers(seen))

        assert result.route == "agents"
        assert result.agents == ["expenses", "incomes"]
        assert (
            result.reply_text
            == "✅ expenses: almuerzo 20k\n\n✅ incomes: me pagaron 2M"
        )

    def test_child_request_keeps_verified_identity_and_profile(self, claude):
        claude.messages.create.side_effect = [
            _response(
                "tool_use", _tool_use("delegate_to_expenses", {"task": "almuerzo 20k"})
            ),
            _response("end_turn"),
        ]
        profile = {
            "type": "business",
            "business": {"industry": "x", "description": "y"},
        }
        parent = _request(profile=profile)
        seen = []

        router_agent.route(parent, _stub_handlers(seen))

        child = seen[0][1]
        assert child.text == "almuerzo 20k"
        assert (child.user_id, child.message_id, child.phone) == (
            7,
            42,
            "+573001234567",
        )
        assert child.profile == profile

    def test_no_tool_lets_the_model_choose_a_user(self):
        for tool in router_agent.TOOLS:
            properties = tool["input_schema"]["properties"]
            assert not any("user" in key or "phone" in key for key in properties), tool[
                "name"
            ]

    def test_router_text_is_ignored_when_agents_replied(self, claude):
        claude.messages.create.side_effect = [
            _response(
                "tool_use", _tool_use("delegate_to_expenses", {"task": "almuerzo 20k"})
            ),
            _response("end_turn", MagicMock(type="text", text="Registré 999.999")),
        ]

        result = router_agent.route(_request(), _stub_handlers([]))

        assert "999.999" not in result.reply_text

    def test_failing_agent_is_reported_and_others_still_reply(self, claude):
        claude.messages.create.side_effect = [
            _response(
                "tool_use",
                _tool_use("delegate_to_expenses", {"task": "almuerzo 20k"}, "tu_1"),
                _tool_use("delegate_to_incomes", {"task": "me pagaron 2M"}, "tu_2"),
            ),
            _response("end_turn"),
        ]
        handlers = _stub_handlers([])
        handlers["incomes"] = MagicMock(side_effect=RuntimeError("db down"))

        result = router_agent.route(_request(), handlers)

        assert result.reply_text.startswith("✅ expenses: almuerzo 20k")
        assert "No pude procesar: me pagaron 2M" in result.reply_text

    def test_delegation_limit(self, claude):
        calls = [
            _tool_use("delegate_to_expenses", {"task": f"gasto {i}"}, f"tu_{i}")
            for i in range(5)
        ]
        claude.messages.create.side_effect = [
            _response("tool_use", *calls),
            _response("end_turn"),
        ]
        seen = []

        router_agent.route(_request(), _stub_handlers(seen))

        assert len(seen) == 3  # ROUTER_MAX_DELEGATIONS


class TestOtherRoutes:
    def test_clarify(self, claude):
        claude.messages.create.side_effect = [
            _response(
                "tool_use",
                _tool_use("ask_clarification", {"question": "¿Gasto o ingreso?"}),
            ),
            _response("end_turn"),
        ]

        result = router_agent.route(_request("nequi 50000"), _stub_handlers([]))

        assert (result.route, result.agents) == ("clarify", [])
        assert result.reply_text == "¿Gasto o ingreso?"

    def test_decline(self, claude):
        claude.messages.create.side_effect = [
            _response(
                "tool_use", _tool_use("decline_out_of_scope", {"reason": "poem"})
            ),
            _response("end_turn"),
        ]

        result = router_agent.route(_request("escríbeme un poema"), _stub_handlers([]))

        assert result.route == "decline"
        assert result.reply_text == MSG_OUT_OF_SCOPE

    def test_smalltalk_uses_router_text(self, claude):
        claude.messages.create.return_value = _response(
            "end_turn", MagicMock(type="text", text="¡Hola! ¿Qué registramos hoy?")
        )

        result = router_agent.route(_request("hola"), _stub_handlers([]))

        assert result.route == "none"
        assert result.reply_text == "¡Hola! ¿Qué registramos hoy?"

    def test_empty_smalltalk_falls_back_to_help(self, claude):
        claude.messages.create.return_value = _response("end_turn")

        assert (
            router_agent.route(_request("hola"), _stub_handlers([])).reply_text
            == MSG_ROUTER_EMPTY
        )

    def test_refusal_declines(self, claude):
        claude.messages.create.return_value = _response("refusal")

        result = router_agent.route(_request(), _stub_handlers([]))

        assert (result.route, result.reply_text) == ("decline", MSG_OUT_OF_SCOPE)


class TestSafeguardReplies:
    def test_safeguard_after_work_reports_what_was_done(self, claude):
        claude.messages.create.return_value = _response(
            "tool_use", _tool_use("delegate_to_expenses", {"task": "almuerzo 20k"})
        )

        result = router_agent.route(_request(), _stub_handlers([]))

        assert result.loop_status == "max_iterations"
        assert result.reply_text.startswith("✅ expenses: almuerzo 20k")
        assert result.reply_text.endswith(MSG_PARTIAL_TIMEOUT)

    def test_safeguard_with_nothing_done_sends_error(self, claude):
        claude.messages.create.return_value = _response("max_tokens")

        assert (
            router_agent.route(_request(), _stub_handlers([])).reply_text == MSG_ERROR
        )


class TestPrompt:
    def test_business_profile_reaches_system_prompt(self, claude):
        claude.messages.create.return_value = _response("end_turn")
        profile = {
            "type": "business",
            "business": {
                "industry": "restaurante",
                "description": "Vende almuerzos ejecutivos.",
            },
        }

        router_agent.route(_request(profile=profile), _stub_handlers([]))

        system = claude.messages.create.call_args.kwargs["system"]
        assert "restaurante" in system and "Vende almuerzos ejecutivos." in system

    def test_message_and_history_sent_as_tagged_data(self, claude):
        claude.messages.create.return_value = _response("end_turn")
        history = [
            {"role": "assistant", "content": "¿Gasto o ingreso?"},
        ]

        router_agent.route(
            _request("fue un gasto", conversation=history), _stub_handlers([])
        )

        messages = claude.messages.create.call_args.kwargs["messages"]
        assert messages[0]["role"] == "user"
        assert "assistant: ¿Gasto o ingreso?" in messages[0]["content"]
        assert "<message>fue un gasto</message>" in messages[0]["content"]

    def test_long_input_is_truncated(self, claude):
        claude.messages.create.return_value = _response("end_turn")

        router_agent.route(_request("a" * 5000), _stub_handlers([]))

        content = claude.messages.create.call_args.kwargs["messages"][0]["content"]
        assert len(content) < 1100


class TestHandle:
    def test_handle_wraps_route_result(self, claude):
        claude.messages.create.side_effect = [
            _response(
                "tool_use", _tool_use("delegate_to_reporting", {"task": "resumen"})
            ),
            _response("end_turn"),
        ]
        with patch("finance_tracker.routing.router_agent.reporting") as reporting:
            reporting.handle.return_value = AgentResponse(
                ok=True, reply_text="📊 Resumen"
            )

            response = router_agent.handle(_request("en qué se me va la plata"))

        assert response.reply_text == "📊 Resumen"
        assert response.data == [{"agent": "reporting", "task": "resumen", "ok": True}]


class TestUsersDelegation:
    def test_settings_request_goes_to_the_users_agent(self, claude):
        claude.messages.create.side_effect = [
            _response(
                "tool_use",
                _tool_use("delegate_to_users", {"task": "agrega la categoría mascotas"}),
            ),
            _response("end_turn"),
        ]
        seen = []

        result = router_agent.route(_request("agrega la categoría mascotas"), _stub_handlers(seen))

        assert result.agents == ["users"]
        assert seen[0][1].user_id == 7  # identity comes from the request, not the model

    def test_category_is_created_before_recording_in_it(self, claude):
        claude.messages.create.side_effect = [
            _response(
                "tool_use",
                _tool_use("delegate_to_users", {"task": "crea la categoría mascota"}, "tu_1"),
                _tool_use("delegate_to_expenses", {"task": "concentrado 30000 en mascota"}, "tu_2"),
            ),
            _response("end_turn"),
        ]
        seen = []

        router_agent.route(
            _request("crea la categoría mascota y registra 30k de concentrado"),
            _stub_handlers(seen),
        )

        assert [agent for agent, _ in seen] == ["users", "expenses"]

    def test_users_tool_is_offered_to_the_router(self):
        assert "delegate_to_users" in {t["name"] for t in router_agent.TOOLS}


class TestRepliedTo:
    def test_quoted_message_and_its_records_reach_the_router(self):
        from dataclasses import replace

        replied = {
            "role": "assistant", "date": "2026-10-20T12:00:00-05:00", "source": "database",
            "text": "💸 Gasto registrado: COP 25.000 en comida (2026-10-20)",
            "records": [{"kind": "expense", "id": 12, "amount": 25000.0, "currency": "COP",
                         "category": "comida", "date": "2026-10-20", "description": "almuerzo"}],
        }
        content = router_agent._user_content(replace(_request("eran 15000"), replied_to=replied))

        assert '<replied_to role="assistant" date="2026-10-20T12:00:00-05:00">' in content
        assert "record: expense #12 COP 25,000 comida 2026-10-20 almuerzo" in content
        assert content.index("</replied_to>") < content.index("<message>eran 15000</message>")

    def test_no_block_when_not_a_reply(self):
        assert "<replied_to" not in router_agent._user_content(_request("hola"))
