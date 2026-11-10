import asyncio
import json
import logging
import shlex
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pier.agents.factory import AgentFactory
from pier.models.agent.context import AgentContext
from pier.models.task.config import EnvironmentConfig
from pier.models.trial.config import TrialConfig
from pier.models.trial.paths import TrialPaths
from pier.trial.trial import Trial

import benchmarks.deepswe.baseline as module
import benchmarks.deepswe.completion as completion_module
from benchmarks.deepswe.baseline import BitfunBaseline
from benchmarks.deepswe.contracts import (
    ActorHandoff,
    HandoffError,
    ReviewReport,
    RoleInfrastructureError,
    TaskBudgetExhausted,
    read_handoff,
)
from benchmarks.deepswe.review_environment import Candidate, make_review_environment
from benchmarks.deepswe.session import BitfunSession, SessionOutcome
from benchmarks.deepswe.supervision import EventJournal
from benchmarks.deepswe.work_budget import WorkBudget
from run_deepswe import ROOT, load_job, select_config


@pytest.fixture
def model():
    return {
        "ai": {
            "models": [
                {
                    "id": "test",
                    "model_name": "test",
                    "base_url": "http://model.example:8080/v1",
                    "api_key": "${BASELINE_TEST_KEY}",
                }
            ],
            "default_models": {"primary": "test", "fast": "test"},
        }
    }


def actor_report(handoff_id="a", head="a" * 40):
    return ActorHandoff(
        kind="actor_done",
        handoff_id=handoff_id,
        final=True,
        candidate_head=head,
        summary="Implemented the requirement",
        checks=[
            {
                "desc": "actual test",
                "probe": "cd /app && python3 -m pytest && echo PASS",
            }
        ],
    )


def report(candidate="candidate", verdict="PASS", handoff_id="v"):
    return ReviewReport(
        kind="verifier_report",
        handoff_id=handoff_id,
        final=True,
        candidate_id=candidate,
        verdict=verdict,
        report="Requirement coverage checked",
        findings=["Reproduce the defect"] if verdict == "FAIL" else [],
        evidence=["Actual command and result"],
        evidence_requests=["Supply a reproducible boundary probe"]
        if verdict == "UNVERIFIED"
        else [],
    )


def completion_result(index=1, *, passed=True):
    return {
        "candidate_id": "candidate",
        "passed": passed,
        "rejections": [],
        "dropped": [],
        "checks": [
            {
                "desc": f"actor_requirement_{index}",
                "probe": f"cd /app && python3 /app/probe_{index}.py && echo PASS",
                "passed": passed,
                "return_code": 0 if passed else 1,
                "output": f"RAW_OUTPUT_CANARY_{index}",
                "output_path": f"/host/private/output_{index}.txt",
            }
        ],
    }


def test_modes_preserve_native_default(tmp_path):
    native = load_job(select_config("native"), mode="native")
    assert native.agents[0].import_path.endswith("bitfun_agent:BitfunCli")
    baseline = load_job(select_config("baseline"), mode="baseline")
    obj = AgentFactory.create_agent_from_config(
        baseline.agents[0], logs_dir=tmp_path / "untouched"
    )
    assert isinstance(obj, BitfunBaseline)
    assert not (tmp_path / "untouched").exists()
    with pytest.raises(ValueError, match="matching"):
        load_job(select_config("native"), mode="baseline")
    assert select_config("native") == ROOT / "config/deepswe/native.yaml"


@pytest.mark.parametrize(
    "key",
    ["max_rounds", "verifier_attempts", "actor_timeout_sec", "verifier_timeout_sec"],
)
def test_legacy_budget_requires_explicit_migration(tmp_path, model, key):
    with pytest.raises(ValueError, match="Legacy"):
        BitfunBaseline(logs_dir=tmp_path, bitfun_config=model, **{key: 3})


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_invalid_budget_rejected(tmp_path, model, value):
    with pytest.raises(ValueError):
        BitfunBaseline(logs_dir=tmp_path, bitfun_config=model, total_timeout_sec=value)


@pytest.mark.parametrize(
    "mutation",
    [
        {"verdict": "UNVERIFIER"},
        {"candidate_id": "stale"},
        {"handoff_id": "stale"},
        {"final": False},
        {"report": ""},
        {"evidence": []},
        {"verdict": "FAIL", "findings": []},
        {"findings": ["unresolved"]},
        {"verdict": "UNVERIFIED", "evidence_requests": []},
    ],
)
def test_invalid_report_never_passes(tmp_path, mutation):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({**report().model_dump(), **mutation}))
    with pytest.raises(HandoffError):
        read_handoff(path, "verifier", "v", "candidate")


