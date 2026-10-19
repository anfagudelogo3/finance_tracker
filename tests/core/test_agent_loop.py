from unittest.mock import MagicMock

from finance_tracker.core import agent_loop
from finance_tracker.core.agent_loop import run_tool_loop


def _tool_use(name: str, tool_input: dict, block_id: str = "tu_1") -> MagicMock:
    block = MagicMock(type="tool_use", id=block_id, input=tool_input)
    block.name = name  # `name` is special on MagicMock, set it explicitly
    return block


def _text(text: str) -> MagicMock:
    return MagicMock(type="text", text=text)


def _response(stop_reason: str, *blocks) -> MagicMock:
    return MagicMock(stop_reason=stop_reason, content=list(blocks))


def _run(client, execute_tool=None, clock=None, max_iterations=6, timeout=12):
    return run_tool_loop(
        client=client,
        model="claude-haiku-5-5",
        system="sys",
        tools=[],
        messages=[{"role": "user", "content": "hola"}],
        execute_tool=execute_tool or (lambda name, tool_input: "ok"),
        max_iterations=max_iterations,
        timeout_seconds=timeout,
        max_tokens=2048,
        **({"clock": clock} if clock else {}),
    )


class TestStopReasons:
    def test_runs_tools_until_end_turn(self):
        client = MagicMock()
        client.messages.create.side_effect = [
            _response("tool_use", _tool_use("a", {"x": 1}, "tu_1")),
            _response("tool_use", _tool_use("b", {"y": 2}, "tu_2")),
            _response("end_turn", _text("listo")),
        ]
        calls = []

        outcome = _run(client, lambda name, tool_input: calls.append(name) or "done")

        assert outcome.status == agent_loop.END_TURN
        assert outcome.final_text == "listo"
        assert outcome.iterations == 3
        assert calls == ["a", "b"]

    def test_all_tool_results_of_a_turn_go_back_in_one_user_message(self):
        client = MagicMock()
        first = _response(
            "tool_use", _tool_use("a", {}, "tu_1"), _tool_use("b", {}, "tu_2")
        )
        client.messages.create.side_effect = [first, _response("end_turn")]

        _run(client)

        messages = client.messages.create.call_args_list[1].kwargs["messages"]
        assert messages[1] == {"role": "assistant", "content": first.content}
        results = messages[2]["content"]
        assert messages[2]["role"] == "user"
        assert [r["tool_use_id"] for r in results] == ["tu_1", "tu_2"]

    def test_max_tokens_never_executes_the_truncated_tool_use(self):
        client = MagicMock()
        client.messages.create.return_value = _response(
            "max_tokens", _tool_use("a", {"partial": True})
        )
        execute = MagicMock()

        outcome = _run(client, execute)

        assert outcome.status == agent_loop.MAX_TOKENS
        execute.assert_not_called()

    def test_refusal(self):
        client = MagicMock()
        client.messages.create.return_value = _response("refusal")

        assert _run(client).status == agent_loop.REFUSAL

    def test_unexpected_stop_reason(self):
        client = MagicMock()
        client.messages.create.return_value = _response("pause_turn")

        assert _run(client).status == "unexpected:pause_turn"


class TestSafeguards:
    def test_max_iterations_is_a_safeguard_and_keeps_executed_calls(self):
        client = MagicMock()
        client.messages.create.return_value = _response("tool_use", _tool_use("a", {}))

        outcome = _run(client, max_iterations=3)

        assert outcome.status == agent_loop.MAX_ITERATIONS
        assert client.messages.create.call_count == 3
        assert [c.name for c in outcome.tool_calls] == ["a", "a", "a"]

    def test_timeout_checked_before_each_call(self):
        client = MagicMock()
        client.messages.create.return_value = _response("tool_use", _tool_use("a", {}))
        times = iter([0.0, 0.0, 5.0, 20.0])  # start, iter 1, iter 2, iter 3 (> 12 s)

        outcome = _run(client, clock=lambda: next(times))

        assert outcome.status == agent_loop.TIMEOUT
        assert client.messages.create.call_count == 2

    def test_tool_exception_becomes_is_error_result_and_loop_continues(self):
        client = MagicMock()
        client.messages.create.side_effect = [
            _response("tool_use", _tool_use("a", {})),
            _response("end_turn", _text("ok")),
        ]

        def boom(name, tool_input):
            raise RuntimeError("db down")

        outcome = _run(client, boom)

        assert outcome.status == agent_loop.END_TURN
        assert outcome.tool_calls[0].is_error is True
        result = client.messages.create.call_args_list[1].kwargs["messages"][2][
            "content"
        ][0]
        assert result["is_error"] is True
        assert "db down" in result["content"]
