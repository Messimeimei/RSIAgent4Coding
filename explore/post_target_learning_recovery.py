"""Strict recovery after committed target PASS learning.

Neither admitted boundary replays the completed target, Verifier, or same-Actor
learning transaction. The legacy boundary covers a VM-ready timeout before an
evolution cycle exists. The zero-project boundary covers an allowlisted ebtables
initialization failure after the trigger was durably imported into ``cycle_001``;
that failed cycle remains immutable evidence and recovered work starts in
``cycle_002``.
"""
from pathlib import Path
import dataclasses
import hashlib
import json

from explore.practice_loop import _manifest, _read_memory_tree
from explore.phase2_recovery import read, require, sha
from explore.target_learning import _memory_tree_sha256


LEGACY_BOOT_KIND = "post_target_learning_boot_failure"
ZERO_PROJECT_INFRA_KIND = "post_target_learning_zero_project_infra"
_EBTABLES_FAILURE_PREFIX = (
    "fresh VM boundary failed:",
    "null pre-seal boundary failed:",
    "outer-container firewall verification failed",
)
_EBTABLES_FAILURE_VARIANTS = (
    ("ebtables:", "No chain/target/match by that name"),
    ("The kernel doesn't support the ebtables 'filter' table.",),
)


def _is_allowlisted_ebtables_failure(reason):
    return (
        all(fragment in reason for fragment in _EBTABLES_FAILURE_PREFIX)
        and any(all(fragment in reason for fragment in variant)
                for variant in _EBTABLES_FAILURE_VARIANTS)
    )


def _resolved_hashes(plan):
    values = plan.get("input_sha256")
    require(isinstance(values, dict), "input hashes are missing")
    resolved = {}
    for raw, digest in values.items():
        require(isinstance(raw, str) and isinstance(digest, str),
                "invalid input hash entry")
        path = Path(raw).resolve(strict=True)
        require(sha(path) == digest, "archived input changed: " + raw)
        resolved[str(path)] = digest
    return resolved


def _require_hashed(paths, hashes):
    for path in paths:
        resolved = str(Path(path).resolve(strict=True))
        require(resolved in hashes,
                "recovery plan does not bind input: " + resolved)


