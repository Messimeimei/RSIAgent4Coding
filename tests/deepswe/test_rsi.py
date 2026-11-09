"""Coding transport boundaries plus the actual released BRS/DRS state machines."""

import asyncio
import json
import io
import tarfile
import queue
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from benchmarks.deepswe.baseline import BitfunBaseline
from benchmarks.deepswe.contracts import CompletionCheck, HandoffError
from benchmarks.deepswe.rsi import BitfunRSI, MemoryBaseline
from benchmarks.deepswe.rsi_prompts import practice_prompt, practice_verifier_prompt
from benchmarks.deepswe.rsi_runtime import CodingAudit, CodingRuntime
from benchmarks.deepswe.session import SessionOutcome
from config.settings import Config
from core.actor import EPISTEMIC_CORE
from core.loop import LoopResult
from explore import phase1_wave as wave
from explore.charter import (
    VERIFIER_REQUIREMENT_EVIDENCE, self_evolving_curriculum_charter,
    self_evolving_actor_memory_distillation_msg,
    self_evolving_actor_memory_reconciliation_msg,
)
from explore.practice_loop import PracticeHooks, _atomic_install_memory, _read_memory_tree
from run_deepswe import ROOT, load_job, select_config


@pytest.fixture
def model():
    return {"ai": {"models": [{"id": "test", "model_name": "test",
        "base_url": "http://model.example:8080/v1", "api_key": "${TEST_KEY}"}]}}


def test_mode_keeps_baseline_and_native_independent():
    for mode, agent in [("native", "BitfunCli"), ("baseline", "BitfunBaseline"), ("rsi", "BitfunRSI")]:
        job = load_job(select_config(mode), mode=mode)
        assert job.agents[0].import_path.endswith(":" + agent)
    job = load_job(select_config("rsi"), mode="rsi")
    cfg = job.agents[0].kwargs
    assert cfg["parallel"] == 2 and cfg["brs_project_budget"] == 8
    assert cfg["verifier_bitfun_config"] == cfg["curriculum_bitfun_config"]


def test_reference_charters_are_preserved_except_tools_and_paths():
    charter = self_evolving_curriculum_charter("target", phase2_outcome=True)
    prompt = practice_prompt(charter, curriculum=True, terminal_path="/done", handoff_id="id")
    assert charter.replace("~/.memory", "/home/user/.memory") in prompt
    for charter in [self_evolving_actor_memory_distillation_msg("FAIL", "evidence"),
                    self_evolving_actor_memory_reconciliation_msg()]:
        prompt = practice_prompt(charter, curriculum=False, terminal_path="/done", handoff_id="id")
        assert charter.replace("~/.memory", "/home/user/.memory") in prompt
        assert EPISTEMIC_CORE in prompt
    for allow in [False, True]:
        prompt = practice_verifier_prompt("project", "context", "/report",
            allow_unverified=allow, generation=1)
        assert VERIFIER_REQUIREMENT_EVIDENCE in prompt
        assert "program\":{\"lang\":\"verifier-report" not in prompt
        assert "read-only filesystem" not in prompt
        assert "reconcile" in prompt.lower() or not allow
        assert "86400" not in prompt and "parallel" not in prompt


def test_training_fail_is_terminal_but_final_baseline_repairs(model, tmp_path):
    report = SimpleNamespace(verdict="FAIL")
    old = BitfunBaseline(logs_dir=tmp_path / "old", bitfun_config=model)
    training = MemoryBaseline(logs_dir=tmp_path / "train", bitfun_config=model, memory={}, training=True)
    final = MemoryBaseline(logs_dir=tmp_path / "final", bitfun_config=model, memory={})
    assert not old._terminal_review(report)
    assert training._terminal_review(report) and training.terminal_report is report
    assert not final._terminal_review(report)
    assert not training._terminal_review(SimpleNamespace(verdict="UNVERIFIED"))


