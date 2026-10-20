from urllib.parse import urlencode
from unittest.mock import MagicMock, patch

import pytest

from finance_tracker.handler import _handle_message
from finance_tracker.config import MSG_ERROR


@pytest.fixture(autouse=True)
def _no_hidden_errors(request, caplog):
    """The handler turns any exception into a friendly reply and a 200, so a test
    whose mocks are incomplete can pass through the error path without noticing. Fail
    such tests unless they're marked as expecting the error path."""
    yield
    if request.node.get_closest_marker("expects_error_path"):
        return
    assert "Unhandled error processing message" not in caplog.text, (
        "the handler hit its error path — a collaborator isn't mocked"
    )


@pytest.fixture(autouse=True)
def _no_user_prefs():
    """Every handler test runs as an onboarded personal-profile user unless it
    overrides this."""
    prefs = {"onboarding": {"status": "done"}}
    with patch("finance_tracker.handler.get_user_prefs", return_value=prefs) as mock:
        yield mock


def _make_event(body: str = "almuerzo 32000", message_id: str = "SM123") -> dict:
    form = {
        "From": "whatsapp:+573001234567",
        "Body": body,
        "MessageSid": message_id,
        "NumMedia": "0",
    }
    return {
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"x-twilio-signature": "sig"},
        "body": urlencode(form),
        "isBase64Encoded": False,
    }


def _tool_use_response(key: str, items: list[dict]) -> MagicMock:
    """Claude calling record_<key>."""
    block = MagicMock(type="tool_use", id="tu_1", input={key: items})
    block.name = f"record_{key}"
    return MagicMock(stop_reason="tool_use", content=[block])


def _end_turn() -> MagicMock:
    """Claude ending its turn after the tool results: the loop stops only here."""
    return MagicMock(stop_reason="end_turn", content=[])


class TestExpenseTextPath:
    @patch("finance_tracker.agents.expenses.save_expense")
    @patch("finance_tracker.agents.expenses.get_user_categories")
    @patch("finance_tracker.agents.expenses.client")
    @patch("finance_tracker.handler.send_message")
    @patch("finance_tracker.handler.save_turn")
    @patch("finance_tracker.handler.load_recent_turns")
    @patch("finance_tracker.handler.save_message")
    @patch("finance_tracker.handler.get_or_create_user")
    @patch("finance_tracker.handler.verify_signature")
    def test_text_expense_routes_through_orchestrator_and_persists(
        self,
        mock_verify,
        mock_get_user,
        mock_save_message,
        mock_load_recent_turns,
        mock_save_turn,
        mock_send_message,
        mock_claude_client,
        mock_get_categories,
        mock_save_expense,
    ):
        mock_verify.return_value = True
        mock_get_user.return_value = 7
        mock_save_message.return_value = 1
        mock_load_recent_turns.return_value = []
        mock_get_categories.return_value = ["comida", "otro"]
        mock_send_message.return_value = "SIDxxx"
        mock_save_expense.return_value = 101
        mock_claude_client.messages.create.side_effect = [_tool_use_response(
            "expenses",
            [
                {
                    "amount": 32000,
                    "currency": "COP",
                    "category": "comida",
                    "payment_method": None,
                    "merchant": None,
                    "description": "almuerzo",
                }
            ],
        ), _end_turn()]

        result = _handle_message(_make_event("almuerzo 32000"))

        assert result["statusCode"] == 200
        mock_save_expense.assert_called_once()
        mock_send_message.assert_called_once()
        assert mock_save_turn.call_count == 2  # inbound + outbound


class TestIncomeTextPath:
    @patch("finance_tracker.agents.incomes.save_income")
    @patch("finance_tracker.agents.incomes.get_user_categories")
    @patch("finance_tracker.agents.incomes.client")
    @patch("finance_tracker.handler.send_message")
    @patch("finance_tracker.handler.save_turn")
    @patch("finance_tracker.handler.load_recent_turns")
    @patch("finance_tracker.handler.save_message")
    @patch("finance_tracker.handler.get_or_create_user")
    @patch("finance_tracker.handler.verify_signature")
    def test_income_text_routes_through_orchestrator_and_persists(
        self,
        mock_verify,
        mock_get_user,
        mock_save_message,
        mock_load_recent_turns,
        mock_save_turn,
        mock_send_message,
        mock_claude_client,
        mock_get_categories,
        mock_save_income,
    ):
        mock_verify.return_value = True
        mock_get_user.return_value = 7
        mock_save_message.return_value = 1
        mock_load_recent_turns.return_value = []
        mock_get_categories.return_value = ["salario", "otro"]
        mock_send_message.return_value = "SIDxxx"
        mock_save_income.return_value = 201
        mock_claude_client.messages.create.side_effect = [_tool_use_response(
            "incomes",
            [
                {
                    "amount": 2000000,
                    "currency": "COP",
                    "category": "salario",
                    "description": "pago de salario",
                }
            ],
        ), _end_turn()]

        result = _handle_message(_make_event("me pagaron 2000000 de salario"))

        assert result["statusCode"] == 200
        mock_save_income.assert_called_once()
        mock_send_message.assert_called_once()
        assert mock_save_turn.call_count == 2  # inbound + outbound


