from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.osworld.phase2 import _evolution_artifact_index
from core.self_evolving_loop import (
    ActorLearning,
    EvolutionResult,
    EvolutionStatus,
    SelfEvolvingLoopHooks,
    TargetVerdict,
    TargetVerification,
)
from explore.practice_loop import PracticeInfrastructureError, _manifest
from explore.phase2_recovery import Phase2CurriculumRecovery
from explore.post_target_learning_recovery import (
    ZERO_PROJECT_INFRA_KIND,
    _target_trigger,
    _target_trigger_text,
    build_zero_project_infra_plan,
    continue_after_target_learning,
)
from explore.target_learning import _memory_tree_sha256


@dataclass
class Config:
    max_iters: int = 500
    wall_clock_secs: float = 36000


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def boundary(tmp_path):
    root = tmp_path / 'attempt'
    cycle = root / 'target_cycles/cycle_001'
    learning = cycle / 'terminal_learning'
    memory = {'lesson.md': b'grounded actor memory'}
    (root / 'active_memory').mkdir(parents=True)
    (root / 'active_memory/lesson.md').write_bytes(memory['lesson.md'])
    cfg = Config()
    config_file = tmp_path / 'config.json'
    save(config_file, asdict(cfg))
    report = 'VERDICT: PASS\nObserved target evidence.'
    diagnosis = 'Actor-generated diagnosis.'
    pairs = [{'role': 'user', 'content': 'original observation'},
             {'role': 'assistant', 'content': 'original response'}]
    transcripts = [cycle / 'transcript.json',
                   learning / 'memory_distillation/segment_000/transcript.json',
                   learning / 'memory_reconciliation/segment_000/transcript.json',
                   learning / 'learning_diagnosis/attempt_000/segment_000/transcript.json']
    for i, path in enumerate(transcripts, 1):
        save(path, {'system': 'original system', 'messages': pairs * i})
    save(learning / 'outcome.json', dict(target_cycle=1, target_verifier_verdict='PASS',
        memory_before={}, memory_after=_manifest(memory), verifier_report=report,
        actor_learning_diagnosis=diagnosis, active_history_messages_before=2,
        active_history_messages_after=8))
    (learning / 'learning_diagnosis.md').write_text(diagnosis)
    verdict = cycle / 'iter_001/verify2.json'
    save(verdict, dict(verdict='pass', findings=report))
    save(root / 'manifest.json', dict(target_direction_sha256=hashlib.sha256(b'target').hexdigest(),
        initial_memory={'manifest': {}},
        protocol=dict(curriculum_memory_access='none', stop_policy='curriculum_review',
                      official_evaluator_calls=0, official_evaluator_feedback_enters_learning=False),
        configs={'curriculum': dict(path=str(config_file), sha256=hashlib.sha256(config_file.read_bytes()).hexdigest(),
                                    effective=asdict(cfg))}))
    events = [dict(event='TARGET_LEARNING_COMPLETED', payload=dict(target_cycle=1, verdict='PASS')),
              dict(event='PASS_NEXT_EXPERIENCE_REVIEW_STARTED', payload=dict(target_cycle=1, prospective_evolution=1))]
    (root / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
    log = tmp_path / 'failed.log'
    log.write_text('TimeoutError: VM failed to become ready within timeout period')
    plan = dict(recovery_kind='post_target_learning_boot_failure', status='admitted_for_isolated_recovery',
        attempt_root=str(root), completed_projects=0, completed_curriculum_turns=0,
        prior_curriculum_iters=0, prior_curriculum_wall_secs=0, input_sha256={},
        curriculum_memory_access='none', target_verification=str(verdict), failed_runtime_log=str(log))
    path = tmp_path / 'recovery.json'
    save(path, plan)
    return SimpleNamespace(root=root, cycle=cycle, learning=learning, memory=memory,
                           plan=plan, path=path, cfg=cfg, transcripts=transcripts)


def admit(b):
    return Phase2CurriculumRecovery.admit(b.path, b.root, {'curriculum': b.cfg}, {}, 'target', 'curriculum_review')


@pytest.fixture
def zero_project_infra_boundary(boundary):
    b = boundary
    failed = b.root / 'evolution_cycles/cycle_001'
    for name in ('curriculum', 'episodes', 'memory_journal'):
        (failed / name).mkdir(parents=True)
    (failed / 'memory').mkdir(parents=True)
    (failed / 'memory/lesson.md').write_bytes(b.memory['lesson.md'])
    learning = json.loads((b.learning / 'outcome.json').read_text())
    save(failed / 'trigger/outcome.json', _target_trigger('target', learning))
    (failed / 'trigger/outcome.md').write_text(
        _target_trigger_text('target', learning))
    reason = (
        'fresh VM boundary failed: null pre-seal boundary failed: '
        'outer-container firewall verification failed (exit 1): '
        'ebtables: No chain/target/match by that name\n')
    failed_runtime_log = Path(b.plan['failed_runtime_log'])
    failed_runtime_log.write_text('RuntimeError: ' + reason)
    tree = _memory_tree_sha256(b.memory)
    save(failed / 'state.json', {
        'schema_version': 1,
        'kind': 'unified_evolution_cycle',
        'status': 'infra',
        'projects': 0,
        'learning_experiences': 0,
        'reason': reason,
        'terminal_text': '',
        'target_sha256': hashlib.sha256(b'target').hexdigest(),
        'trigger_report_sha256': hashlib.sha256(
            learning['verifier_report'].encode()).hexdigest(),
        'trigger_authority': 'phase2_outcome_protocol',
        'triggering_outcome': 'PASS',
        'curriculum_route_sha256': hashlib.sha256(b'').hexdigest(),
        'memory_manifest': _manifest(b.memory),
        'memory_tree_sha256': tree,
    })
    events = [json.loads(line) for line in
              (b.root / 'events.jsonl').read_text().splitlines()]
    events.append({
        'event': 'EVOLUTION_EVOLVE_TRIGGER_IMPORTED',
        'payload': {
            'status': 'completed',
            'state': {
                'project_open': False,
                'memory_phase_open': False,
                'recovery_open': False,
            },
            'payload': {
                'target_sha256': hashlib.sha256(b'target').hexdigest(),
                'trigger_report_sha256': hashlib.sha256(
                    learning['verifier_report'].encode()).hexdigest(),
                'trigger_diagnosis_sha256': hashlib.sha256(
                    learning['actor_learning_diagnosis'].encode()).hexdigest(),
                'trigger_authority': 'phase2_outcome_protocol',
                'triggering_outcome': 'PASS',
                'curriculum_route_sha256': hashlib.sha256(b'').hexdigest(),
                'memory_tree_sha256': tree,
            },
        },
    })
    (b.root / 'events.jsonl').write_text(
        '\n'.join(json.dumps(event) for event in events))
    b.plan = build_zero_project_infra_plan(
        b.root, 'none', failed_runtime_log)
    save(b.path, b.plan)
    b.failed_cycle = failed
    b.failed_runtime_log = failed_runtime_log
    return b


def test_admits_without_replaying_target_or_curriculum(boundary):
    result = admit(boundary)
    assert result.post_target_learning
    assert result.initial_evolution_memory == boundary.memory
    assert result.history == [] and result.notes == ''
    assert result.projects == result.completed_curriculum_turns == 0
    assert result.remaining_iters == 500 and result.remaining_wall == 36000
    assert not (boundary.root / 'evolution_cycles').exists()


@pytest.mark.parametrize('marker', ['result.json', 'memory_frozen', 'evolution_cycles', 'audit_rejects.jsonl'])
def test_rejects_advanced_or_rejected_boundary(boundary, marker):
    (boundary.root / marker).touch()
    with pytest.raises(PracticeInfrastructureError):
        admit(boundary)


@pytest.mark.parametrize('change', ['memory', 'verdict', 'diagnosis', 'context', 'config',
                                    'event', 'curriculum', 'feedback', 'boot_fault', 'input_hash'])
def test_rejects_invalid_saved_boundary(boundary, change):
    b = boundary
    if change == 'memory':
        (b.root / 'active_memory/lesson.md').write_text('changed')
    elif change == 'verdict':
        save(Path(b.plan['target_verification']), dict(verdict='wrong', findings='FAIL'))
    elif change == 'diagnosis':
        (b.learning / 'learning_diagnosis.md').write_text('host advice')
    elif change == 'context':
        save(b.transcripts[-1], dict(system='changed', messages=[]))
    elif change == 'config':
        b.cfg.max_iters += 1
    elif change == 'event':
        (b.root / 'events.jsonl').write_text('{}\n{}')
    elif change == 'curriculum':
        b.plan['completed_curriculum_turns'] = 1; save(b.path, b.plan)
    elif change == 'feedback':
        save(b.root / 'grading.json', dict(score=1))
    elif change == 'boot_fault':
        Path(b.plan['failed_runtime_log']).write_text('ordinary task failure')
    elif change == 'input_hash':
        b.plan['input_sha256'] = {str(b.root / 'events.jsonl'): 'bad'}; save(b.path, b.plan)
    with pytest.raises((PracticeInfrastructureError, KeyError)):
        admit(b)


@pytest.mark.parametrize('status,suffix', [(EvolutionStatus.READY_FOR_RETRY, 'ready'),
                                         (EvolutionStatus.STALLED, 'stalled')])
def test_zero_practice_keeps_existing_pass_without_new_target(boundary, status, suffix):
    recovery = admit(boundary)
    calls = []
    def evolve(*args):
        calls.append(args)
        return EvolutionResult(status, boundary.memory, 0, 'original Curriculum decision')
    hooks = SimpleNamespace(evolve=evolve, on_event=lambda *args: None)
    result = continue_after_target_learning(recovery, 'target', hooks)
    assert len(calls) == 1 and calls[0][1] is TargetVerdict.PASS
    assert result.termination == 'verifier_pass_curriculum_' + suffix
    assert result.environment is None and result.output is None
    assert result.target_cycles == 1 and result.practice_projects == result.evolutions == 0
    assert result.memory == boundary.memory


def test_zero_practice_cannot_rewrite_actor_memory(boundary):
    hooks = SimpleNamespace(evolve=lambda *args: EvolutionResult(EvolutionStatus.READY_FOR_RETRY, {}, 0))
    with pytest.raises(PracticeInfrastructureError, match='changed Actor memory'):
        continue_after_target_learning(admit(boundary), 'target', hooks)


@pytest.mark.parametrize('status', [EvolutionStatus.READY_FOR_RETRY, EvolutionStatus.STALLED])
def test_completed_practice_requires_fresh_target(boundary, monkeypatch, status):
    import core.self_evolving_loop as loop
    calls = []
    sentinel = object()
    def next_target(direction, memory, hooks, *, start):
        calls.append(start)
        assert memory == {'new.md': b'practice memory'}
        return sentinel
    monkeypatch.setattr(loop, 'run_self_evolving_loop', next_target)
    hooks = SimpleNamespace(evolve=lambda *args: EvolutionResult(status, {'new.md': b'practice memory'}, 1),
                            on_event=lambda *args: None)
    assert continue_after_target_learning(admit(boundary), 'target', hooks) is sentinel
    assert calls[0].target_cycles == calls[0].evolutions == calls[0].practice_projects == 1
    assert calls[0].final_after_stall == (status is EvolutionStatus.STALLED)


def test_zero_project_infra_admission_preserves_failed_cycle(
        zero_project_infra_boundary):
    b = zero_project_infra_boundary
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in b.failed_cycle.rglob('*') if path.is_file()}

    recovery = admit(b)

    assert recovery.recovery_kind == ZERO_PROJECT_INFRA_KIND
    assert recovery.post_target_learning
    assert recovery.projects == recovery.completed_curriculum_turns == 0
    assert recovery.initial_evolution_memory == b.memory
    assert recovery.evolution_artifact_offset == 1
    assert recovery.failed_evolution_cycle == str(b.failed_cycle.resolve())
    assert recovery.cycle == b.root / 'evolution_cycles/cycle_002'
    assert before == {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in b.failed_cycle.rglob('*') if path.is_file()}