def test_coding_corpus_allows_only_current_public_task_and_checks_actual_streams(tmp_path):
    target = "one two three four five six seven eight nine current target"
    other = "another task has completely different private held out instructions here"
    for name, text in [("one", target), ("two", other)]:
        path = tmp_path / "tasks" / name / "instruction.md"
        path.parent.mkdir(parents=True)
        path.write_text(text)
    audit = CodingAudit(tmp_path / "corpus.json", target, tmp_path / "tasks")
    assert not audit.text(target, authorized_instruction=target)
    assert audit.text(other)
    assert audit.text("cat /logs/verifier/reward.txt")
    assert not audit.text("pytest /app/tests/test_api.py; read public tests/test.sh")
    assert audit.text("cat /tests/test.sh")
    assert not audit.text("cat /app/tests/test.sh")
    (tmp_path / "stream-x.jsonl").write_text(json.dumps({"event": {"text": other}}) + "\n")
    assert audit.transcripts(tmp_path, authorized_instruction=target)


def test_brs_parallel_two_uses_same_snapshot_then_ordered_same_actor_commits(tmp_path, monkeypatch):
    slots = queue.Queue()
    for i in range(2):
        slots.put(i)
    active = set()
    guard = threading.Lock()
    barrier = threading.Barrier(2)
    starts, commits, contexts = [], [], {}
    seed = {"prior.md": b"prior experience"}
    _atomic_install_memory(str(tmp_path / "memory"), seed)

    def factory():
        slot = slots.get(timeout=3)
        with guard:
            active.add(slot)
        def close():
            with guard:
                active.remove(slot)
            slots.put(slot)
        return SimpleNamespace(close=close), SimpleNamespace(slot=slot)

    decision = wave.WaveDecision("WAVE", "three experiments", tuple(
        wave.WaveProject(f"p{i}", "Build in /home/user/evolution_project") for i in range(3)), "{}")
    monkeypatch.setattr(wave, "_author_wave", lambda **kw: (decision, []))
    monkeypatch.setattr(wave, "_capture_wave_fixtures", lambda **kw: None)
    monkeypatch.setattr(wave, "_push_canonical_memory", lambda *a: None)
    monkeypatch.setattr(wave, "_replay", lambda *a: None)
    def execute(**kw):
        lease, vm = factory()
        index = kw["project_index"]
        assert _read_memory_tree(str(kw["wave_memory_dir"])) == seed
        contexts[index] = [{"bitfun_session_id": f"actor-{index}"}]
        starts.append(index)
        if index < 3:
            barrier.wait(timeout=3)
        return wave._BranchRuntime(index, kw["project"], kw["episode_dir"], lease, vm,
            contexts[index], "STATUS: SUBMIT", "VERDICT: PASS", "PASS", seed)
    monkeypatch.setattr(wave, "_execute_branch", execute)
    def commit(**kw):
        branch = kw["branch"]
        assert sorted(starts) == [1, 2, 3]  # all verdicts before any memory write
        assert branch.actor_history is contexts[branch.project_index]
        before = _read_memory_tree(str(kw["canonical_memory_dir"]))
        assert len(before) == branch.project_index
        after = {**before, f"lesson{branch.project_index}.md": b"owned lesson"}
        _atomic_install_memory(str(kw["canonical_memory_dir"]), after)
        commits.append(branch.project_index)
        return {"before": {}, "after": {}, "changes": {}}
    monkeypatch.setattr(wave, "_commit_branch_memory", commit)
    result = wave.evolve_parallel_phase1(root=str(tmp_path), target_direction="target",
        actor_cfg=None, curriculum_cfg=None, memory_cfg=None, verifier_control_cfg=None,
        vm_factory=factory, corpus_path="unused", project_budget=1, max_parallel=2,
        hooks=PracticeHooks(validate_corpus=lambda p: {}, audit_text=lambda *a, **k: []),
        park_verified_branches=True)
    assert result.status == "budget_exhausted" and result.projects == 3
    assert commits == [1, 2, 3] and not active