class TestExcelBypassesOrchestrator:
    @patch("finance_tracker.handler.send_message")
    @patch("finance_tracker.handler.load_recent_turns", return_value=[])
    @patch("finance_tracker.handler.orchestrator")
    @patch("finance_tracker.handler.send_document")
    @patch("finance_tracker.handler.upload_and_sign")
    @patch("finance_tracker.handler.create_excel_bytes")
    @patch("finance_tracker.handler.get_incomes", return_value=[])
    @patch("finance_tracker.handler.get_expenses")
    @patch("finance_tracker.handler.parse_report_request")
    @patch("finance_tracker.handler.save_turn")
    @patch("finance_tracker.handler.save_message")
    @patch("finance_tracker.handler.get_or_create_user")
    @patch("finance_tracker.handler.verify_signature")
    def test_excel_request_does_not_reach_orchestrator(
        self,
        mock_verify,
        mock_get_user,
        mock_save_message,
        mock_save_turn,
        mock_parse_report,
        mock_get_expenses,
        mock_get_incomes,
        mock_create_excel,
        mock_upload,
        mock_send_document,
        mock_orchestrator,
        mock_load_recent_turns,
        mock_send_message,
    ):
        mock_verify.return_value = True
        mock_get_user.return_value = 7
        mock_save_message.return_value = 1
        mock_parse_report.return_value = {
            "min_date": "2026-08-01",
            "max_date": "2026-08-14",
        }
        mock_get_expenses.return_value = []
        mock_create_excel.return_value = b"xlsx-bytes"
        mock_upload.return_value = "https://example.com/presigned"
        mock_send_document.return_value = "SIDxxx"

        result = _handle_message(_make_event("exportame el excel de mis gastos"))

        assert result["statusCode"] == 200
        mock_orchestrator.handle_message.assert_not_called()
        mock_send_message.assert_not_called()  # no error reply: the Excel path really ran
        mock_get_incomes.assert_called_once_with(7, "2026-08-01", "2026-08-14")
        mock_create_excel.assert_called_once_with([], [])
        assert mock_send_document.call_args.args[2] == "movimientos_2026-08-01_2026-08-14.xlsx"


class TestReportTextPath:
    """Unlike excel (unchanged, still bypasses the orchestrator), report requests now
    route through orchestrator.handle_message → reporting_agent, per Phase 3. This
    exercises the real deterministic date-range path end to end — "resumen de mis
    gastos" needs no Claude call at all."""

    @patch("finance_tracker.agents.reporting.get_incomes", return_value=[])
    @patch("finance_tracker.agents.reporting.get_expenses")
    @patch("finance_tracker.handler.send_message")
    @patch("finance_tracker.handler.save_turn")
    @patch("finance_tracker.handler.load_recent_turns")
    @patch("finance_tracker.handler.save_message")
    @patch("finance_tracker.handler.get_or_create_user")
    @patch("finance_tracker.handler.verify_signature")
    def test_report_text_routes_through_orchestrator_to_reporting_agent(
        self,
        mock_verify,
        mock_get_user,
        mock_save_message,
        mock_load_recent_turns,
        mock_save_turn,
        mock_send_message,
        mock_get_expenses,
        mock_get_incomes,
    ):
        mock_verify.return_value = True
        mock_get_user.return_value = 7
        mock_save_message.return_value = 1
        mock_load_recent_turns.return_value = []
        mock_get_expenses.return_value = [
            {"amount": 32000, "currency": "COP", "category": "comida"},
        ]
        mock_send_message.return_value = "SIDxxx"

        result = _handle_message(_make_event("resumen de mis gastos"))

        assert result["statusCode"] == 200
        mock_get_expenses.assert_called_once()
        mock_get_incomes.assert_called_once()
        assert mock_send_message.call_args.args[1].startswith("📊 Resumen")
        mock_send_message.assert_called_once()
        assert mock_save_turn.call_count == 2  # inbound + outbound


