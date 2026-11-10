import asyncio
import json
import os
import py_compile
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from benchmarks.deepswe.rsi_integrity import VERIFIER_CACHE_ENV, inventory, recover
from benchmarks.deepswe.rsi_runtime import CodingRuntime
from explore.practice_loop import PracticeInfrastructureError


def project(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "answer.py").write_text("def answer(): return 42\n")
    (root / "test_answer.py").write_text("from answer import answer\ndef test_answer(): assert answer() == 42\n")
    return root


def git_project(tmp_path):
    root = project(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "rsi@test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "RSI"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "answer.py", "test_answer.py"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
    return root


def run_pytest(root, cache_env=None):
    env = {k: v for k, v in os.environ.items()
           if k not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "PYTEST_ADDOPTS"}}
    env.update(cache_env or {})
    return subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=root,
                          env=env, capture_output=True, text=True, check=True)


def test_actual_pytest_cache_is_recovered_without_changing_candidate(tmp_path):
    root = project(tmp_path)
    before = inventory([str(root)])
    run_pytest(root)
    assert (root / ".pytest_cache").is_dir()
    assert list(root.glob("__pycache__/*.pyc"))
    result = recover(before)
    assert result["status"] == "cache_recovered" and result["removed"]
    assert inventory([str(root)]) == before
    assert recover(before)["status"] == "unchanged"


def test_cache_environment_prevents_pytest_and_import_pollution(tmp_path):
    root = project(tmp_path)
    before = inventory([str(root)])
    cache_env = {k: v.replace("/tmp/verifier-scratch", str(tmp_path / "scratch"))
                 for k, v in VERIFIER_CACHE_ENV.items()}
    run_pytest(root, cache_env)
    assert inventory([str(root)]) == before
    assert (tmp_path / "scratch/pytest-cache").is_dir()


def test_cache_environment_redirects_hypothesis_storage(tmp_path):
    pytest.importorskip("hypothesis", reason="Real Hypothesis also runs in the Docker recovery smoke")
    root = project(tmp_path)
    (root / "test_property.py").write_text(
        "from hypothesis import given, settings, strategies as st\n"
        "@settings(max_examples=10, deadline=None)\n"
        "@given(st.text())\n"
        "def test_round_trip(value): assert value == value[::-1][::-1]\n"
    )
    before = inventory([str(root)])
    cache_env = {k: v.replace("/tmp/verifier-scratch", str(tmp_path / "scratch"))
                 for k, v in VERIFIER_CACHE_ENV.items()}
    run_pytest(root, cache_env)
    assert inventory([str(root)]) == before
    assert not (root / ".hypothesis").exists()
    assert (tmp_path / "scratch/hypothesis").is_dir()


@pytest.mark.parametrize("mutation", ["source", "missing", "extra", "symlink", "cache_code", "existing_cache"])
def test_real_drift_is_rejected_and_no_evidence_is_deleted(tmp_path, mutation):
    root = project(tmp_path)
    old_cache = root / ".pytest_cache/v/cache/nodeids"
    if mutation == "existing_cache":
        old_cache.parent.mkdir(parents=True)
        old_cache.write_text('[]')
    before = inventory([str(root)])
    generated = root / "__pycache__/answer.cpython-312.pyc"
    generated.parent.mkdir()
    py_compile.compile(str(root / "answer.py"), cfile=str(generated), doraise=True)
    if mutation == "source":
        (root / "answer.py").write_text("def answer(): return 0\n")
    elif mutation == "missing":
        (root / "answer.py").unlink()
    elif mutation == "extra":
        (root / "unexpected.txt").write_text("not a cache")
    elif mutation == "symlink":
        (root / "__pycache__/external.pyc").symlink_to(tmp_path / "outside")
    elif mutation == "cache_code":
        (root / "__pycache__/injected.py").write_text("raise RuntimeError()")
    else:
        old_cache.write_text('["changed"]')
    mutated = inventory([str(root)])
    result = recover(before)
    assert result["status"] == "rejected" and not result["removed"]
    assert inventory([str(root)]) == mutated