def test_report_symlink_rejected(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(report().model_dump_json())
    link = tmp_path / "link.json"
    link.symlink_to(source)
    with pytest.raises(HandoffError):
        read_handoff(link, "verifier", "v", "candidate")


def test_session_commands_preserve_identity(tmp_path, model):
    session = BitfunSession(logs_dir=tmp_path, bitfun_config=model)
    prompt_path = '/tmp/rsiagent-bitfun/prompt.txt'
    command = shlex.split(session.command(prompt_path).split(" &\n")[0])[-1]
    assert "--session-id " + session.session_id in command
    assert '< ' + shlex.quote(prompt_path) in command
    assert "--output-format stream-json" in command
    session.calls = 1
    resumed = shlex.split(session.command(prompt_path).split(" &\n")[0])[-1]
    assert "--resume " + session.session_id in resumed
    assert f"stream-{session.session_id}-002.jsonl" in resumed


@pytest.mark.asyncio
async def test_timeout_is_not_unverified_and_settles_process(tmp_path, model):
    session = BitfunSession(logs_dir=tmp_path, bitfun_config=model)
    settled = asyncio.Event()

    async def work(*args):
        await settled.wait()

    session.exec_as_agent = work
    session.stop = AsyncMock(side_effect=lambda *args, **kw: settled.set())
    result = await session.execute(SimpleNamespace(upload_file=AsyncMock()), "task", 0.01, role="actor", handoff_id="a")
    assert result.status == "budget_exhausted" and result.report is None
    assert settled.is_set()
    session.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_handoff_detected_before_natural_exit(tmp_path, model):
    session = BitfunSession(logs_dir=tmp_path, bitfun_config=model)
    ended = asyncio.Event()

    async def work(*args):
        (tmp_path / "handoffs/a.json").write_text(actor_report().model_dump_json())
        event = {
            "event": {
                "type": "ToolEvent",
                "session_id": session.session_id,
                "tool_event": {"event_type": "Completed", "tool_id": "t"},
            }
        }
        (tmp_path / f"stream-{session.session_id}-001.jsonl").write_text(
            json.dumps(event) + "\n"
        )
        await ended.wait()

    session.exec_as_agent = work
    session.stop = AsyncMock(side_effect=lambda *args, **kw: ended.set())
    result = await session.execute(SimpleNamespace(upload_file=AsyncMock()), "task", 3, role="actor", handoff_id="a")
    assert result.status == "handoff" and ended.is_set()


def test_event_accounting_and_precise_stagnation(tmp_path):
    journal = EventJournal(tmp_path / "stream", "s")

    def feed(kind, **fields):
        journal.feed({"event": {"type": kind, "session_id": "s", **fields}})

    for index in range(3):
        feed("ToolEvent", tool_event={"event_type": "Queued", "tool_id": str(index)})
        assert journal.active_tools
        feed(
            "ToolEvent",
            tool_event={
                "event_type": "Started",
                "tool_id": str(index),
                "tool_name": "Read",
                "params": {"path": "/app/a"},
            },
        )
        feed(
            "ToolEvent",
            tool_event={
                "event_type": "Completed",
                "tool_id": str(index),
                "result": "same",
            },
        )
    assert journal.repeated_actions() and not journal.active_tools
    assert journal.summary()["input_tokens"] is None
    feed("TokenUsageUpdated", input_tokens=100, output_tokens=10)
    feed("TokenUsageUpdated", input_tokens=200, output_tokens=20)
    assert journal.summary()["input_tokens"] == 300
    assert journal.summary()["output_tokens"] == 30
    assert journal.summary()["cached_tokens"] is None
    journal.feed(
        {
            "event": {
                "type": "SubagentSessionLinked",
                "session_id": "child",
                "parent_session_id": "s",
            }
        }
    )
    assert journal.summary()["has_subagents"]
    journal.signatures.clear()
    assert not journal.repeated_actions()


def test_private_mounts_and_credential_free_selfcheck(tmp_path, model):
    directory = tmp_path / "environment"
    directory.mkdir()
    (directory / "Dockerfile").write_text("FROM python:3.12-slim\n")
    actor = SimpleNamespace(
        environment_dir=directory,
        environment_name="task",
        default_user=None,
        logger=logging.getLogger("test"),
        task_env_config=EnvironmentConfig(
            docker_image="python:3.12-slim", cpus=2, memory_mb=8192
        ),
        _cpu_resource_mode="limit",
        _memory_resource_mode="limit",
        _inference_ports=[8080],
        _mounts_json=[
            {
                "type": "bind",
                "source": "/host/" + name,
                "target": target,
                "read_only": True,
            }
            for name, target in [
                ("bitfun", "/usr/local/bin/bitfun"),
                ("uv", "/usr/local/bin/uv"),
                ("actor", "/logs/agent"),
                ("tests", "/tests"),
                ("repo", "/app"),
            ]
        ],
    )
    reviewer = BitfunSession(logs_dir=tmp_path / "review/agent", bitfun_config=model)
    env = make_review_environment(
        actor, reviewer, tmp_path / "review", tmp_path / "private"
    )
    mounts = {m["target"]: m for m in env._mounts_json}
    assert "/tests" not in mounts and "/app" not in mounts
    assert "/tmp/rsiagent-bitfun/user" not in mounts
    assert (
        Path(mounts["/tmp/rsiagent-bitfun/home"]["source"]) == tmp_path / "private/home"
    )
    assert env.network_allowlist.domains == ["model.example"]
    isolated = make_review_environment(actor, None, tmp_path / "checks")
    assert not isolated.network_allowlist.domains
    assert {m["target"] for m in isolated._mounts_json} == {
        "/usr/local/bin/uv",
        "/logs/agent",
    }


def test_exec_stall_ignores_delivery_metadata_but_not_running_processes(tmp_path):
    journal = EventJournal(tmp_path / "stream", "s")
    for index in range(3):
        for subtype, fields in [
            ("Started", {"params": {"cmd": "same"}}),
            (
                "Completed",
                {
                    "result": {
                        "output": "same output",
                        "exit_code": 0,
                        "completion": {"status": "exited"},
                        "wall_time_seconds": index,
                        "chunk_id": str(index),
                    }
                },
            ),
        ]:
            journal.feed(
                {
                    "event": {
                        "type": "ToolEvent",
                        "session_id": "s",
                        "tool_event": {
                            "event_type": subtype,
                            "tool_id": str(index),
                            "tool_name": "ExecCommand",
                            **fields,
                        },
                    }
                }
            )
    assert journal.repeated_actions()
    journal.feed(
        {
            "event": {
                "type": "ToolEvent",
                "session_id": "s",
                "tool_event": {
                    "event_type": "Completed",
                    "tool_id": "long",
                    "tool_name": "ExecCommand",
                    "result": {
                        "output": "",
                        "completion": {"status": "running"},
                        "session_id": "process-1",
                    },
                },
            }
        }
    )
    assert not journal.repeated_actions()


@pytest.fixture
def controller(tmp_path, model, monkeypatch):
    obj = BitfunBaseline(
        logs_dir=tmp_path / "trial/agent", bitfun_config=model, total_timeout_sec=60
    )
    state = {"repo": "/app", "head": "b" * 40, "status": ""}
    actors = []

    def session(*args):
        fake = SimpleNamespace(
            session_id=f"session-{len(actors)}",
            calls=0,
            prepare=AsyncMock(),
            collect=AsyncMock(),
            last_outcome=None,
            logs_dir=Path(args[0]),
            populate_context_post_run=lambda _: None,
        )

        async def execute(environment, prompt, timeout, **kwargs):
            fake.calls += 1
            state["head"] = "a" * 40
            fake.last_outcome = SessionOutcome(
                "handoff", actor_report(kwargs["handoff_id"]), {}
            )
            return fake.last_outcome

        fake.execute = AsyncMock(side_effect=execute)
        actors.append(fake)
        return fake

    obj._session = session
    obj._capture_repo_baseline = AsyncMock()
    obj._capture_repo_final_state = AsyncMock()
    monkeypatch.setattr(
        module,
        "Task",
        lambda _: SimpleNamespace(
            config=SimpleNamespace(metadata={"base_commit_hash": "b" * 40})
        ),
    )
    monkeypatch.setattr(
        module, "repository_state", AsyncMock(side_effect=lambda _: dict(state))
    )

    async def capture(environment, base, destination):
        return Candidate(
            base,
            state["head"],
            "/app",
            state["status"],
            "patch",
            tmp_path / "candidate.patch",
        )

    monkeypatch.setattr(module, "capture_candidate", capture)
    monkeypatch.setattr(
        module,
        "run_completion_checks",
        AsyncMock(return_value={"passed": True, "checks": []}),
    )
    return (
        obj,
        actors,
        state,
        SimpleNamespace(environment_dir=tmp_path / "task/environment"),
    )


def scripted_actors(obj, state, scripts):
    """Supply Actor calls; all controller recovery/exit decisions remain real."""
    original = obj._session
    scripts = iter(scripts)

    def session(*args):
        fake = original(*args)
        script = iter(next(scripts))

        async def execute(environment, prompt, timeout, **kwargs):
            status, head, dirty = next(script)
            fake.calls += 1
            state.update(head=head * 40, status=dirty)
            fake.last_outcome = SessionOutcome(
                status,
                actor_report(kwargs["handoff_id"], head * 40)
                if status == "handoff" else None,
                {"model_calls": 1},
                last_action={"tool": "Read", "arguments": {"path": "/app/a"}},
                last_result=f"observed {head}",
            )
            return fake.last_outcome

        fake.execute = AsyncMock(side_effect=execute)
        return fake

    obj._session = session


@pytest.mark.asyncio
async def test_unverified_evidence_then_fail_repair_then_pass(controller):
    obj, actors, state, env = controller
    verdicts = iter(["UNVERIFIED", "FAIL", "PASS"])

    async def review(instruction, environment, candidate, *args, **kwargs):
        return report(candidate.candidate_id, next(verdicts))

    obj._verify = AsyncMock(side_effect=review)
    await obj.run("task", env, AgentContext())
    assert actors[0].calls == 3 and len(actors) == 1
    assert "boundary probe" in actors[0].execute.call_args_list[1].args[1]
    assert "Reproduce the defect" in actors[0].execute.call_args_list[2].args[1]
    assert obj.summary["stop_reason"] == "verifier_pass"
    assert [r["verdict"] for r in obj.summary["rounds"]] == [
        "UNVERIFIED",
        "FAIL",
        "PASS",
    ]


@pytest.mark.asyncio
async def test_no_fixed_three_round_limit(controller):
    obj, actors, state, env = controller

    async def review(instruction, environment, candidate, round_number, *args, **kwargs):
        value = report(candidate.candidate_id, "FAIL" if round_number < 5 else "PASS")
        value.evidence = [f"new observed result {round_number}"]
        return value

    obj._verify = review
    await obj.run("task", env, AgentContext())
    assert actors[0].calls == 5 and obj.summary["verdict"] == "PASS"


@pytest.mark.asyncio
async def test_missing_report_is_infrastructure_error_not_unverified(controller):
    obj, actors, state, env = controller
    obj._verify = AsyncMock(side_effect=RoleInfrastructureError("missing report"))
    with pytest.raises(RoleInfrastructureError):
        await obj.run("task", env, AgentContext())
    assert obj.summary["status"] == "infrastructure_error"
    assert obj.summary["verdict"] is None


@pytest.mark.asyncio
async def test_completion_failure_returns_to_actor_before_review(
    controller, monkeypatch
):
    obj, actors, state, env = controller
    monkeypatch.setattr(
        module,
        "run_completion_checks",
        AsyncMock(
            side_effect=[
                {"passed": False, "checks": []},
                {"passed": True, "checks": []},
            ]
        ),
    )
    obj._verify = AsyncMock(return_value=report())
    await obj.run("task", env, AgentContext())
    assert actors[0].calls == 2
    obj._verify.assert_awaited_once()


@pytest.mark.asyncio
async def test_unchanged_actor_enforced(controller):
    obj, actors, state, env = controller

    async def review(*args, **kwargs):
        state["status"] = "M unexpected.py"
        return report()

    obj._verify = review
    with pytest.raises(RoleInfrastructureError, match="changed during"):
        await obj.run("task", env, AgentContext())


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_context_policy", [False, True])
async def test_stagnation_refreshes_context_and_swaps_roles_once(controller, model, reference_context_policy):
    obj, actors, state, env = controller
    obj.reference_context_policy = reference_context_policy
    obj.verifier_config = {**model, "marker": "reviewer"}
    configs = []
    original = obj._session

    def session(path, config):
        configs.append(config)
        fake = original(path, config)
        execute = fake.execute.side_effect
        if len(actors) == 1:

            async def stall(*args, **kwargs):
                fake.calls += 1
                fake.last_outcome = SessionOutcome(
                    "stalled", last_action={"cmd": "x"}, last_result="same"
                )
                return fake.last_outcome

            fake.execute = AsyncMock(side_effect=stall)
        else:
            fake.execute = AsyncMock(side_effect=execute)
        return fake

    obj._session = session
    obj._verify = AsyncMock(side_effect=[report(verdict="FAIL"), report()])
    await obj.run("task", env, AgentContext())
    assert len(actors) == 2 and configs[1]["marker"] == "reviewer"
    assert len(obj.summary["actor_session_ids"]) == 2
    assert (obj.baseline_dir / "actor-recovery.json").exists()
    assert obj._verify.call_args_list[0].args[4]["marker"] == "reviewer"
    assert "marker" not in obj._verify.call_args_list[1].args[4]
    assert obj.summary["stall_reviews"][0]["verdict"] == "FAIL"
    if reference_context_policy:
        assert all(c["ai"]["context_policy"]["mode"] == "work_log" for c in configs)
        assert all(call.args[4]["ai"]["context_policy"]["mode"] == "full_history"
                   for call in obj._verify.call_args_list)


@pytest.mark.asyncio
async def test_completion_runs_validated_probe_and_checks_exit(tmp_path, monkeypatch):
    env = SimpleNamespace(
        exec=AsyncMock(
            return_value=SimpleNamespace(stdout="PASS\n", stderr="", return_code=1)
        )
    )

    @asynccontextmanager
    async def isolated(*args):
        assert args[1] is None
        yield env

    monkeypatch.setattr(completion_module, "review_environment", isolated)
    monkeypatch.setattr(
        completion_module, "review_tree_digest", AsyncMock(return_value="same")
    )
    candidate = Candidate("b" * 40, "a" * 40, "/app", "", "hash", tmp_path / "patch")
    result = await completion_module.run_completion_checks(
        object(), candidate, actor_report(), "task", tmp_path / "checks"
    )
    assert not result["passed"]
    env.exec.assert_awaited_once()


@pytest.mark.asyncio
async def test_persistent_verifier_and_same_session_report_recovery(
    controller, monkeypatch
):
    obj, actors, state, env = controller
    obj.baseline_dir.mkdir(parents=True)
    obj.summary = {"rounds": []}

    obj._work_budget = WorkBudget(60)
    observed = []

    @asynccontextmanager
    async def isolated(actor_env, reviewer, candidate, destination, private):
        observed.append((reviewer.session_id, private))
        yield env

    monkeypatch.setattr(module, "review_environment", isolated)
    monkeypatch.setattr(module, "review_tree_digest", AsyncMock(return_value="same"))
    replies = iter(
        [
            SessionOutcome("missing_handoff"),
            SessionOutcome("handoff", report()),
            SessionOutcome("handoff", report()),
        ]
    )
    obj._invoke = AsyncMock(side_effect=lambda *a: next(replies))
    candidate = Candidate("b" * 40, "a" * 40, "/app", "", "hash", Path("/patch"))
    await obj._verify("task", env, candidate, 1, obj.verifier_config)
    await obj._verify(
        "task", env, candidate, 2, obj.verifier_config, review_id="stall-001"
    )
    assert len(actors) == 1 and observed[0] == observed[1]
    assert obj._invoke.await_count == 3
    assert actors[0].logs_dir == obj.baseline_dir / "reviews/stall-001/agent"


@pytest.mark.asyncio
async def test_verifier_candidate_mutation_is_infrastructure_error(
    controller, monkeypatch
):
    obj, actors, state, env = controller
    obj.baseline_dir.mkdir(parents=True)
    obj.summary = {"rounds": []}

    obj._work_budget = WorkBudget(60)

    @asynccontextmanager
    async def isolated(*args):
        yield env

    monkeypatch.setattr(module, "review_environment", isolated)
    monkeypatch.setattr(
        module, "review_tree_digest", AsyncMock(side_effect=["original", "modified"])
    )
    obj._invoke = AsyncMock(return_value=SessionOutcome("handoff", report()))
    candidate = Candidate("b" * 40, "a" * 40, "/app", "", "hash", Path("/patch"))
    with pytest.raises(RoleInfrastructureError, match="modified candidate"):
        await obj._verify("task", env, candidate, 1, obj.verifier_config)


@pytest.mark.asyncio
async def test_repeated_dirty_handoffs_stop_normally_without_review(controller):
    obj, actors, state, env = controller
    original = obj._session

    def session(*args):
        fake = original(*args)
        execute = fake.execute.side_effect

        async def dirty(*a, **kw):
            outcome = await execute(*a, **kw)
            state["status"] = "M unfinished.py"
            return outcome

        fake.execute = AsyncMock(side_effect=dirty)
        return fake

    obj._session = session
    obj.stall_recovery = False
    obj._verify = AsyncMock()
    await obj.run("task", env, AgentContext())
    obj._verify.assert_not_awaited()
    assert obj.summary["status"] == "incomplete"
    assert obj.summary["stop_reason"] == "stall_recovery_disabled"
    assert obj.summary["verdict"] is None
    assert obj.summary["stall_reviews"][0]["inspection"] == "skipped_dirty_candidate"
    assert len(actors) == 1 and actors[0].calls == 3
    module.run_completion_checks.assert_not_awaited()
    assert state["status"] == "M unfinished.py"


@pytest.mark.asyncio
async def test_transport_timeout_is_not_task_budget(controller):
    obj, actors, state, env = controller
    obj._verify = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        await obj.run("task", env, AgentContext())
    assert obj.summary["stop_reason"] == "infrastructure_timeout"
    assert obj.summary["verdict"] is None


@pytest.mark.asyncio
async def test_native_round_cap_is_a_segment_not_report_error(tmp_path, model):
    session = BitfunSession(logs_dir=tmp_path, bitfun_config=model)

    async def work(*args):
        event = {
            "event": {
                "type": "DialogTurnCompleted",
                "session_id": session.session_id,
                "finish_reason": "max_rounds",
                "success": True,
            }
        }
        (tmp_path / f"stream-{session.session_id}-001.jsonl").write_text(
            json.dumps(event) + "\n"
        )

    session.exec_as_agent = work
    outcome = await session.execute(SimpleNamespace(upload_file=AsyncMock()), "task", 3, role="actor", handoff_id="a")
    assert outcome.status == "segment_exhausted"
    assert outcome.metrics["native_finish_reason"] == "max_rounds"


@pytest.mark.asyncio
async def test_actor_segment_continuation_does_not_consume_report_retries(controller):
    obj, actors, state, env = controller
    obj.report_recovery_attempts = 0
    original = obj._session

    def session(*args):
        fake = original(*args)
        execute = fake.execute.side_effect

        async def segment(*a, **kw):
            if fake.calls < 3:
                fake.calls += 1
                fake.last_outcome = SessionOutcome("segment_exhausted")
                return fake.last_outcome
            return await execute(*a, **kw)

        fake.execute = AsyncMock(side_effect=segment)
        return fake

    obj._session = session
    obj._verify = AsyncMock(return_value=report())
    await obj.run("task", env, AgentContext())
    assert len(actors) == 1 and actors[0].calls == 4
    assert obj.summary["stop_reason"] == "verifier_pass"
    assert "same" in actors[0].execute.call_args_list[-1].args[1].lower()


@pytest.mark.asyncio
async def test_verifier_segments_continue_without_report_retries(
    controller, monkeypatch
):
    obj, actors, state, env = controller
    obj.report_recovery_attempts = 0
    obj.baseline_dir.mkdir(parents=True)
    obj.summary = {"rounds": []}

    obj._work_budget = WorkBudget(60)

    @asynccontextmanager
    async def isolated(*args):
        yield env

    monkeypatch.setattr(module, "review_environment", isolated)
    monkeypatch.setattr(module, "review_tree_digest", AsyncMock(return_value="same"))
    obj._invoke = AsyncMock(
        side_effect=[SessionOutcome("segment_exhausted")] * 3
        + [SessionOutcome("handoff", report())]
    )
    candidate = Candidate("b" * 40, "a" * 40, "/app", "", "hash", Path("/patch"))
    result = await obj._verify("task", env, candidate, 1, obj.verifier_config)
    assert result.verdict == "PASS"
    assert len(actors) == 1 and obj._invoke.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_enabled", [True, False])
async def test_stall_inspection_can_complete_without_handoff(controller, recovery_enabled):
    obj, actors, state, env = controller
    obj.stall_recovery = recovery_enabled
    scripted_actors(obj, state, [[("stalled", "c", "")]])
    obj._verify = AsyncMock(return_value=report())
    await obj.run("task", env, AgentContext())

    assert len(actors) == 1 and actors[0].calls == 1
    assert obj.summary["status"] == "completed"
    assert obj.summary["stop_reason"] == "stall_verifier_pass"
    assert obj.summary["rounds"] == []
    assert obj.summary["stall_reviews"][0]["candidate"]["head"] == "c" * 40
    assert obj._verify.call_args.args[2].head == "c" * 40
    assert obj._verify.call_args.kwargs["evidence_leads"] == ""
    assert not (obj.baseline_dir / "actor-recovery.json").exists()
    obj._capture_repo_final_state.assert_awaited_once_with(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["FAIL", "UNVERIFIED"])
async def test_repeated_stall_returns_incomplete_and_preserves_latest_head(controller, verdict):
    obj, actors, state, env = controller
    scripted_actors(obj, state, [[("stalled", "a", "")], [("stalled", "c", "")]])
    obj._verify = AsyncMock(return_value=report(verdict=verdict))
    await obj.run("task", env, AgentContext())

    assert len(actors) == 2 and all(actor.calls == 1 for actor in actors)
    assert obj.summary["status"] == "incomplete"
    assert obj.summary["stop_reason"] == "stall_recovery_exhausted"
    assert obj.summary["verdict"] == verdict
    assert obj.summary["final_repository"] == state
    assert obj.summary["final_candidate_id"].startswith("c" * 40)
    assert [r["review_id"] for r in obj.summary["stall_reviews"]] == [
        "stall-001", "stall-002"
    ]
    obj._capture_repo_final_state.assert_awaited_once_with(env)


@pytest.mark.asyncio
async def test_second_stall_pass_wins_over_recovery_exhaustion(controller):
    obj, actors, state, env = controller
    scripted_actors(obj, state, [[("stalled", "a", "")], [("stalled", "c", "")]])
    obj._verify = AsyncMock(side_effect=[report(verdict="FAIL"), report()])
    await obj.run("task", env, AgentContext())
    assert len(actors) == 2
    assert obj.summary["stop_reason"] == "stall_verifier_pass"
    assert obj.summary["verdict"] == "PASS"


@pytest.mark.asyncio
async def test_disabled_recovery_still_inspects_before_normal_exit(controller):
    obj, actors, state, env = controller
    obj.stall_recovery = False
    scripted_actors(obj, state, [[("stalled", "c", "")]])
    obj._verify = AsyncMock(return_value=report(verdict="FAIL"))
    await obj.run("task", env, AgentContext())
    obj._verify.assert_awaited_once()
    assert len(actors) == 1
    assert obj.summary["status"] == "incomplete"
    assert obj.summary["stop_reason"] == "stall_recovery_disabled"
    assert state["head"] == "c" * 40


@pytest.mark.asyncio
async def test_identical_candidates_use_same_stall_exit_path(controller):
    obj, actors, state, env = controller
    scripted_actors(obj, state, [[("handoff", "a", "")] * 3] * 2)
    obj._verify = AsyncMock(return_value=report(verdict="FAIL"))
    await obj.run("task", env, AgentContext())

    assert [actor.calls for actor in actors] == [3, 3]
    assert len(obj.summary["rounds"]) == 6
    assert obj._verify.await_count == 8  # Six ordinary + two forced inspections.
    assert obj.summary["stop_reason"] == "stall_recovery_exhausted"
    assert [r["cause"] for r in obj.summary["stall_reviews"]] == [
        "repeated_candidates", "repeated_candidates"
    ]


@pytest.mark.asyncio
async def test_failed_completion_still_allows_forced_stall_inspection(controller, monkeypatch):
    obj, actors, state, env = controller
    scripted_actors(obj, state, [[("handoff", "a", "")] * 3])
    monkeypatch.setattr(
        module, "run_completion_checks",
        AsyncMock(return_value=completion_result(passed=False)),
    )
    obj._verify = AsyncMock(return_value=report())
    await obj.run("task", env, AgentContext())
    obj._verify.assert_awaited_once()
    assert obj._verify.call_args.kwargs["review_id"] == "stall-001"
    assert obj.summary["stop_reason"] == "stall_verifier_pass"
    assert len(actors) == 1


@pytest.mark.asyncio
async def test_stall_review_preserves_infrastructure_errors(controller):
    obj, actors, state, env = controller
    scripted_actors(obj, state, [[("stalled", "a", "")]])
    obj._verify = AsyncMock(side_effect=RoleInfrastructureError("missing report"))
    with pytest.raises(RoleInfrastructureError, match="missing report"):
        await obj.run("task", env, AgentContext())
    assert len(actors) == 1
    assert obj.summary["status"] == "infrastructure_error"
    assert obj.summary["verdict"] is None


@pytest.mark.asyncio
async def test_stall_inspection_budget_exhaustion_preserves_candidate(controller):
    obj, actors, state, env = controller
    scripted_actors(obj, state, [[("stalled", "c", "")]])
    obj._verify = AsyncMock(side_effect=TaskBudgetExhausted("shared deadline"))
    await obj.run("task", env, AgentContext())
    assert len(actors) == 1
    assert obj.summary["status"] == "incomplete"
    assert obj.summary["stop_reason"] == "task_budget_exhausted"
    assert obj.summary["final_repository"]["head"] == "c" * 40
    assert obj.summary["verdict"] is None
    obj._capture_repo_final_state.assert_awaited_once_with(env)


@pytest.mark.asyncio
async def test_no_fresh_actor_if_budget_expires_during_recovery_collection(controller):

    obj, actors, state, env = controller
    scripted_actors(obj, state, [[("stalled", "c", "")]])
    obj._verify = AsyncMock(return_value=report(verdict="FAIL"))

    async def collect(*args):
        obj._work_budget.started = obj._work_budget.clock() - 61

    obj._collect = collect
    await obj.run("task", env, AgentContext())
    assert len(actors) == 1
    assert obj.summary["stop_reason"] == "task_budget_exhausted"
    assert obj.summary["final_repository"]["head"] == "c" * 40


@pytest.mark.asyncio
async def test_stall_review_rejects_actor_repository_mutation(controller):
    obj, actors, state, env = controller
    scripted_actors(obj, state, [[("stalled", "a", "")]])

    async def changed(*args, **kwargs):
        state["head"] = "c" * 40
        return report()

    obj._verify = changed
    with pytest.raises(RoleInfrastructureError, match="changed during isolated stall"):
        await obj.run("task", env, AgentContext())
    assert obj.summary["status"] == "infrastructure_error"
    assert obj.summary["verdict"] is None


@pytest.mark.asyncio
async def test_stall_worklog_contains_history_and_only_actor_observations(controller, monkeypatch):
    obj, actors, state, env = controller
    obj.allow_role_swap = False
    scripted_actors(obj, state, [
        [("handoff", "a", ""), ("stalled", "c", "")],
        [("handoff", "d", "")],
    ])
    obj.logs_dir.mkdir(parents=True)
    # Real native events, including a child session and private reasoning that
    # must not be copied into the factual tool record.
    events = [
        {"type": "ThinkingChunk", "session_id": "session-0", "text": "PRIVATE_REASONING"},
        {"type": "ToolEvent", "session_id": "session-0", "tool_event": {
            "event_type": "Started", "tool_id": "t", "tool_name": "ExecCommand",
            "params": {"command": "pytest boundary_test.py"},
        }},
        {"type": "ToolEvent", "session_id": "session-0", "tool_event": {
            "event_type": "Completed", "tool_id": "t", "result": "2 tests failed",
        }},
        {"type": "ToolEvent", "session_id": "another-session", "tool_event": {
            "event_type": "Completed", "tool_id": "x", "result": "PRIVATE_CHILD",
        }},
    ]
    (obj.logs_dir / "stream-session-0-001.jsonl").write_text(
        "\n".join(json.dumps({"event": e}) for e in events) + "\n"
    )
    monkeypatch.setattr(
        module, "run_completion_checks", AsyncMock(return_value=completion_result())
    )
    obj._calls.append({
        "role": "verifier", "metrics": None, "private": "PRIVATE_VERIFIER_TRACE"
    })
    obj._verify = AsyncMock(side_effect=[
        report(verdict="UNVERIFIED"), report(verdict="FAIL"), report()
    ])
    await obj.run("task", env, AgentContext())

    saved = (obj.baseline_dir / "actor-recovery.json").read_text()
    worklog = json.loads(saved)
    assert (obj.logs_dir / "actor-worklog.json").read_text() == saved
    assert worklog["repository"]["head"] == "c" * 40
    assert worklog["latest_candidate"]["head"] == "c" * 40
    assert worklog["rounds"][0]["candidate"]["head"] == "a" * 40
    assert worklog["rounds"][0]["completion"]["checks"][0]["passed"]
    assert worklog["rounds"][0]["review"]["verdict"] == "UNVERIFIED"
    assert worklog["stall_reviews"][0]["review"]["verdict"] == "FAIL"
    assert worklog["last_observation"] == "observed c"
    assert [c["status"] for c in worklog["actor_calls"]] == ["handoff", "stalled"]
    observations = worklog["actor_tool_history"][0]["observations"]
    assert observations[0]["action"]["arguments"]["command"] == "pytest boundary_test.py"
    assert observations[0]["observation"] == "2 tests failed"
    assert "PRIVATE_" not in saved
    prompt = actors[1].execute.call_args.args[1]
    assert "The machine RETAINS all of its progress" in prompt
    assert "/logs/agent/actor-worklog.json" in prompt
    assert all(call.kwargs["evidence_leads"] == "" for call in obj._verify.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("internal_verdict", ["FAIL", "UNVERIFIED"])
async def test_pier_grading_runs_after_actor_stall_exhaustion(
    controller, tmp_path, internal_verdict
):
    """Exercise Pier's real exception/collect/grade routing without a container."""
    from pier.models.task.config import TaskConfig

    obj, actors, state, env = controller
    scripted_actors(obj, state, [[("stalled", "a", "")], [("stalled", "c", "")]])
    obj._verify = AsyncMock(return_value=report(verdict=internal_verdict))

    trial = object.__new__(Trial)
    trial.config = TrialConfig(
        task={"path": tmp_path / "task"}, trial_name="stalled-baseline",
        trials_dir=tmp_path,
    )
    trial._trial_paths = TrialPaths(tmp_path / "pier-trial")
    trial._trial_paths.mkdir()
    trial._task = SimpleNamespace(
        name="task", checksum="test-checksum", has_steps=False, config=TaskConfig()
    )
    env.run_healthcheck = AsyncMock()
    env.env_paths = SimpleNamespace(agent_dir=Path("/logs/agent"))
    trial._environment = env
    trial._agent = obj
    trial._logger = logging.getLogger("test-pier-stall")
    trial._result = None
    for method in (
        "_invoke_hooks", "_setup_environment", "_setup_agent", "_maybe_download_logs",
        "_run_pre_artifacts_script", "_run_collect_hooks", "_maybe_upload_agent_logs",
        "_collect_artifacts", "_stop_agent_environment", "_cleanup_and_finalize",
    ):
        setattr(trial, method, AsyncMock())
    trial._close_logger_handler = Mock()
    trial._maybe_populate_agent_context = Mock()

    async def grade():
        trial._collect_artifacts.assert_awaited_once()
        obj._capture_repo_final_state.assert_awaited_once_with(env)
        assert obj.summary["status"] == "incomplete"
        assert obj.summary["stop_reason"] == "stall_recovery_exhausted"
        assert state["head"] == "c" * 40

    from pier.trial.execution import TrialExecution
    trial._task.instruction = "task"
    trial._execution = TrialExecution(trial._task, obj, env, 90, 30, 30)
    trial._run_verification = AsyncMock(side_effect=grade)
    result = await trial.run()
    assert result.exception_info is None
    trial._run_verification.assert_awaited_once()


@pytest.mark.asyncio
async def test_verifier_prompt_discloses_only_requested_evidence(controller, monkeypatch):
    obj, actors, state, env = controller
    verdicts = iter(["FAIL", "UNVERIFIED", "UNVERIFIED", "FAIL", "PASS"])
    received = []
    original_invoke = obj._invoke

    @asynccontextmanager
    async def isolated(*args):
        yield env

    async def invoke(session, environment, prompt, role, handoff_id, candidate_id=None):
        if role == "actor":
            return await original_invoke(
                session, environment, prompt, role, handoff_id, candidate_id
            )
        received.append((session.session_id, prompt))
        return SessionOutcome(
            "handoff", report(candidate_id, next(verdicts), handoff_id)
        )

    monkeypatch.setattr(module, "review_environment", isolated)
    monkeypatch.setattr(module, "review_tree_digest", AsyncMock(return_value="same"))
    monkeypatch.setattr(
        module, "run_completion_checks",
        AsyncMock(side_effect=[completion_result(i) for i in range(1, 6)]),
    )
    obj._invoke = invoke
    await obj.run("task", env, AgentContext())

    assert len(received) == 5 and len({s for s, _ in received}) == 1
    expected_disclosure = [False, False, True, True, False]
    assert [r["actor_evidence_disclosed"] for r in obj.summary["rounds"]] == (
        expected_disclosure
    )
    for index, ((_, prompt), disclosed) in enumerate(
        zip(received, expected_disclosure), start=1
    ):
        assert ("ACTOR-SUPPLIED EVIDENCE LEADS" in prompt) == disclosed
        assert (f"actor_requirement_{index}" in prompt) == disclosed
        assert (f"/app/probe_{index}.py" in prompt) == disclosed
        if disclosed:
            assert "Harness execution classification: PASS" in prompt
        assert "RAW_OUTPUT_CANARY" not in prompt
        assert "/host/private/" not in prompt
        assert "Implemented the requirement" not in prompt


@pytest.mark.asyncio
async def test_method_messages_reach_separate_role_sessions_and_model_configs(controller, monkeypatch):
    from core.actor import EPISTEMIC_CORE
    from explore.charter import VERIFIER_REQUIREMENT_EVIDENCE

    obj, _, _, env = controller
    obj.verifier_config = {
        "ai": {
            "models": [{"id": "review-model", "base_url": "http://review.invalid/v1"}],
            "default_models": {"primary": "review-model"},
        }
    }
    sessions = []
    received = []

    def session(logs_dir, config):
        real = BitfunBaseline._session(obj, logs_dir, config)
        real.prepare = AsyncMock()
        real.collect = AsyncMock()
        real.populate_context_post_run = lambda _: None

        async def execute(environment, prompt, timeout, **kwargs):
            real.calls += 1
            received.append((real, kwargs["role"], prompt))
            value = (
                actor_report(kwargs["handoff_id"], "b" * 40)
                if kwargs["role"] == "actor" else
                report(kwargs["candidate_id"], handoff_id=kwargs["handoff_id"])
            )
            real.last_outcome = SessionOutcome("handoff", value, {})
            return real.last_outcome

        real.execute = execute
        sessions.append(real)
        return real

    @asynccontextmanager
    async def isolated(*args):
        yield env

    obj._session = session
    monkeypatch.setattr(module, "review_environment", isolated)
    monkeypatch.setattr(module, "review_tree_digest", AsyncMock(return_value="same"))
    await obj.run("authoritative coding task", env, AgentContext())

    assert obj.summary["stop_reason"] == "verifier_pass"
    assert len(sessions) == 2 and sessions[0].session_id != sessions[1].session_id
    assert sessions[0]._bitfun_config == obj.role_configs()["actor"]
    assert sessions[1]._bitfun_config == obj.role_configs()["verifier"]
    assert [role for _, role, _ in received] == ["actor", "verifier"]
    assert EPISTEMIC_CORE in received[0][2]
    assert "A passing subset of a partial inventory is not completion." in received[0][2]
    assert VERIFIER_REQUIREMENT_EVIDENCE in received[1][2]
    assert all("authoritative coding task" in prompt for _, _, prompt in received)
    protocol = json.loads((obj.baseline_dir / "protocol.json").read_text())
    assert {"core/actor.py", "core/verifier_runtime.py", "explore/charter.py"} <= (
        protocol["source_sha256"].keys()
    )
    assert protocol["prompt_delivery"] == "role_message_to_bitfun_session_with_native_system_prompt"


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["failed_check", "dirty_candidate", "missing_handoff", "segment_exhausted"])
async def test_evidence_request_survives_actor_retries(controller, monkeypatch, interruption):
    obj, actors, state, env = controller
    script = [("handoff", "a", "")]
    if interruption == "dirty_candidate":
        script.append(("handoff", "b", "M unfinished.py"))
    elif interruption == "failed_check":
        script.append(("handoff", "b", ""))
    else:
        script.append((interruption, "b", ""))
    script.append(("handoff", "c", ""))
    scripted_actors(obj, state, [script])
    checks = [completion_result(1)]
    if interruption == "failed_check":
        checks.append(completion_result(2, passed=False))
    checks.append(completion_result(3))
    monkeypatch.setattr(module, "run_completion_checks", AsyncMock(side_effect=checks))
    obj._verify = AsyncMock(side_effect=[report(verdict="UNVERIFIED"), report()])
    await obj.run("task", env, AgentContext())

    assert actors[0].calls == 3 and obj._verify.await_count == 2
    assert obj._verify.call_args_list[0].kwargs["evidence_leads"] == ""
    leads = obj._verify.call_args_list[1].kwargs["evidence_leads"]
    assert "actor_requirement_3" in leads and "/app/probe_3.py" in leads
    assert "actor_requirement_1" not in leads and "actor_requirement_2" not in leads
    assert "RAW_OUTPUT_CANARY" not in leads


@pytest.mark.asyncio
@pytest.mark.parametrize("role_swap", [False, True])
async def test_stall_inspection_and_fresh_actor_keep_evidence_boundary(controller, role_swap):
    obj, actors, state, env = controller
    obj.allow_role_swap = role_swap
    obj.verifier_config = {**obj.verifier_config, "marker": "different_model"}
    scripted_actors(obj, state, [
        [("handoff", "a", ""), ("stalled", "c", "")],
        [("handoff", "d", "")],
    ])
    obj._verify = AsyncMock(side_effect=[
        report(verdict="UNVERIFIED"), report(verdict="UNVERIFIED"), report()
    ])
    await obj.run("task", env, AgentContext())
    assert len(actors) == 2 and obj._verify.await_count == 3
    assert all(call.kwargs["evidence_leads"] == "" for call in obj._verify.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [False, True])
async def test_report_recovery_preserves_evidence_boundary(controller, monkeypatch, requested):

    obj, actors, state, env = controller
    obj.baseline_dir.mkdir(parents=True)
    obj.summary = {"rounds": []}
    obj._work_budget = WorkBudget(60)

    @asynccontextmanager
    async def isolated(*args):
        yield env

    monkeypatch.setattr(module, "review_environment", isolated)
    monkeypatch.setattr(module, "review_tree_digest", AsyncMock(return_value="same"))
    obj._invoke = AsyncMock(
        side_effect=[SessionOutcome("missing_handoff"), SessionOutcome("handoff", report())]
    )
    candidate = Candidate("b" * 40, "a" * 40, "/app", "", "hash", Path("/patch"))
    leads = completion_module.actor_evidence_leads(completion_result()) if requested else ""
    await obj._verify("task", env, candidate, 1, obj.verifier_config, evidence_leads=leads)
    for call in obj._invoke.call_args_list:
        prompt = call.args[2]
        assert ("actor_requirement_1" in prompt) == requested
        assert "RAW_OUTPUT_CANARY" not in prompt
        assert "/host/private/" not in prompt