def _artifact_tree_sha256(root):
    """Hash file bytes and directory layout for immutable failure evidence."""
    root = Path(root).resolve(strict=True)
    rows = []
    for path in sorted(root.rglob("*"), key=lambda value:
                       value.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        require(not path.is_symlink(), "artifact tree contains a symlink")
        if path.is_dir():
            rows.append(f"d\t{relative}")
        elif path.is_file():
            rows.append(f"f\t{relative}\t{path.stat().st_size}\t{sha(path)}")
        else:
            require(False, "artifact tree contains a non-file entry")
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def _target_trigger(target, learning):
    return {
        "schema_version": 1,
        "kind": "target_verifier_learning_outcome",
        "target": target,
        "verifier_report": learning["verifier_report"],
        "verifier_outcome": "PASS",
        "trigger_authority": "phase2_outcome_protocol",
        "curriculum_routing_report": None,
        "actor_learning_diagnosis": learning["actor_learning_diagnosis"],
        "memory_before": learning["memory_after"],
    }


def _target_trigger_text(target, learning):
    return (
        "ORIGINAL NORTH-STAR TARGET (verbatim):\n" + target
        + "\n\nPERSISTENT TARGET VERIFIER AGENT REPORT (verbatim):\n"
        + learning["verifier_report"]
        + "\n\nLIFECYCLE TRANSITION:\nThe declared self-evolving benchmark "
        "protocol entered learning after the target Verifier Agent PASS. No "
        "Curriculum Agent routing decision was requested or fabricated."
        + "\n\nSAME TARGET ACTOR AGENT LEARNING DIAGNOSIS (verbatim):\n"
        + learning["actor_learning_diagnosis"]
    )


def _common_target_learning_boundary(
        plan_path, root, configs, initial_memory, target, stop_policy):
    plan_path, root = Path(plan_path), Path(root)
    plan = read(plan_path)
    require(plan.get("status") == "admitted_for_isolated_recovery",
            "plan is not admitted")
    require(root.resolve() == Path(plan["attempt_root"]).resolve(), "root changed")
    require(stop_policy == "curriculum_review", "stop policy changed")
    require(plan.get("completed_projects") == 0
            and plan.get("completed_curriculum_turns") == 0,
            "Curriculum or practice work already exists")
    require(plan.get("prior_curriculum_iters") == 0
            and plan.get("prior_curriculum_wall_secs") == 0,
            "unexpected prior Curriculum budget")
    hashes = _resolved_hashes(plan)
    for marker in ("result.json", "memory_frozen", "curriculum_terminal.md",
                   "audit_rejects.jsonl"):
        require(not (root / marker).exists(),
                "boundary already advanced: " + marker)
    require(sorted(path.name for path in (root / "target_cycles").iterdir()) ==
            ["cycle_001"], "target cycle count changed")
    for path in root.rglob("boundary_audit.json"):
        audit = read(path)
        require(audit.get("status") != "quarantined" and not audit.get("hits"),
                "quarantined transcript")

    manifest_path = root / "manifest.json"
    manifest = read(manifest_path)
    require(manifest["target_direction_sha256"] == hashlib.sha256(
        target.encode()).hexdigest(), "target direction changed")
    require(manifest["initial_memory"]["manifest"] == _manifest(initial_memory),
            "initial memory changed")
    protocol = manifest["protocol"]
    access = plan["curriculum_memory_access"]
    require(access in ("none", "read_only")
            and protocol["curriculum_memory_access"] == access
            and protocol["stop_policy"] == stop_policy
            and protocol["official_evaluator_calls"] == 0
            and not protocol["official_evaluator_feedback_enters_learning"],
            "protocol boundary changed")
    require(not list(root.rglob("grading.json")),
            "official grading entered Phase2")
    for role, cfg in configs.items():
        saved = manifest["configs"][role]
        require(saved["sha256"] == sha(saved["path"]),
                "role config bytes changed")
        effective = json.loads(json.dumps(dataclasses.asdict(cfg)))
        require(saved["effective"] == effective,
                "effective role config changed")

    cycle = root / "target_cycles/cycle_001"
    learning_root = cycle / "terminal_learning"
    learning_path = learning_root / "outcome.json"
    learning = read(learning_path)
    require(learning["target_cycle"] == 1
            and learning["target_verifier_verdict"] == "PASS",
            "completed target PASS learning required")
    require(learning["memory_before"] == _manifest(initial_memory),
            "memory chain changed")
    memory = _read_memory_tree(str(root / "active_memory"))
    require(learning["memory_after"] == _manifest(memory),
            "committed memory changed")

    verifier_path = Path(plan["target_verification"])
    require(verifier_path.resolve(strict=True).is_relative_to(cycle.resolve()),
            "verdict from another target")
    verifier = read(verifier_path)
    require(verifier["verdict"] == "pass"
            and verifier["findings"] == learning["verifier_report"],
            "target verdict and learning disagree")
    history_paths = [
        cycle / "transcript.json",
        learning_root / "memory_distillation/segment_000/transcript.json",
        learning_root / "memory_reconciliation/segment_000/transcript.json",
        learning_root / "learning_diagnosis/attempt_000/segment_000/transcript.json",
    ]
    histories = [read(path) for path in history_paths]
    require(all(a["system"] == b["system"]
                and b["messages"][:len(a["messages"])] == a["messages"]
                for a, b in zip(histories, histories[1:])),
            "same-Actor history prefix lost")
    require(len(histories[0]["messages"]) ==
            learning["active_history_messages_before"]
            and len(histories[-1]["messages"]) ==
            learning["active_history_messages_after"],
            "learning history counters changed")
    require(all(history["messages"] and len(history["messages"]) % 2 == 0
                and all(message.get("role") ==
                        ("user" if index % 2 == 0 else "assistant")
                        for index, message in enumerate(history["messages"]))
                for history in histories), "incomplete Actor context")
    diagnosis_path = learning_root / "learning_diagnosis.md"
    require(diagnosis_path.read_text() == learning["actor_learning_diagnosis"],
            "diagnosis changed")
    return {
        "plan_path": plan_path,
        "root": root,
        "plan": plan,
        "cycle": cycle,
        "learning_root": learning_root,
        "learning_path": learning_path,
        "learning": learning,
        "memory": memory,
        "access": access,
        "history_paths": history_paths,
        "diagnosis_path": diagnosis_path,
        "verifier_path": verifier_path,
        "manifest_path": manifest_path,
        "hashes": hashes,
    }


def build_zero_project_infra_plan(
        root, curriculum_memory_access, failed_runtime_log):
    """Return a secret-free candidate plan without changing the attempt."""
    require(curriculum_memory_access in ("none", "read_only"),
            "unknown Curriculum memory access mode")
    root = Path(root).resolve(strict=True)
    failed_runtime_log = Path(failed_runtime_log).resolve(strict=True)
    failed_cycle = (root / "evolution_cycles/cycle_001").resolve(strict=True)
    failed_tree_sha256 = _artifact_tree_sha256(failed_cycle)
    verdicts = sorted((root / "target_cycles/cycle_001").rglob("verify2.json"))
    passing = [path for path in verdicts if read(path).get("verdict") == "pass"]
    require(len(passing) == 1, "expected one persisted target PASS verdict")
    learning_root = root / "target_cycles/cycle_001/terminal_learning"
    critical = [
        root / "manifest.json",
        root / "events.jsonl",
        learning_root / "outcome.json",
        learning_root / "learning_diagnosis.md",
        root / "target_cycles/cycle_001/transcript.json",
        learning_root / "memory_distillation/segment_000/transcript.json",
        learning_root / "memory_reconciliation/segment_000/transcript.json",
        learning_root / "learning_diagnosis/attempt_000/segment_000/transcript.json",
        passing[0],
        failed_cycle / "state.json",
        failed_cycle / "trigger/outcome.json",
        failed_cycle / "trigger/outcome.md",
        failed_runtime_log,
        *(path for path in (root / "active_memory").rglob("*") if path.is_file()),
        *(path for path in (failed_cycle / "memory").rglob("*") if path.is_file()),
    ]
    return {
        "schema_version": 1,
        "recovery_kind": ZERO_PROJECT_INFRA_KIND,
        "status": "admitted_for_isolated_recovery",
        "attempt_root": str(root),
        "completed_projects": 0,
        "completed_curriculum_turns": 0,
        "prior_curriculum_iters": 0,
        "prior_curriculum_wall_secs": 0,
        "curriculum_memory_access": curriculum_memory_access,
        "target_verification": str(passing[0].resolve()),
        "failed_evolution_cycle": str(failed_cycle),
        "failed_evolution_tree_sha256": failed_tree_sha256,
        "failed_runtime_log": str(failed_runtime_log),
        "input_sha256": {
            str(path.resolve(strict=True)): sha(path) for path in critical
        },
    }


def admit_post_target_learning(cls, plan_path, root, configs, initial_memory,
                               target, stop_policy):
    boundary = _common_target_learning_boundary(
        plan_path, root, configs, initial_memory, target, stop_policy)
    plan = boundary["plan"]
    root = boundary["root"]
    learning = boundary["learning"]
    memory = boundary["memory"]
    require(plan.get("recovery_kind") == LEGACY_BOOT_KIND,
            "wrong post-target recovery kind")
    require(not (root / "evolution_cycles").exists(),
            "boundary already advanced: evolution_cycles")
    events = [json.loads(line) for line in
              (root / "events.jsonl").read_text().splitlines()]
    require(len(events) >= 2
            and events[-2]["event"] == "TARGET_LEARNING_COMPLETED"
            and events[-2]["payload"]["target_cycle"] == 1
            and events[-2]["payload"]["verdict"] == "PASS"
            and events[-1]["event"] == "PASS_NEXT_EXPERIENCE_REVIEW_STARTED"
            and events[-1]["payload"]["target_cycle"] == 1
            and events[-1]["payload"]["prospective_evolution"] == 1,
            "not the pre-Curriculum boundary")
    require("TimeoutError: VM failed to become ready within timeout period" in
            Path(plan["failed_runtime_log"]).read_text(),
            "VM boot fault is not recorded")
    trigger = dict(
        target=target,
        trigger_authority="phase2_outcome_protocol",
        verifier_outcome="PASS",
        verifier_report=learning["verifier_report"],
        actor_learning_diagnosis=learning["actor_learning_diagnosis"],
        memory_before=learning["memory_after"],
    )
    curriculum = configs["curriculum"]
    return cls(
        boundary["plan_path"], root, root / "evolution_cycles/cycle_001",
        target, learning, trigger, {}, [], "", "", curriculum.max_iters,
        curriculum.wall_clock_secs, {}, "", projects=0, project_records=[],
        completed_curriculum_turns=0, initial_evolution_memory=memory,
        curriculum_memory_access=boundary["access"],
        post_target_learning=True, recovery_kind=LEGACY_BOOT_KIND,
        recovery_plan_sha256=sha(boundary["plan_path"]))


def admit_zero_project_infra(cls, plan_path, root, configs, initial_memory,
                             target, stop_policy):
    """Admit a committed PASS followed by an allowlisted zero-project failure."""
    boundary = _common_target_learning_boundary(
        plan_path, root, configs, initial_memory, target, stop_policy)
    plan = boundary["plan"]
    root = boundary["root"]
    learning = boundary["learning"]
    memory = boundary["memory"]
    require(plan.get("schema_version") == 1, "unsupported recovery-plan schema")
    require(plan.get("recovery_kind") == ZERO_PROJECT_INFRA_KIND,
            "wrong zero-project infrastructure recovery kind")

    evolution_root = root / "evolution_cycles"
    require(evolution_root.is_dir(), "failed evolution root is missing")
    require(sorted(path.name for path in evolution_root.iterdir()) == ["cycle_001"],
            "failed evolution cycle set changed")
    failed_cycle = Path(plan.get("failed_evolution_cycle", ""))
    require(failed_cycle.resolve() == (evolution_root / "cycle_001").resolve(),
            "failed evolution cycle changed")
    expected_entries = {
        "curriculum", "episodes", "memory", "memory_journal",
        "state.json", "trigger",
    }
    require({path.name for path in failed_cycle.iterdir()} == expected_entries,
            "failed evolution boundary contains unexpected artifacts")
    failed_tree_sha256 = _artifact_tree_sha256(failed_cycle)
    require(plan.get("failed_evolution_tree_sha256") == failed_tree_sha256,
            "failed evolution evidence tree changed")
    for name in ("curriculum", "episodes", "memory_journal"):
        directory = failed_cycle / name
        require(directory.is_dir() and not any(directory.iterdir()),
                f"{name} contains Curriculum or practice work")
    require(sorted(path.name for path in (failed_cycle / "trigger").iterdir()) ==
            ["outcome.json", "outcome.md"], "trigger evidence changed")

    cycle_memory = _read_memory_tree(str(failed_cycle / "memory"))
    require(cycle_memory == memory, "failed evolution memory changed")
    state_path = failed_cycle / "state.json"
    state = read(state_path)
    expected_tree = _memory_tree_sha256(memory)
    require(state.get("schema_version") == 1
            and state.get("kind") == "unified_evolution_cycle"
            and state.get("status") == "infra"
            and state.get("projects") == 0
            and state.get("learning_experiences") == 0
            and state.get("terminal_text") == ""
            and state.get("target_sha256") == hashlib.sha256(
                target.encode()).hexdigest()
            and state.get("trigger_report_sha256") == hashlib.sha256(
                learning["verifier_report"].encode()).hexdigest()
            and state.get("trigger_authority") == "phase2_outcome_protocol"
            and state.get("triggering_outcome") == "PASS"
            and state.get("curriculum_route_sha256") == hashlib.sha256(b"").hexdigest()
            and state.get("memory_manifest") == _manifest(memory)
            and state.get("memory_tree_sha256") == expected_tree,
            "failed evolution state is not the exact zero-project boundary")
    reason = str(state.get("reason", ""))
    require(_is_allowlisted_ebtables_failure(reason),
            "infrastructure fault is not an allowlisted ebtables failure")
    failed_runtime_log = Path(plan.get("failed_runtime_log", ""))
    log_text = failed_runtime_log.read_text()
    require(reason.strip() in log_text,
            "failed evolution reason is absent from the runtime log")

    trigger_path = failed_cycle / "trigger/outcome.json"
    trigger_text_path = failed_cycle / "trigger/outcome.md"
    trigger = read(trigger_path)
    require(trigger == _target_trigger(target, learning),
            "imported trigger differs from committed target learning")
    require(trigger_text_path.read_text() == _target_trigger_text(target, learning),
            "imported trigger narrative changed")

    events_path = root / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    require(len(events) >= 3, "post-target event boundary is incomplete")
    learned_event, review_event, imported_event = events[-3:]
    require(learned_event.get("event") == "TARGET_LEARNING_COMPLETED"
            and learned_event.get("payload", {}).get("target_cycle") == 1
            and learned_event.get("payload", {}).get("verdict") == "PASS"
            and review_event.get("event") == "PASS_NEXT_EXPERIENCE_REVIEW_STARTED"
            and review_event.get("payload", {}).get("target_cycle") == 1
            and review_event.get("payload", {}).get("prospective_evolution") == 1,
            "target PASS review boundary changed")
    imported = imported_event.get("payload", {})
    require(imported_event.get("event") == "EVOLUTION_EVOLVE_TRIGGER_IMPORTED"
            and imported.get("status") == "completed"
            and imported.get("state") == {
                "project_open": False,
                "memory_phase_open": False,
                "recovery_open": False,
            }
            and imported.get("payload") == {
                "target_sha256": hashlib.sha256(target.encode()).hexdigest(),
                "trigger_report_sha256": hashlib.sha256(
                    learning["verifier_report"].encode()).hexdigest(),
                "trigger_diagnosis_sha256": hashlib.sha256(
                    learning["actor_learning_diagnosis"].encode()).hexdigest(),
                "trigger_authority": "phase2_outcome_protocol",
                "triggering_outcome": "PASS",
                "curriculum_route_sha256": hashlib.sha256(b"").hexdigest(),
                "memory_tree_sha256": expected_tree,
            }, "imported trigger event changed")

    required_inputs = [
        boundary["manifest_path"], events_path, boundary["learning_path"],
        boundary["diagnosis_path"], boundary["verifier_path"],
        state_path, trigger_path, trigger_text_path, failed_runtime_log,
        *boundary["history_paths"],
        *(path for path in (root / "active_memory").rglob("*") if path.is_file()),
        *(path for path in (failed_cycle / "memory").rglob("*") if path.is_file()),
    ]
    _require_hashed(required_inputs, boundary["hashes"])

    curriculum = configs["curriculum"]
    return cls(
        boundary["plan_path"], root, root / "evolution_cycles/cycle_002",
        target, learning, trigger, {}, [], "",
        _target_trigger_text(target, learning), curriculum.max_iters,
        curriculum.wall_clock_secs, {}, "", projects=0, project_records=[],
        completed_curriculum_turns=0, initial_evolution_memory=memory,
        curriculum_memory_access=boundary["access"],
        post_target_learning=True, recovery_kind=ZERO_PROJECT_INFRA_KIND,
        evolution_artifact_offset=1,
        failed_evolution_cycle=str(failed_cycle.resolve()),
        failed_evolution_tree_sha256=failed_tree_sha256,
        failed_runtime_log=str(failed_runtime_log.resolve()),
        failed_runtime_log_sha256=sha(failed_runtime_log),
        recovery_plan_sha256=sha(boundary["plan_path"]))


def continue_after_target_learning(recovery, direction, hooks):
    """Call Curriculum once without replaying the committed target transaction."""
    from core.self_evolving_loop import (
        EvolutionResult, EvolutionStatus, SelfEvolvingLoopResult,
        SelfEvolvingStart, TargetVerdict, run_self_evolving_loop,
    )
    require(recovery.post_target_learning
            and (recovery.projects == 0
                 or recovery.resume_evolution_cycle)
            and recovery.trigger["verifier_outcome"] == "PASS",
            "wrong recovery boundary")
    trigger = recovery.trigger
    evolved = hooks.evolve(
        direction, TargetVerdict.PASS, trigger["verifier_report"],
        trigger["actor_learning_diagnosis"], recovery.initial_evolution_memory, 1)
    require(isinstance(evolved, EvolutionResult), "invalid evolution result")
    if evolved.projects == 0:
        termination = (
            "verifier_pass_curriculum_ready"
            if evolved.status is EvolutionStatus.READY_FOR_RETRY
            else "verifier_pass_curriculum_stalled")
        require(evolved.memory == recovery.initial_evolution_memory,
                "zero-practice Curriculum changed Actor memory")
        hooks.on_event("PASS_NEXT_EXPERIENCE_REVIEW_COMPLETED", {
            "target_cycle": 1,
            "projects": 0,
            "reason": evolved.reason,
            "termination": termination,
        })
        hooks.on_event("TARGET_LOOP_TERMINATED", {
            "target_cycle": 1,
            "verdict": "PASS",
            "termination": termination,
            "evolutions": 0,
            "recovered_after_target_learning": True,
        })
        return SelfEvolvingLoopResult(
            None, None, TargetVerdict.PASS, trigger["verifier_report"],
            evolved.memory, 1, 0, 0, termination)
    start = SelfEvolvingStart(
        target_cycles=1,
        evolutions=1,
        practice_projects=evolved.projects,
        final_after_stall=evolved.status is EvolutionStatus.STALLED,
    )
    hooks.on_event("RECOVERED_EVOLUTION_COMPLETED", {
        "target_cycle": 1,
        "evolution": 1,
        "projects": evolved.projects,
        "status": evolved.status.value,
        "recovery_plan": str(recovery.plan_path),
        "artifact_cycle": 1 + recovery.evolution_artifact_offset,
    })
    return run_self_evolving_loop(direction, evolved.memory, hooks, start=start)