def test_zero_project_infra_admits_missing_filter_table(
        zero_project_infra_boundary):
    b = zero_project_infra_boundary
    reason = (
        "fresh VM boundary failed: null pre-seal boundary failed: "
        "outer-container firewall verification failed (exit 255): "
        "The kernel doesn't support the ebtables 'filter' table.\n"
    )
    state_path = b.failed_cycle / 'state.json'
    state = json.loads(state_path.read_text())
    state['reason'] = reason
    save(state_path, state)
    b.failed_runtime_log.write_text('RuntimeError: ' + reason)
    b.plan = build_zero_project_infra_plan(
        b.root, 'none', b.failed_runtime_log)
    save(b.path, b.plan)

    recovery = admit(b)

    assert recovery.recovery_kind == ZERO_PROJECT_INFRA_KIND
    assert recovery.projects == 0


@pytest.mark.parametrize('change', [
    'fault', 'projects', 'learning', 'curriculum', 'practice', 'event',
    'trigger', 'memory', 'log', 'extra_cycle', 'unhashed',
])
def test_zero_project_infra_rejects_drift(
        zero_project_infra_boundary, change):
    b = zero_project_infra_boundary
    state_path = b.failed_cycle / 'state.json'
    state = json.loads(state_path.read_text())
    if change == 'fault':
        state['reason'] = 'fresh VM boundary failed: ordinary timeout'
        save(state_path, state)
    elif change == 'projects':
        state['projects'] = 1
        save(state_path, state)
    elif change == 'learning':
        state['learning_experiences'] = 1
        save(state_path, state)
    elif change == 'curriculum':
        (b.failed_cycle / 'curriculum/turn.txt').write_text('model call')
    elif change == 'practice':
        (b.failed_cycle / 'episodes/ep001').mkdir()
    elif change == 'event':
        events = (b.root / 'events.jsonl').read_text().splitlines()
        (b.root / 'events.jsonl').write_text('\n'.join(events[:-1]))
    elif change == 'trigger':
        trigger = json.loads(
            (b.failed_cycle / 'trigger/outcome.json').read_text())
        trigger['verifier_outcome'] = 'FAIL'
        save(b.failed_cycle / 'trigger/outcome.json', trigger)
    elif change == 'memory':
        (b.failed_cycle / 'memory/lesson.md').write_text('changed')
    elif change == 'log':
        b.failed_runtime_log.write_text('unrelated runtime failure')
    elif change == 'extra_cycle':
        (b.root / 'evolution_cycles/cycle_002').mkdir()
    else:
        b.plan['input_sha256'].pop(str(
            (b.failed_cycle / 'state.json').resolve()))
        save(b.path, b.plan)
        with pytest.raises(PracticeInfrastructureError):
            admit(b)
        return
    b.plan = build_zero_project_infra_plan(
        b.root, 'none', b.failed_runtime_log)
    save(b.path, b.plan)
    with pytest.raises(PracticeInfrastructureError):
        admit(b)


