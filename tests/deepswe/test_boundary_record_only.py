"""Leakage findings are retained as errors without rejecting training work."""

import io
import json
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from benchmarks.deepswe.baseline import BitfunBaseline
from benchmarks.deepswe.rsi import MemoryBaseline
from benchmarks.deepswe.rsi_runtime import CodingAudit
from explore.practice_loop import PracticeHooks, _audit_prompt, _audit_agent_artifacts, _capture_owned_tree


@pytest.fixture
def audit(tmp_path):
    tasks = tmp_path / 'tasks'
    (tasks / 'target').mkdir(parents=True)
    (tasks / 'target/instruction.md').write_text('Implement a public target')
    (tasks / 'other').mkdir()
    (tasks / 'other/instruction.md').write_text('another task has completely different private held out instructions here')
    return CodingAudit(tmp_path / 'rsi/corpus.json', 'Implement a public target', tasks)


def hooks(audit, **kwargs):
    return PracticeHooks(audit_text=audit.record_text,
        audit_transcripts=audit.record_transcripts,
        audit_captured=audit.record_captured, audit_memory=audit.memory, **kwargs)


def errors(audit):
    return [json.loads(line) for line in (audit.path.parent / 'boundary-errors.jsonl').read_text().splitlines()]


def tool_stream(root, *, quoted=False):
    root.mkdir(parents=True, exist_ok=True)
    event = {'event': {'type': 'ToolEvent', 'tool_event': {
        'event_type': 'Completed', 'tool_name': 'Read',
        'result': {'content': ('Do not read ' if quoted else 'cat ') + '/logs/verifier/reward.json'}}}}
    (root / 'stream-test.jsonl').write_text(json.dumps(event) + '\n')


@pytest.mark.parametrize('phase', ['curriculum', 'practice', 'verifier-report', 'distillation', 'reconciliation'])
def test_text_boundary_records_error_and_shared_engine_continues(audit, phase):
    _audit_prompt(hooks(audit), f'{phase}: cat /tests/test.sh', audit.instruction)
    record = errors(audit)[-1]
    assert record['finding_count'] == 1
    assert record['status'] == record['severity'] == 'error'
    assert record['policy'] == 'record_only' and record['training_action'] == 'continue'


@pytest.mark.parametrize('quoted', [False, True])
def test_actual_access_and_prompt_echo_both_log_without_rejecting(audit, tmp_path, quoted):
    sink = tmp_path / 'curriculum'
    tool_stream(sink, quoted=quoted)
    _audit_agent_artifacts(hooks(audit), str(sink), audit.instruction)
    assert errors(audit)[-1]['scope'] == 'transcripts'
    assert errors(audit)[-1]['findings'][0]['field'].endswith('.result.content')


def test_candidate_capture_continues_with_leakage_record(audit, tmp_path):
    sink = tmp_path / 'candidate'
    sink.mkdir()
    data = b'cat /tests/test.sh'
    with tarfile.open(sink / 'materials.tgz', 'w:gz') as archive:
        item = tarfile.TarInfo('evolution_project/leak.txt')
        item.size = len(data)
        archive.addfile(item, io.BytesIO(data))
    _capture_owned_tree(hooks(audit, capture_project=lambda *a, **kw: {'ok': True}),
                        object(), 'project', str(sink), audit.instruction)
    assert errors(audit)[-1]['scope'] == 'candidate'


def test_memory_leakage_is_recorded_without_dropping_files(audit, tmp_path):
    memory = {'private.md': b'cat /logs/verifier/reward.json',
              'other.md': b'another task has completely different private held out instructions here',
              'lesson.md': b'normal lesson'}
    accepted = hooks(audit).audit_memory(memory, audit.path, tmp_path / 'memory/audit_rejects.jsonl',
                                        authorized_instruction=audit.instruction, require_corpus=True)
    assert accepted == memory
    record = errors(audit)[-1]
    assert record['scope'] == 'memory'
    assert {item['kind'] for item in record['findings']} == {'sealed-evaluation-surface', 'other-task-instruction'}


@pytest.mark.parametrize('training', [True, False])
@pytest.mark.parametrize('role', ['actor', 'verifier'])
@pytest.mark.asyncio
async def test_phase2_and_phase3_roles_continue_after_findings(audit, tmp_path, monkeypatch, training, role):
    outcome = SimpleNamespace(status='handoff')
    monkeypatch.setattr(BitfunBaseline, '_invoke', AsyncMock(return_value=outcome))
    sink = tmp_path / 'target'
    tool_stream(sink)
    agent = MemoryBaseline(logs_dir=tmp_path / 'agent', memory={}, training=training,
                           boundary_audit=audit, bitfun_config={'ai': {'models': []}})
    result = await agent._invoke(SimpleNamespace(session_id=role, logs_dir=sink),
                                 object(), 'task', role, 'handoff')
    assert result is outcome
    assert errors(audit)[-1]['scope'] == 'transcripts'


def test_diagnostic_io_failure_does_not_interrupt_training(audit, monkeypatch):
    def fail(*args):
        raise OSError('disk diagnostic failure')
    monkeypatch.setattr('benchmarks.deepswe.rsi_runtime.write_json', fail)
    assert audit.record_text('cat /tests/test.sh') == []


def test_each_detection_is_retained_in_run_log(audit):
    audit.record_text('cat /tests/test.sh')
    audit.record_text('cat /tests/test.sh')
    assert len(errors(audit)) == 2


@pytest.mark.parametrize('surface', ['transcripts', 'captured'])
def test_audit_read_failure_is_nonfatal(audit, tmp_path, monkeypatch, surface):
    def fail(*args, **kwargs):
        raise PermissionError('unreadable audit artifact')
    monkeypatch.setattr(audit, surface, fail)
    record = getattr(audit, 'record_' + surface)
    assert record(tmp_path / surface) == []
    assert errors(audit)[-1]['findings'] == [{'kind': 'audit-read-error', 'error_type': 'PermissionError'}]
