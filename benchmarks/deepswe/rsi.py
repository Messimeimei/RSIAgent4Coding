"""完整 RSI 流程控制。注册成为 Pier """

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import threading
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from pier.models.agent.context import AgentContext

from config.settings import Config
from core.self_evolving_loop import (
    ActorLearning, EvolutionResult, EvolutionStatus, SelfEvolvingLoopHooks,
    TargetVerdict, TargetVerification, run_self_evolving_loop,
)
from explore.charter import memory_preamble
from explore.phase1_wave import evolve_parallel_phase1
from explore.practice_loop import _atomic_install_memory, _listing, _read_memory_tree
from explore.target_learning import _memory_tree_sha256, _promote_learning
from explore.unified_evolution import UnifiedCurriculumSession, evolve_until_ready
from .baseline import BitfunBaseline
from .bitfun_agent import BitfunCli, credential_variables
from .context_policy import for_role
from .contracts import RoleInfrastructureError
from .rsi_runtime import CodingAudit, CodingRuntime, CodingVM, write_json


class MemoryBaseline(BitfunBaseline):
    """Existing baseline loop; only memory attachment and training FAIL exit vary."""

    def __init__(self, *args, memory, training=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.memory = dict(memory)
        self.training = training
        self.active_actor = None
        self.terminal_report = None
        self.memory_delivered = set()

    def _terminal_review(self, report):
        terminal = report.verdict == "PASS" or (self.training and report.verdict == "FAIL")
        if terminal:
            self.terminal_report = report
        return terminal

    async def _invoke(self, session, environment, prompt, role, *args, **kwargs):
        if role == "actor":
            self.active_actor = session
            if session.session_id not in self.memory_delivered:
                prompt += "\n\n" + memory_preamble(_listing(self.memory)).replace(
                    "~/.memory", "/home/user/.memory")
                if self.training:
                    prompt += (
                        "\nPHASE-2 TRAINING LIFECYCLE: A grounded FAIL ends this target "
                        "attempt and enters same-Actor learning, then Curriculum practice. "
                        "UNVERIFIED requests evidence in this same attempt. A PASS is also "
                        "followed by same-Actor learning and Curriculum review. No official "
                        "evaluator feedback is available."
                    )
                self.memory_delivered.add(session.session_id)
        return await super()._invoke(session, environment, prompt, role, *args, **kwargs)


class BitfunRSI(BitfunCli):
    def __init__(self, *args, verifier_bitfun_config=None,
                 curriculum_bitfun_config=None, total_timeout_sec=10200,
                 learning_timeout_sec=86400, target_learning_timeout_sec=36000,
                 brs_project_budget=8, parallel=2, report_recovery_attempts=2,
                 checkpoint_repair_timeout_sec=3600,
                 command_timeout_sec=600, stall_recovery=True, allow_role_swap=True,
                 reference_context_policy=False, resume_from=None, **kwargs):
        super().__init__(*args, **kwargs)
        if type(reference_context_policy) is not bool:
            raise ValueError("reference_context_policy must be a boolean")
        self.reference_context_policy = reference_context_policy
        self.resume_from = Path(resume_from).resolve() if resume_from is not None else None
        self.verifier_config = copy.deepcopy(verifier_bitfun_config or self._bitfun_config)
        self.curriculum_config = copy.deepcopy(curriculum_bitfun_config or self.verifier_config)
        for value in (total_timeout_sec, learning_timeout_sec, target_learning_timeout_sec,
                      checkpoint_repair_timeout_sec):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("RSI work ceilings must be finite positive seconds")
        if type(parallel) is not int or parallel < 1:
            raise ValueError("parallel must be a positive integer")
        if type(brs_project_budget) is not int or brs_project_budget < 0:
            raise ValueError("brs_project_budget must be a nonnegative integer")
        if type(report_recovery_attempts) is not int or not 0 <= report_recovery_attempts <= 5:
            raise ValueError("Invalid report recovery allowance")
        for cfg in self.role_configs().values():
            credential_variables(cfg)
        self.total_timeout_sec = total_timeout_sec
        self.learning_timeout_sec = learning_timeout_sec
        self.target_learning_timeout_sec = target_learning_timeout_sec
        self.brs_project_budget = brs_project_budget
        self.parallel = parallel
        self.report_recovery_attempts = report_recovery_attempts
        self.checkpoint_repair_timeout_sec = checkpoint_repair_timeout_sec
        self.command_timeout_sec = command_timeout_sec
        self.stall_recovery, self.allow_role_swap = stall_recovery, allow_role_swap
        self.rsi_dir = self.logs_dir.parent / "rsi"
        self.final_agent = None
        self.event_lock = threading.Lock()
        from .rsi_budget import install_rsi_budget_support
        install_rsi_budget_support()

    @staticmethod
    def name():
        return "bitfun-rsi"

    def role_configs(self):
        return {
            "actor": for_role(self._bitfun_config, "practice_actor", enabled=self.reference_context_policy),
            "verifier": for_role(self.verifier_config, "verifier", enabled=self.reference_context_policy),
            "curriculum": for_role(self.curriculum_config, "curriculum", enabled=self.reference_context_policy),
        }

    def network_allowlist(self):
        from pier.agents.network import allowlist_from_urls
        return allowlist_from_urls([model["base_url"] for cfg in self.role_configs().values()
            for model in cfg.get("ai", {}).get("models", []) if model.get("enabled", True)])

    def _session(self, logs_dir, config, *, session_id=None):
        from .session import BitfunSession
        return BitfunSession(logs_dir=Path(logs_dir), bitfun_config=config,
            binary_path=self._binary_path, output_patch_path=None,
            exec_agent=self._exec_agent, extra_env=self._extra_env, logger=self.logger,
            session_id=session_id)

    def event(self, kind, **payload):
        record = {"time": datetime.now(timezone.utc).isoformat(), "event": kind, **payload}
        with self.event_lock:
            with (self.rsi_dir / "events.jsonl").open("a") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def role(self, name):
        return Config(model="bitfun:" + name, max_iters=2000,
            wall_clock_secs=self.learning_timeout_sec,
            practice_mode=True, independent_verify=False, agent_decided_stop=True,
            max_resumes=0, history_keep_pairs=0, vision_model="",
            agentic_verifier_config=str(Path(__file__).parents[2] /
                "config/deepswe/rsi-practice-verifier.yaml"),
            verifier_unverified_evidence=True, verifier_hide_actor_memory=True)

    def baseline(self, logs_dir, memory, *, training):
        return MemoryBaseline(logs_dir=Path(logs_dir), memory=memory, training=training,
            bitfun_config=self._bitfun_config, verifier_bitfun_config=self.verifier_config,
            binary_path=self._binary_path, output_patch_path=None,
            exec_agent=self._exec_agent, extra_env=self._extra_env, logger=self.logger,
            total_timeout_sec=self.target_learning_timeout_sec if training else self.total_timeout_sec,
            report_recovery_attempts=self.report_recovery_attempts,
            command_timeout_sec=self.command_timeout_sec,
            stall_recovery=self.stall_recovery, allow_role_swap=self.allow_role_swap,
            reference_context_policy=self.reference_context_policy)

    def freeze(self, source, destination):
        memory = _read_memory_tree(str(source))
        _atomic_install_memory(str(destination), memory)
        record = {"path": str(destination), "files": len(memory),
                  "bytes": sum(map(len, memory.values())), "sha256": _memory_tree_sha256(memory)}
        write_json(Path(destination).with_suffix(".manifest.json"), record)
        return memory, record

    def phase2(self, runtime, instruction, initial):
        root = self.rsi_dir / "phase2"
        active_memory = root / "memory"
        _atomic_install_memory(str(active_memory), initial)
        curriculum = UnifiedCurriculumSession()
        updates = 0

        def fresh_environment(direction, cycle):
            lease, vm = runtime.acquire(target=True)
            return SimpleNamespace(lease=lease, vm=vm, cycle=cycle)

        def work(actor, verifier, environment, direction):
            _atomic_install_memory(str(active_memory), actor.memory)
            if not runtime.hooks.push_memory(environment.vm, str(active_memory)):
                raise RoleInfrastructureError("Target memory upload failed")
            agent = self.baseline(environment.vm.environment.trial_paths.agent_dir,
                                  actor.memory, training=True)
            agent.baseline_dir = root / "target_cycles" / f"cycle_{actor.cycle:03d}"
            runtime.call(agent.run(direction, environment.vm.environment, AgentContext()))
            return agent

        def verify(verifier, environment, direction, agent):
            summary = agent.summary
            # Only committed reports with a completed attempt are terminal learning evidence.
            if summary["status"] in {"completed", "learning"} and agent.terminal_report is not None:
                report = agent.terminal_report.model_dump()
                verdict = report["verdict"]
                return TargetVerification(TargetVerdict(verdict),
                    "VERDICT: " + verdict + "\n\n" + json.dumps(report, ensure_ascii=False))
            return TargetVerification(TargetVerdict.UNVERIFIED,
                "Target attempt ended without a terminal grounded verdict: " + summary["stop_reason"])

        def learn(actor, environment, direction, output, verdict, report, memory):
            nonlocal updates
            updates += 1
            _atomic_install_memory(str(active_memory), memory)
            if not runtime.hooks.push_memory(environment.vm, str(active_memory)):
                raise RoleInfrastructureError("Canonical target memory restore failed")
            native = output.active_actor
            if native is None:
                raise RoleInfrastructureError("Same target Actor session is unavailable")
            episode = output.baseline_dir / "terminal_learning"
            history = runtime.adopt(native, working_dir="/app")
            # Preserve the actual active model after an authorized stall role swap.
            learned, diagnosis, _ = _promote_learning(
                hooks=runtime.hooks, vm=environment.vm, cfg=self.role("actor"),
                lineage=root, episode_dir=episode, experience_index=updates,
                before_memory=memory, actor_history=history,
                terminal_outcome=verdict.value, verifier_report=report, target=direction,
                audit_mode="exam", emit=self.event, project_open=False,
                corpus_path=str(runtime.audit.path), memory_dir=active_memory,
                journal_dir=root / "memory_journal", experience_kind="coding-phase2-target",
                target_visible_inputs=())
            write_json(episode / "outcome.json", {"verdict": verdict.value,
                "diagnosis": diagnosis, "same_actor_session_id": native.session_id,
                "memory_sha256": _memory_tree_sha256(learned)})
            return ActorLearning(learned, diagnosis)

        def evolve(direction, verdict, report, diagnosis, memory, index):
            lease, vm = runtime.acquire()
            try:
                result = evolve_until_ready(vm, str(root / "evolution_cycles" / f"cycle_{index:03d}"),
                    direction, report, diagnosis, memory,
                    self.role("actor"), self.role("verifier"), self.role("curriculum"), self.role("actor"),
                    corpus_path=str(runtime.audit.path), trigger_authority="phase2_outcome_protocol",
                    triggering_outcome=verdict.value, curriculum_memory_access="read_only",
                    agentic_verifier_cfg=self.role("actor"), target_visible_inputs=(),
                    hooks=runtime.hooks, session=curriculum, event_sink=self.event)
            finally:
                primary = sys.exception()
                try:
                    lease.close()
                except Exception as cleanup_error:
                    if primary is None:
                        raise
                    primary.add_note(f"DRS cleanup also failed: {runtime.checkpoints.clean(cleanup_error)}")
            if result.status not in {"ready_for_retry", "stalled"}:
                raise RoleInfrastructureError("DRS practice did not finish: " + result.status + ": " + result.reason)
            return EvolutionResult(EvolutionStatus.READY_FOR_RETRY if result.status == "ready_for_retry"
                else EvolutionStatus.STALLED, _read_memory_tree(result.memory_dir), result.projects, result.reason)

        result = run_self_evolving_loop(instruction, initial, SelfEvolvingLoopHooks(
            fresh_target_environment=fresh_environment,
            fresh_target_verifier=lambda *args: {},
            fresh_target_actor=lambda memory, cycle: SimpleNamespace(memory=dict(memory), cycle=cycle),
            actor_work=work, verifier_verify=verify, actor_learn=learn, evolve=evolve,
            learn_on_pass=True, curriculum_after_pass=True,
            release_target_environment=lambda env: env.lease.close(),
            on_event=lambda kind, payload: self.event(kind, **payload)))
        result.environment.lease.close()
        _atomic_install_memory(str(active_memory), result.memory)
        write_json(root / "phase-result.json", {
            "status": result.termination, "verdict": result.verifier_verdict.value,
            "target_cycles": result.target_cycles, "practice_projects": result.practice_projects,
            "learning_updates": updates, "official_evaluator_calls": 0,
            "transition_ready": result.verifier_verdict != TargetVerdict.UNVERIFIED})
        if result.verifier_verdict == TargetVerdict.UNVERIFIED:
            raise RoleInfrastructureError("DRS ended without a grounded target outcome; frozen evaluation not admitted")
        return self.freeze(active_memory, root / "memory_frozen")

    async def run(self, instruction, environment, context):
        self.rsi_dir.mkdir(parents=True, exist_ok=False)
        if self.resume_from is not None:
            from .rsi_resume import prepare_resume
            try:
                await asyncio.to_thread(prepare_resume, self.resume_from, self.rsi_dir,
                                        owner=self, instruction=instruction)
            except Exception as error:
                write_json(self.rsi_dir / "result.json", {
                    "status": "infrastructure_error", "phase": "resume_validation",
                    "error_type": type(error).__name__, "error": str(error)})
                raise
        root = Path(__file__).parents[2]
        sources = [*Path(__file__).parent.glob("*.py"),
            *[root / "core" / name for name in ("self_evolving_loop.py", "actor.py", "verifier_runtime.py")],
            root / "config/deepswe/rsi-practice-verifier.yaml",
            *[root / "explore" / name for name in ("phase1_wave.py", "practice_loop.py",
                "unified_evolution.py", "target_learning.py", "charter.py", "practice_evidence_recovery.py",
                "phase1_boundary_recovery.py")]]
        write_json(self.rsi_dir / "protocol.json", {
            "reference_context_policy": self.reference_context_policy,
            "target_actor_context_policy": for_role(self._bitfun_config, "target_actor",
                enabled=self.reference_context_policy).get("ai", {}).get("context_policy", {"mode": "native"}),
            "mode": "rsi", "parallel": self.parallel, "brs_project_budget": self.brs_project_budget,
            "brs_budget_boundary": "after_complete_wave", "phase2_stop_policy": "curriculum_review",
            "drs_project_cap": None, "curriculum_memory_access": "read_only",
            "target_query_conditioned": True, "phase3_work_sec": self.total_timeout_sec,
            "learning_role_work_sec": self.learning_timeout_sec,
            "training_target_work_sec": self.target_learning_timeout_sec,
            "checkpoint_repair_timeout_sec": self.checkpoint_repair_timeout_sec,
            "pier_timeout_policy": "RSI-only per-role and per-target work clocks; no global DRS timer",
            "budgets_disclosed_to_roles": False, "roles": self.role_configs(),
            "source_sha256": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
            "phase3": "fresh baseline with frozen memory; official evaluation only after phase3"})
        audit = CodingAudit(self.rsi_dir / "public-instruction-corpus.json", instruction,
                            environment.environment_dir.parent.parent)
        runtime = CodingRuntime(self, environment, self.rsi_dir / "runtime",
                                asyncio.get_running_loop(), audit)
        result = {"status": "running", "phase": "phase1"}
        write_json(self.rsi_dir / "result.json", result)
        try:
            from .rsi_resume import validate_coding_boundary
            self.event("PHASE1_RESUMING" if self.resume_from is not None else "PHASE1_STARTED")
            phase1 = await runtime.run_engine(evolve_parallel_phase1,
                root=str(self.rsi_dir / "phase1"), target_direction=instruction,
                actor_cfg=self.role("actor"), curriculum_cfg=self.role("curriculum"),
                memory_cfg=self.role("actor"), verifier_control_cfg=self.role("actor"),
                vm_factory=runtime.acquire, corpus_path=str(audit.path), hooks=runtime.hooks,
                event_sink=self.event, project_budget=self.brs_project_budget,
                checkpoint_projects=(0, 4, 8), max_parallel=self.parallel,
                target_query_conditioned=True, park_verified_branches=True,
                resume_completed_boundary=self.resume_from is not None,
                boundary_validator=validate_coding_boundary)
            write_json(self.rsi_dir / "phase1/phase-result.json", vars(phase1))
            if phase1.status not in {"saturated", "budget_exhausted"}:
                raise RoleInfrastructureError("BRS did not reach its transition: " + phase1.status + ": " + phase1.reason)
            initial, brs_record = self.freeze(phase1.memory_dir, self.rsi_dir / "phase1/memory_frozen")
            result.update(phase="phase2", brs_memory=brs_record)
            write_json(self.rsi_dir / "result.json", result)
            self.event("PHASE2_STARTED", brs_memory_sha256=brs_record["sha256"])
            memory, frozen = await runtime.run_engine(self.phase2, runtime, instruction, initial)
            if _memory_tree_sha256(_read_memory_tree(brs_record["path"])) != brs_record["sha256"]:
                raise RoleInfrastructureError("Frozen BRS memory drifted")
            result.update(phase="phase3", frozen_memory=frozen)
            write_json(self.rsi_dir / "result.json", result)
            self.event("PHASE3_STARTED", frozen_memory_sha256=frozen["sha256"])
            # The Pier target container has never been given to a learning role.
            # Only memory crosses into this clean final target attempt.
            vm = CodingVM(runtime)
            vm.environment = environment
            if not await asyncio.to_thread(runtime.hooks.push_memory, vm, frozen["path"]):
                raise RoleInfrastructureError("Frozen evaluation memory upload failed")
            agent = self.baseline(self.logs_dir, memory, training=False)
            agent.baseline_dir = self.rsi_dir / "phase3"
            self.final_agent = agent
            await agent.run(instruction, environment, context)
            if _memory_tree_sha256(_read_memory_tree(frozen["path"])) != frozen["sha256"]:
                raise RoleInfrastructureError("Phase3 changed frozen host memory")
            result.update(status="completed", phase3_verdict=agent.summary["verdict"],
                phase3_stop_reason=agent.summary["stop_reason"], frozen_memory_unchanged=True)
            self.event("PHASE3_COMPLETED", verdict=agent.summary["verdict"])
        except BaseException as error:
            result.update(status="cancelled" if isinstance(error, asyncio.CancelledError)
                else "infrastructure_error", error_type=type(error).__name__,
                error=runtime.checkpoints.clean(error),
                traceback=runtime.checkpoints.clean(traceback.format_exc()))
            raise
        finally:
            primary = sys.exception()
            try:
                await runtime.close()
            except Exception as error:
                result['cleanup_error'] = runtime.checkpoints.clean(traceback.format_exc())
                if primary is None:
                    result.update(status='infrastructure_error',
                        error_type=type(error).__name__, error=runtime.checkpoints.clean(error))
                    raise
                primary.add_note('RSI cleanup also failed; see rsi/result.json')
            finally:
                write_json(self.rsi_dir / "result.json", result)

    def populate_context_post_run(self, context):
        if self.final_agent is not None:
            self.final_agent.populate_context_post_run(context)
        context.metadata = context.metadata or {}
        context.metadata["rsi"] = {"result_path": "rsi/result.json", "parallel": self.parallel,
            "official_evaluation_scope": "phase3 only", "training_usage": "rsi/runtime and phase1/phase2 native logs"}
        # Phase3 usage alone is not total RSI usage. Do not report it as such.
        context.n_input_tokens = context.n_output_tokens = context.n_cache_tokens = None
        context.cost_usd = None