def test_zero_project_recovery_does_not_replay_committed_target(
        zero_project_infra_boundary):
    recovery = admit(zero_project_infra_boundary)
    calls = []

    def evolve(*args):
        calls.append(args)
        return EvolutionResult(
            EvolutionStatus.READY_FOR_RETRY,
            zero_project_infra_boundary.memory,
            0,
            'no additional practice',
        )

    events = []
    hooks = SimpleNamespace(
        evolve=evolve,
        on_event=lambda event, payload: events.append((event, payload)),
    )
    result = continue_after_target_learning(recovery, 'target', hooks)

    assert len(calls) == 1
    assert calls[0][1] is TargetVerdict.PASS
    assert calls[0][-1] == 1
    assert result.target_cycles == 1
    assert result.practice_projects == result.evolutions == 0
    assert result.termination == 'verifier_pass_curriculum_ready'
    assert events[-1][1]['recovered_after_target_learning'] is True


def test_recovered_practice_starts_target_two_and_next_artifact_cycle_three(
        zero_project_infra_boundary):
    recovery = admit(zero_project_infra_boundary)
    target_starts = []
    evolution_calls = []

    def fresh_environment(_direction, cycle):
        target_starts.append(cycle)
        return {'cycle': cycle}

    def evolve(_direction, _verdict, _report, _diagnosis, memory, index):
        evolution_calls.append((index, _evolution_artifact_index(recovery, index)))
        learned = dict(memory)
        learned[f'practice_{index}.md'] = b'committed practice'
        return EvolutionResult(
            EvolutionStatus.READY_FOR_RETRY if index == 1
            else EvolutionStatus.STALLED,
            learned,
            1,
            'completed practice',
        )

    hooks = SelfEvolvingLoopHooks(
        fresh_target_environment=fresh_environment,
        fresh_target_verifier=lambda environment, _direction, _cycle: environment,
        fresh_target_actor=lambda memory, cycle: {'memory': memory, 'cycle': cycle},
        actor_work=lambda actor, _verifier, environment, _direction: environment,
        verifier_verify=lambda _verifier, _environment, _direction, output:
            TargetVerification(
                TargetVerdict.FAIL if output['cycle'] == 2 else TargetVerdict.PASS,
                'VERDICT: FAIL\nmore practice' if output['cycle'] == 2
                else 'VERDICT: PASS\nfinal target'),
        actor_learn=lambda actor, _environment, _direction, _output,
            _verdict, _report, memory: ActorLearning(
                dict(memory), f"learning for target cycle {actor['cycle']}"),
        evolve=evolve,
        learn_on_pass=True,
        curriculum_after_pass=True,
    )

    result = continue_after_target_learning(recovery, 'target', hooks)

    assert target_starts == [2, 3]
    assert evolution_calls == [(1, 2), (2, 3)]
    assert result.target_cycles == 3
    assert result.evolutions == result.practice_projects == 2
    assert result.termination == 'verifier_pass'
