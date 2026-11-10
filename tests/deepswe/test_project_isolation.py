import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config.settings import Config
from explore import phase1_wave as wave, unified_evolution as evolution
from explore.practice_loop import (
    PracticeHooks, PracticeBoundaryError, PracticeInfrastructureError,
    _atomic_install_memory, _manifest, _read_memory_tree,
)
from benchmarks.deepswe.rsi import BitfunRSI


def hooks(**kwargs):
    return PracticeHooks(validate_corpus=lambda *a: {}, audit_text=lambda *a, **k: [],
        audit_memory=lambda memory, *a, **kw: dict(memory), push_memory=lambda *a: True,
        **kwargs)


def run_wave(tmp_path, monkeypatch, failures=(), learning_failures=(), isolate=True, corrupt=False):
    seed = {'seed.md': b'trusted'}
    _atomic_install_memory(str(tmp_path / 'memory'), seed)
    projects = tuple(wave.WaveProject(f'p{i}', 'Build in /home/user/evolution_project') for i in range(1, 6))
    decision = wave.WaveDecision('WAVE', 'practice', projects, json.dumps({
        'decision': 'WAVE', 'rationale': 'practice',
        'projects': [{'id': p.project_id, 'instruction': p.instruction} for p in projects]}))
    monkeypatch.setattr(wave, '_author_wave', lambda **kw: (decision, []))
    monkeypatch.setattr(wave, '_capture_wave_fixtures', lambda **kw: None)
    monkeypatch.setattr(wave, '_push_canonical_memory', lambda *a: None)
    monkeypatch.setattr(wave, '_replay', lambda *a: None)
    visited, learned = [], []
    def factory():
        return SimpleNamespace(close=lambda: None), object()
    def execute(**kw):
        i = kw['project_index']; visited.append(i)
        kw['episode_dir'].mkdir(parents=True, exist_ok=True)
        (kw['episode_dir'] / 'private.txt').write_text(f'PRIVATE-BRANCH-{i}')
        if i in failures:
            raise PracticeBoundaryError('untrusted model output')
        return wave._BranchRuntime(i, kw['project'], kw['episode_dir'], *factory(), [],
            'handoff', 'grounded report', 'FAIL' if i == 3 else 'PASS', seed)
    monkeypatch.setattr(wave, '_execute_branch', execute)
    def commit(**kw):
        i = kw['branch'].project_index
        before = _read_memory_tree(str(kw['canonical_memory_dir']))
        after = {**before, f'lesson{i}.md': kw['branch'].terminal_outcome.encode()}
        _atomic_install_memory(str(kw['canonical_memory_dir']), after)
        if i in learning_failures:
            raise PracticeInfrastructureError('learning failed after staging bytes')
        if corrupt:
            _atomic_install_memory(str(tmp_path / 'memory'), {'CORRUPTED': b'bad'})
        learned.append(i)
        return {'before': _manifest(before), 'after': _manifest(after), 'changes': {}}
    monkeypatch.setattr(wave, '_commit_branch_memory', commit)
    result = wave.evolve_parallel_phase1(root=str(tmp_path), target_direction='target',
        actor_cfg=None, curriculum_cfg=None, memory_cfg=None, verifier_control_cfg=None,
        vm_factory=factory, corpus_path='unused', project_budget=1, max_parallel=1,
        hooks=hooks(), isolate_failed_projects=isolate)
    return result, visited, learned


