import asyncio
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from benchmarks.deepswe.baseline import BitfunBaseline
from benchmarks.deepswe.model_transport import ModelRelay, RelayHandler, response_error
from benchmarks.deepswe.prompts import actor_prompt, verifier_prompt
from benchmarks.deepswe.session import BitfunSession
from benchmarks.deepswe.work_budget import WorkBudget
from core.actor import ACTOR_METHOD
from pier.models.agent.context import AgentContext
from pier.trial.execution import AgentTimeoutError, TrialExecution


@pytest.fixture
def model():
    return {
        "ai": {
            "models": [
                {
                    "id": "test",
                    "base_url": "http://model.invalid/v1",
                    "api_key": "${TEST_KEY}",
                }
            ]
        }
    }


def event(kind, at, request="one", **extra):
    return dict(event=kind, at=at, request_id=request, **extra)


def test_no_time_disclosure_or_submission_pressure_in_either_role():
    prompts = [
        actor_prompt("task", 1, "a"),
        verifier_prompt("task", SimpleNamespace(candidate_id="candidate"), 1, "v"),
    ]
    for prompt in prompts:
        context = json.loads(prompt.splitlines()[0].removeprefix("BASELINE_CONTEXT: "))
        assert {"remaining_task_seconds", "task_deadline_utc"}.isdisjoint(context)
        for phrase in (
            "seconds",
            "deadline",
            "wall time",
            "as soon as",
            "early concrete",
            "leave time",
            "Check the clock",
            "10200",
            "10800",
            "remaining_task_seconds",
            "task_deadline_utc",
        ):
            assert phrase not in prompt
        # The reference's own BANK PARTIAL PROGRESS principle mentions a generic
        # budget. Preserve that doctrine; forbid any added timing/pressure text.
        reference_partial_progress = ACTOR_METHOD.split(
            "7. BANK PARTIAL PROGRESS.", 1
        )[1].split("8. COMPUTE YOUR DECISIONS.", 1)[0]
        assert "budget" not in prompt.replace(reference_partial_progress, "")


def test_failed_request_and_backoff_are_free_successful_inference_is_work():
    now = [100.0]
    budget = WorkBudget(60, clock=lambda: now[0])
    budget.feed(event("request_start", 102))
    now[0] = 112
    budget.feed(event("pause_start", 112, started=102))
    assert budget.remaining() == 58
    now[0] = 142
    assert budget.remaining() == 58
    budget.feed(event("pause_end", 142))
    now[0] = 150
    budget.feed(event("request_end", 150))
    assert budget.remaining() == 50  # 2 seconds of work + 8 successful inference
    assert budget.summary()["infra_pause_sec"] == 40
    assert budget.summary()["infra_pauses"] == 1


def test_only_initial_setup_is_outside_work_clock():
    now = [0.0]
    budget = WorkBudget(60, clock=lambda: now[0])
    with budget.initial_setup():
        now[0] = 30
        assert budget.remaining() == 60
    now[0] += 5
    assert budget.remaining() == 55
    assert budget.summary()["initial_setup_sec"] == 30


@pytest.mark.asyncio
async def test_new_overlapping_request_cannot_hide_an_exhausted_work_clock():
    budget = WorkBudget(0.04)
    with pytest.raises(TimeoutError):
        async with budget.limit():
            budget.feed(event("request_start", budget.clock()))
            await asyncio.sleep(0.1)
            budget.feed(event("request_end", budget.clock()))
            budget.feed(event("request_start", budget.clock(), "two"))
            await asyncio.sleep(0.1)


def test_overlapping_failures_are_not_double_credited_and_partial_records_wait(
    tmp_path,
):
    now = [0.0]
    budget = WorkBudget(60, clock=lambda: now[0])
    path = tmp_path / "events.jsonl"
    budget.watch(path)
    records = [
        event("request_start", 0),
        event("pause_start", 4, started=0),
        event("request_start", 2, "two"),
        event("pause_start", 6, "two", started=2),
    ]
    path.write_text("\n".join(json.dumps(e) for e in records) + "\n")
    now[0] = 10
    assert budget.remaining() == 60
    assert budget.remaining() == 60
    with path.open("a") as stream:
        stream.write(json.dumps(event("request_end", 10))[:-1])
    budget.poll()
    assert "one" in budget.requests
    with path.open("a") as stream:
        stream.write("}\n" + json.dumps(event("request_end", 12, "two")) + "\n")
    now[0] = 15
    assert budget.remaining() == 57
    assert budget.paused_seconds == 12