@pytest.mark.expects_error_path
class TestErrorContract:
    @patch("finance_tracker.agents.expenses.get_user_categories")
    @patch("finance_tracker.agents.expenses.client")
    @patch("finance_tracker.handler.send_message")
    @patch("finance_tracker.handler.save_turn")
    @patch("finance_tracker.handler.load_recent_turns")
    @patch("finance_tracker.handler.save_message")
    @patch("finance_tracker.handler.get_or_create_user")
    @patch("finance_tracker.handler.verify_signature")
    def test_unhandled_exception_sends_msg_error_and_returns_200(
        self,
        mock_verify,
        mock_get_user,
        mock_save_message,
        mock_load_recent_turns,
        mock_save_turn,
        mock_send_message,
        mock_claude_client,
        mock_get_categories,
    ):
        mock_verify.return_value = True
        mock_get_user.return_value = 7
        mock_save_message.return_value = 1
        mock_load_recent_turns.return_value = []
        mock_get_categories.return_value = ["comida"]
        mock_claude_client.messages.create.side_effect = RuntimeError("boom")

        result = _handle_message(_make_event("almuerzo 32000"))

        assert result["statusCode"] == 200
        mock_send_message.assert_called_once_with("+573001234567", MSG_ERROR)


class TestConversationHistoryWiring:
    """Direct test of the Phase 4 wiring itself, independent of any live Claude
    behavior — asserts load_recent_turns' output reaches AgentRequest.conversation."""

    @patch("finance_tracker.handler.send_message")
    @patch("finance_tracker.handler.orchestrator")
    @patch("finance_tracker.handler.save_turn")
    @patch("finance_tracker.handler.load_recent_turns")
    @patch("finance_tracker.handler.save_message")
    @patch("finance_tracker.handler.get_or_create_user")
    @patch("finance_tracker.handler.verify_signature")
    def test_agent_request_carries_loaded_conversation_history(
        self,
        mock_verify,
        mock_get_user,
        mock_save_message,
        mock_load_recent_turns,
        mock_save_turn,
        mock_orchestrator,
        mock_send_message,
    ):
        mock_verify.return_value = True
        mock_get_user.return_value = 7
        mock_save_message.return_value = 1
        history = [
            {"role": "user", "content": "cuánto gasté esta semana"},
            {"role": "assistant", "content": "📊 Resumen 22 jun – 25 jun 2026: ..."},
        ]
        mock_load_recent_turns.return_value = history
        mock_orchestrator.handle_message.return_value = MagicMock(
            reply_text="ok", reply_attachment=None
        )

        _handle_message(_make_event("¿y la anterior?"))

        mock_load_recent_turns.assert_called_once_with(7)
        agent_request = mock_orchestrator.handle_message.call_args[0][0]
        assert agent_request.conversation == history


class TestDuplicateMessage:
    @patch("finance_tracker.handler.orchestrator")
    @patch("finance_tracker.handler.save_message")
    @patch("finance_tracker.handler.get_or_create_user")
    @patch("finance_tracker.handler.verify_signature")
    def test_duplicate_message_short_circuits_before_orchestrator(
        self,
        mock_verify,
        mock_get_user,
        mock_save_message,
        mock_orchestrator,
    ):
        mock_verify.return_value = True
        mock_get_user.return_value = 7
        mock_save_message.return_value = None  # ON CONFLICT DO NOTHING → duplicate

        result = _handle_message(_make_event("almuerzo 32000"))

        assert result["statusCode"] == 200
        mock_orchestrator.handle_message.assert_not_called()


