"""Failures before Agent work must not leak resources or lose the root cause."""

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from benchmarks.deepswe.docker_environment import BitfunDockerEnvironment
from benchmarks.deepswe.docker_lifecycle import (
    cleanup_environment, network_lifecycle_lock, run_compose_process,
)
from benchmarks.deepswe.rsi_runtime import CodingRuntime


def environment(tmp_path):
    return SimpleNamespace(
        trial_paths=SimpleNamespace(trial_dir=tmp_path),
        prepare_logs_for_host=AsyncMock(),
        _run_docker_compose_command=AsyncMock(),
        _cleanup_resources_compose_file=Mock())


@pytest.mark.asyncio
async def test_log_failure_does_not_skip_scoped_cleanup(tmp_path):
    env = environment(tmp_path)
    env.prepare_logs_for_host.side_effect = RuntimeError('logs failed')
    env._run_docker_compose_command.side_effect = [TimeoutError('slow daemon'), None]
    await cleanup_environment(env)
    assert env._run_docker_compose_command.await_count == 2
    assert env._run_docker_compose_command.call_args.args[0] == [
        'down', '--volumes', '--remove-orphans']
    env._cleanup_resources_compose_file.assert_called_once()
    assert len((tmp_path / 'cleanup-errors.jsonl').read_text().splitlines()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('keep,delete,command', [
    (False, True, ['down', '--volumes', '--remove-orphans']),
    (False, False, ['down']), (True, True, ['stop'])])
async def test_pier_stop_uses_same_scoped_cleanup(tmp_path, keep, delete, command):
    env = environment(tmp_path)
    env._keep_containers = keep
    env.prepare_logs_for_host.side_effect = RuntimeError('logs failed')
    await BitfunDockerEnvironment.stop(env, delete)
    assert env._run_docker_compose_command.call_args.args[0] == command


@pytest.mark.asyncio
async def test_runtime_retains_failed_handle_and_cleans_other_vms(tmp_path):
    first, second = environment(tmp_path / 'first'), environment(tmp_path / 'second')
    first._run_docker_compose_command.side_effect = RuntimeError('daemon unavailable')
    vms = [SimpleNamespace(environment=env, closed=False, leased=True, uses_slot=False)
           for env in (first, second)]
    runtime = object.__new__(CodingRuntime)
    runtime.vms, runtime.active_calls = vms, set()
    with pytest.raises(ExceptionGroup, match='cleanup failed'):
        await runtime.close()
    assert vms[0].environment is first and not vms[0].closed
    assert vms[1].environment is None and vms[1].closed
    first._cleanup_resources_compose_file.assert_not_called()
    first._run_docker_compose_command.side_effect = None
    await runtime.close()
    assert vms[0].environment is None


@pytest.mark.asyncio
async def test_reset_cannot_overwrite_preserved_workspace():
    runtime = object.__new__(CodingRuntime)
    env = object()
    vm = SimpleNamespace(environment=env, checkpoint_blocked=True)
    with pytest.raises(RuntimeError, match='preserved for repair'):
        await runtime.reset_async(vm)
    assert vm.environment is env


@pytest.mark.asyncio
async def test_lock_serializes_and_cancellation_releases_waiter():
    entered = asyncio.Event()
    async def wait():
        async with network_lifecycle_lock(timeout=1):
            entered.set()
    async with network_lifecycle_lock():
        task = asyncio.create_task(wait())
        await asyncio.sleep(.02)
        assert not entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await wait()
    assert entered.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [True, False])
async def test_compose_subprocess_is_reaped_on_cancel_or_timeout(tmp_path, cancel):
    pidfile = tmp_path / 'pid'
    code = f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(60)"
    task = asyncio.create_task(run_compose_process(
        [sys.executable, '-c', code], dict(os.environ), 60 if cancel else .2))
    for _ in range(100):
        if pidfile.exists():
            break
        await asyncio.sleep(.01)
    assert pidfile.exists()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_code', [0, 1])
async def test_json_stdout_is_not_corrupted_by_cli_warnings(exit_code):
    code, output = await run_compose_process([
        sys.executable, '-c',
        f"import sys; print('{{}}'); print('warning',file=sys.stderr); sys.exit({exit_code})"],
        dict(os.environ), 5, stdout_only=True)
    assert code == exit_code
    if code:
        assert 'warning' in output
    else:
        assert json.loads(output) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('message,retries', [
    ('networks have overlapping IPv4', 3), ('permission denied', 1)])
async def test_start_retries_only_network_conflict_and_keeps_images(tmp_path, monkeypatch, message, retries):
    from pier.environments.base import ExecResult
    import benchmarks.deepswe.docker_environment as module
    env = object.__new__(BitfunDockerEnvironment)
    env.session_id = 'owned-project'
    env.trial_paths = SimpleNamespace(trial_dir=tmp_path)
    env._prepare_network_ipam = AsyncMock(return_value={})
    commands = []
    async def once(command, *args, **kwargs):
        commands.append(command)
        return ExecResult(return_code=1 if command[0] == 'up' else 0, stdout=message)
    env._compose_once = once
    monkeypatch.setattr(module.asyncio, 'sleep', AsyncMock())
    with pytest.raises(RuntimeError, match=message):
        await env._run_docker_compose_command(['up', '-d'])
    assert len(commands) == retries * 2
    assert all('--rmi' not in command for command in commands)
    result = await env._run_docker_compose_command(['up', '-d'], check=False)
    assert result.return_code == 1
    await env._run_docker_compose_command(['down', '--rmi', 'all', '--volumes'])
    assert commands[-1] == ['down', '--volumes']


@pytest.mark.asyncio
async def test_review_cleanup_does_not_replace_primary_failure(tmp_path, monkeypatch):
    import benchmarks.deepswe.review_environment as module
    env = environment(tmp_path)
    env.trial_paths.mkdir = lambda: None
    env.start = AsyncMock(side_effect=ValueError('original start failure'))
    env._run_docker_compose_command.side_effect = RuntimeError('cleanup failure')
    monkeypatch.setattr(module, 'make_review_environment', lambda *a: env)
    with pytest.raises(ValueError, match='original start failure') as caught:
        async with module.review_environment(None, None, None, tmp_path):
            pytest.fail('must not enter')
    assert 'cleanup also failed' in caught.value.__notes__[0]


@pytest.mark.asyncio
async def test_compose_internal_parallelism_is_limited(tmp_path, monkeypatch):
    import benchmarks.deepswe.docker_environment as module
    env = object.__new__(BitfunDockerEnvironment)
    env.session_id = 'owned'
    env.environment_dir = tmp_path
    monkeypatch.setattr(BitfunDockerEnvironment, '_docker_compose_paths', property(lambda self: []))
    env._env_vars = SimpleNamespace(to_env_dict=lambda **k: {})
    env._compose_task_env = env._persistent_env = {}
    env._windows_container_name = None
    process = AsyncMock(return_value=(0, ''))
    monkeypatch.setattr(module, 'run_compose_process', process)
    await env._compose_once(['up', '-d'])
    assert process.call_args.args[1]['COMPOSE_PARALLEL_LIMIT'] == '1'
    await env._compose_once(['exec', 'main', 'bitfun'])
    assert process.call_args.args[2] is None