@pytest.mark.asyncio
async def test_watchdog_survives_pause_then_expires_on_work_time(tmp_path):
    budget = WorkBudget(0.1)
    now = budget.clock
    with pytest.raises(TimeoutError):
        async with budget.limit() as timeout:
            budget.feed(event("request_start", now()))
            budget.feed(event("pause_start", now(), started=now()))
            await asyncio.sleep(0.16)
            assert not timeout.expired()
            budget.feed(event("pause_end", now()))
            budget.feed(event("request_end", now()))
            await asyncio.sleep(0.2)
    assert timeout.expired()


@pytest.mark.asyncio
async def test_successful_long_request_still_charges_work_time():
    budget = WorkBudget(0.04)
    with pytest.raises(TimeoutError):
        async with budget.limit():
            budget.feed(event("request_start", budget.clock()))
            await asyncio.sleep(0.1)
            budget.feed(event("request_end", budget.clock()))
            await asyncio.sleep(0.1)
    assert budget.summary()["infra_pause_sec"] == 0
    assert budget.elapsed >= 0.1


@pytest.mark.asyncio
async def test_pier_outer_clock_uses_same_pause_record_and_preserves_native_timeout(
    tmp_path, model
):
    # Call Pier's actual dispatch entry, including the plugin adapter.
    baseline = BitfunBaseline(
        logs_dir=tmp_path, bitfun_config=model, total_timeout_sec=0.08
    )

    async def run(*_):
        budget = baseline._work_budget
        budget.feed(event("request_start", budget.clock()))
        budget.feed(event("pause_start", budget.clock(), started=budget.clock()))
        await asyncio.sleep(0.18)  # exceeds even the outer wall ceiling
        budget.feed(event("pause_end", budget.clock()))
        budget.feed(event("request_end", budget.clock()))
        await asyncio.sleep(0.01)

    baseline.run = run
    execution = TrialExecution(None, baseline, object(), 0.12, 30, 30)
    await execution.run_agent(instruction="task", context=AgentContext())
    assert baseline._work_budget.summary()["infra_pause_sec"] >= 0.18

    native = SimpleNamespace()

    async def native_run(**_):
        await asyncio.sleep(0.18)

    native.run = native_run
    execution.agent = native
    with pytest.raises(AgentTimeoutError):
        await execution.run_agent(instruction="task", context=AgentContext())


def fake_relay(tmp_path, responses):
    # Construct only the HTTP forwarding core; no listening socket or GPU needed.
    relay = object.__new__(ModelRelay)
    relay.events = tmp_path / "transport.jsonl"
    relay.retry_seconds = 0
    relay.retry_backoffs = (0, 0, 0)
    relay.journal_lock = threading.Lock()
    relay.opener = SimpleNamespace(open=Mock(side_effect=responses))
    return relay


def response(body, content_type="application/json"):
    result = io.BytesIO(body)
    result.headers = {"Content-Type": content_type}
    return result


@pytest.mark.parametrize("failure", [429, 500, 502, 503, 401, 403, "connection"])
def test_relay_retries_exact_request_without_new_agent_turn(tmp_path, failure):
    body = b'{"choices":[{"message":{"content":"ok"}}]}'
    error = (
        URLError("temporary disconnect")
        if failure == "connection"
        else HTTPError(
            "http://model.invalid", failure, "unavailable", {}, io.BytesIO(b"")
        )
    )
    relay = fake_relay(tmp_path, [error, error, error, error, response(body)])
    request = Request(
        "http://model.invalid/v1/chat/completions",
        data=b'{"messages":["task"]}',
        headers={"Authorization": "Bearer test-key"},
    )
    status, _, result = relay.fetch("one", request, threading.Event())
    with result:
        assert result.read() == body
    assert status == 200
    assert relay.opener.open.call_count == 5
    assert all(call.args[0] is request for call in relay.opener.open.call_args_list)
    events = [json.loads(line) for line in relay.events.read_text().splitlines()]
    assert [e["event"] for e in events] == ["pause_start", "pause_end"]
    assert "test-key" not in relay.events.read_text()
    assert "messages" not in relay.events.read_text()


def test_recovery_within_short_retry_batch_remains_charged_work(tmp_path):
    relay = fake_relay(tmp_path, [URLError("temporary"), response(b'{"choices":[{}]}')])
    status, _, data = relay.fetch(
        "one", Request("http://model.invalid"), threading.Event()
    )
    data.close()
    assert status == 200
    assert relay.opener.open.call_count == 2
    assert not relay.events.exists()