def test_phase2_uses_reference_fail_pass_practice_fresh_retry_and_memory(model, tmp_path, monkeypatch):
    import benchmarks.deepswe.rsi as module
    owner = BitfunRSI(logs_dir=tmp_path / "agent", bitfun_config=model)
    owner.rsi_dir.mkdir()
    target_sessions, learned, curriculum_sessions, released = [], [], [], []
    counter = 0
    verdicts = iter(["FAIL", "PASS", "PASS"])
    def acquire(**kwargs):
        nonlocal counter
        counter += 1
        lease = SimpleNamespace(close=lambda: released.append(counter))
        environment = SimpleNamespace(trial_paths=SimpleNamespace(agent_dir=tmp_path / f"env{counter}/agent"))
        return lease, SimpleNamespace(environment=environment)
    runtime = SimpleNamespace(acquire=acquire, call=asyncio.run,
        audit=SimpleNamespace(path=tmp_path / "corpus"), hooks=PracticeHooks(push_memory=lambda *a: True),
        adopt=lambda native, **k: [{"bitfun_session_id": native.session_id}])
    def baseline(logs, memory, training):
        verdict = next(verdicts)
        session = SimpleNamespace(session_id=str(uuid.uuid4()))
        target_sessions.append((session, dict(memory)))
        report = SimpleNamespace(model_dump=lambda: {"verdict": verdict, "report": "observed evidence"})
        return SimpleNamespace(run=AsyncMock(), active_actor=session, terminal_report=report,
            summary={"status": "learning" if verdict == "FAIL" else "completed"})
    monkeypatch.setattr(owner, "baseline", baseline)
    def promote(**kw):
        index = len(learned) + 1
        assert kw["actor_history"][0]["bitfun_session_id"] == target_sessions[-1][0].session_id
        assert kw["terminal_outcome"] == ["FAIL", "PASS", "PASS"][index - 1]
        memory = {**kw["before_memory"], f"target{index}.md": b"lesson"}
        _atomic_install_memory(str(kw["memory_dir"]), memory)
        learned.append(index)
        return memory, "Actor causal diagnosis", kw["actor_history"]
    monkeypatch.setattr(module, "_promote_learning", promote)
    def evolve(vm, path, direction, report, diagnosis, memory, *args, **kw):
        curriculum_sessions.append(kw["session"])
        index = len(curriculum_sessions)
        assert kw["trigger_authority"] == "phase2_outcome_protocol"
        assert kw["curriculum_memory_access"] == "read_only"
        assert diagnosis == "Actor causal diagnosis"
        memory_dir = Path(path) / "memory"
        new = {**memory, f"practice{index}.md": b"practice"} if index < 3 else memory
        _atomic_install_memory(str(memory_dir), new)
        return SimpleNamespace(status="ready_for_retry", reason="Curriculum evidence",
            memory_dir=str(memory_dir), projects=1 if index < 3 else 0)
    monkeypatch.setattr(module, "evolve_until_ready", evolve)
    memory, record = owner.phase2(runtime, "immutable target", {"brs.md": b"seed"})
    assert len(target_sessions) == 3 and len({s.session_id for s, _ in target_sessions}) == 3
    assert len({id(s) for s in curriculum_sessions}) == 1
    assert "practice1.md" in target_sessions[1][1] and "practice2.md" in target_sessions[2][1]
    assert learned == [1, 2, 3] and memory["brs.md"] == b"seed"
    result = json.loads((owner.rsi_dir / "phase2/phase-result.json").read_text())
    assert result["status"] == "verifier_pass_curriculum_ready"
    assert result["practice_projects"] == 2 and result["official_evaluator_calls"] == 0


