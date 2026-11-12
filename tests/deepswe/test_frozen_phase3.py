import importlib
import json
import shlex
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.deepswe.contracts import RoleInfrastructureError


@pytest.fixture
def phase3(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / '.runtime/deepswe'))
    return importlib.import_module('phase3_eval.frozen_phase3_agent')


def agent(phase3, tmp_path):
    memory = tmp_path / 'memory'
    (memory / 'nested/deeper').mkdir(parents=True)
    (memory / 'INDEX.md').write_text('index')
    (memory / 'nested/deeper/lesson.md').write_text('verified lesson')
    return phase3.FrozenMemoryPhase3(logs_dir=tmp_path / 'agent',
        frozen_memory_path=str(memory),
        bitfun_config={'ai': {'models': []}})


class LocalEnvironment:
    def __init__(self, root, *, tamper=False):
        self.root, self.tamper = root, tamper
        self.root.mkdir()
    async def upload_file(self, source, target):
        shutil.copyfile(source, self.root / 'memory.tgz')
    async def exec(self, command, **kwargs):
        command = command.replace('/home/user/.memory', str(self.root / 'memory'))
        command = command.replace('/tmp/rsi-phase3-memory.tgz', str(self.root / 'memory.tgz'))
        if self.tamper:
            args = shlex.split(command)
            expected = json.loads(args[-1])
            expected['nested/deeper/lesson.md'] = '0' * 64
            args[-1] = json.dumps(expected)
            command = shlex.join(args)
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True, timeout=20)
        return SimpleNamespace(return_code=result.returncode, stdout=result.stdout, stderr=result.stderr)


@pytest.mark.asyncio
async def test_nested_memory_is_transferred_and_hash_verified(phase3, tmp_path):
    obj = agent(phase3, tmp_path)
    env = LocalEnvironment(tmp_path / 'guest')
    await obj._push_frozen_memory(env)
    assert (env.root / 'memory/nested/deeper/lesson.md').read_text() == 'verified lesson'
    assert obj.phase3_meta['delivery'] == {'verified': True, 'files': 2}


@pytest.mark.asyncio
async def test_guest_hash_mismatch_is_rejected(phase3, tmp_path):
    obj = agent(phase3, tmp_path)
    with pytest.raises(RoleInfrastructureError, match='hash verification failed'):
        await obj._push_frozen_memory(LocalEnvironment(tmp_path / 'guest', tamper=True))


@pytest.mark.asyncio
async def test_host_memory_drift_before_launch_is_rejected(phase3, tmp_path):
    obj = agent(phase3, tmp_path)
    (obj.frozen_memory_path / 'INDEX.md').write_text('changed')
    with pytest.raises(RoleInfrastructureError, match='changed before upload'):
        await obj._push_frozen_memory(LocalEnvironment(tmp_path / 'guest'))


def test_launcher_root_follows_checkout(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / '.runtime/deepswe'))
    launcher = importlib.import_module('phase3_eval.run_phase3_frozen')
    assert launcher.ROOT == root


@pytest.mark.asyncio
async def test_empty_original_memory_stays_empty_in_container(phase3, tmp_path):
    source, frozen = tmp_path / 'source', tmp_path / 'frozen'
    source.mkdir(); frozen.mkdir()
    obj = phase3.FrozenMemoryPhase3(logs_dir=tmp_path/'agent', frozen_memory_path=str(frozen),
        source_memory_path=str(source), bitfun_config={'ai': {'models': []}})
    env = LocalEnvironment(tmp_path / 'guest')
    await obj._push_frozen_memory(env)
    assert list((env.root/'memory').iterdir()) == []
    assert obj.phase3_meta['delivery'] == {'verified': True, 'files': 0}


@pytest.mark.parametrize('mutation', ['extra', 'rename', 'contents'])
def test_modified_or_enriched_freeze_is_rejected(phase3, tmp_path, mutation):
    source, frozen = tmp_path/'source', tmp_path/'frozen'
    source.mkdir(); frozen.mkdir()
    (source/'INDEX.md').write_text('Actor-generated original')
    (frozen/'INDEX.md').write_text('Actor-generated original')
    if mutation == 'extra': (frozen/'summary.md').write_text('manual guidance')
    if mutation == 'rename': (frozen/'INDEX.md').rename(frozen/'SOURCE_INDEX.md')
    if mutation == 'contents': (frozen/'INDEX.md').write_text('manual index')
    with pytest.raises(ValueError, match='source paths and bytes exactly'):
        phase3.FrozenMemoryPhase3(logs_dir=tmp_path/'agent', frozen_memory_path=str(frozen),
            source_memory_path=str(source), bitfun_config={'ai': {'models': []}})


def test_extra_memory_prompt_is_rejected(phase3, tmp_path):
    with pytest.raises(ValueError, match='extra Actor prompt content is forbidden'):
        phase3.FrozenMemoryPhase3(logs_dir=tmp_path/'agent', frozen_memory_path=str(tmp_path),
            memory_provenance='manual provenance hint', bitfun_config={'ai': {'models': []}})


@pytest.mark.asyncio
async def test_actor_prompt_matches_standard_frozen_memory_framework(phase3, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from benchmarks.deepswe.baseline import BitfunBaseline
    from benchmarks.deepswe.rsi import MemoryBaseline
    capture = AsyncMock()
    monkeypatch.setattr(BitfunBaseline, '_invoke', capture)
    frozen = agent(phase3, tmp_path)
    standard = MemoryBaseline(logs_dir=tmp_path/'standard', memory=frozen.memory,
        training=False, bitfun_config={'ai': {'models': []}})
    session = SimpleNamespace(session_id='actor')
    await frozen._invoke(session, object(), 'public task', 'actor', 'handoff')
    await standard._invoke(session, object(), 'public task', 'actor', 'handoff')
    prompts = [call.args[2] for call in capture.call_args_list]
    assert prompts[0] == prompts[1]
    assert 'MEMORY PROVENANCE:' not in prompts[0] and 'verified lesson' not in prompts[0]