def test_failed_branches_do_not_cancel_queue_or_promote_partial_memory(tmp_path, monkeypatch):
    result, visited, learned = run_wave(tmp_path, monkeypatch, failures={2}, learning_failures={4})
    assert visited == [1, 2, 3, 4, 5] and learned == [1, 3, 5]
    assert result.status == 'partial'
    assert _read_memory_tree(result.memory_dir) == {
        'seed.md': b'trusted', 'lesson1.md': b'PASS', 'lesson3.md': b'FAIL', 'lesson5.md': b'PASS'}
    state = json.loads((tmp_path / 'state.json').read_text())
    assert state['committed_projects'] == 3 and len(state['isolated_projects']) == 2
    for index in (2, 4):
        folder = tmp_path / 'quarantine/episodes' / f'ep{index:03d}'
        assert (folder / 'private.txt').exists()
        record = json.loads((folder / 'isolation.json').read_text())
        assert record['terminal_outcome'] is None and record['memory_promoted'] is False
        assert not (tmp_path / 'episodes' / f'ep{index:03d}').exists()
    feedback = (tmp_path / 'waves/wave_001/accepted_outcomes.md').read_text()
    assert 'PRIVATE-BRANCH-' not in feedback and 'PROJECT 2' not in feedback and 'PROJECT 4' not in feedback
    assert not (tmp_path / 'memory_journal/ep0004.tgz').exists()


def test_all_failed_projects_keep_prior_memory_without_fabricated_pass(tmp_path, monkeypatch):
    result, visited, learned = run_wave(tmp_path, monkeypatch, failures={1, 2, 3, 4, 5})
    assert result.status == 'partial' and len(visited) == 5 and learned == []
    assert _read_memory_tree(result.memory_dir) == {'seed.md': b'trusted'}


def test_default_reference_mode_still_aborts_on_boundary_failure(tmp_path, monkeypatch):
    result, _, learned = run_wave(tmp_path, monkeypatch, failures={2}, isolate=False)
    assert result.status == 'quarantined' and not learned


def test_canonical_memory_corruption_cannot_be_skipped(tmp_path, monkeypatch):
    result, _, _ = run_wave(tmp_path, monkeypatch, corrupt=True)
    assert result.status == 'infra'
    assert 'Trusted memory changed' in result.reason


@pytest.mark.parametrize('failure', ['interrupt', 'corpus'])
def test_global_failure_does_not_advance_to_partial(tmp_path, monkeypatch, failure):
    def broken_author(**kw):
        raise KeyboardInterrupt()
    def broken_corpus(*args):
        raise PracticeInfrastructureError('public corpus drift')
    monkeypatch.setattr(wave, '_author_wave', broken_author)
    monkeypatch.setattr(wave, '_push_canonical_memory', lambda *a: None)
    config = hooks()
    if failure == 'corpus':
        config.validate_corpus = broken_corpus
    result = wave.evolve_parallel_phase1(root=str(tmp_path), target_direction='target',
        actor_cfg=None, curriculum_cfg=None, memory_cfg=None, verifier_control_cfg=None,
        vm_factory=lambda: (SimpleNamespace(close=lambda: None), object()), corpus_path='unused',
        hooks=config, isolate_failed_projects=True)
    assert result.status == 'infra'
    assert not (tmp_path / 'quarantine').exists()