def test_original_fixtures_are_also_checked(tmp_path):
    root = project(tmp_path)
    fixtures = tmp_path / "original"
    fixtures.mkdir()
    (fixtures / "input.json").write_text('{"input": 1}')
    before = inventory([str(root), str(fixtures)])
    (fixtures / "input.json").write_text('{"input": 2}')
    result = recover(before)
    assert result["status"] == "rejected"
    assert str(fixtures / "input.json") in result["changed"]


def test_git_index_stat_cache_refresh_is_recovered(tmp_path):
    root = git_project(tmp_path)
    before = inventory([str(root)])
    # Force a fresh stat timestamp without changing the tracked bytes.
    source = root / "answer.py"
    previous = source.stat()
    os.utime(source, ns=(previous.st_atime_ns, previous.st_mtime_ns + 2_000_000_000))
    subprocess.run(["git", "-C", str(root), "update-index", "--really-refresh"],
                   check=True, capture_output=True)
    after = inventory([str(root)])
    assert after != before
    result = recover(before)
    assert result["status"] == "metadata_recovered"
    assert result["git_index_metadata_drift"] == [str(root / ".git/index")]
    assert result["restored_git_indexes"] == [str(root / ".git/index")]
    assert inventory([str(root)]) == before


def test_git_index_staged_content_change_is_rejected(tmp_path):
    root = git_project(tmp_path)
    before = inventory([str(root)])
    (root / "answer.py").write_text("def answer(): return 0\n")
    subprocess.run(["git", "-C", str(root), "add", "answer.py"], check=True)
    result = recover(before)
    assert result["status"] == "rejected"
    assert str(root / ".git/index") in result["changed"]


@pytest.mark.parametrize("flag", ["--chmod=+x", "--assume-unchanged", "--skip-worktree"])
def test_git_index_only_semantic_change_is_rejected(tmp_path, flag):
    root = git_project(tmp_path)
    before = inventory([str(root)])
    subprocess.run(["git", "-C", str(root), "update-index", flag, "answer.py"], check=True)
    result = recover(before)
    assert result["status"] == "rejected"
    assert result["changed"] == [str(root / ".git/index")]
    assert not result["restored_git_indexes"]


@pytest.mark.parametrize("mutation", ["head", "ref", "object"])
def test_git_history_changes_are_rejected(tmp_path, mutation):
    root = git_project(tmp_path)
    before = inventory([str(root)])
    if mutation == "head":
        (root / ".git/HEAD").write_text("ref: refs/heads/other\n")
    elif mutation == "ref":
        ref = root / ".git/refs/heads/master"
        if not ref.exists():
            ref = root / ".git/refs/heads/main"
        ref.write_text("0" * 40 + "\n")
    else:
        (root / ".git/objects/zzzz").write_bytes(b"changed")
    result = recover(before)
    assert result["status"] == "rejected"
    assert result["changed"] or result["unexpected"]


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,expected", [("PASS", "pass"), ("FAIL", "wrong")])
async def test_verifier_cache_recovery_keeps_verdict_and_calls_model_once(tmp_path, verdict, expected):
    report = f"Evidence from independent tests.\nVERDICT: {verdict}\n"
    native = SimpleNamespace(session_id="verifier-id", rsi_work_usage=(7, 0))
    runtime = object.__new__(CodingRuntime)
    runtime.execute = AsyncMock(return_value=(report, native))
    runtime.integrity_command = AsyncMock(side_effect=[{}, {"status": "prepared"},
        {"status": "cache_recovered", "removed": ["cache"]}, {"status": "verified"}])
    events = []
    runtime.owner = SimpleNamespace(event=lambda *a, **kw: events.append((a, kw)))
    session = SimpleNamespace(inspections=0)
    vm = SimpleNamespace()
    cfg = SimpleNamespace(wall_clock_secs=100, max_iters=30, verifier_unverified_evidence=False)
    result, returned = await runtime.verify_async("project", vm, cfg,
        sink=SimpleNamespace(root=tmp_path), session=session)
    assert result == expected and returned == report
    assert runtime.execute.await_count == 1 and not vm.integrity_blocked
    assert session.bitfun_session_id == "verifier-id"
    assert events[0][0] == ("VERIFIER_CACHE_RECOVERED",)
    assert (tmp_path / "inspection_001/integrity.json").is_file()


