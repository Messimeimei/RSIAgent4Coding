import asyncio
import io
import json
import shutil
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from benchmarks.deepswe.rsi_checkpoint import NativeCheckpoints, archive_info
from benchmarks.deepswe.rsi_runtime import CodingRuntime
from benchmarks.deepswe.session import SessionOutcome
from explore.practice_loop import PracticeInfrastructureError
from tools.rsi_recovery import inspect, retry


def make_archive(path, identity="test-session", unsafe=None):
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(f"home/projects/home-user/sessions/{identity}/metadata.json")
        data = json.dumps({"sessionId": identity}).encode()
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
        if unsafe:
            member = tarfile.TarInfo(unsafe)
            archive.addfile(member, io.BytesIO(b""))


class Environment:
    def __init__(self, tmp_path, failures=0, corrupt=0):
        self.trial_paths = SimpleNamespace(agent_dir=tmp_path / "env/agent")
        self.trial_paths.agent_dir.mkdir(parents=True)
        self.archive = tmp_path / "source.tgz"
        make_archive(self.archive)
        self.failures, self.corrupt = failures, corrupt
        self.commands = []
        self.archives = 0

    async def exec(self, command, **kwargs):
        self.commands.append(command)
        is_archive = command.startswith("tar czf") or "SNAPSHOT" in command or "def inventory" in command
        if is_archive:
            self.archives += 1
        failed = is_archive and self.archives <= self.failures
        return SimpleNamespace(return_code=2 if failed else 0,
            stdout="", stderr="disk full credential-secret" if failed else "")

    async def download_file(self, remote, path):
        if self.corrupt:
            self.corrupt -= 1
            Path(path).write_bytes(b"truncated")
        else:
            shutil.copyfile(self.archive, path)

    async def upload_file(self, *args):
        pass