@pytest.mark.asyncio
async def test_frozen_phase3_uses_pristine_pier_container_and_no_training_candidate(model, tmp_path, monkeypatch):
    import benchmarks.deepswe.rsi as module
    owner = BitfunRSI(logs_dir=tmp_path / "agent", bitfun_config=model)
    original = SimpleNamespace(environment_dir=tmp_path / "tasks/task/environment")
    seen = []
    def phase1(**kw):
        assert kw["max_parallel"] == 2 and kw["park_verified_branches"]
        memory = Path(kw["root"]) / "memory"
        _atomic_install_memory(str(memory), {"brs.md": b"BRS"})
        return SimpleNamespace(status="budget_exhausted", memory_dir=str(memory), reason="boundary")
    monkeypatch.setattr(module, "evolve_parallel_phase1", phase1)
    monkeypatch.setattr(module, "CodingAudit", lambda *a: SimpleNamespace(path=tmp_path / "corpus"))
    class Runtime:
        def __init__(self, *args):
            self.audit = args[-1]
            self.hooks = SimpleNamespace(push_memory=lambda vm, path: seen.append((vm.environment, path)) or True)
        async def run_engine(self, fn, *a, **kw):
            return fn(*a, **kw)
        async def close(self):
            pass
        def acquire(self):
            raise AssertionError("stub phase1 must not call Docker")
    monkeypatch.setattr(module, "CodingRuntime", Runtime)
    async def immediate(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(module.asyncio, "to_thread", immediate)
    def phase2(runtime, instruction, initial):
        assert initial == {"brs.md": b"BRS"}
        source = owner.rsi_dir / "phase2/memory"
        _atomic_install_memory(str(source), {**initial, "drs.md": b"DRS"})
        return owner.freeze(source, owner.rsi_dir / "phase2/memory_frozen")
    monkeypatch.setattr(owner, "phase2", phase2)
    async def run(instruction, environment, context):
        assert environment is original
        assert instruction == "public target"
    final = SimpleNamespace(run=run, summary={"verdict": "PASS", "stop_reason": "verifier_pass"})
    def baseline(logs, memory, training):
        assert not training and memory == {"brs.md": b"BRS", "drs.md": b"DRS"}
        return final
    monkeypatch.setattr(owner, "baseline", baseline)
    await owner.run("public target", original, SimpleNamespace())
    assert seen[0][0] is original
    result = json.loads((owner.rsi_dir / "result.json").read_text())
    assert result["frozen_memory_unchanged"] and result["status"] == "completed"


@pytest.mark.asyncio
async def test_native_session_continues_across_segment_and_records_only_done_probe_leads(tmp_path):
    class Environment:
        def __init__(self):
            self.trial_paths = SimpleNamespace(agent_dir=tmp_path / "env/agent")
            self.trial_paths.agent_dir.mkdir(parents=True)
            self.commands = []
        async def exec(self, command, **kwargs):
            self.commands.append((command, kwargs))
            return SimpleNamespace(return_code=0, stdout="", stderr="")
        async def download_file(self, remote, local):
            metadata = json.dumps({"sessionId": native.session_id}).encode()
            with tarfile.open(local, "w:gz") as archive:
                member = tarfile.TarInfo(f"home/projects/home-user/sessions/{native.session_id}/metadata.json")
                member.size = len(metadata)
                archive.addfile(member, io.BytesIO(metadata))
        async def upload_file(self, *args):
            pass
    class Session:
        def __init__(self):
            self.session_id, self.calls = str(uuid.uuid4()), 0
            self.logs_dir = environment.trial_paths.agent_dir
        async def prepare(self, env):
            pass
        async def collect(self, env):
            pass
        async def execute(self, env, prompt, timeout, **kwargs):
            self.calls += 1
            await env.exec("native command")
            if self.calls == 1:
                return SessionOutcome("segment_exhausted", metrics={"model_calls": 200, "completed_tools": 3})
            path = self.logs_dir / "handoffs" / (kwargs["handoff_id"] + ".json")
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"kind": "rsi_done", "handoff_id": kwargs["handoff_id"],
                "final": True, "checks": [{"desc": "boundary output", "probe": "python3 /tmp/probe.py && printf 'PASS\\n'"}]}))
            report = kwargs["terminal_reader"](path)
            return SessionOutcome("handoff", report=report,
                metrics={"model_calls": 3, "completed_tools": 2})
    environment = Environment()
    native = Session()
    owner = SimpleNamespace(parallel=2, report_recovery_attempts=2,
        _session=lambda logs, config: native, role_configs=lambda: {"actor": {}})
    audit = SimpleNamespace(instruction="target", validate=lambda *a: {}, text=lambda *a, **kw: [],
        transcripts=lambda *a, **kw: [], captured=lambda *a, **kw: [], memory=lambda *a, **kw: {})
    runtime = CodingRuntime(owner, environment, tmp_path / "runtime", asyncio.get_running_loop(), audit)
    result, session = await runtime.execute(SimpleNamespace(environment=environment),
        "independent-channel probes among your checks", "actor", tmp_path / "sink")
    assert result.iters == 203 and session is native and native.calls == 2
    history = runtime.adopt(native)
    leads = runtime.evidence_leads(history, "target", Config())
    assert "python3 /tmp/probe.py" in leads and "native command" not in leads
    assert all(kwargs["cwd"] == "/home/user" for command, kwargs in environment.commands
               if command == "native command")


