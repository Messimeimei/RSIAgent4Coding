"""Bounded Docker command cleanup and cross-process network creation locking."""

from __future__ import annotations

import asyncio
import fcntl
import os
import json
import signal
import time
from contextlib import asynccontextmanager
from pathlib import Path


@asynccontextmanager
async def network_lifecycle_lock(timeout=600):
    # Share the lock across event loops and controllers using this adapter.
    # Do not leave a worker thread holding a lock after cancellation.
    path = Path('/tmp') / f'rsiagent-docker-network-{os.getuid()}.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError('Timed out waiting for RSI Docker network lock')
                await asyncio.sleep(0.1)
        yield
    finally:
        os.close(fd)


async def run_compose_process(args, env, timeout, *, stdout_only=False):
    """Reap the CLI on timeout AND cancellation before allowing scoped cleanup."""
    process = await asyncio.create_subprocess_exec(
        *args, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE if stdout_only else asyncio.subprocess.STDOUT,
        start_new_session=True)
    communication = asyncio.create_task(process.communicate())
    try:
        output, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout)
    except BaseException:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(asyncio.shield(communication), 10)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await communication
        raise
    # Successful machine-readable output must not include CLI deprecation or
    # environment warnings. On failure retain stderr for the diagnostic/retry.
    if process.returncode and stderr:
        output += b'\n' + stderr
    return process.returncode, output.decode(errors='replace')


def network_conflict(message):
    return any(marker in message.lower() for marker in (
        'networks have overlapping ipv4',
        'pool overlaps with other one on this address space',
    ))


async def cleanup_environment(environment, *, command=None):
    """Clean only this compose project; retain config if cleanup needs repair.

    Log permission adjustment is best effort, but must never prevent down. An
    idempotent down can be retried; running an Agent cannot. Callers retain their
    environment handle until this function succeeds.
    """
    records = []
    try:
        try:
            await asyncio.wait_for(environment.prepare_logs_for_host(), 30)
        except Exception as error:
            records.append({'stage': 'prepare_logs', 'error': str(error),
                            'error_type': type(error).__name__})
        for attempt in range(1, 3):
            try:
                await environment._run_docker_compose_command(
                    command or ['down', '--volumes', '--remove-orphans'], timeout_sec=120)
                break
            except Exception as error:
                records.append({'stage': 'down', 'attempt': attempt,
                                'error': str(error), 'error_type': type(error).__name__})
                if attempt == 2:
                    raise
        environment._cleanup_resources_compose_file()
    finally:
        if records:
            path = environment.trial_paths.trial_dir / 'cleanup-errors.jsonl'
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('a') as stream:
                    for record in records:
                        stream.write(json.dumps({'time': time.time(), **record}) + '\n')
            except OSError:
                # Diagnostic I/O must not replace a failure from Docker.
                pass