def checkpoint(tmp_path, **kwargs):
    return NativeCheckpoints(tmp_path / "rsi/runtime/private-sessions", retry_delay=0,
                             secrets=["credential-secret"], **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("failures,corrupt", [(1, 0), (0, 1), (2, 0)])
async def test_recovers_tar_transfer_or_fallback_without_replaying_handoff(tmp_path, failures, corrupt):
    env = Environment(tmp_path, failures, corrupt)
    vm = SimpleNamespace(environment=env)
    session = SimpleNamespace(session_id="test-session", calls=0, logs_dir=env.trial_paths.agent_dir,
                              prepare=AsyncMock(), collect=AsyncMock())
    async def execute(*args, **kwargs):
        session.calls += 1
        return SessionOutcome("handoff", report={"final": True}, metrics={"model_calls": 10})
    session.execute = AsyncMock(side_effect=execute)
    audit = SimpleNamespace(instruction="target", validate=lambda *a: {}, text=lambda *a: [],
        transcripts=lambda *a, **kw: [], captured=lambda *a: [], memory=lambda *a: {})
    audit.record_text, audit.record_transcripts, audit.record_captured = audit.text, audit.transcripts, audit.captured
    owner = SimpleNamespace(parallel=1, role_configs=lambda: {"curriculum": {}},
                            _session=lambda *args: session)
    runtime = CodingRuntime(owner, env, tmp_path / "rsi/runtime", asyncio.get_running_loop(), audit)
    runtime.checkpoints = checkpoint(tmp_path)
    result, _ = await runtime.execute(vm, "task", "curriculum", tmp_path / "sink")
    assert result.status == "done" and result.iters == 10
    assert session.execute.await_count == 1
    saved = json.loads((tmp_path / "sink/outcome-001.json").read_text())
    assert saved["checkpoint_status"] == "ready" and saved["report"] == {"final": True}
    assert not vm.checkpoint_blocked
    events = (runtime.checkpoints.root / "test-session.events.jsonl").read_text()
    assert "credential-secret" not in events
    records = [json.loads(line) for line in events.splitlines()]
    assert any(r["stage"] == "checkpoint_failed" for r in records)
    if failures == 2:
        assert saved["checkpoint"]["method"] == "stable_python_snapshot"


def test_inspect_includes_network_and_branch_root_causes(tmp_path):
    paths = {
        'rsi/result.json': {'status': 'infrastructure_error', 'phase': 'phase1'},
        'rsi/phase1/episodes/ep002/branch_exception.json': {'stage': 'acquire_vm', 'error': 'network overlap'},
        'result.json': {'exception_info': {'exception_type': 'RoleInfrastructureError'}, 'verifier_result': None},
    }
    for name, data in paths.items():
        path = tmp_path/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    (tmp_path/'docker-lifecycle.jsonl').write_text('\n'.join(json.dumps(data) for data in (
        {'event': 'started'}, {'event': 'start_failed', 'network_conflict': True})))
    report = inspect(tmp_path)
    assert report['trial_exception']['exception_type'] == 'RoleInfrastructureError'
    assert report['official_verifier_result'] is None
    assert len(report['infrastructure_errors']) == 2
    assert all(r.get('event') != 'started' for item in report['infrastructure_errors'] for r in item['records'])


@pytest.mark.asyncio
async def test_permanent_failure_keeps_last_good_and_workspace(tmp_path):
    env = Environment(tmp_path)
    cp = checkpoint(tmp_path, repair_timeout=0)
    vm = SimpleNamespace(environment=env)
    session = SimpleNamespace(session_id="test-session", calls=1)
    old = await cp.save(vm, session, "curriculum", "/home/user", tmp_path / "outcome.json")
    original = (cp.root / "test-session.json").read_bytes()
    env.failures = 100
    session.calls = 2
    with pytest.raises(PracticeInfrastructureError, match="workspace preserved"):
        await cp.save(vm, session, "curriculum", "/home/user", tmp_path / "outcome.json")
    assert (cp.root / "test-session.json").read_bytes() == original
    assert archive_info(cp.root / old["archive"], "test-session")["sha256"] == old["sha256"]
    assert vm.checkpoint_blocked
    await CodingRuntime.close_vm(SimpleNamespace(), vm)
    assert vm.environment is env and not any("down" in cmd for cmd in env.commands)
    report = inspect(tmp_path)["sessions"][0]
    assert report["status"] == "stopped_preserved"
    assert not report["last_committed_checkpoint"]["covers_current_call"]
    with pytest.raises(ValueError, match="live controller"):
        retry(tmp_path, "test-session")


@pytest.mark.asyncio
async def test_live_repair_request_recovers_same_pending_call(tmp_path):
    env = Environment(tmp_path, failures=100)
    cp = checkpoint(tmp_path, repair_timeout=20, auto_retry_interval=100)
    vm = SimpleNamespace(environment=env)
    session = SimpleNamespace(session_id="test-session", calls=2)
    task = asyncio.create_task(cp.save(vm, session, "curriculum", "/home/user", tmp_path / "outcome.json"))
    path = cp.root / "test-session.recovery.json"
    async with asyncio.timeout(5):
        while not path.exists() or json.loads(path.read_text())["status"] != "waiting_for_checkpoint_repair":
            await asyncio.sleep(.01)
    env.failures = 0
    assert retry(tmp_path, "test-session")["requested"]
    result = await task
    assert result["call"] == 2 and env.archives == 4 and not vm.checkpoint_blocked


@pytest.mark.asyncio
async def test_restore_refuses_role_stale_call_or_corruption_before_upload(tmp_path):
    env = Environment(tmp_path)
    cp = checkpoint(tmp_path)
    session = SimpleNamespace(session_id="test-session", calls=1)
    record = await cp.save(SimpleNamespace(environment=env), session, "actor", "/home/user", tmp_path / "outcome")
    env.upload_file = AsyncMock()
    with pytest.raises(PracticeInfrastructureError, match="role"):
        await cp.restore(env, session, "verifier", "/home/user")
    session.calls = 2
    with pytest.raises(PracticeInfrastructureError, match="call"):
        await cp.restore(env, session, "actor", "/home/user")
    session.calls = 1
    path = cp.root / record["archive"]
    path.write_bytes(path.read_bytes()[:-8])
    with pytest.raises((EOFError, OSError, ValueError)):
        await cp.restore(env, session, "actor", "/home/user")
    env.upload_file.assert_not_awaited()


@pytest.mark.parametrize("unsafe", ["../escape", "/tmp/escape", "home/../../escape"])
def test_rejects_unsafe_native_archives(tmp_path, unsafe):
    path = tmp_path / "archive.tgz"
    make_archive(path, unsafe=unsafe)
    with pytest.raises(ValueError, match="Unsafe"):
        archive_info(path, "test-session")
