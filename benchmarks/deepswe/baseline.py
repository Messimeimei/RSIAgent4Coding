"""bitfun 的 baseline 评测流程"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pier.agents.network import allowlist_from_urls
from pier.models.agent.context import AgentContext
from pier.models.task.task import Task

from .bitfun_agent import BitfunCli, credential_variables
from .context_policy import for_role
from .completion import actor_evidence_leads, run_completion_checks
from .contracts import RoleInfrastructureError, TaskBudgetExhausted
from .prompts import actor_prompt, verifier_prompt
from .review_environment import (
    capture_candidate,
    repository_fingerprint,
    repository_state,
    review_environment,
    review_tree_digest,
)
from .session import BitfunSession
from .supervision import EventJournal
from .work_budget import WorkBudget
from .pier_budget import install_work_budget_support


class BitfunBaseline(BitfunCli):
    def __init__(
        self,
        *args,
        verifier_bitfun_config=None,
        total_timeout_sec=10200,
        report_recovery_attempts=2,
        command_timeout_sec=600,
        stall_recovery=True,
        allow_role_swap=True,
        reference_context_policy=False,
        **kwargs,
    ):
        legacy = {
            "max_rounds",
            "verifier_attempts",
            "actor_timeout_sec",
            "verifier_timeout_sec",
        } & kwargs.keys()
        if legacy:
            raise ValueError(
                "Legacy baseline budgets must be migrated to v2: "
                + ", ".join(sorted(legacy))
            )
        super().__init__(*args, **kwargs)
        self.verifier_config = copy.deepcopy(
            verifier_bitfun_config or self._bitfun_config
        )
        credential_variables(self.verifier_config)
        for value in (total_timeout_sec, command_timeout_sec):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(
                    "Baseline time budgets must be finite positive seconds"
                )
        if (
            type(report_recovery_attempts) is not int
            or not 0 <= report_recovery_attempts <= 5
        ):
            raise ValueError("report_recovery_attempts must be between 0 and 5")
        if type(stall_recovery) is not bool or type(allow_role_swap) is not bool:
            raise ValueError("Recovery switches must be booleans")
        if type(reference_context_policy) is not bool:
            raise ValueError("reference_context_policy must be a boolean")
        self.reference_context_policy = reference_context_policy
        self.total_timeout_sec = total_timeout_sec
        self.command_timeout_sec = command_timeout_sec
        self.report_recovery_attempts = report_recovery_attempts
        self.stall_recovery = stall_recovery
        self.allow_role_swap = allow_role_swap
        self.baseline_dir = self.logs_dir.parent / "baseline"
        self.summary = None
        self._reviewers = {}
        self._calls = []
        self._work_budget = None
        install_work_budget_support()

    @staticmethod
    def name():
        return "bitfun-baseline"

    def role_configs(self):
        return {
            "actor": for_role(self._bitfun_config, "target_actor", enabled=self.reference_context_policy),
            "verifier": for_role(self.verifier_config, "verifier", enabled=self.reference_context_policy),
        }

    def network_allowlist(self):
        configs = (
            self.role_configs().values()
            if self.allow_role_swap
            else [self._bitfun_config]
        )
        return allowlist_from_urls(
            [
                model["base_url"]
                for config in configs
                for model in config.get("ai", {}).get("models", [])
                if model.get("enabled", True)
            ]
        )

    def _session(self, logs_dir, config):
        return BitfunSession(
            logs_dir=Path(logs_dir),
            bitfun_config=config,
            binary_path=self._binary_path,
            output_patch_path=None,
            exec_agent=self._exec_agent,
            extra_env=self._extra_env,
            logger=self.logger,
        )

    def _remaining(self):
        remaining = self._work_budget.remaining()
        if remaining <= 0:
            raise TaskBudgetExhausted("Shared task work budget exhausted")
        return remaining

    def _flush(self):
        if self.summary is not None:
            path = self.baseline_dir / "result.json"
            pending = path.with_suffix(".tmp")
            pending.write_text(json.dumps(self.summary, indent=2) + "\n")
            pending.replace(path)

    def _record_protocol(self):
        import core.actor
        import core.checks
        import core.verifier_runtime
        import explore.charter

        files = list(Path(__file__).parent.glob("*.py")) + [
            Path(source.__file__)
            for source in (core.actor, core.checks, core.verifier_runtime, explore.charter)
        ]
        record = {
            "protocol_version": 2,
            "reference_context_policy": self.reference_context_policy,
            "source_sha256": {
                str(path.relative_to(Path(__file__).parents[2])): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in files
            },
            "roles": self.role_configs(),
            "prompt_policy": "shared_reference_method_with_coding_interface_adaptations",
            "prompt_delivery": "role_message_to_bitfun_session_with_native_system_prompt",
            "shared_work_sec": self.total_timeout_sec,
            "budget_policy": "undisclosed_work_ceiling_excluding_recoverable_model_transport",
            "command_timeout_sec": self.command_timeout_sec,
            "report_recovery_attempts": self.report_recovery_attempts,
            "stall_recovery": self.stall_recovery,
            "allow_role_swap": self.allow_role_swap,
            "native_segment_policy": "Resume max_rounds in the same session within shared work budget; do not consume report recovery attempts",
            "actor_evidence_policy": "reproducible_leads_only_after_unverified",
            "stall_policy": "inspect_current_committed_head_before_recovery_or_incomplete_exit",
            "actor_worklog_policy": "factual_rounds_and_actor_tool_observations",
            "no_official_feedback": True,
        }
        (self.baseline_dir / "protocol.json").write_text(
            json.dumps(record, indent=2) + "\n"
        )

    def event(self, kind, **payload):
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event": kind,
            **payload,
        }
        with (self.baseline_dir / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._flush()

    async def _invoke(
        self, session, environment, prompt, role, handoff_id, candidate_id=None
    ):
        self.event(
            role + "_start",
            session_id=session.session_id,
            resume=session.calls > 0,
            round=len(self.summary["rounds"]) + (role == "actor"),
            handoff_id=handoff_id,
            remaining_sec=self._remaining(),
        )
        try:
            return await session.execute(
                environment,
                prompt,
                self._remaining(),
                role=role,
                handoff_id=handoff_id,
                candidate_id=candidate_id,
                progress_probe=lambda: repository_fingerprint(environment),
                detect_stall=True,
                work_budget=self._work_budget,
            )
        finally:
            outcome = session.last_outcome
            if outcome is not None:
                self._calls.append(
                    {
                        "role": role,
                        "session_id": session.session_id,
                        "call": session.calls,
                        "status": outcome.status,
                        "metrics": outcome.metrics,
                        **(
                            {
                                "last_action": outcome.last_action,
                                "last_result": outcome.last_result,
                            }
                            if role == "actor"
                            else {}
                        ),
                    }
                )
                self.event(
                    role + "_end",
                    session_id=session.session_id,
                    status=outcome.status,
                    metrics=outcome.metrics,
                )

    async def _collect(self, session, environment):
        try:
            await session.collect(environment)
            session.populate_context_post_run(AgentContext())
            trajectory = session.logs_dir / "trajectory.json"
            if trajectory.exists():
                shutil.copyfile(
                    trajectory,
                    session.logs_dir / f"trajectory-{session.session_id}.json",
                )
        except Exception as error:
            self.event(
                "artifact_collection_error",
                session_id=session.session_id,
                error_type=type(error).__name__,
            )

    async def _verify(
        self,
        instruction,
        actor_environment,
        candidate,
        round_number,
        config,
        *,
        evidence_leads="",
        review_id=None,
    ):
        config = for_role(config, "verifier", enabled=self.reference_context_policy)
        key = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        destination = self.baseline_dir / "reviews" / (
            review_id or f"round-{round_number:03d}"
        )
        reviewer = self._reviewers.get(key)
        if reviewer is None:
            reviewer = self._session(destination / "agent", config)
            self._reviewers[key] = reviewer
        reviewer.logs_dir = destination / "agent"
        private_root = self.baseline_dir / "verifier-private" / reviewer.session_id
        self.summary["verifier_session_ids"] = [
            r.session_id for r in self._reviewers.values()
        ]
        async with review_environment(
            actor_environment, reviewer, candidate, destination, private_root
        ) as environment:
            await reviewer.prepare(environment)
            original = await review_tree_digest(environment, candidate.base_commit)
            recovery = None
            failures = 0
            try:
                while True:
                    handoff_id = uuid.uuid4().hex
                    prompt = verifier_prompt(
                        instruction,
                        candidate,
                        round_number,
                        handoff_id,
                        actor_evidence_leads=evidence_leads,
                        recovery=recovery,
                    )
                    outcome = await self._invoke(
                        reviewer,
                        environment,
                        prompt,
                        "verifier",
                        handoff_id,
                        candidate.candidate_id,
                    )
                    if (
                        await review_tree_digest(environment, candidate.base_commit)
                        != original
                    ):
                        raise RoleInfrastructureError(
                            "Verifier modified candidate tracked state"
                        )
                    if outcome.status == "handoff":
                        return outcome.report
                    if outcome.status == "budget_exhausted":
                        raise TaskBudgetExhausted(
                            "Verifier exhausted shared task budget"
                        )
                    if outcome.status == "segment_exhausted":
                        self.event(
                            "verifier_segment_continue", session_id=reviewer.session_id
                        )
                        recovery = (
                            "The native BitFun model-round segment ended. "
                            "Continue this SAME investigation and preserve all "
                            "previous evidence. This is not an invalid report or a verdict. "
                            "Publish at the new report path when a defensible verdict is ready."
                        )
                        continue
                    failures += 1
                    if failures > self.report_recovery_attempts:
                        raise RoleInfrastructureError(
                            "Verifier report recovery exhausted without a valid report"
                        )
                    self.event(
                        "verifier_report_recovery",
                        session_id=reviewer.session_id,
                        attempt=failures,
                        cause=outcome.status,
                    )
                    recovery = (
                        "Your preceding invocation did not publish a valid terminal report. "
                        "This is a transport/protocol recovery, NOT an UNVERIFIED verdict. "
                        "Continue the same investigation; use your saved tool observations. "
                        "Stop repeating unchanged actions. Publish the report at the NEW path. "
                        f"Last action: {json.dumps(outcome.last_action)}\n"
                        f"Last observed result: {outcome.last_result[-16000:]}"
                    )
            finally:
                await self._collect(reviewer, environment)

    async def _verify_stalled_candidate(
        self,
        instruction,
        environment,
        base_commit,
        round_number,
        verifier_config,
        cause,
    ):
        """Inspect the latest committed candidate before changing Actor context.

        The reference loop treats a stall as an exit condition for an
        independent inspection: the machine may already contain a complete
        solution even though the Actor never published its terminal message.
        Coding candidates are inspected from a patch snapshot so the Verifier
        still cannot mutate the Actor worktree.  Uncommitted files are recorded
        in the snapshot but deliberately excluded from that patch.
        """

        self._remaining()
        review_id = f"stall-{len(self.summary['stall_reviews']) + 1:03d}"
        destination = self.baseline_dir / "candidates" / review_id
        candidate = await capture_candidate(environment, base_commit, destination)
        self.summary["final_candidate_id"] = candidate.candidate_id
        entry = {
            "review_id": review_id,
            "round": round_number,
            "cause": cause,
            "candidate": candidate.to_dict(),
            "candidate_scope": "committed_head_only",
            "actor_evidence_disclosed": False,
            "verdict": None,
        }
        self.summary["stall_reviews"].append(entry)
        self.summary.update(phase="stall_verifier", verdict=None)
        self.event(
            "stall_review_start",
            review_id=review_id,
            round=round_number,
            cause=cause,
            candidate_id=candidate.candidate_id,
        )
        # A dirty worktree has no safe candidate for the isolated Verifier.  Do
        # not let a PASS on the older HEAD accidentally turn uncommitted work
        # into a completed task; a fresh Actor can commit or clean it up.
        if candidate.status:
            entry.update(
                inspection="skipped_dirty_candidate",
                reason="uncommitted Actor changes are excluded from candidate patch",
            )
            self.event(
                "stall_review_skipped",
                review_id=review_id,
                round=round_number,
                candidate_id=candidate.candidate_id,
                reason="dirty_candidate",
            )
            return None
        # Like core.loop's stall-exit _inspect, this forced inspection is
        # independent of Done/self-checks, even after an UNVERIFIED request.
        review = await self._verify(
            instruction,
            environment,
            candidate,
            round_number,
            verifier_config,
            evidence_leads="",
            review_id=review_id,
        )
        if await repository_state(environment) != {
            "repo": candidate.repo,
            "head": candidate.head,
            "status": candidate.status,
        }:
            raise RoleInfrastructureError(
                "Actor repository changed during isolated stall checking"
            )
        entry.update(
            review=review.model_dump(),
            verdict=review.verdict,
            actor_unchanged_after_review=True,
        )
        self.summary["verdict"] = review.verdict
        self.event(
            "stall_review",
            review_id=review_id,
            round=round_number,
            candidate_id=candidate.candidate_id,
            verdict=review.verdict,
            dirty_candidate=bool(candidate.status),
        )
        return review

    def _build_actor_worklog(self, actor, outcome, feedback, state):
        """Preserve public feedback and Actor facts, never Verifier traces.

        Native tool observations include work done before the first handoff and
        between handoffs. Replaying only Actor streams also keeps fresh-context
        recovery from importing the Verifier's private investigation.
        """
        calls = [copy.deepcopy(c) for c in self._calls if c["role"] == "actor"]
        tool_history = []
        for call in calls:
            stream = self.logs_dir / (
                f"stream-{call['session_id']}-{call['call']:03d}.jsonl"
            )
            journal = EventJournal(
                stream, call["session_id"], record_observations=True
            )
            journal.poll()
            tool_history.append(
                {
                    "session_id": call["session_id"],
                    "call": call["call"],
                    "stream": f"/logs/agent/{stream.name}",
                    "stream_available": stream.is_file(),
                    "observations": journal.observations,
                }
            )
        return {
            "repository": state,
            "latest_candidate": (
                self.summary["stall_reviews"][-1]["candidate"]
                if self.summary.get("stall_reviews")
                else None
            ),
            "pending_feedback": feedback,
            "last_action": outcome.last_action,
            "last_observation": outcome.last_result,
            "prior_actor_session_id": actor.session_id,
            "rounds": copy.deepcopy(self.summary["rounds"]),
            "stall_reviews": copy.deepcopy(self.summary["stall_reviews"]),
            "actor_calls": calls,
            "actor_tool_history": tool_history,
        }

    async def _handle_actor_stall(
        self,
        instruction,
        actor,
        environment,
        base_commit,
        round_number,
        actor_config,
        verifier_config,
        outcome,
        feedback,
        recovery_count,
        cause,
    ):
        review = await self._verify_stalled_candidate(
            instruction, environment, base_commit, round_number, verifier_config, cause
        )
        if review is not None and self._terminal_review(review):
            self.summary.update(
                status="completed" if review.verdict == "PASS" else "learning",
                stop_reason="stall_verifier_" + review.verdict.lower(),
            )
            return None
        feedback = json.dumps(
            {
                "prior_feedback": feedback,
                "stall_review": review.model_dump() if review else None,
                "stall_reason": (
                    "candidate_dirty_or_uncommitted"
                    if review is None
                    else "verifier_did_not_pass"
                ),
            },
            ensure_ascii=False,
        )
        resumed = await self._recover_actor(
            actor, environment, actor_config, verifier_config,
            outcome, feedback, recovery_count,
        )
        return (*resumed, feedback) if resumed else None

    async def _recover_actor(
        self,
        actor,
        environment,
        actor_config,
        verifier_config,
        outcome,
        feedback,
        recovery_count,
    ):
        if not self.stall_recovery or recovery_count:
            reason = (
                "stall_recovery_exhausted" if recovery_count else "stall_recovery_disabled"
            )
            self.summary.update(status="incomplete", stop_reason=reason)
            self.event(
                "actor_context_recovery_exhausted",
                session_id=actor.session_id,
                recovery_count=recovery_count,
                reason=reason,
            )
            return None
        self._remaining()
        await self._collect(actor, environment)
        state = await repository_state(environment)
        handover = self._build_actor_worklog(actor, outcome, feedback, state)
        serialized = json.dumps(handover, indent=2, ensure_ascii=False) + "\n"
        (self.baseline_dir / "actor-recovery.json").write_text(serialized)
        # The full factual record remains readable by the fresh Actor. The
        # reference handoff_message caps inline context to avoid recreating a
        # saturated conversation; it also instructs the Actor to preserve work.
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "actor-worklog.json").write_text(serialized)
        from core.actor import handoff_message

        recovery = handoff_message(serialized) + (
            "\n\nFull factual work log: /logs/agent/actor-worklog.json. "
            "Read it with your tools for earlier candidates, feedback and observed "
            "actions/results. Actor handoff summaries are historical claims; "
            "recheck them against the repository."
        )
        self._remaining()
        if self.allow_role_swap:
            actor_config, verifier_config = verifier_config, actor_config
        actor_config = for_role(actor_config, "target_actor", enabled=self.reference_context_policy)
        verifier_config = for_role(verifier_config, "verifier", enabled=self.reference_context_policy)
        fresh = self._session(self.logs_dir, actor_config)
        await fresh.prepare(environment)
        self.summary["actor_session_ids"].append(fresh.session_id)
        self.event(
            "actor_context_recovery",
            old_session_id=actor.session_id,
            session_id=fresh.session_id,
            role_swap=self.allow_role_swap,
            preserved_repository=state,
        )
        return (
            fresh,
            actor_config,
            verifier_config,
            recovery,
        )

    async def run(self, instruction, environment, context):
        """
        运行主入口，传入
        """
        self.baseline_dir.mkdir(parents=True, exist_ok=True)
        self._record_protocol()
        actor_config = for_role(self._bitfun_config, "target_actor", enabled=self.reference_context_policy)
        verifier_config = for_role(self.verifier_config, "verifier", enabled=self.reference_context_policy)
        actor = self._session(self.logs_dir, actor_config)
        started = time.monotonic()
        if self._work_budget is None:
            self._work_budget = WorkBudget(self.total_timeout_sec)
        self.summary = {
            "mode": "baseline",
            "protocol_version": 2,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "actor_session_id": actor.session_id,
            "actor_session_ids": [actor.session_id],
            "verifier_session_ids": [],
            "status": "running",
            "stop_reason": "running",
            "verdict": None,
            "rounds": [],
            "stall_reviews": [],
            "budget": {
                "shared_work_sec": self.total_timeout_sec,
                "disclosed_to_roles": False,
                "max_rounds": None,
                "command_timeout_sec": self.command_timeout_sec,
            },
            "official_grading": "Unchanged Pier official grader runs after controller; no scores enter feedback",
        }
        captured = False
        recovery_count = 0
        feedback = recovery = None
        protocol_failures = 0
        no_progress = []
        actor_evidence_requested = False
        work_timeout = None
        try:
            # Like VM boot/setup in the reference, this precedes the work loop.
            # Its own runtime timeout still detects a stuck container operation.
            with self._work_budget.initial_setup():
                async with asyncio.timeout(300):
                    task = Task(environment.environment_dir.parent)
                    initial = await repository_state(environment)
                    base = task.config.metadata.get("base_commit_hash")
                    if not base or initial["head"] != base or initial["status"]:
                        raise RoleInfrastructureError(
                            "Baseline requires the clean pinned task base"
                        )
                    await actor.prepare(environment)
                    await self._capture_repo_baseline(environment)
                    captured = True
            async with self._work_budget.limit() as work_timeout:
                while True:
                    round_number = len(self.summary["rounds"]) + 1
                    handoff_id = uuid.uuid4().hex
                    self.summary["verdict"] = None
                    self.summary["phase"] = "actor"
                    outcome = await self._invoke(
                        actor,
                        environment,
                        actor_prompt(
                            instruction,
                            round_number,
                            handoff_id,
                            feedback,
                            recovery,
                        ),
                        "actor",
                        handoff_id,
                    )
                    recovery = None
                    if outcome.status == "budget_exhausted":
                        raise TaskBudgetExhausted("Actor exhausted shared task budget")
                    if outcome.status == "segment_exhausted":
                        self.event(
                            "actor_segment_continue", session_id=actor.session_id
                        )
                        recovery = (
                            "The native BitFun model-round segment ended. Continue the SAME "
                            "session and repository. Preserve "
                            "your implementation and pending review feedback. This is not a "
                            "new task or an invalid report. Complete and test the solution, "
                            "then publish at the new handoff path when ready."
                        )
                        continue
                    if outcome.status == "stalled":
                        resumed = await self._handle_actor_stall(
                            instruction, actor, environment, base, round_number,
                            actor_config, verifier_config, outcome, feedback,
                            recovery_count, "repeated_actions",
                        )
                        if resumed is None:
                            break
                        (
                            actor,
                            actor_config,
                            verifier_config,
                            recovery,
                            feedback,
                        ) = resumed
                        recovery_count += 1
                        protocol_failures = 0
                        no_progress.clear()
                        actor_evidence_requested = False
                        continue
                    if outcome.status != "handoff":
                        protocol_failures += 1
                        if protocol_failures > self.report_recovery_attempts:
                            raise RoleInfrastructureError(
                                "Actor handoff recovery exhausted"
                            )
                        feedback = (
                            "No valid Actor terminal handoff was received. Continue this same session; "
                            "preserve the solution, include executed/falsifiable probes, and publish the "
                            "required JSON at the new handoff path. Previous pending feedback:\n"
                            + (feedback or "None")
                        )
                        self.event(
                            "actor_report_recovery",
                            cause=outcome.status,
                            attempt=protocol_failures,
                        )
                        continue
                    protocol_failures = 0
                    candidate = await capture_candidate(
                        environment,
                        base,
                        self.baseline_dir / "candidates" / f"round-{round_number:03d}",
                    )
                    entry = {
                        "round": round_number,
                        "actor_session_id": actor.session_id,
                        "actor_handoff": outcome.report.model_dump(),
                        "candidate": candidate.to_dict(),
                        "verdict": None,
                    }
                    self.summary["rounds"].append(entry)
                    self.summary["final_candidate_id"] = candidate.candidate_id
                    if (
                        candidate.status
                        or candidate.head != outcome.report.candidate_head
                    ):
                        feedback = (
                            "Completion rejected: commit all intended changes, leave a clean worktree, "
                            "and publish the actual current git HEAD. No correctness verdict was issued."
                        )
                        entry["completion_error"] = feedback
                    else:
                        self.summary["phase"] = "completion_checks"
                        self.event("completion_checks_start", round=round_number)
                        completion = await run_completion_checks(
                            environment,
                            candidate,
                            outcome.report,
                            instruction,
                            self.baseline_dir / "checks" / f"round-{round_number:03d}",
                            self.command_timeout_sec,
                        )
                        entry["completion"] = completion
                        self.event(
                            "completion_checks_end",
                            round=round_number,
                            passed=completion["passed"],
                        )
                        if not completion["passed"]:
                            feedback = (
                                "Runtime completion probes rejected or failed. Correct the solution/probes:\n"
                                + json.dumps(completion)
                            )
                        else:
                            self.summary["phase"] = "verifier"
                            # A pending UNVERIFIED request is consumed at the next
                            # ordinary inspection, not by failed completion probes.
                            evidence_leads = (
                                actor_evidence_leads(completion)
                                if actor_evidence_requested
                                else ""
                            )
                            actor_evidence_requested = False
                            entry["actor_evidence_disclosed"] = bool(evidence_leads)
                            report = await self._verify(
                                instruction,
                                environment,
                                candidate,
                                round_number,
                                verifier_config,
                                evidence_leads=evidence_leads,
                            )
                            actor_evidence_requested = report.verdict == "UNVERIFIED"
                            entry["review"] = report.model_dump()
                            entry["verdict"] = report.verdict
                            self.summary["verdict"] = report.verdict
                            feedback = json.dumps(
                                report.model_dump(), ensure_ascii=False
                            )
                            self.event(
                                "review",
                                round=round_number,
                                verdict=report.verdict,
                                candidate_id=candidate.candidate_id,
                                actor_evidence_disclosed=bool(evidence_leads),
                            )
                    after = await repository_state(environment)
                    if after != {
                        "repo": candidate.repo,
                        "head": candidate.head,
                        "status": candidate.status,
                    }:
                        raise RoleInfrastructureError(
                            "Actor repository changed during isolated checking"
                        )
                    entry["actor_unchanged_after_review"] = True
                    if entry.get("review") and self._terminal_review(report):
                        self.summary.update(
                            status="completed" if report.verdict == "PASS" else "learning",
                            stop_reason="verifier_" + report.verdict.lower(),
                        )
                        break
                    # Exact recurrence requires unchanged submitted code AND identical
                    # probes/feedback. Ordinary iterative progress never triggers this.
                    recurrence = json.dumps(
                        [
                            candidate.patch_sha256,
                            candidate.status,
                            [c.model_dump() for c in outcome.report.checks],
                            {
                                k: v
                                for k, v in (entry.get("review") or {}).items()
                                if k not in {"handoff_id", "candidate_id"}
                            },
                            entry.get("completion_error"),
                            [
                                (c["passed"], c["output"])
                                for c in entry.get("completion", {}).get("checks", [])
                            ],
                        ],
                        sort_keys=True,
                    )
                    no_progress.append(recurrence)
                    if len(no_progress) >= 3 and len(set(no_progress[-3:])) == 1:
                        resumed = await self._handle_actor_stall(
                            instruction, actor, environment, base, round_number,
                            actor_config, verifier_config, outcome, feedback,
                            recovery_count, "repeated_candidates",
                        )
                        if resumed is None:
                            break
                        (
                            actor,
                            actor_config,
                            verifier_config,
                            recovery,
                            feedback,
                        ) = resumed
                        recovery_count += 1
                        protocol_failures = 0
                        no_progress.clear()
                        actor_evidence_requested = False
                    self.event(
                        "actor_feedback",
                        round=round_number,
                        reason=entry["verdict"] or "completion_failed",
                    )
                self.summary["final_repository"] = await repository_state(environment)
        except TaskBudgetExhausted:
            self.summary.update(
                status="incomplete", stop_reason="task_budget_exhausted"
            )
            self.event("task_budget_exhausted")
        except TimeoutError:
            if work_timeout is None or not work_timeout.expired():
                self.summary.update(
                    status="infrastructure_error", stop_reason="infrastructure_timeout"
                )
                raise
            self.summary.update(
                status="incomplete", stop_reason="task_budget_exhausted"
            )
            self.event("task_budget_exhausted")
        except asyncio.CancelledError:
            self.summary.update(status="cancelled", stop_reason="cancelled")
            raise
        except Exception as error:
            self.summary.update(
                status="infrastructure_error",
                stop_reason="infrastructure_error",
                error_type=type(error).__name__,
                error=str(error)
                if isinstance(error, RoleInfrastructureError)
                else None,
            )
            raise
        finally:
            if captured:
                try:
                    await asyncio.wait_for(
                        self._capture_repo_final_state(environment), timeout=60
                    )
                    self.summary["final_repository"] = await asyncio.wait_for(
                        repository_state(environment), timeout=20
                    )
                    await self._collect(actor, environment)
                except Exception as error:
                    self.summary["artifact_collection_error"] = type(error).__name__
            self.summary["calls"] = self._calls
            self.summary["usage"] = self._usage()
            self.summary["elapsed_sec"] = round(time.monotonic() - started, 3)
            self.summary["budget"].update(self._work_budget.summary())
            self.summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            self._flush()

    def _terminal_review(self, report):
        """Baseline repairs FAIL; the RSI training subclass learns from it."""
        return report.verdict == "PASS"

    def _usage(self):
        by_role = {}
        for role in ("actor", "verifier"):
            records = [
                call["metrics"]
                for call in self._calls
                if call["role"] == role and call["metrics"]
            ]
            by_role[role] = {
                key: sum(record[key] for record in records)
                if records and all(record.get(key) is not None for record in records)
                else None
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cached_tokens",
                    "model_calls",
                )
            }
        return by_role

    def populate_context_post_run(self, context):
        # Preserve the native Actor trajectory, but publish combined observed role
        # usage separately so review tokens are not silently discarded.
        super().populate_context_post_run(context)
        usage = (self.summary or {}).get("usage", {})
        for field, key in [
            ("n_input_tokens", "input_tokens"),
            ("n_output_tokens", "output_tokens"),
            ("n_cache_tokens", "cached_tokens"),
        ]:
            values = [usage.get(role, {}).get(key) for role in ("actor", "verifier")]
            setattr(
                context,
                field,
                sum(values) if all(value is not None for value in values) else None,
            )
        context.cost_usd = None
        context.metadata = context.metadata or {}
        context.metadata["baseline"] = {
            "protocol_version": 2,
            "result_path": "baseline/result.json",
            "events_path": "baseline/events.jsonl",
            "verdict": (self.summary or {}).get("verdict"),
            "stop_reason": (self.summary or {}).get("stop_reason"),
            "usage_by_role": usage,
            "usage_scope": "Observed native events for both roles; may exclude subagents/compression",
        }
