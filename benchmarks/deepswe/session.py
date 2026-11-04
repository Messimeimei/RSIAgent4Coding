"""Supervised BitFun sessions with terminal handoffs and native event checkpoints."""

from __future__ import annotations

import asyncio
import json
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path

from .bitfun_agent import BitfunCli
from .contracts import HandoffError, RoleInfrastructureError, read_handoff
from .supervision import EventJournal
from .work_budget import WorkBudget


@dataclass
class SessionOutcome:
    status: str
    report: object = None
    metrics: dict = None
    last_action: dict = None
    last_result: str = ""
    error: str | None = None


class BitfunSession(BitfunCli):
    def __init__(self, *args, session_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = (
            str(uuid.UUID(session_id)) if session_id else str(uuid.uuid4())
        )
        self.calls = 0
        self.last_outcome = None
        self._pid_file = f"/tmp/rsiagent-bitfun/{self.session_id}.pid"

    def _find_main_session_dir(self):
        path = self.logs_dir / "bitfun/sessions" / self.session_id
        return path if path.is_dir() else None

    async def prepare(self, environment):
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        await self.setup(environment)
        await self.exec_as_agent(environment, self._build_register_config_command())

    def command(self, prompt, *, bootstrap=""):
        args = [
            self._binary_path,
            "exec",
            "--auto",
            "--agent",
            self._exec_agent,
            "--output-format",
            "stream-json",
            "--no-verify-final-changes",
        ]
        args += ["--resume" if self.calls else "--session-id", self.session_id]
        args += ["--", prompt]
        stream_path = f"/logs/agent/stream-{self.session_id}-{self.calls + 1:03d}.jsonl"
        inner = (
            "set -o pipefail\n" + bootstrap
            + shlex.join(args)
            + f" 2>&1 | tee {stream_path}\nexit ${{PIPESTATUS[0]}}"
        )
        return (
            f"setsid bash -c {shlex.quote(inner)} &\n"
            "session_pid=$!\n"
            f'printf "%s" "$session_pid" > {self._pid_file}\n'
            'wait "$session_pid"\nsession_rc=$?\n'
            f"rm -f {self._pid_file}\n"
            'exit "$session_rc"'
        )

    async def stop(self, environment, *, force=False):
        signal = "KILL" if force else "INT"
        result = await asyncio.wait_for(
            environment.exec(
                command=(
                    f"if test -f {self._pid_file}; then read -r session_pid < {self._pid_file}; "
                    'case "$session_pid" in *[!0-9]*|"") exit 1;; esac; '
                    f'kill -{signal} -- "-$session_pid" 2>/dev/null || true; fi'
                )
            ),
            timeout=15,
        )
        if result.return_code:
            raise RoleInfrastructureError("Unable to signal the active BitFun session")

    async def execute(
        self,
        environment,
        prompt,
        timeout_sec,
        *,
        role,
        handoff_id,
        candidate_id=None,
        progress_probe=None,
        detect_stall=True,
        work_budget=None,
        terminal_reader=None,
    ):
        stream_path = (
            self.logs_dir / f"stream-{self.session_id}-{self.calls + 1:03d}.jsonl"
        )
        journal = EventJournal(
            stream_path, self.session_id, self.logs_dir / "bitfun.txt"
        )
        self.last_outcome = None
        handoff_path = self.logs_dir / "handoffs" / f"{handoff_id}.json"
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        budget = work_budget or WorkBudget(timeout_sec)
        transport_path = None
        if work_budget is not None:
            transport_id = f"{self.session_id}-{self.calls + 1:03d}"
            transport_path = self.logs_dir / f"transport-{transport_id}.jsonl"
            budget.watch(transport_path)
            source = Path(__file__).with_name("model_transport.py")
            remote = f"/tmp/rsiagent-bitfun/transport-{transport_id}.py"
            ready = f"/tmp/rsiagent-bitfun/transport-{transport_id}.ready"
            await environment.upload_file(source, remote)
            # Launch inside the SAME process group as the CLI. Cancellation and
            # normal segment completion both terminate the relay and its retries.
            bootstrap = (
                f"python3 {shlex.quote(remote)} "
                '"$BITFUN_USER_ROOT/config/app.json" '
                f"/logs/agent/{transport_path.name} {ready} >/dev/null 2>&1 &\n"
                "transport_pid=$!\n"
                'trap \'kill "$transport_pid" 2>/dev/null || true; '
                'wait "$transport_pid" 2>/dev/null || true\' EXIT\n'
                f"for attempt in {{1..200}}; do test -f {ready} && break; "
                'kill -0 "$transport_pid" 2>/dev/null || exit 1; sleep 0.05; done\n'
                f"test -f {ready} || exit 1\n"
            )
            # Configuration may still point at a relay from a previous segment.
            # Restore original endpoints before starting this invocation's relay.
            await self.exec_as_agent(environment, self._build_register_config_command())
            command = self.command(prompt, bootstrap=bootstrap)
        else:
            command = self.command(prompt)
        self.calls += 1
        before = await progress_probe() if progress_probe else None
        observed_tools = 0
        task = asyncio.create_task(self.exec_as_agent(environment, command))
        status = None
        report = None
        error = None
        unsettled = False
        try:
            while True:
                journal.poll()
                # Publication is accepted only after its tool action has settled.
                if not journal.active_tools and (
                    journal.completed_tools or task.done()
                ):
                    try:
                        report = (
                            terminal_reader(handoff_path)
                            if terminal_reader is not None
                            else read_handoff(handoff_path, role, handoff_id, candidate_id)
                        )
                    except HandoffError:
                        report = None
                    if report is not None:
                        status = "handoff"
                        break
                    if (
                        detect_stall
                        and progress_probe
                        and journal.completed_tools != observed_tools
                    ):
                        current = await progress_probe()
                        observed_tools = journal.completed_tools
                        if current != before:
                            journal.signatures.clear()
                            before = current
                        elif journal.repeated_actions():
                            status = "stalled"
                            break
                if task.done():
                    try:
                        task.result()
                    except Exception as error:
                        status = "execution_error"
                        failure = type(error).__name__
                    else:
                        journal.poll()
                        failure = None
                        status = (
                            "invalid_handoff"
                            if handoff_path.exists()
                            else "missing_handoff"
                        )
                        if (
                            status == "missing_handoff"
                            and journal.native_finish_reason == "max_rounds"
                        ):
                            status = "segment_exhausted"
                    error = failure
                    break
                if budget.remaining() <= 0 and not budget.requests:
                    status = "budget_exhausted"
                    break
                await asyncio.sleep(0.25)
        finally:
            if not task.done():
                try:
                    await self.stop(environment)
                    await asyncio.wait_for(asyncio.shield(task), timeout=20)
                except (Exception, asyncio.CancelledError):
                    try:
                        await self.stop(environment, force=True)
                        await asyncio.wait_for(asyncio.shield(task), timeout=10)
                    except (Exception, asyncio.CancelledError):
                        unsettled = not task.done()
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            else:
                await asyncio.gather(task, return_exceptions=True)
            journal.poll()
            if transport_path is not None:
                budget.finish(transport_path)
            checkpoint = {
                "status": status or "cancelled",
                "session_id": self.session_id,
                "call": self.calls,
                "handoff_id": handoff_id,
                "last_action": journal.last_action,
                "last_result": journal.last_result,
                "metrics": journal.summary(),
                "error": error,
            }
            (
                self.logs_dir / f"checkpoint-{self.session_id}-{self.calls:03d}.json"
            ).write_text(json.dumps(checkpoint, indent=2) + "\n")
            self.last_outcome = SessionOutcome(
                status or "cancelled",
                report,
                journal.summary(),
                journal.last_action,
                journal.last_result,
                error,
            )
        if unsettled:
            raise RoleInfrastructureError(
                "BitFun did not settle after bounded process cleanup"
            )
        return self.last_outcome

    async def collect(self, environment):
        await asyncio.wait_for(
            self.exec_as_agent(environment, self._cp_back_command()), timeout=60
        )
        await asyncio.wait_for(self._capture_final_app_config(environment), timeout=30)