@pytest.mark.parametrize('failure_stage', ['practice', 'learning', 'curriculum'])
def test_phase2_preserves_committed_practice_and_isolates_failed_successor(tmp_path, monkeypatch, failure_stage):
    monkeypatch.setattr(evolution, '_fresh_vm', lambda *a: None)
    monkeypatch.setattr(evolution, '_push_canonical_memory', lambda *a: None)
    monkeypatch.setattr(evolution, '_require_owned_project_shape', lambda *a: None)
    monkeypatch.setattr(evolution, '_capture_owned_tree', lambda *a: None)
    calls = 0
    def author(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2 and failure_stage == 'curriculum':
            raise PracticeInfrastructureError('malformed curriculum completion')
        return SimpleNamespace(token='PROJECT', body='Build in /home/user/evolution_project',
                               text='project'), [], None
    monkeypatch.setattr(evolution, '_run_handoff_phase', author)
    def practice(**kw):
        if kw['project_index'] == 2 and failure_stage == 'practice':
            raise PracticeBoundaryError('private failed evidence')
        return {'before_memory': _read_memory_tree(str(kw['memory_dir'])),
            'actor_history': [], 'actor_handoff': 'done', 'terminal_outcome': 'FAIL',
            'verifier_report': 'VERDICT: FAIL\nobserved defect'}
    monkeypatch.setattr(evolution, '_run_practice_attempt', practice)
    def learn(**kw):
        memory = {**kw['before_memory'], f'lesson{calls}.md': b'grounded FAIL lesson'}
        _atomic_install_memory(str(kw['memory_dir']), memory)
        if calls == 2:
            raise PracticeInfrastructureError('unfinished memory learning')
        return memory, 'diagnosis', []
    monkeypatch.setattr(evolution, '_promote_learning', learn)
    initial = {'seed.md': b'prior target learning'}
    result = evolution.evolve_until_ready(object(), str(tmp_path), 'target',
        'VERDICT: FAIL\nobserved defect', 'diagnosis', initial,
        Config(), Config(), Config(), Config(), corpus_path='unused',
        trigger_authority='phase2_outcome_protocol', hooks=hooks(read_guest_text=lambda *a: ''),
        isolate_failed_projects=True)
    assert result.status == 'partial'
    assert _read_memory_tree(result.memory_dir) == {**initial, 'lesson1.md': b'grounded FAIL lesson'}
    assert json.loads((tmp_path / 'episodes/ep001/outcome.json').read_text())['terminal_outcome'] == 'FAIL'
    state = json.loads((tmp_path / 'state.json').read_text())
    assert len(state['isolated_projects']) == 1
    assert 'lesson2.md' not in _read_memory_tree(result.memory_dir)


@pytest.mark.parametrize('stage', ['target', 'learning', 'practice', 'verification', 'unverified', 'setup', 'adopt'])
def test_rsi_phase2_freezes_only_last_trusted_memory_after_subtask_failure(tmp_path, monkeypatch, stage):
    import benchmarks.deepswe.rsi as module
    owner = BitfunRSI(logs_dir=tmp_path / 'agent', bitfun_config={'ai': {'models': [{
        'base_url': 'https://model.invalid/v1', 'api_key': '${TEST_KEY}'}]}}, isolate_failed_projects=True)
    runtime = SimpleNamespace(hooks=hooks(), audit=SimpleNamespace(path=tmp_path / 'corpus'),
        checkpoints=SimpleNamespace(clean=str), call=asyncio.run,
        adopt=lambda *a, **kw: [])
    def acquire(**kwargs):
        if stage == 'setup':
            raise OSError('task container unavailable')
        return SimpleNamespace(close=lambda: None), SimpleNamespace(environment=SimpleNamespace(
            trial_paths=SimpleNamespace(agent_dir=tmp_path / 'environment/agent')))
    runtime.acquire = acquire
    if stage == 'adopt':
        def broken_adopt(*args, **kwargs):
            raise PracticeInfrastructureError('native checkpoint unavailable')
        runtime.adopt = broken_adopt
    def baseline(*args, **kwargs):
        return SimpleNamespace(run=AsyncMock(side_effect=RuntimeError('task runtime failed') if stage == 'target' else None),
            active_actor=SimpleNamespace(session_id='target-actor'), terminal_report=SimpleNamespace(model_dump=lambda: {'verdict': 'FAIL'}),
            summary={} if stage == 'verification' else {'status': 'incomplete', 'stop_reason': 'budget'}
            if stage == 'unverified' else {'status': 'learning'})
    monkeypatch.setattr(owner, 'baseline', baseline)
    def learn(**kw):
        memory = {**kw['before_memory'], 'target.md': b'valid target lesson'}
        _atomic_install_memory(str(kw['memory_dir']), memory)
        if stage == 'learning':
            raise PracticeInfrastructureError('incomplete target learning')
        return memory, 'diagnosis', []
    monkeypatch.setattr(module, '_promote_learning', learn)
    def evolve(vm, root, direction, report, diagnosis, memory, *a, **kw):
        root = Path(root)
        accepted = {**memory, 'practice.md': b'previous complete practice'}
        _atomic_install_memory(str(root / 'memory'), accepted)
        (root / 'state.json').write_text(json.dumps({'memory_manifest': _manifest(accepted),
            'isolated_projects': [{'status': 'isolated', 'memory_promoted': False}]}))
        return SimpleNamespace(root=str(root), status='partial', memory_dir=str(root / 'memory'))
    monkeypatch.setattr(module, 'evolve_until_ready', evolve)
    memory, _ = owner.phase2(runtime, 'target', {'seed.md': b'BRS memory'})
    assert memory == ({'seed.md': b'BRS memory', 'target.md': b'valid target lesson',
                       'practice.md': b'previous complete practice'} if stage == 'practice' else {'seed.md': b'BRS memory'})
    result = json.loads((owner.rsi_dir / 'phase2/phase-result.json').read_text())
    assert result['status'] == 'partial' and result['verdict'] is None and result['transition_ready']
    assert owner.training_degraded


@pytest.mark.asyncio
async def test_partial_learning_reaches_fresh_phase3_with_explicit_degraded_status(tmp_path, monkeypatch):
    import benchmarks.deepswe.rsi as module
    owner = BitfunRSI(logs_dir=tmp_path / 'agent', bitfun_config={'ai': {'models': [{
        'base_url': 'https://model.invalid/v1', 'api_key': '${TEST_KEY}'}]}}, isolate_failed_projects=True)
    target = SimpleNamespace(environment_dir=tmp_path / 'tasks/task/environment')
    def phase1(**kw):
        assert kw['isolate_failed_projects']
        memory = Path(kw['root']) / 'memory'
        _atomic_install_memory(str(memory), {'good.md': b'accepted BRS memory'})
        return SimpleNamespace(status='partial', memory_dir=str(memory), reason='one isolated branch')
    monkeypatch.setattr(module, 'evolve_parallel_phase1', phase1)
    monkeypatch.setattr(module, 'CodingAudit', lambda *a: SimpleNamespace(path=tmp_path / 'corpus'))
    class Runtime:
        def __init__(self, *args):
            self.hooks = hooks()
            self.audit = args[-1]
            self.checkpoints = SimpleNamespace(clean=str)
        async def run_engine(self, fn, *args, **kw):
            return await asyncio.to_thread(fn, *args, **kw)
        def acquire(self, **kwargs):
            return SimpleNamespace(close=lambda: None), SimpleNamespace(environment=SimpleNamespace(
                trial_paths=SimpleNamespace(agent_dir=tmp_path / 'practice/agent')))
        def call(self, coroutine):
            return asyncio.run(coroutine)
        async def close(self):
            pass
    monkeypatch.setattr(module, 'CodingRuntime', Runtime)
    final_calls = []
    def baseline(logs, memory, training):
        async def run(instruction, environment, context):
            if training:
                raise PracticeInfrastructureError('isolated target attempt')
            assert environment is target and memory == {'good.md': b'accepted BRS memory'}
            final_calls.append(instruction)
        return SimpleNamespace(run=run, summary={'verdict': 'PASS', 'stop_reason': 'verifier_pass'},
                               populate_context_post_run=lambda ctx: None)
    monkeypatch.setattr(owner, 'baseline', baseline)
    context = SimpleNamespace(metadata=None)
    await owner.run('original task', target, context)
    owner.populate_context_post_run(context)
    assert final_calls == ['original task']
    outcome = json.loads((owner.rsi_dir / 'result.json').read_text())
    assert outcome['status'] == 'completed' and outcome['training_status'] == 'partial'
    assert outcome['phase1_status'] == 'partial' and outcome['phase3_verdict'] == 'PASS'
    assert context.metadata['rsi']['training_status'] == 'partial'
    phase2 = json.loads((owner.rsi_dir / 'phase2/phase-result.json').read_text())
    assert phase2['verdict'] is None and phase2['status'] == 'partial'