class TestProfileWiring:
    @patch("finance_tracker.handler.orchestrator")
    @patch("finance_tracker.handler.send_message")
    @patch("finance_tracker.handler.save_turn")
    @patch("finance_tracker.handler.load_recent_turns")
    @patch("finance_tracker.handler.save_message")
    @patch("finance_tracker.handler.get_or_create_user")
    @patch("finance_tracker.handler.verify_signature")
    def test_agent_request_carries_latest_profile(
        self,
        mock_verify,
        mock_get_user,
        mock_save_message,
        mock_load_recent_turns,
        mock_save_turn,
        mock_send_message,
        mock_orchestrator,
        _no_user_prefs,
    ):
        mock_verify.return_value = True
        mock_get_user.return_value = 7
        mock_save_message.return_value = 1
        mock_load_recent_turns.return_value = []
        business = {
            "type": "business",
            "business": {"industry": "restaurante", "description": "Vende almuerzos"},
        }
        _no_user_prefs.return_value = {"profile": business, "onboarding": {"status": "done"}}
        mock_orchestrator.handle_message.return_value = MagicMock(reply_text="ok")

        # A report-looking message from a business user must not take the report
        # fast path in handler.py — it reaches the orchestrator (and the router).
        _handle_message(_make_event("cuánto vendí esta semana"))

        _no_user_prefs.assert_called_once_with(7)
        agent_request = mock_orchestrator.handle_message.call_args[0][0]
        assert agent_request.profile == business

    @patch("finance_tracker.handler.orchestrator")
    @patch("finance_tracker.handler.send_message")
    @patch("finance_tracker.handler.save_turn")
    @patch("finance_tracker.handler.load_recent_turns")
    @patch("finance_tracker.handler.save_message")
    @patch("finance_tracker.handler.get_or_create_user")
    @patch("finance_tracker.handler.verify_signature")
    def test_missing_profile_defaults_to_personal(
        self,
        mock_verify,
        mock_get_user,
        mock_save_message,
        mock_load_recent_turns,
        mock_save_turn,
        mock_send_message,
        mock_orchestrator,
    ):
        mock_verify.return_value = True
        mock_get_user.return_value = 7
        mock_save_message.return_value = 1
        mock_load_recent_turns.return_value = []
        mock_orchestrator.handle_message.return_value = MagicMock(reply_text="ok")

        _handle_message(_make_event("nequi 50000"))

        agent_request = mock_orchestrator.handle_message.call_args[0][0]
        assert agent_request.profile == {"type": "personal"}


_PIPELINE = [
    "finance_tracker.handler.verify_signature",
    "finance_tracker.handler.get_or_create_user",
    "finance_tracker.handler.save_message",
    "finance_tracker.handler.load_recent_turns",
    "finance_tracker.handler.save_turn",
    "finance_tracker.handler.send_message",
    "finance_tracker.handler.orchestrator",
    "finance_tracker.handler.onboarding",
]


@pytest.fixture
def pipeline():
    """The handler's collaborators, mocked; onboarding is replaced as a whole."""
    from contextlib import ExitStack

    with ExitStack() as stack:
        mocks = {path.rsplit(".", 1)[1]: stack.enter_context(patch(path)) for path in _PIPELINE}
        mocks["verify_signature"].return_value = True
        mocks["get_or_create_user"].return_value = 7
        mocks["save_message"].return_value = 55
        mocks["load_recent_turns"].return_value = []
        mocks["orchestrator"].handle_message.return_value = MagicMock(reply_text="✅ ok")
        yield mocks


class TestOnboardingWiring:
    def test_onboarding_replies_block_routing(self, pipeline):
        from finance_tracker.agents.onboarding import OnboardingOutcome

        pipeline["onboarding"].handle.return_value = OnboardingOutcome(
            replies=["bienvenida", "pregunta"], prefs={}
        )

        _handle_message(_make_event("almuerzo 20000"))

        sent = [c.args[1] for c in pipeline["send_message"].call_args_list]
        assert sent == ["bienvenida", "pregunta"]
        pipeline["orchestrator"].handle_message.assert_not_called()

    def test_held_message_is_replayed_with_its_own_id_and_timestamp(self, pipeline):
        from finance_tracker.agents.onboarding import OnboardingOutcome

        held = {"text": "almuerzo 20000", "message_type": "text", "message_id": 41,
                "now": "2026-10-19T22:00:00-05:00"}
        prefs = {"profile": {"type": "personal"}, "default_currency": "USD",
                 "onboarding": {"status": "done"}}
        pipeline["onboarding"].handle.return_value = OnboardingOutcome(
            replies=[], prefs=prefs, replay=held
        )

        _handle_message(_make_event("sí"))

        request = pipeline["orchestrator"].handle_message.call_args.args[0]
        assert request.text == "almuerzo 20000"
        assert request.message_id == 41
        assert request.now == "2026-10-19T22:00:00-05:00"
        assert request.default_currency == "USD"

    def test_not_handled_routes_normally_with_bogota_time(self, pipeline):
        from finance_tracker.agents.onboarding import OnboardingOutcome

        pipeline["onboarding"].handle.return_value = OnboardingOutcome(
            replies=[], prefs={"onboarding": {"status": "done"}}, handled=False
        )

        _handle_message(_make_event("nequi 50000"))

        request = pipeline["orchestrator"].handle_message.call_args.args[0]
        assert request.message_id == 55
        assert request.now.endswith("-05:00")  # America/Bogota

    @patch("finance_tracker.handler.update_message_transcript")
    @patch("finance_tracker.handler.transcribe_audio", return_value="me pagaron 2 millones")
    @patch("finance_tracker.handler.store_all_media")
    def test_voice_note_reaches_onboarding_as_its_transcript(
        self, mock_store, mock_transcribe, mock_update, pipeline
    ):
        from finance_tracker.agents.onboarding import OnboardingOutcome

        mock_store.return_value = [
            {"bytes": b"x", "ext": "ogg", "content_type": "audio/ogg", "s3_prefix": "audio"}
        ]
        pipeline["onboarding"].handle.return_value = OnboardingOutcome(replies=["ok"], prefs={})
        event = _make_event("")
        event["body"] += "&NumMedia=1&MediaUrl0=https%3A%2F%2Fx&MediaContentType0=audio%2Fogg"

        with patch("finance_tracker.handler.extract_message") as mock_extract:
            mock_extract.return_value = {
                "phone": "+573001234567", "message_id": "SM9", "text": "",
                "media": [{"s3_prefix": "audio"}],
            }
            _handle_message(event)

        args = pipeline["onboarding"].handle.call_args.args
        assert args[2] == "me pagaron 2 millones" and args[3] == "audio"


