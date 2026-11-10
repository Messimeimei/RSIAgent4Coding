import io
import json
import os
import subprocess
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from benchmarks.deepswe.contracts import HandoffError, read_role_completion
from benchmarks.deepswe.rsi_integrity import (
    inventory, prepare_working_copy, recover, verify_working_copy,
)
from benchmarks.deepswe.rsi_runtime import CodingAudit
from benchmarks.deepswe.session import BitfunSession


@pytest.fixture
def audit(tmp_path):
    target = 'implement a local candidate without copying another project'
    for name, text in {
        'target': target,
        'other': 'another task has completely different private held out instructions here',
        'placeholder': 'Default heading display: [[p#h]] -> [p > h](p#h), [[#h]] -> [h](#h)',
    }.items():
        path = tmp_path / 'tasks' / name / 'instruction.md'
        path.parent.mkdir(parents=True)
        path.write_text(text)
    return CodingAudit(tmp_path / 'corpus.json', target, tmp_path / 'tasks')


def archive(root, files):
    with tarfile.open(root / 'materials.tgz', 'w:gz') as stream:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            stream.addfile(info, io.BytesIO(content))


def test_machine_code_and_placeholder_tokens_do_not_become_other_task_text(audit, tmp_path):
    binary = bytes.fromhex('7f454c4600ff482400502400482400502400482400482400482400482400')
    archive(tmp_path, {'target/debug/optimize': binary})
    assert audit.captured(tmp_path) == []
    assert audit.text('h p h p h h h h') == []
    diagnostic = json.loads((tmp_path / 'boundary-audit.json').read_text())
    assert diagnostic['status'] == 'passed' and diagnostic['binary_files_scanned'] == 1


@pytest.mark.parametrize('prefix', [b'', b'\x7fELF\x00\xff'])
def test_real_other_task_text_is_detected_in_source_and_embedded_binary_strings(audit, tmp_path, prefix):
    archive(tmp_path, {'candidate': prefix + b'another task has completely different private held out instructions here\x00'})
    hits = audit.captured(tmp_path)
    assert any(hit['kind'] == 'other-task-instruction' for hit in hits)
    assert hits[0]['member'] == 'candidate'


def test_binary_segments_are_not_joined_across_invalid_bytes(audit, tmp_path):
    archive(tmp_path, {'binary': b'another task has completely\x00different private held out instructions here'})
    assert audit.captured(tmp_path) == []


def test_prohibition_prompt_does_not_self_trigger_but_actual_access_does(audit, tmp_path):
    (tmp_path / 'transcript.json').write_text(json.dumps({'prompt': 'Never read /logs/verifier/reward.json'}))
    stream = tmp_path / 'stream-test.jsonl'
    stream.write_text(json.dumps({'event': {'type': 'TextChunk', 'text': 'Do not read /logs/verifier/reward.json'}})+'\n')
    assert audit.transcripts(tmp_path) == []
    stream.write_text(json.dumps({'event': {'type': 'ToolEvent', 'tool_event': {
        'event_type': 'Started', 'tool_name': 'ExecCommand',
        'params': {'cmd': 'ls /logs/verifier; cat /logs/verifier/reward.json'}}}})+'\n')
    hits = audit.transcripts(tmp_path)
    assert hits[0]['kind'] == 'sealed-evaluation-surface'
    assert hits[0]['line'] == 1 and hits[0]['field'].endswith('.params.cmd')
    assert json.loads((tmp_path / 'boundary-audit.json').read_text())['status'] == 'rejected'


def cache_value(duration):
    return json.dumps({'version': '3.0.0', 'results': [[':test.ts', {'duration': duration, 'failed': False}]]})


def candidate(tmp_path):
    root = tmp_path / 'original'
    cache = root / 'node_modules/.vite/vitest' / ('a' * 40) / 'results.json'
    cache.parent.mkdir(parents=True)
    cache.write_text(cache_value(1))
    (root / 'source.ts').write_text('export const answer = 42;')
    return root, cache.relative_to(root)


def test_existing_vitest_cache_can_change_only_in_verified_working_copy(tmp_path):
    root, cache = candidate(tmp_path)
    before = inventory([str(root)])
    copy = tmp_path / 'scratch/candidate'
    workspace = prepare_working_copy(before, copy)
    (copy / cache).write_text(cache_value(2))
    result = verify_working_copy(workspace)
    assert result['status'] == 'verified' and result['result_caches'] == [cache.as_posix()]
    assert recover(before)['status'] == 'unchanged'
    assert inventory([str(root)]) == before
    (root / cache).write_text(cache_value(2))
    assert recover(before)['status'] == 'rejected'