@pytest.mark.asyncio
async def test_phase3_memory_is_only_inventory_in_actor_prompt(model, tmp_path, monkeypatch):
    seen = []
    async def invoke(self, session, environment, prompt, *a, **kw):
        seen.append(prompt)
    monkeypatch.setattr(BitfunBaseline, "_invoke", invoke)
    agent = MemoryBaseline(logs_dir=tmp_path / "agent", bitfun_config=model,
        memory={"lesson.md": b"PRIVATE MEMORY BODY"})
    actor = SimpleNamespace(session_id="actor")
    await agent._invoke(actor, None, "task", "actor", "id")
    await agent._invoke(actor, None, "task", "actor", "next")
    await agent._invoke(SimpleNamespace(session_id="verifier"), None, "task", "verifier", "v")
    assert "lesson.md" in seen[0] and "PRIVATE MEMORY BODY" not in seen[0]
    assert "lesson.md" not in seen[1] and "lesson.md" not in seen[2]


@pytest.mark.asyncio
async def test_rsi_budget_dispatch_is_scoped_and_idempotent(model, tmp_path, monkeypatch):
    from pier.trial.execution import TrialExecution
    from benchmarks.deepswe.rsi_budget import install_rsi_budget_support
    delegated = AsyncMock()
    delegated._baseline_work_budget = True
    delegated._rsi_phase_budgets = False
    monkeypatch.setattr(TrialExecution, "run_agent", delegated)
    install_rsi_budget_support()
    first = TrialExecution.run_agent
    install_rsi_budget_support()
    assert TrialExecution.run_agent is first
    native = SimpleNamespace(agent=object())
    await first(native, instruction="native", context=None)
    assert delegated.await_count == 1
    agent = BitfunRSI(logs_dir=tmp_path / "agent", bitfun_config=model)
    agent.run = AsyncMock()
    trial = SimpleNamespace(agent=agent, environment=object())
    await first(trial, instruction="rsi", context=None)
    assert delegated.await_count == 1 and agent.run.await_count == 1


def test_publication_retries_share_phase_clock_but_learning_has_new_clock(tmp_path):
    runtime = object.__new__(CodingRuntime)
    runtime.phase_usage = {}
    runtime.sessions = {'actor': (object(), '/home/user')}
    native = SimpleNamespace(session_id='actor')
    seen = []
    async def execute(*args, **kwargs):
        seen.append((kwargs['seconds'], kwargs['max_iters']))
        return LoopResult(status='done', iters=2, wall_secs=3), native
    runtime.execute = execute
    runtime.call = asyncio.run
    runtime.adopt = lambda *a, **kw: [{'bitfun_session_id': 'actor'}]
    cfg = SimpleNamespace(model='bitfun:actor', wall_clock_secs=10, max_iters=5)
    history = None
    for attempt in range(3):
        _, history = runtime.run_attempt('publish', object(), cfg,
            SimpleNamespace(root=tmp_path/f'actor/attempt_001/segment_{attempt:03d}'),
            initial_history=history)
    assert seen == [(10, 5), (7, 3), (4, 1)]
    with pytest.raises(RuntimeError, match='cumulative publication'):
        runtime.run_attempt('publish', object(), cfg,
            SimpleNamespace(root=tmp_path/'actor/attempt_001/segment_003'), initial_history=history)
    runtime.run_attempt('distill', object(), cfg,
        SimpleNamespace(root=tmp_path/'memory_distillation/segment_000'), initial_history=history)
    assert seen[-1] == (10, 5)