@pytest.mark.parametrize("status", [400, 404, 422])
def test_fatal_request_errors_do_not_pause_or_loop(tmp_path, status):
    error = HTTPError(
        "http://model.invalid", status, "bad request", {}, io.BytesIO(b"bad")
    )
    relay = fake_relay(tmp_path, [error])
    actual, _, data = relay.fetch(
        "one", Request("http://model.invalid"), threading.Event()
    )
    with data:
        assert data.read() == b"bad"
    assert actual == status
    assert relay.opener.open.call_count == 1
    assert not relay.events.exists()


def test_sse_done_ends_request_without_waiting_for_keep_alive_eof(tmp_path):
    body = b'data: {"choices":[{"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    class KeepAlive(io.BytesIO):
        headers = {"Content-Type": "text/event-stream"}

        def readline(self):
            value = super().readline()
            if not value:
                raise TimeoutError("HTTP connection remains open after SSE DONE")
            return value

    relay = fake_relay(tmp_path, [KeepAlive(body)])
    status, _, data = relay.fetch(
        "one", Request("http://model.invalid"), threading.Event()
    )
    with data:
        assert data.read() == body
    assert status == 200
    assert relay.opener.open.call_count == 1
    assert not relay.events.exists()


@pytest.mark.parametrize(
    "body,kind,expected",
    [
        (b'{"error":{"code":503}}', "application/json", 503),
        (b'{"error":{"code":400}}', "application/json", 400),
        (b'{"choices":[]}', "application/json", 503),
        (b'data: {"error":{"code":429}}\n\n', "text/event-stream", 429),
        (
            b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
            "text/event-stream",
            503,
        ),
        (
            b'data: {"choices":[{"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
            "text/event-stream",
            None,
        ),
    ],
)
def test_provider_and_partial_stream_errors_are_not_success(body, kind, expected):
    data = io.BytesIO(body)
    assert response_error(data, kind) == expected
    assert data.read() == body


def test_cancelled_recoverable_wait_does_not_issue_another_request(tmp_path):
    cancelled = threading.Event()
    relay = fake_relay(tmp_path, [])

    def fail(*_, **__):
        cancelled.set()
        raise URLError("disconnected")

    relay.opener.open.side_effect = fail
    assert relay.fetch("one", Request("http://model.invalid"), cancelled) is None
    assert relay.opener.open.call_count == 1


def test_http_handler_preserves_payload_path_and_response_with_transport_events(
    tmp_path,
):
    result = b'data: {"choices":[{"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    relay = fake_relay(tmp_path, [response(result, "text/event-stream")])
    relay.routes = {"model-0": "https://provider.invalid"}
    payload = b'{"messages":[{"role":"user","content":"task"}],"stream":true}'
    handler = object.__new__(RelayHandler)
    handler.server = relay
    handler.path = "/model-0/v1/chat/completions?version=test"
    handler.command = "POST"
    handler.headers = {
        "Content-Length": str(len(payload)),
        "Content-Type": "application/json",
        "Authorization": "Bearer test-token",
        "Host": "127.0.0.1",
    }
    handler.rfile = io.BytesIO(payload)
    handler.wfile = io.BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler._forward()
    assert handler.wfile.getvalue() == result
    request = relay.opener.open.call_args.args[0]
    assert (
        request.full_url == "https://provider.invalid/v1/chat/completions?version=test"
    )
    assert request.data == payload
    assert request.get_header("Authorization") == "Bearer test-token"
    assert request.get_header("Host") is None
    records = [json.loads(line) for line in relay.events.read_text().splitlines()]
    assert [e["event"] for e in records] == ["request_start", "request_end"]
    assert handler.close_connection


@pytest.mark.asyncio
async def test_session_bootstrap_preserves_identity_and_finishes_transport_clock(
    tmp_path, model
):
    session = BitfunSession(logs_dir=tmp_path, bitfun_config=model)
    env = SimpleNamespace(upload_file=AsyncMock())
    budget = WorkBudget(5)
    commands = []

    async def execute(environment, command):
        commands.append(command)
        if "setsid" in command:
            path = list(budget.paths)[-1]
            path.write_text(json.dumps(event("request_start", budget.clock())) + "\n")

    session.exec_as_agent = execute
    for index in range(2):
        result = await session.execute(
            env, "task", 5, role="actor", handoff_id=str(index), work_budget=budget
        )
        assert result.status == "missing_handoff"
        assert not budget.requests
    assert "--session-id " + session.session_id in commands[1]
    assert "--resume " + session.session_id in commands[3]
    assert "transport_pid=$!" in commands[1]
    assert "trap " in commands[1]
    assert not budget.paths
    assert env.upload_file.await_count == 2
