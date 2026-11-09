"""Stopped BRS recovery preserves the committed ledger and rejects partial work."""
import asyncio
import hashlib
import io
import json
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from benchmarks.deepswe.rsi import BitfunRSI
from benchmarks.deepswe.rsi_checkpoint import archive_info, atomic_json
from benchmarks.deepswe.rsi_resume import (
    inspect_resume, load_resume_job, prepare_resume, resume_summary, validate_coding_boundary,
)
from benchmarks.deepswe.rsi_runtime import CodingRuntime
from benchmarks.deepswe.session import SessionOutcome
from explore import phase1_wave as wave
from explore.practice_loop import PracticeHooks, PracticeInfrastructureError, _manifest, _read_memory_tree


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def archive(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, 'w:gz') as tf:
        for name, data in contents.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            tf.addfile(entry, io.BytesIO(data))


@pytest.fixture
def stopped(tmp_path):
    trial = tmp_path / 'jobs/old/trial'
    root = trial / 'rsi'
    model = {'ai': {'models': [{'base_url': 'http://model.invalid/v1', 'api_key': '${TEST_KEY}'}]}}
    owner = BitfunRSI(logs_dir=tmp_path / 'new/agent', bitfun_config=model)
    task = tmp_path / 'tasks/example'
    task.mkdir(parents=True)
    (task / 'task.toml').write_text('')
    (task / 'instruction.md').write_text('public target')
    from pier.models.task.task import Task
    atomic_json(trial / 'config.json', {'task': {'path': str(task)}})
    atomic_json(trial / 'result.json', {'finished_at': '2026-11-06T00:00:00Z', 'task_checksum': Task(task).checksum})
    atomic_json(root / 'result.json', {'status': 'infrastructure_error', 'phase': 'phase1'})
    atomic_json(root / 'protocol.json', {'parallel': 2, 'brs_project_budget': 8,
        'phase3_work_sec': 10200, 'learning_role_work_sec': 86400, 'training_target_work_sec': 36000,
        'roles': owner.role_configs(), 'reference_context_policy': False})
    lineage = root / 'phase1'
    memory, records = {}, []
    for i in range(1, 7):
        before = _manifest(memory)
        memory[f'lesson{i}.md'] = f'validated lesson {i}'.encode()
        ep = lineage / f'episodes/ep{i:03d}'
        atomic_json(ep / 'outcome.json', {'project_index': i, 'project_id': f'p{i}',
            'terminal_outcome': 'FAIL' if i == 2 else 'PASS', 'wave_index': 1,
            'memory_before': before, 'memory_after': _manifest(memory)})
        (ep / 'outcome.md').write_text(f'unchanged report {i}')
        for d in ('fixtures', 'candidates/cycle_001'):
            path = ep / d
            archive(path / 'materials.tgz', {'evolution_project/a.txt': b'candidate'})
            (path / 'materials.MANIFEST.sha256').write_text(
                hashlib.sha256(b'candidate').hexdigest() + '  evolution_project/a.txt\n')
    (lineage / 'memory').mkdir()
    for name, value in memory.items():
        (lineage / 'memory' / name).write_bytes(value)
    atomic_json(lineage / 'state.json', {'status': 'infra', 'projects': 6, 'last_project': 6,
        'waves': 2, 'phase1_parallel_waves': True, 'project_budget': 8,
        'checkpoint_projects': [0, 4, 8], 'max_parallel': 2, 'target_query_conditioned': True,
        'target_sha256': hashlib.sha256(b'public target').hexdigest(), 'memory_manifest': _manifest(memory)})
    atomic_json(lineage / 'waves/wave_001/curriculum_decision.json', {'decision': 'WAVE',
        'rationale': 'practice', 'projects': [{'id': f'p{i}', 'instruction': 'Build in /home/user/evolution_project'}
                                           for i in range(1, 7)]})
    (lineage / 'waves/wave_001/outcomes.md').write_text('six original outcomes including FAIL')
    (root / 'events.jsonl').write_text(json.dumps({'event': 'PHASE1_WAVE_COMPLETED',
        'payload': {'wave': 1, 'total_projects': 6}}) + '\n')
    sid = str(uuid.uuid4())
    private = root / 'runtime/private-sessions'
    for call in (2, 3):
        path = private / f'{sid}-{call:03d}.tgz'
        archive(path, {f'home/projects/home-user/sessions/{sid}/metadata.json':
                      json.dumps({'sessionId': sid}).encode()})
        record = {'schema_version': 1, 'session_id': sid, 'call': call, 'role': 'curriculum',
            'working_dir': '/home/user', 'archive': path.name, 'method': 'tar', **archive_info(path, sid)}
        records.append(record)
        folder = lineage / f'curriculum/wave_{call-1:03d}/segment_000'
        atomic_json(folder / f'outcome-{call:03d}.json', {'status': 'handoff' if call == 2 else 'execution_error',
            'checkpoint_status': 'ready', 'session_id': sid, 'call': call, 'checkpoint': record,
            'metrics': {'model_calls': 23 if call == 2 else 0, 'completed_tools': 22 if call == 2 else 0}})
        atomic_json(folder / 'transcript.json', {'bitfun_session_id': sid,
            'completion': 'handoff' if call == 2 else 'execution_error', 'native_archive': str(path)})
    atomic_json(private / f'{sid}.json', records[-1])
    atomic_json(root / f'runtime/environments/old/agent/checkpoint-{sid}-003.json', {
        'session_id': sid, 'call': 3, 'status': 'execution_error',
        'error': "OSError: [Errno 7] Argument list too long: 'docker'"})
    from pier.models.job.config import JobConfig
    job = JobConfig.model_validate({'job_name': 'old', 'jobs_dir': str(trial.parent.parent),
        'agents': [{'import_path': 'benchmarks.deepswe.rsi:BitfunRSI', 'kwargs': {'bitfun_config': model}}],
        'datasets': [{'path': str(task.parent), 'task_names': ['example']}]})
    atomic_json(trial.parent / 'config.json', job.model_dump(mode='json'))
    return SimpleNamespace(trial=trial, root=root, lineage=lineage, owner=owner,
        task=task, sid=sid, checkpoint=records[0], memory=memory)