class TestPendingConfirmationWiring:
    def test_confirmation_reply_is_sent_and_nothing_is_routed(self, pipeline):
        from finance_tracker.agents.onboarding import OnboardingOutcome

        prefs = {"onboarding": {"status": "done"}, "pending_action": {"tool": "x"}}
        pipeline["onboarding"].handle.return_value = OnboardingOutcome(
            replies=[], prefs=prefs, handled=False
        )
        with patch(
            "finance_tracker.handler.pending.resolve", return_value=("✅ hecho", {})
        ) as resolve:
            _handle_message(_make_event("sí"))

        assert resolve.call_args.args[:3] == (7, prefs, "sí")
        assert resolve.call_args.args[4] == 55  # the confirming message causes the change
        assert [c.args[1] for c in pipeline["send_message"].call_args_list] == ["✅ hecho"]
        pipeline["orchestrator"].handle_message.assert_not_called()

    def test_cancelled_confirmation_routes_the_message(self, pipeline):
        from finance_tracker.agents.onboarding import OnboardingOutcome

        pipeline["onboarding"].handle.return_value = OnboardingOutcome(
            replies=[], prefs={"onboarding": {"status": "done"}}, handled=False
        )
        with patch(
            "finance_tracker.handler.pending.resolve",
            return_value=(None, {"default_currency": "EUR"}),
        ):
            _handle_message(_make_event("uber 15000"))

        request = pipeline["orchestrator"].handle_message.call_args.args[0]
        assert request.default_currency == "EUR"  # the prefs returned by resolve_pending


class TestReplyContextWiring:
    def _not_onboarding(self, pipeline):
        from finance_tracker.agents.onboarding import OnboardingOutcome

        pipeline["onboarding"].handle.return_value = OnboardingOutcome(
            replies=[], prefs={"onboarding": {"status": "done"}}, handled=False
        )

    def test_swipe_reply_context_reaches_the_agents(self, pipeline):
        self._not_onboarding(pipeline)
        replied = {"role": "assistant", "text": "💸 Gasto…", "date": None, "records": [], "source": "database"}
        event = _make_event("eran 15000")
        event["body"] += "&OriginalRepliedMessageSid=SMbot&OriginalRepliedMessageSender=whatsapp%3A%2B14155238886"

        with patch("finance_tracker.handler.reply_context.resolve", return_value=replied) as resolve:
            _handle_message(event)

        assert resolve.call_args.args == (7, "+573001234567", "SMbot")
        request = pipeline["orchestrator"].handle_message.call_args.args[0]
        assert request.replied_to == replied

    def test_outbound_turn_stores_sid_and_the_message_it_answers(self, pipeline):
        self._not_onboarding(pipeline)
        pipeline["send_message"].return_value = "SMout"

        _handle_message(_make_event("nequi 50000"))

        inbound, outbound = pipeline["save_turn"].call_args_list
        assert inbound.kwargs == {"message_id": 55}
        assert outbound.args[1] == "assistant"
        assert outbound.kwargs == {"external_id": "SMout", "message_id": 55}
