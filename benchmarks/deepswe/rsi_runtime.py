"""BitFun/Pier adapters for the unchanged synchronous reference RSI engines.

Reference engines run on worker threads; every Docker/BitFun operation is scheduled
on Pier's owning event loop. A reset creates a new compose project. Only exact
fixtures, canonical memory and the selected role's native session cross resets.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import tarfile
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

from core.loop import LoopResult
from core.verifier import _parse_agentic_verifier_report
from explore.practice_loop import PracticeHooks, PracticeInfrastructureError, _read_guest_text
from explore import commit
from .contracts import HandoffError, CompletionCheck
from .review_environment import make_review_environment
from .rsi_prompts import practice_prompt, practice_verifier_prompt
from .rsi_checkpoint import NativeCheckpoints, atomic_json
from .bitfun_agent import credential_variables
from .rsi_integrity import VERIFIER_CACHE_ENV
from .supervision import EventJournal
from .work_budget import WorkBudget
from .docker_lifecycle import cleanup_environment


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    pending.replace(path)


class CodingAudit:
    """Host-only instruction corpus; no OSWorld-specific names or grader data."""

    def __init__(self, path, instruction, task_root):
        self.path = Path(path)
        self.instruction = instruction
        self.allowed = commit._shingles(instruction)
        instructions = sorted(Path(task_root).glob("*/instruction.md"))
        if not instructions:
            raise ValueError("DeepSWE public instruction corpus is missing")
        self.grams = set().union(*(commit._shingles(p.read_text()) for p in instructions))
        write_json(self.path, sorted(self.grams))
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        write_json(self.path.with_suffix(".manifest.json"), {
            "benchmark": "DeepSWE", "instruction_count": len(instructions),
            "sha256": self.digest, "source": "public instruction.md only",
        })

    def validate(self, path):
        if Path(path) != self.path or hashlib.sha256(self.path.read_bytes()).hexdigest() != self.digest:
            raise PracticeInfrastructureError("Public instruction corpus drift")
        return {"benchmark": "DeepSWE", "sha256": self.digest}

    def text(self, text, mode="practice", authorized_instruction="", **kwargs):
        hits = []
        # Public task wording is allowed. Private benchmark artifact locations are not.
        if re.search(
            r"(?<![\w./-])/(?:tests/(?:test\.sh|test\.patch|grader\.py|config\.json)"
            r"(?=$|[\s'\"`])|solution/(?:solve\.sh|solution\.patch)(?=$|[\s'\"`])"
            r"|logs/verifier/)", text):
            hits.append({"kind": "sealed-evaluation-surface"})
        if (commit._shingles(text) & self.grams) - self.allowed - commit._shingles(authorized_instruction):
            hits.append({"kind": "other-task-instruction"})
        return hits

    def transcripts(self, root, **kwargs):
        hits = []
        for path in Path(root).rglob("transcript.json"):
            hits.extend(self.text(path.read_text(), **kwargs))
        for path in Path(root).rglob("stream-*.jsonl"):
            # Inspect decoded native events, including actual observations, not
            # merely the host reference to their location.
            with path.open(errors="replace") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    def inspect(value):
                        if isinstance(value, str):
                            hits.extend(self.text(value, **kwargs))
                        elif isinstance(value, dict):
                            for child in value.values():
                                inspect(child)
                        elif isinstance(value, list):
                            for child in value:
                                inspect(child)
                    inspect(event)
        return hits

    def captured(self, root, **kwargs):
        hits = []
        with tarfile.open(Path(root) / "materials.tgz") as archive:
            for item in archive:
                hits.extend(self.text(item.name, **kwargs))
                if item.isfile():
                    hits.extend(self.text(archive.extractfile(item).read().decode("utf-8", "ignore"), **kwargs))
        return hits

    def memory(self, files, corpus_path, reject_log, **kwargs):
        self.validate(corpus_path)
        kwargs.pop("require_corpus", None)
        return {name: data for name, data in files.items()
                if not self.text(name + "\n" + data.decode("utf-8", "ignore"), **kwargs)}


class CodingVM:
    def __init__(self, runtime):
        self.runtime = runtime
        self.environment = None
        self.closed = False
        self.leased = False
        self.uses_slot = False

    def run_command(self, command, timeout=120, cap=None, **kwargs):
        # Original memory transport assumes /home/user is the guest user's home.
        # Spell that path explicitly instead of changing HOME on the host or guest.
        command = command.replace("~/.memory", "/home/user/.memory")
        command = command.replace("cd ~ &&", "cd /home/user &&")
        result = self.runtime.call(self.environment.exec(command=command, timeout_sec=timeout))
        return (result.stdout or "") + (result.stderr or "")

    def fetch_file(self, path, max_bytes=None):
        with tempfile.TemporaryDirectory(dir=self.runtime.root) as directory:
            dest = Path(directory) / "file"
            self.runtime.call(self.environment.download_file(path, dest))
            data = dest.read_bytes()
            if max_bytes is not None and len(data) > max_bytes:
                raise PracticeInfrastructureError("Requested file exceeds transfer bound")
            return data

    def push_file(self, source, destination):
        self.runtime.call(self.environment.upload_file(source, destination))
        return True

    def close(self):
        if self.leased:
            self.runtime.call(self.runtime.release_vm(self))


class CodingRuntime:
    def __init__(self, owner, template, root, loop, audit):
        self.owner, self.template = owner, template
        self.root, self.loop, self.audit = Path(root), loop, audit
        self.root.mkdir(parents=True, exist_ok=True)
        self.sessions = {}
        self.adopted_live = set()
        self.vms = []
        self.lock = threading.Lock()
        self.pending = set()
        self.active_calls = set()
        self.aborted = False
        self.done_checks = {}
        self.phase_usage = {}
        self.capacity = threading.BoundedSemaphore(owner.parallel)
        credentials = set().union(*(credential_variables(config)
                                    for config in owner.role_configs().values()))
        extra = getattr(owner, "_extra_env", {})
        self.checkpoints = NativeCheckpoints(self.root / "private-sessions",
            secrets=[extra.get(key) or os.environ.get(key) for key in credentials],
            repair_timeout=getattr(owner, "checkpoint_repair_timeout_sec", 3600))
        self.hooks = PracticeHooks(
            run_attempt=self.run_attempt, reset_vm=self.reset,
            read_guest_text=self.read_text,
            validate_corpus=audit.validate, audit_text=audit.text,
            audit_transcripts=audit.transcripts, audit_captured=audit.captured,
            audit_memory=audit.memory, pull_memory=self.pull_memory,
            verify_agentic=self.verify, actor_evidence_leads=self.evidence_leads,
            role_work_usage=lambda session: session.bitfun_work_usage,
            branch_failure=self.fail_pending_branches,
        )

    def call(self, coroutine):
        with self.lock:
            if self.aborted:
                coroutine.close()
                raise PracticeInfrastructureError("RSI execution was cancelled")
            future = asyncio.run_coroutine_threadsafe(self.tracked_call(coroutine), self.loop)
            self.pending.add(future)
        try:
            return future.result()
        finally:
            with self.lock:
                self.pending.discard(future)

    async def tracked_call(self, coroutine):
        task = asyncio.current_task()
        self.active_calls.add(task)
        try:
            return await coroutine
        except Exception as error:
            try:
                with (self.root / 'operation-errors.jsonl').open('a') as stream:
                    stream.write(json.dumps({'time': time.time(),
                        'operation': getattr(getattr(coroutine, 'cr_code', None), 'co_name', 'unknown'),
                        'error_type': type(error).__name__,
                        'traceback': self.checkpoints.clean(traceback.format_exc())}) + '\n')
            except OSError:
                pass
            raise
        finally:
            self.active_calls.discard(task)

    def fail_pending_branches(self, error):
        with self.lock:
            self.aborted = True
            pending = list(self.pending)
        self.owner.event("RSI_BRANCHES_ABORTED", error_type=type(error).__name__,
                         error=self.checkpoints.clean(error), pending_calls=len(pending))
        for future in pending:
            future.cancel()

    async def run_engine(self, function, *args, **kwargs):
        worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            with self.lock:
                self.aborted = True
                pending = list(self.pending)
            for future in pending:
                future.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            raise

    def acquire(self, *, target=False):
        if not target:
            while not self.capacity.acquire(timeout=1):
                if self.aborted:
                    raise PracticeInfrastructureError("RSI execution was cancelled")
        vm = CodingVM(self)
        vm.leased = True
        vm.uses_slot = not target
        with self.lock:
            self.vms.append(vm)
        try:
            self.reset(vm, "")
        except BaseException:
            try:
                self.call(self.release_vm(vm))
            except BaseException:
                pass  # Top-level cleanup still owns any live environment.
            raise
        return vm, vm

    def reset(self, vm, target):
        self.call(self.reset_async(vm))
        return {"ok": True}

    async def reset_async(self, vm):
        if getattr(vm, "checkpoint_blocked", False) or getattr(vm, "integrity_blocked", False):
            raise PracticeInfrastructureError("Workspace preserved for repair; reset is forbidden")
        if vm.environment is not None:
            await self.close_vm(vm)
        destination = self.root / "environments" / uuid.uuid4().hex
        vm.environment = make_review_environment(self.template, self.owner, destination)
        vm.closed = False
        vm.environment.trial_paths.mkdir()
        await vm.environment.start(force_build=False)
        result = await vm.environment.exec(
            command="mkdir -p /home/user /tmp/verifier-scratch /tmp/rsiagent-bitfun",
        )
        if result.return_code:
            raise PracticeInfrastructureError("Cannot prepare coding practice workspace")

    async def close_vm(self, vm):
        if getattr(vm, "checkpoint_blocked", False) or getattr(vm, "integrity_blocked", False):
            # The stopped native process has completed useful work. Keep its
            # workspace available for repair; never clean it up in a finally.
            return
        environment = vm.environment
        if environment is None:
            return
        await cleanup_environment(environment)
        vm.environment = None
        vm.closed = True

    async def close(self):
        # concurrent Future.cancel() wakes the reference worker before the native
        # process group's finally block has settled. Wait before deleting Docker.
        if self.active_calls:
            await asyncio.gather(*list(self.active_calls), return_exceptions=True)
        errors = []
        for vm in self.vms:
            if vm.leased or vm.environment is not None:
                try:
                    await self.release_vm(vm)
                except Exception as error:
                    errors.append(error)
        if errors:
            raise ExceptionGroup("RSI environment cleanup failed", errors)

    async def release_vm(self, vm):
        try:
            await self.close_vm(vm)
        finally:
            if vm.leased:
                vm.leased = False
                if vm.uses_slot:
                    self.capacity.release()

    def read_text(self, vm, path):
        path = path.replace("~/", "/home/user/")
        return _read_guest_text(vm, path)

    def pull_memory(self, vm):
        check = vm.run_command(
            "test -d /home/user/.memory && "
            "test -z \"$(find /home/user/.memory -type l -print -quit)\" && echo MEMORY_TREE_OK"
        )
        if "MEMORY_TREE_OK" not in check:
            raise PracticeInfrastructureError("Memory must be a real tree without symlinks")
        return commit.pull_memory(vm)

    def adopt(self, session, *, working_dir="/app"):
        if session.session_id not in self.sessions:
            self.adopted_live.add(session.session_id)
        self.sessions[session.session_id] = (session, working_dir)
        return [{"role": "user", "content": "Native BitFun session continuity"},
                {"role": "assistant", "content": "Native session archive is authoritative",
                 "bitfun_session_id": session.session_id}]

    def run_attempt(self, prompt, vm, cfg, sink, *, initial_history=None, **kwargs):
        history = list(initial_history or [])
        session_id = next((m.get("bitfun_session_id") for m in reversed(history)
                           if m.get("bitfun_session_id")), None)
        curriculum = cfg.model == "bitfun:curriculum"
        role = "curriculum" if curriculum else "actor"
        # The reference retries missing publications in the same phase. Keep
        # that phase's emergency work clock cumulative, including retries after
        # an rsi_done with no usable project/diagnosis. New phases get new clocks.
        scope = Path(sink.root)
        while re.fullmatch(r"(?:segment|attempt)_\d+", scope.name):
            scope = scope.parent
        used_iters, used_wall = self.phase_usage.get((session_id, str(scope)), (0, 0))
        seconds = kwargs.get("wall_budget", cfg.wall_clock_secs - used_wall)
        max_iters = kwargs.get("iters_budget", cfg.max_iters - used_iters)
        if seconds <= 0 or max_iters <= 0:
            raise PracticeInfrastructureError(f"{role} cumulative publication work ceiling exhausted: {scope}")
        result, session = self.call(self.execute(
            vm, prompt, role, sink.root, session_id=session_id,
            seconds=seconds, max_iters=max_iters,
        ))
        self.phase_usage[(session.session_id, str(scope))] = (
            used_iters + result.iters, used_wall + result.wall_secs)
        return result, self.adopt(session, working_dir=self.sessions[session.session_id][1])

    def evidence_leads(self, history, project, cfg):
        from core.checks import validate
        from core.loop import _actor_evidence_leads
        identity = history[-1]["bitfun_session_id"]
        accepted, _, _ = validate(
            [check.model_dump() for check in self.done_checks.get(identity, [])],
            cfg.max_checks, instruction=project,
            witness_gates=getattr(cfg, "done_witness_gates", False))
        return _actor_evidence_leads(accepted, [])

    async def execute(self, vm, prompt, role, sink_root, *, session_id=None,
                      seconds=86400, max_iters=2000, allow_unverified=False,
                      generation=1, evidence_leads="", context=""):
        environment = vm.environment
        sink_root = Path(sink_root)
        sink_root.mkdir(parents=True, exist_ok=True)
        log_dir = environment.trial_paths.agent_dir
        if session_id:
            session_id = str(uuid.UUID(session_id))
            if session_id not in self.sessions:
                # A new controller has no Python session objects. Reconstruct
                # identity from a committed checkpoint; never silently restart.
                manifest = self.checkpoints.root / f"{session_id}.json"
                if not manifest.is_file() or manifest.is_symlink():
                    raise PracticeInfrastructureError("Missing native checkpoint for resumed controller")
                record = json.loads(manifest.read_text())
                if (record.get("session_id") != session_id or record.get("role") != role
                        or record.get("working_dir") not in {"/home/user", "/app"}
                        or type(record.get("call")) is not int or record["call"] < 1):
                    raise PracticeInfrastructureError("Invalid native checkpoint identity/role/call")
                session = self.owner._session(log_dir, self.owner.role_configs()[role], session_id=session_id)
                session.calls = record["call"]
                self.sessions[session_id] = (session, record["working_dir"])
            session, working_dir = self.sessions[session_id]
            session.logs_dir = log_dir
        else:
            session = self.owner._session(log_dir, self.owner.role_configs()[role])
            working_dir = "/home/user"
            self.sessions[session.session_id] = (session, working_dir)
        if role == "verifier":
            session._extra_env = {**session._extra_env, **VERIFIER_CACHE_ENV}
        # A native conversation is restored ONLY to its owning role's container.
        # Target learning initially adopts the actual live baseline Actor.
        if session.session_id in self.adopted_live:
            self.adopted_live.remove(session.session_id)
            restored = True
        else:
            restored = await self.checkpoints.restore(environment, session, role, working_dir)
        # No other role's session files can survive a reuse of this container.
        if not restored and not session.calls:
            cleared = await environment.exec(command="rm -rf /tmp/rsiagent-bitfun/home")
            if cleared.return_code:
                raise PracticeInfrastructureError("Cannot isolate fresh native session")
        await session.prepare(environment)
        budget = WorkBudget(seconds)
        model_calls = tools_run = failures = 0
        while True:
            if budget.remaining() <= 0 or model_calls >= max_iters:
                raise PracticeInfrastructureError("Role emergency work ceiling exhausted")
            handoff_id = uuid.uuid4().hex
            remote = "/logs/agent/handoffs/" + handoff_id + ".json"
            if role == "verifier":
                rendered = practice_verifier_prompt(prompt, context, remote,
                    allow_unverified=allow_unverified, generation=generation,
                    evidence_leads=evidence_leads)

                def reader(path):
                    if not path.is_file() or path.is_symlink():
                        raise HandoffError("Missing verifier report")
                    try:
                        report = path.read_text()
                    except (OSError, UnicodeError) as error:
                        raise HandoffError("Unreadable verifier report") from error
                    if _parse_agentic_verifier_report(report, allow_unverified=allow_unverified) is None:
                        raise HandoffError("Invalid verifier verdict token")
                    return report
            else:
                evidence_requested = "independent-channel probes among your checks" in prompt
                rendered = practice_prompt(prompt, curriculum=role == "curriculum",
                    terminal_path=remote, handoff_id=handoff_id,
                    evidence_requested=evidence_requested,
                    target_learning=working_dir == "/app")

                def reader(path):
                    try:
                        value = json.loads(path.read_text())
                    except (ValueError, OSError):
                        raise HandoffError("Missing role completion")
                    if not isinstance(value, dict):
                        raise HandoffError("Role completion must be an object")
                    expected = {"kind": "rsi_done", "handoff_id": handoff_id, "final": True}
                    if path.is_symlink() or any(value.get(k) != v for k, v in expected.items()) or set(value) - set(expected) - {"checks"}:
                        raise HandoffError("Invalid role completion identity")
                    try:
                        checks = [CompletionCheck.model_validate(v) for v in value.get("checks", [])]
                    except (ValueError, TypeError):
                        raise HandoffError("Invalid evidence checks")
                    if evidence_requested and not checks:
                        raise HandoffError("Evidence response requires reproducible checks")
                    self.done_checks[session.session_id] = checks
                    return value
            (sink_root / f"prompt-{session.calls + 1:03d}.txt").write_text(rendered)
            # Native project identity/cwd remains stable over reset and memory turns.
            wrapped = _WorkingDirectory(environment, working_dir)
            try:
                outcome = await session.execute(wrapped, rendered, budget.remaining(),
                    role=role, handoff_id=handoff_id, work_budget=budget,
                    terminal_reader=reader, detect_stall=False)
            except BaseException:
                try:
                    await session.collect(environment)
                except Exception as error:
                    self.checkpoints.event(session.session_id, stage="collect_failed",
                                           error=self.checkpoints.clean(error))
                raise
            # Persist the accepted completion BEFORE any fragile copy/archive.
            vm.checkpoint_blocked = True
            outcome_path = sink_root / f"outcome-{session.calls:03d}.json"
            atomic_json(outcome_path, {"session_id": session.session_id, "role": role,
                "call": session.calls, "status": outcome.status, "report": outcome.report,
                "metrics": outcome.metrics, "handoff_id": handoff_id,
                "error": outcome.error,
                "checkpoint_status": "pending"})
            if not isinstance(outcome.metrics, dict) or any(
                    type(outcome.metrics.get(key, 0)) is not int or outcome.metrics.get(key, 0) < 0
                    for key in ("model_calls", "completed_tools")):
                raise PracticeInfrastructureError(f"Invalid BitFun usage metrics; outcome preserved: {outcome_path}")
            model_calls += outcome.metrics.get("model_calls", 0)
            tools_run += outcome.metrics.get("completed_tools", 0)
            stream = log_dir / f"stream-{session.session_id}-{session.calls:03d}.jsonl"
            if stream.exists():
                shutil.copyfile(stream, sink_root / stream.name)
            transcript = {
                "bitfun_session_id": session.session_id,
                "prompt": rendered,
                "stream": str(stream), "completion": outcome.status,
            }
            write_json(sink_root / "transcript.json", transcript)
            self._save_evidence(stream, session.session_id, sink_root, session.calls)
            pause = "native-checkpoint-" + uuid.uuid4().hex
            budget.feed(dict(request_id=pause, event="request_start"))
            budget.feed(dict(request_id=pause, event="pause_start", started=time.monotonic()))
            try:
                # Native home is authoritative. Copy-back logs are supplementary;
                # their failure cannot invalidate an otherwise valid handoff.
                for attempt in range(3):
                    try:
                        await session.collect(environment)
                        break
                    except Exception as error:
                        self.checkpoints.event(session.session_id, stage="collect_failed",
                            attempt=attempt + 1, error=self.checkpoints.clean(error))
                        if attempt < 2:
                            await asyncio.sleep(self.checkpoints.retry_delay)
                checkpoint = await self.checkpoints.save(vm, session, role, working_dir, outcome_path)
            finally:
                budget.feed(dict(request_id=pause, event="request_end", at=time.monotonic()))
            transcript["native_archive"] = str(self.checkpoints.root / checkpoint["archive"])
            write_json(sink_root / "transcript.json", transcript)
            saved_outcome = json.loads(outcome_path.read_text())
            atomic_json(outcome_path, {**saved_outcome, "checkpoint_status": "ready",
                                      "checkpoint": checkpoint})
            write_json(sink_root / f"usage-{session.calls:03d}.json", {
                "role": role, "session_id": session.session_id,
                "call": session.calls, "status": outcome.status,
                "metrics": outcome.metrics, "budget": budget.summary(),
            })
            # The reference also audits verifier artifacts before admitting a
            # verdict. It does not reinterpret the report's correctness.
            if self.audit.transcripts(sink_root, authorized_instruction=self.audit.instruction):
                from explore.practice_loop import PracticeBoundaryError
                raise PracticeBoundaryError("BitFun role transcript failed the coding boundary")
            if outcome.status == "handoff":
                session.rsi_work_usage = (model_calls, budget.paused_seconds)
                if role == "verifier":
                    (sink_root / "verifier_report.md").write_text(outcome.report)
                    return outcome.report, session
                result = LoopResult(status="done", iters=model_calls, turns=model_calls,
                    programs_run=tools_run, wall_secs=budget.elapsed,
                    infra_pause_secs=budget.paused_seconds, infra_pauses=budget.pauses)
                write_json(sink_root / "result.json", vars(result))
                return result, session
            if outcome.status == "segment_exhausted":
                prompt += "\nContinue this SAME context; the native model-round segment ended, not the phase."
                continue
            if outcome.status in {"execution_error", "budget_exhausted"}:
                raise PracticeInfrastructureError("BitFun role stopped: " + outcome.status
                    + (": " + self.checkpoints.clean(outcome.error) if outcome.error else ""))
            failures += 1
            if failures > self.owner.report_recovery_attempts:
                raise PracticeInfrastructureError("BitFun completion transport recovery exhausted")
            prompt += "\nThe completion envelope was missing or invalid. Continue this same phase and publish it after completing the required files."

    def _save_evidence(self, path, identity, sink_root, call):
        journal = EventJournal(path, identity, record_observations=True)
        journal.poll()
        for index, item in enumerate(journal.observations, 1):
            root = Path(sink_root) / f"call_{call:03d}" / f"iter_{index:04d}"
            root.mkdir(parents=True, exist_ok=True)
            args = item["action"]["arguments"]
            command = (args.get("command") or args.get("cmd")) if isinstance(args, dict) else None
            if isinstance(command, str):
                (root / "program.sh").write_text(command)
            (root / "trace.txt").write_text(item["observation"])
            write_json(root / "trace_meta.json", {"native_tool": item["action"]["tool"], "status": item["status"]})

    def verify(self, project, vm, cfg, *, sink, session, context="",
               actor_evidence_leads="", wall_budget=None, iters_budget=None, **kwargs):
        return self.call(self.verify_async(project, vm, cfg, sink=sink, session=session,
            context=context, actor_evidence_leads=actor_evidence_leads,
            wall_budget=wall_budget, iters_budget=iters_budget))

    async def integrity_command(self, vm, operation, snapshot=None):
        script = Path(__file__).with_name("rsi_integrity.py").read_text()
        remote = "/tmp/rsi-integrity-" + uuid.uuid4().hex + ".json"
        command = "python3 -c " + shlex.quote(script) + " " + operation
        if snapshot is not None:
            await vm.environment.upload_file(snapshot, remote)
            command += " " + remote
        result = await vm.environment.exec(command=command, timeout_sec=180)
        if result.return_code:
            raise PracticeInfrastructureError("Verifier integrity inspection failed: "
                + self.checkpoints.clean(result.stderr or result.stdout))
        return json.loads(result.stdout)

    async def verify_async(self, project, vm, cfg, *, sink, session, context="",
                           actor_evidence_leads="", wall_budget=None, iters_budget=None):
        session.inspections += 1
        inspection = Path(sink.root) / f"inspection_{session.inspections:03d}"
        before_path = inspection / "integrity-before.json"
        atomic_json(before_path, await self.integrity_command(vm, "inventory"))
        report, native = await self.execute(
            vm, project, "verifier", inspection,
            session_id=getattr(session, "bitfun_session_id", None),
            seconds=wall_budget or cfg.wall_clock_secs,
            max_iters=iters_budget or cfg.max_iters,
            allow_unverified=cfg.verifier_unverified_evidence,
            generation=session.inspections, context=context,
            evidence_leads=actor_evidence_leads,
        )
        session.bitfun_session_id = native.session_id
        session.bitfun_work_usage = native.rsi_work_usage
        # The model has settled and its report is durable. Repair only disposable
        # cache additions, without resampling the verdict or touching Actor work.
        vm.integrity_blocked = True
        integrity = await self.integrity_command(vm, "recover", before_path)
        atomic_json(inspection / "integrity.json", integrity)
        if integrity["status"] == "rejected":
            raise PracticeInfrastructureError(
                f"Verifier changed protected candidate/fixtures; workspace preserved; see {inspection / 'integrity.json'}")
        vm.integrity_blocked = False
        if integrity["status"] == "cache_recovered":
            self.owner.event("VERIFIER_CACHE_RECOVERED", session_id=native.session_id,
                inspection=str(inspection), removed=len(integrity["removed"]))
        if integrity.get("restored_git_indexes"):
            self.owner.event("VERIFIER_GIT_INDEX_RECOVERED", session_id=native.session_id,
                inspection=str(inspection), paths=integrity["restored_git_indexes"])
        return _parse_agentic_verifier_report(report,
            allow_unverified=cfg.verifier_unverified_evidence), report


class _WorkingDirectory:
    """Delegate Pier operations while fixing BitFun's native project identity."""
    def __init__(self, environment, cwd):
        self.environment, self.cwd = environment, cwd

    def __getattr__(self, name):
        return getattr(self.environment, name)

    async def exec(self, command, cwd=None, **kwargs):
        return await self.environment.exec(command=command, cwd=cwd or self.cwd, **kwargs)