def test_new_job_copies_committed_boundary_and_preserves_source(stopped):
    s = stopped
    original = snapshot(s.trial)
    plan = inspect_resume(s.trial, owner=s.owner)
    assert plan['checkpoint'] == s.checkpoint  # call 2, not failed call 3's latest manifest
    assert resume_summary(plan)['committed_projects'] == 6
    assert not resume_summary(plan)['requires_original_container']
    config = load_resume_job(s.trial, 'new-job')
    assert config.agents[0].kwargs['resume_from'] == str(s.trial)
    destination = s.owner.rsi_dir
    receipt = prepare_resume(s.trial, destination, owner=s.owner, instruction='public target')
    assert receipt['curriculum_checkpoint_call'] == 2
    assert json.loads((destination / f'runtime/private-sessions/{s.sid}.json').read_text()) == s.checkpoint
    assert _read_memory_tree(str(destination / 'phase1/memory')) == s.memory
    assert snapshot(s.trial) == original


@pytest.mark.parametrize('fault', ['memory', 'partial_learning', 'pending_tools', 'pending_stream',
    'phase2', 'quarantine', 'target', 'archive', 'candidate', 'roles', 'active', 'unknown_launch'])
def test_ineligible_resume_is_rejected_without_mutating_source(stopped, fault):
    s = stopped
    if fault == 'memory':
        (s.lineage / 'memory/lesson1.md').write_text('uncommitted')
    elif fault == 'partial_learning':
        (s.lineage / 'episodes/ep007/memory_distillation').mkdir(parents=True)
    elif fault == 'pending_tools':
        p = s.lineage / 'curriculum/wave_002/segment_000/outcome-003.json'
        d = json.loads(p.read_text()); d['metrics']['completed_tools'] = 1; atomic_json(p, d)
    elif fault == 'pending_stream':
        (s.lineage / 'curriculum/wave_002/segment_000/stream-native.jsonl').touch()
    elif fault == 'phase2':
        (s.root / 'phase2').mkdir()
    elif fault == 'quarantine':
        p = s.lineage / 'state.json'; d = json.loads(p.read_text()); d['status'] = 'quarantined'; atomic_json(p, d)
    elif fault == 'target':
        (s.task / 'instruction.md').write_text('different target')
    elif fault == 'archive':
        (s.root / 'runtime/private-sessions' / s.checkpoint['archive']).write_bytes(b'truncated')
    elif fault == 'candidate':
        (s.lineage / 'episodes/ep001/candidates/cycle_001/materials.MANIFEST.sha256').write_text('')
    elif fault == 'roles':
        s.owner.parallel = 4
    elif fault == 'active':
        atomic_json(s.trial / 'result.json', {'finished_at': None})
    elif fault == 'unknown_launch':
        (s.root / f'runtime/environments/old/agent/checkpoint-{s.sid}-003.json').unlink()
    original = snapshot(s.trial)
    with pytest.raises((PracticeInfrastructureError, ValueError, OSError, EOFError)):
        prepare_resume(s.trial, s.owner.rsi_dir, owner=s.owner, instruction='public target')
    assert snapshot(s.trial) == original and not s.owner.rsi_dir.exists()