@pytest.mark.parametrize('mutation', ['source', 'missing', 'extra', 'fake_cache', 'symlink'])
def test_working_copy_cannot_hide_source_changes_or_unrecognized_cache_files(tmp_path, mutation):
    root, cache = candidate(tmp_path)
    copy = tmp_path / 'scratch/candidate'
    workspace = prepare_working_copy(inventory([str(root)]), copy)
    if mutation == 'source':
        (copy / 'source.ts').write_text('export const answer = 0;')
    elif mutation == 'missing':
        (copy / 'source.ts').unlink()
    elif mutation == 'extra':
        (copy / cache.parent / 'injected.js').write_text('malicious source')
    elif mutation == 'fake_cache':
        (copy / cache).write_text('{"code":"changed"}')
    else:
        (copy / cache).unlink()
        (copy / cache).symlink_to(root / cache)
    assert verify_working_copy(workspace)['status'] == 'rejected'


def test_copy_rebases_internal_links_without_touching_original(tmp_path):
    root, cache = candidate(tmp_path)
    (root / 'cache-link').symlink_to(root / cache)
    before = inventory([str(root)])
    copy = tmp_path / 'scratch/candidate'
    prepare_working_copy(before, copy)
    (copy / 'cache-link').write_text(cache_value(3))
    assert (root / cache).read_text() == cache_value(1)
    assert inventory([str(root)]) == before


def test_working_copy_never_replaces_an_ancestor_of_original(tmp_path):
    root, _ = candidate(tmp_path)
    before = inventory([str(root)])
    with pytest.raises(ValueError, match='Invalid verification workspace'):
        prepare_working_copy(before, root.parent)
    assert inventory([str(root)]) == before


def test_working_copy_git_stat_refresh_is_not_a_source_edit(tmp_path):
    root, _ = candidate(tmp_path)
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', 'source.ts'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'base'], check=True)
    before = inventory([str(root)])
    copy = tmp_path / 'scratch/candidate'
    workspace = prepare_working_copy(before, copy)
    file = copy / 'source.ts'
    previous = file.stat()
    os.utime(file, ns=(previous.st_atime_ns, previous.st_mtime_ns + 2_000_000_000))
    subprocess.run(['git', '-C', str(copy), 'update-index', '--really-refresh'], check=True)
    report = verify_working_copy(workspace)
    assert report['status'] == 'verified'
    assert report['git_index_metadata_drift'] == ['.git/index']
    assert inventory([str(root)]) == before


@pytest.mark.parametrize('update,reason', [
    ({'handoff_id': 'old'}, 'current invocation'),
    ({'summary': 'extra'}, 'Only kind'),
    ({'final': 'true'}, 'JSON boolean'),
    ({'final': 1}, 'JSON boolean'),
    ({'kind': 'actor_done'}, 'rsi_done'),
    ({'checks': {}}, 'array'),
])
def test_completion_errors_are_specific_without_relaxing_contract(tmp_path, update, reason):
    path = tmp_path / 'done.json'
    value = {'kind': 'rsi_done', 'handoff_id': 'current', 'final': True, **update}
    path.write_text(json.dumps(value))
    with pytest.raises(HandoffError, match=reason):
        read_role_completion(path, 'current')


def test_completion_accepts_current_valid_identity_and_checks(tmp_path):
    path = tmp_path / 'done.json'
    value = {'kind': 'rsi_done', 'handoff_id': 'current', 'final': True,
             'checks': [{'desc': 'check output', 'probe': 'python check.py'}]}
    path.write_text(json.dumps(value))
    report, checks = read_role_completion(path, 'current', require_checks=True)
    assert report == value and checks[0].probe == 'python check.py'


@pytest.mark.asyncio
async def test_final_report_read_propagates_diagnostic_even_with_stale_active_tool(tmp_path):
    model = {'ai': {'models': [{'id': 'test', 'model_name': 'test',
        'base_url': 'https://model.invalid/v1', 'api_key': '${TEST_KEY}'}]}}
    session = BitfunSession(logs_dir=tmp_path, bitfun_config=model)
    async def work(*args, **kwargs):
        (tmp_path / 'handoffs/current.json').write_text(json.dumps({
            'kind': 'rsi_done', 'handoff_id': 'old', 'final': True}))
        (tmp_path / f'stream-{session.session_id}-001.jsonl').write_text(json.dumps({'event': {
            'type': 'ToolEvent', 'session_id': session.session_id,
            'tool_event': {'event_type': 'Started', 'tool_id': 'pending'}}})+'\n')
    session.exec_as_agent = work
    outcome = await session.execute(SimpleNamespace(upload_file=AsyncMock()), 'test', 3,
        role='curriculum', handoff_id='current',
        terminal_reader=lambda path: read_role_completion(path, 'current')[0])
    assert outcome.status == 'invalid_handoff'
    assert 'current invocation' in outcome.handoff_error
    saved = json.loads(next(tmp_path.glob('checkpoint-*.json')).read_text())
    assert saved['handoff_error'] == outcome.handoff_error