@pytest.mark.asyncio
async def test_verifier_real_drift_preserves_workspace_and_diagnostic(tmp_path):
    runtime = object.__new__(CodingRuntime)
    runtime.execute = AsyncMock(return_value=("VERDICT: PASS", SimpleNamespace(
        session_id="verifier-id", rsi_work_usage=(1, 0))))
    runtime.integrity_command = AsyncMock(side_effect=[{}, {"status": "prepared"},
        {"status": "rejected", "changed": ["answer.py"]}, {"status": "verified"}])
    env = object()
    vm = SimpleNamespace(environment=env)
    cfg = SimpleNamespace(wall_clock_secs=100, max_iters=30, verifier_unverified_evidence=False)
    with pytest.raises(PracticeInfrastructureError, match="workspace preserved"):
        await runtime.verify_async("project", vm, cfg, sink=SimpleNamespace(root=tmp_path),
                                   session=SimpleNamespace(inspections=0))
    await runtime.close_vm(vm)
    assert vm.environment is env and vm.integrity_blocked
    assert runtime.execute.await_count == 1
    assert json.loads((tmp_path / "inspection_001/integrity.json").read_text())["changed"] == ["answer.py"]


@pytest.mark.asyncio
async def test_cancelled_native_operation_settles_before_runtime_close(tmp_path):
    owner = SimpleNamespace(parallel=1, role_configs=lambda: {"actor": {}}, event=lambda *a, **kw: None)
    audit = SimpleNamespace(validate=lambda *a: {}, text=lambda *a: [], transcripts=lambda *a: [],
                            captured=lambda *a: [], memory=lambda *a: {})
    runtime = CodingRuntime(owner, None, tmp_path / "runtime", asyncio.get_running_loop(), audit)
    started = asyncio.Event()
    settled = []
    async def native():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(.05)
            settled.append(True)
    async def worker():
        return await asyncio.to_thread(runtime.call, native())
    work = asyncio.create_task(worker())
    await started.wait()
    runtime.fail_pending_branches(RuntimeError("branch failed"))
    await asyncio.gather(work, return_exceptions=True)
    await runtime.close()
    assert settled == [True] and runtime.aborted


def test_brs_coding_abort_hook_drains_active_work_without_partial_memory(tmp_path, monkeypatch):
    import threading
    from explore import phase1_wave as wave
    from explore.practice_loop import PracticeHooks
    barrier = threading.Barrier(2)
    aborted = threading.Event()
    reasons, commits = [], []
    decision = wave.WaveDecision("WAVE", "two branches", tuple(
        wave.WaveProject(f"p{i}", "Build in /home/user/evolution_project") for i in range(2)), "{}")
    monkeypatch.setattr(wave, "_author_wave", lambda **kw: (decision, []))
    monkeypatch.setattr(wave, "_capture_wave_fixtures", lambda **kw: None)
    monkeypatch.setattr(wave, "_push_canonical_memory", lambda *a: None)
    def execute(**kw):
        barrier.wait(timeout=3)
        if kw["project_index"] == 1:
            raise PracticeInfrastructureError("real candidate drift")
        assert aborted.wait(timeout=3)
        raise PracticeInfrastructureError("sibling cancelled")
    monkeypatch.setattr(wave, "_execute_branch_with_infra_retry", execute)
    monkeypatch.setattr(wave, "_commit_branch_memory", lambda **kw: commits.append(True))
    def abort(error):
        reasons.append(str(error))
        aborted.set()
    result = wave.evolve_parallel_phase1(root=str(tmp_path), target_direction="target",
        actor_cfg=None, curriculum_cfg=None, memory_cfg=None, verifier_control_cfg=None,
        vm_factory=lambda: (SimpleNamespace(close=lambda: None), object()),
        corpus_path="unused", project_budget=2, max_parallel=2,
        hooks=PracticeHooks(validate_corpus=lambda p: {}, audit_text=lambda *a, **kw: [],
                            branch_failure=abort))
    assert result.status == "infra" and "real candidate drift" in result.reason
    assert reasons == ["real candidate drift"] and not commits