def test_reference_state_machine_resumes_next_wave_without_replaying_projects(stopped, monkeypatch):
    s = stopped
    prepare_resume(s.trial, s.owner.rsi_dir, owner=s.owner, instruction='public target')
    calls = []
    def author(**kw):
        calls.append(kw)
        assert kw['history'][-1]['bitfun_session_id'] == s.sid
        assert kw['previous_wave_outcomes'] == 'six original outcomes including FAIL'
        assert Path(kw['sink_root']).name == 'wave_002'
        return wave.WaveDecision('SATURATED', 'enough practice', (),
            '{"decision":"SATURATED","rationale":"enough practice"}'), kw['history']
    monkeypatch.setattr(wave, '_author_wave', author)
    monkeypatch.setattr(wave, '_push_canonical_memory', lambda *a: None)
    monkeypatch.setattr(wave, '_execute_branch', lambda **kw: pytest.fail('completed projects replayed'))
    result = wave.evolve_parallel_phase1(root=str(s.owner.rsi_dir / 'phase1'), target_direction='public target',
        actor_cfg=None, curriculum_cfg=None, memory_cfg=None, verifier_control_cfg=None,
        vm_factory=lambda: (SimpleNamespace(close=lambda: None), object()), corpus_path='unused',
        hooks=PracticeHooks(validate_corpus=lambda *a: None, audit_text=lambda *a, **kw: []),
        project_budget=8, checkpoint_projects=(0, 4, 8), max_parallel=2, target_query_conditioned=True,
        resume_completed_boundary=True, boundary_validator=validate_coding_boundary)
    assert result.status == 'saturated' and result.projects == 6 and len(calls) == 1
    assert _read_memory_tree(result.memory_dir) == s.memory
    assert json.loads((s.owner.rsi_dir / 'phase1/episodes/ep002/outcome.json').read_text())['terminal_outcome'] == 'FAIL'


