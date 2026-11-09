"""Validated native checkpoints and in-place recovery without replaying model work.

The manifest is the commit point. Failed transfers never replace a good archive.
After bounded retries, a live controller waits with the original workspace intact;
this is deliberately not a claim of whole-job recovery after process death.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import re
import shlex
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from explore.practice_loop import PracticeInfrastructureError


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with pending.open("x") as stream:
        os.chmod(pending, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def archive_info(path, session_id):
    """Read the complete gzip stream (including CRC), reject unsafe tar members."""
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    with gzip.open(path, "rb") as stream:
        while stream.read(1024 * 1024):
            pass
    identity_found = False
    members = set()
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if (name.is_absolute() or ".." in name.parts or not name.parts
                    or name.parts[0] != "home" or name in members
                    or not (member.isfile() or member.isdir())):
                raise ValueError("Unsafe native archive member: " + member.name)
            members.add(name)
            if member.name.endswith(f"/sessions/{session_id}/metadata.json"):
                if not member.isfile() or member.size > 1024 * 1024:
                    raise ValueError("Invalid native session metadata")
                metadata = json.load(archive.extractfile(member))
                if metadata.get("sessionId") != session_id:
                    raise ValueError("Native session identity mismatch")
                identity_found = True
    if not identity_found:
        raise ValueError("Native archive lacks the requested session metadata")
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size,
            "members": len(members)}


# Independent fallback: copy only stable regular files/directories, then archive
# the private snapshot. Directory/file changes during copying fail closed.
SNAPSHOT_SCRIPT = """
import os, shutil, stat, sys, tarfile, tempfile
from pathlib import Path
source = Path('/tmp/rsiagent-bitfun/home')
def inventory():
    result = {}
    for p in [source, *source.rglob('*')]:
        s = p.lstat()
        if not (stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode)):
            raise RuntimeError('Non-regular native state: ' + str(p))
        result[str(p.relative_to(source))] = (s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    return result
before = inventory()
with tempfile.TemporaryDirectory(prefix='rsi-native-snapshot-') as directory:
    target = Path(directory) / 'home'
    shutil.copytree(source, target)
    if before != inventory():
        raise RuntimeError('Native session changed during snapshot')
    with tarfile.open(sys.argv[1], 'w:gz', dereference=False) as archive:
        archive.add(target, arcname='home')
"""


class NativeCheckpoints:
    def __init__(self, root, *, secrets=(), repair_timeout=3600, retry_delay=2,
                 auto_retry_interval=60):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.secrets = tuple(value for value in secrets if value)
        self.repair_timeout = repair_timeout
        self.retry_delay = retry_delay
        self.auto_retry_interval = auto_retry_interval

    def clean(self, text):
        text = str(text or "")
        for value in self.secrets:
            text = text.replace(value, "[REDACTED]")
        text = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", text)
        return text[-8192:]

    def event(self, session_id, **data):
        path = self.root / f"{session_id}.events.jsonl"
        record = {"time": datetime.now(timezone.utc).isoformat(), **data}
        with path.open("a") as stream:
            os.chmod(path, 0o600)
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()

    async def command(self, environment, command, identity, stage, timeout=180):
        result = await asyncio.wait_for(
            environment.exec(command=command, timeout_sec=timeout), timeout + 10)
        self.event(identity, stage=stage, return_code=result.return_code,
                   stdout=self.clean(result.stdout), stderr=self.clean(result.stderr))
        if result.return_code:
            raise RuntimeError(f"{stage} returned {result.return_code}: "
                               + self.clean(result.stderr or result.stdout))
        return result

    async def save_once(self, environment, identity, call, role, cwd, attempt):
        token = uuid.uuid4().hex
        remote = f"/tmp/rsi-session-{token}.tgz"
        pending = self.root / f".{token}.partial"
        destination = self.root / f"{identity}-{call:03d}-{token}.tgz"
        method = "tar" if attempt < 3 else "stable_python_snapshot"
        try:
            command = (f"tar czf {remote} -C /tmp/rsiagent-bitfun home"
                       if method == "tar" else
                       "python3 -c " + shlex.quote(SNAPSHOT_SCRIPT) + " " + remote)
            await self.command(environment, command, identity, method)
            await asyncio.wait_for(environment.download_file(remote, pending), 180)
            os.chmod(pending, 0o600)
            info = await asyncio.to_thread(archive_info, pending, identity)
            with pending.open("rb") as stream:
                os.fsync(stream.fileno())
            pending.replace(destination)
            record = {"schema_version": 1, "session_id": identity, "call": call,
                      "role": role, "working_dir": cwd, "archive": destination.name,
                      "method": method, **info}
            atomic_json(self.root / f"{identity}.json", record)
            self.event(identity, stage="checkpoint_committed", call=call, **info)
            return record
        finally:
            pending.unlink(missing_ok=True)
            try:
                await asyncio.wait_for(environment.exec(command=f"rm -f {remote}",
                                                         timeout_sec=10), 15)
            except Exception as error:
                self.event(identity, stage="temporary_cleanup", error=self.clean(error))

    async def save(self, vm, session, role, cwd, outcome_path):
        vm.checkpoint_blocked = True
        identity = session.session_id
        status_path = self.root / f"{identity}.recovery.json"
        request_path = self.root / f"{identity}.retry.json"
        incident = uuid.uuid4().hex
        started = time.monotonic()
        status = {"schema_version": 1, "incident": incident, "session_id": identity,
                  "call": session.calls, "role": role, "working_dir": cwd,
                  "controller_pid": os.getpid(), "outcome": str(outcome_path),
                  "environment": str(vm.environment.trial_paths.agent_dir.parent),
                  "compose_project": getattr(vm.environment, "session_id", None),
                  "attempts": 0, "status": "saving"}
        while True:
            for attempt in range(1, 4):
                status.update(status="saving", attempts=status["attempts"] + 1,
                              heartbeat=time.time())
                atomic_json(status_path, status)
                try:
                    record = await self.save_once(vm.environment, identity, session.calls,
                                                  role, cwd, attempt)
                    status.update(status="ready", checkpoint=record, heartbeat=time.time())
                    atomic_json(status_path, status)
                    vm.checkpoint_blocked = False
                    return record
                except Exception as error:
                    # Preserve this environment even if cancellation interrupts repair.
                    vm.checkpoint_blocked = True
                    status["error"] = self.clean(f"{type(error).__name__}: {error}")
                    self.event(identity, stage="checkpoint_failed", call=session.calls,
                               attempt=status["attempts"], error=status["error"])
                    if attempt < 3:
                        await asyncio.sleep(self.retry_delay)
            try:
                await self.command(vm.environment,
                    "df -h /tmp /logs/agent; df -i /tmp /logs/agent; "
                    "ls -ld /tmp/rsiagent-bitfun/home; ps -eo pid,ppid,pgid,stat,comm",
                    identity, "diagnostics", timeout=15)
            except Exception as error:
                self.event(identity, stage="diagnostics_failed", error=self.clean(error))
            status.update(status="waiting_for_checkpoint_repair", heartbeat=time.time())
            atomic_json(status_path, status)
            next_retry = time.monotonic() + self.auto_retry_interval
            while True:
                if time.monotonic() - started >= self.repair_timeout:
                    status.update(status="stopped_preserved", heartbeat=time.time())
                    atomic_json(status_path, status)
                    raise PracticeInfrastructureError(
                        f"Native checkpoint repair exhausted; workspace preserved; inspect {status_path}")
                try:
                    request = json.loads(request_path.read_text())
                except (OSError, ValueError):
                    request = {}
                if request.get("incident") == incident:
                    request_path.unlink(missing_ok=True)
                    break
                if time.monotonic() >= next_retry:
                    break
                status["heartbeat"] = time.time()
                atomic_json(status_path, status)
                await asyncio.sleep(2)

    async def restore(self, environment, session, role, cwd):
        identity = session.session_id
        manifest = self.root / f"{identity}.json"
        if not manifest.exists():
            if session.calls:
                raise PracticeInfrastructureError("Missing committed native checkpoint for continued session")
            return False
        record = json.loads(manifest.read_text())
        for key, expected in {"session_id": identity, "role": role,
                              "working_dir": cwd, "call": session.calls}.items():
            if record.get(key) != expected:
                raise PracticeInfrastructureError("Native checkpoint mismatch: " + key)
        name = record["archive"]
        if Path(name).name != name or not name.endswith(".tgz"):
            raise PracticeInfrastructureError("Invalid native checkpoint path")
        path = self.root / name
        info = await asyncio.to_thread(archive_info, path, identity)
        if any(record.get(key) != value for key, value in info.items()):
            raise PracticeInfrastructureError("Native checkpoint integrity mismatch")
        for attempt in range(1, 4):
            token = uuid.uuid4().hex
            remote = f"/tmp/rsi-restore-{token}.tgz"
            staging = f"/tmp/rsiagent-bitfun/restore-{token}"
            try:
                await asyncio.wait_for(environment.upload_file(path, remote), 180)
                await self.command(environment,
                    f"mkdir -p {staging} && tar xzf {remote} -C {staging}", identity, "restore_extract")
                script = (
                    "import os,shutil; from pathlib import Path; "
                    f"stage=Path({staging!r}); root=stage.parent; home=root/'home'; "
                    "backup=stage/'previous'; "
                    "os.replace(home,backup) if home.exists() else None; "
                    "\ntry: os.replace(stage/'home',home)"
                    "\nexcept BaseException:"
                    "\n if backup.exists(): os.replace(backup,home)"
                    "\n raise"
                    "\nshutil.rmtree(stage)"
                )
                await self.command(environment, "python3 -c " + shlex.quote(script), identity, "restore_install")
                return True
            except Exception as error:
                self.event(identity, stage="restore_failed", attempt=attempt, error=self.clean(error))
                if attempt == 3:
                    raise PracticeInfrastructureError("Native restore failed; see checkpoint events") from error
                await asyncio.sleep(self.retry_delay)
            finally:
                try:
                    await asyncio.wait_for(environment.exec(command=f"rm -f {remote}", timeout_sec=10), 15)
                except Exception:
                    pass