@pytest.mark.asyncio
async def test_new_controller_rebuilds_session_identity_and_call_from_archive(stopped, monkeypatch):
    s = stopped
    prepare_resume(s.trial, s.owner.rsi_dir, owner=s.owner, instruction='public target')
    env = SimpleNamespace(trial_paths=SimpleNamespace(agent_dir=s.owner.logs_dir), exec=AsyncMock())
    s.owner.logs_dir.mkdir(parents=True)
    audit = SimpleNamespace(instruction='public target', validate=lambda *a: {}, text=lambda *a, **kw: [],
        transcripts=lambda *a, **kw: [], captured=lambda *a, **kw: [], memory=lambda *a, **kw: {})
    runtime = CodingRuntime(s.owner, env, s.owner.rsi_dir / 'runtime', asyncio.get_running_loop(), audit)
    async def restore(environment, session, role, cwd):
        assert session.session_id == s.sid and session.calls == 2 and role == 'curriculum' and cwd == '/home/user'
        return True
    runtime.checkpoints.restore = AsyncMock(side_effect=restore)
    runtime.checkpoints.save = AsyncMock(return_value={**s.checkpoint, 'call': 3})
    from benchmarks.deepswe.session import BitfunSession
    monkeypatch.setattr(BitfunSession, 'prepare', AsyncMock())
    monkeypatch.setattr(BitfunSession, 'collect', AsyncMock())
    async def execute(self, *a, **kw):
        assert self.calls == 2
        self.calls += 1
        return SessionOutcome('handoff', report={'final': True}, metrics={'model_calls': 1})
    monkeypatch.setattr(BitfunSession, 'execute', execute)
    result, session = await runtime.execute(SimpleNamespace(environment=env), 'next wave', 'curriculum',
        s.owner.rsi_dir / 'next', session_id=s.sid)
    assert result.status == 'done' and session.calls == 3
    runtime.checkpoints.restore.assert_awaited_once()
    with pytest.raises(PracticeInfrastructureError, match='Missing native checkpoint'):
        await runtime.execute(SimpleNamespace(environment=env), 'task', 'actor', s.owner.rsi_dir / 'missing',
                              session_id=str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_resumed_controller_advances_to_drs_and_fresh_final_baseline(stopped, monkeypatch):
    import benchmarks.deepswe.rsi as module
    from explore.practice_loop import _atomic_install_memory
    s = stopped
    s.owner.resume_from = s.trial
    original = snapshot(s.trial)
    target_environment = SimpleNamespace(environment_dir=s.task / 'environment')
    received = []
    hooks = PracticeHooks(validate_corpus=lambda *a: None, audit_text=lambda *a, **kw: [],
                          push_memory=lambda vm, path: received.append(_read_memory_tree(path)) or True)

    class Runtime:
        def __init__(self, *args):
            self.audit, self.hooks = args[-1], hooks
            self.checkpoints = SimpleNamespace(clean=str)
        async def run_engine(self, fn, *a, **kw):
            return fn(*a, **kw)
        def acquire(self):
            return SimpleNamespace(close=lambda: None), object()
        async def close(self):
            pass

    monkeypatch.setattr(module, 'CodingRuntime', Runtime)
    monkeypatch.setattr(module, 'CodingAudit', lambda *a: SimpleNamespace(path=s.root / 'corpus'))
    monkeypatch.setattr(wave, '_push_canonical_memory', lambda *a: None)
    def author(**kw):
        assert kw['history'][-1]['bitfun_session_id'] == s.sid
        return wave.WaveDecision('SATURATED', 'complete', (),
            '{"decision":"SATURATED","rationale":"complete","projects":[]}'), kw['history']
    monkeypatch.setattr(wave, '_author_wave', author)
    monkeypatch.setattr(wave, '_execute_branch', lambda **kw: pytest.fail('committed work replayed'))
    def phase2(runtime, instruction, initial):
        assert initial == s.memory and instruction == 'public target'
        memory = s.owner.rsi_dir / 'phase2/memory'
        _atomic_install_memory(str(memory), {**initial, 'drs.md': b'new lesson'})
        return s.owner.freeze(memory, s.owner.rsi_dir / 'phase2/memory_frozen')
    monkeypatch.setattr(s.owner, 'phase2', phase2)
    async def final_run(instruction, environment, context):
        assert environment is target_environment
    def baseline(logs, memory, training):
        assert not training and memory == {**s.memory, 'drs.md': b'new lesson'}
        return SimpleNamespace(run=final_run, summary={'verdict': 'PASS', 'stop_reason': 'verifier_pass'})
    monkeypatch.setattr(s.owner, 'baseline', baseline)
    await s.owner.run('public target', target_environment, SimpleNamespace())
    result = json.loads((s.owner.rsi_dir / 'result.json').read_text())
    assert result['status'] == 'completed' and result['frozen_memory_unchanged']
    assert json.loads((s.owner.rsi_dir / 'phase1/phase-result.json').read_text())['projects'] == 6
    assert received == [{**s.memory, 'drs.md': b'new lesson'}]
    assert snapshot(s.trial) == original
