"""Strict recovery for an open practice whose Actor killed the guest controller.

The failed Actor action is never replayed. The same project, Actor conversation,
committed pre-project memory, and remaining role budget continue in a fresh VM.
No PASS/FAIL or learning transaction is fabricated for the interrupted practice.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import tarfile
from pathlib import Path

from explore import target_learning as V
from explore.phase2_recovery import read, require, sha
from explore.post_target_learning_recovery import _artifact_tree_sha256
from explore.practice_evidence_recovery import paired
from explore.practice_loop import (
    _UNIFIED_CURRICULUM_TOKEN,
    _manifest,
    _parse_handoff,
    _read_memory_tree,
    _safe_memory_name,
)


RECOVERY_KIND = "post_target_learning_actor_infra_practice"
RECOVERY_OBSERVATION = """HARNESS RECOVERY BOUNDARY:
Your last program was not replayed. It terminated the guest command controller,
which did not recover during the full mechanical recovery window, so the prior VM
was discarded. This is a fresh VM containing the same original project fixtures
and the same canonical pre-project memory. No candidate publication or semantic
verdict exists for this practice. Re-inspect the live state and recreate any
needed project changes before publishing your handoff. Your same private
conversation continues and all 15 prior Actor turns remain charged."""


def _files(root: Path):
    return [path for path in root.rglob("*") if path.is_file()]


def _load_journal(path: Path) -> dict[str, bytes]:
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        require(
            all(member.isfile() and _safe_memory_name(member.name)
                for member in members)
            and len({member.name for member in members}) == len(members),
            "unsafe target-learning journal",
        )
        return {
            member.name: archive.extractfile(member).read()
            for member in members
        }


def _heredoc(program: str, destination: str) -> str:
    marker = f"cat > {destination} <<'EOF'\n"
    require(program.count(marker) == 1,
            "Curriculum publication does not contain one literal notes heredoc")
    body = program.split(marker, 1)[1]
    require("\nEOF\n" in body,
            "Curriculum notes heredoc is unterminated")
    return body.split("\nEOF\n", 1)[0] + "\n"


def _find_curriculum_publication(turn: Path) -> Path:
    candidates = []
    for path in sorted(turn.glob("segment_*/iter_*/program.*")):
        text = path.read_text(errors="strict")
        if ("/home/user/project_next.md" in text
                and "/home/user/curriculum_notes.md" in text):
            candidates.append(path)
    require(len(candidates) == 1,
            "expected one completed Curriculum project publication")
    return candidates[0]


def build_actor_infra_recovery_plan(root, curriculum_memory_access):
    """Return a secret-free candidate plan without changing the lineage."""
    require(curriculum_memory_access in ("none", "read_only"),
            "unknown Curriculum memory access mode")
    root = Path(root).resolve(strict=True)
    cycle = (root / "evolution_cycles/cycle_002").resolve(strict=True)
    state = read(cycle / "state.json")
    pending = int(state.get("projects", 0))
    completed = int(state.get("learning_experiences", -1))
    require(pending == completed + 1 and completed >= 1,
            "not an open post-learning practice boundary")
    episode = cycle / f"episodes/ep{pending:03d}"
    actor_segment = episode / "actor/attempt_001/segment_000"
    actor_result = read(actor_segment / "result.json")
    curriculum_turn = cycle / f"curriculum/turn_{pending:03d}"
    publication = _find_curriculum_publication(curriculum_turn)
    prior_plan = root / "recovery_plans/phase2_after_target_pass_ebtables.json"
    prior = read(prior_plan)
    target_verification = Path(prior["target_verification"]).resolve(strict=True)
    critical = [
        root / "manifest.json",
        root / "events.jsonl",
        root / "target_cycles/cycle_001/terminal_learning/outcome.json",
        root / "target_cycles/cycle_001/terminal_learning/learning_diagnosis.md",
        target_verification,
        root / "memory_journal/ep001.tgz",
        prior_plan,
        cycle / "state.json",
        cycle / "trigger/outcome.json",
        cycle / "trigger/outcome.md",
        cycle / "episodes/ep001/outcome.json",
        cycle / "episodes/ep001/outcome.md",
        episode / "project.md",
        publication,
        publication.parent / "trace.txt",
        publication.parent / "trace_meta.json",
        curriculum_turn / "segment_000/transcript.json",
        actor_segment / "result.json",
        actor_segment / "transcript.json",
        *_files(episode / "fixtures"),
        *_files(actor_segment),
        *_files(root / "active_memory"),
        *_files(cycle / "memory"),
    ]
    unique = {str(path.resolve(strict=True)): path for path in critical}
    return {
        "schema_version": 1,
        "recovery_kind": RECOVERY_KIND,
        "status": "admitted_for_isolated_recovery",
        "attempt_root": str(root),
        "curriculum_memory_access": curriculum_memory_access,
        "completed_projects": completed,
        "completed_curriculum_turns": pending,
        "pending_project_index": pending,
        "source_transcript": str(
            curriculum_turn / "segment_000/transcript.json"),
        "curriculum_publication_program": str(publication),
        "actor_transcript": str(actor_segment / "transcript.json"),
        "prior_role_budget": {
            "prior_actor_iters": int(actor_result["iters"]),
            "prior_actor_wall_secs": float(actor_result["wall_secs"]),
            "prior_verifier_iters": 0,
            "prior_verifier_wall_secs": 0.0,
        },
        "prior_recovery_plan": str(prior_plan),
        "target_verification": str(target_verification),
        "failed_evolution_cycle": prior["failed_evolution_cycle"],
        "failed_evolution_tree_sha256": prior[
            "failed_evolution_tree_sha256"],
        "failed_runtime_log": prior["failed_runtime_log"],
        "failed_runtime_log_sha256": sha(prior["failed_runtime_log"]),
        "failed_practice_attempt": str(actor_segment),
        "failed_practice_attempt_tree_sha256": _artifact_tree_sha256(
            actor_segment),
        "input_sha256": {
            path: sha(source) for path, source in sorted(unique.items())
        },
    }


def admit_actor_infra_recovery(
        cls, plan_path, root, configs, initial_memory, target, stop_policy):
    """Admit the exact committed-practice/open-Actor infrastructure boundary."""
    plan_path, root = Path(plan_path), Path(root)
    plan = read(plan_path)
    require(plan.get("schema_version") == 1
            and plan.get("recovery_kind") == RECOVERY_KIND,
            "wrong Actor-infrastructure recovery kind")
    require(plan.get("status") == "admitted_for_isolated_recovery"
            and stop_policy == "curriculum_review",
            "recovery authorization/policy changed")
    require(root.resolve() == Path(plan["attempt_root"]).resolve(),
            "root changed")
    require(not (root / "result.json").exists(),
            "attempt already completed")
    for path, expected in plan["input_sha256"].items():
        require(sha(path) == expected,
                "archived Actor-infrastructure input changed: " + path)

    require(sorted(path.name for path in (root / "target_cycles").iterdir())
            == ["cycle_001"], "target cycle count changed")
    require(sorted(path.name for path in (root / "evolution_cycles").iterdir())
            == ["cycle_001", "cycle_002"],
            "evolution cycle count changed")
    prior_plan = read(plan["prior_recovery_plan"])
    failed_cycle = Path(plan["failed_evolution_cycle"]).resolve(strict=True)
    require(prior_plan.get("recovery_kind") ==
            "post_target_learning_zero_project_infra"
            and prior_plan.get("failed_evolution_tree_sha256") ==
            plan["failed_evolution_tree_sha256"]
            and _artifact_tree_sha256(failed_cycle) ==
            plan["failed_evolution_tree_sha256"],
            "prior zero-project recovery evidence changed")

    manifest = read(root / "manifest.json")
    require(manifest["target_direction_sha256"] == hashlib.sha256(
        target.encode()).hexdigest(), "target direction changed")
    require(manifest["initial_memory"]["manifest"] == _manifest(initial_memory),
            "initial frozen memory changed")
    require(manifest["protocol"]["curriculum_memory_access"] ==
            plan["curriculum_memory_access"],
            "Curriculum memory access changed")
    for role, cfg in configs.items():
        saved = manifest["configs"][role]
        require(saved["sha256"] == sha(saved["path"])
                and saved["effective"] ==
                json.loads(json.dumps(dataclasses.asdict(cfg))),
                "role configuration changed")

    learning = read(
        root / "target_cycles/cycle_001/terminal_learning/outcome.json")
    target_verification = Path(plan["target_verification"]).resolve(strict=True)
    require(target_verification.is_relative_to(
        (root / "target_cycles/cycle_001").resolve()),
        "target verdict came from another cycle")
    verification = read(target_verification)
    require(learning["target_cycle"] == 1
            and learning["target_verifier_verdict"] == "PASS"
            and verification["verdict"] == "pass"
            and verification["findings"] == learning["verifier_report"],
            "committed target PASS changed")
    require(learning["memory_before"] == _manifest(initial_memory),
            "target-learning input changed")
    trigger_memory = _load_journal(root / "memory_journal/ep001.tgz")
    require(learning["memory_after"] == _manifest(trigger_memory),
            "target-learning output changed")

    cycle = root / "evolution_cycles/cycle_002"
    trigger = read(cycle / "trigger/outcome.json")
    require(trigger["target"] == target
            and trigger["trigger_authority"] == "phase2_outcome_protocol"
            and trigger["verifier_outcome"] == "PASS"
            and trigger["verifier_report"] == learning["verifier_report"]
            and trigger["actor_learning_diagnosis"] ==
            learning["actor_learning_diagnosis"]
            and trigger["memory_before"] == learning["memory_after"],
            "evolution trigger changed")

    completed = int(plan["completed_projects"])
    pending = int(plan["pending_project_index"])
    require(completed == 1 and pending == 2
            and plan["completed_curriculum_turns"] == pending,
            "unsupported completed/open practice boundary")
    records = [read(cycle / f"episodes/ep{i:03d}/outcome.json")
               for i in range(1, completed + 1)]
    require(all(record["project_index"] == index
                and record["terminal_outcome"] in ("PASS", "FAIL")
                for index, record in enumerate(records, 1)),
            "completed practice lacks a grounded verdict")
    memory = _read_memory_tree(str(cycle / "memory"))
    active = _read_memory_tree(str(root / "active_memory"))
    require(records[0]["memory_before"] == learning["memory_after"]
            and records[-1]["memory_after"] == _manifest(memory)
            and active == memory,
            "committed practice memory chain changed")

    state = read(cycle / "state.json")
    require(state["status"] == "infra"
            and state["projects"] == pending
            and state["learning_experiences"] == completed
            and state["reason"] ==
            "ACTOR phase ended at emergency/infrastructure status infra"
            and state["memory_manifest"] == _manifest(memory)
            and state["memory_tree_sha256"] == V._memory_tree_sha256(memory),
            "not the recorded open Actor-infrastructure boundary")

    episode = cycle / f"episodes/ep{pending:03d}"
    require(not any((episode / name).exists() for name in (
        "outcome.json", "verifier_report.md", "candidate",
        "memory_distillation", "memory_reconciliation", "learning_diagnosis")),
        "open practice already reached publication, verification, or learning")
    project_text = (episode / "project.md").read_text()
    saved_project = _parse_handoff(project_text, _UNIFIED_CURRICULUM_TOKEN)
    require(saved_project is not None and saved_project.token == "PROJECT",
            "invalid saved project")

    source = Path(plan["source_transcript"])
    require(source == cycle / "curriculum/turn_002/segment_000/transcript.json",
            "not the committed Curriculum turn")
    curriculum_history = read(source)["messages"]
    actor_source = Path(plan["actor_transcript"])
    actor_history = read(actor_source)["messages"]
    require(paired(curriculum_history) and paired(actor_history),
            "incomplete role history")
    publication = Path(plan["curriculum_publication_program"])
    notes = _heredoc(publication.read_text(),
                     "/home/user/curriculum_notes.md")
    trace = publication.parent / "trace.txt"
    require(read(publication.parent / "trace_meta.json")["exit_code"] == 0
            and "DECISION: PROJECT" in trace.read_text(),
            "Curriculum project publication did not complete")

    actor_segment = Path(plan["failed_practice_attempt"])
    require(actor_segment == episode / "actor/attempt_001/segment_000"
            and _artifact_tree_sha256(actor_segment) ==
            plan["failed_practice_attempt_tree_sha256"],
            "failed Actor evidence changed")
    actor_result = read(actor_segment / "result.json")
    budget = plan["prior_role_budget"]
    require(actor_result["status"] == "infra"
            and actor_result["iters"] == budget["prior_actor_iters"] == 15
            and actor_result["wall_secs"] == budget["prior_actor_wall_secs"]
            and len(list(actor_segment.glob("iter_*/turn.txt"))) == 15,
            "failed Actor budget/evidence changed")
    final_trace = (actor_segment / "iter_15/trace.txt").read_text()
    final_meta = read(actor_segment / "iter_15/trace_meta.json")
    prior_program = (actor_segment / "iter_14/program.sh").read_text()
    require(final_meta["infra_fail"] is True
            and "controller did not recover within 180.0s" in final_trace
            and "kill -TERM \"$MAIN_PID\"" in prior_program
            and "/home/user/server/main.py" in prior_program,
            "Actor-caused controller termination is not recorded")
    events = [json.loads(line) for line in
              (root / "events.jsonl").read_text().splitlines()]
    require(events[-1]["event"] == "EVOLUTION_ACTOR_PHASE_STARTED"
            and events[-1]["payload"]["payload"]["project_index"] == pending,
            "a later lifecycle transition exists")

    for role in ("practice_actor", "practice_verifier"):
        cap = configs[role].max_iters
        used = budget["prior_actor_iters"] if role == "practice_actor" else 0
        require(0 <= used < cap, "remaining role budget exhausted")
    require(0 < budget["prior_actor_wall_secs"] <
            configs["practice_actor"].wall_clock_secs,
            "remaining Actor wall-clock budget exhausted")

    pending_state = dict(
        budget,
        resume_kind="actor_infra",
        actor_history=actor_history,
        verifier_history=[],
        findings="",
        publication=0,
        inspections=0,
        project=saved_project.body,
        recovery_observation=RECOVERY_OBSERVATION,
    )
    latest = (cycle / "episodes/ep001/outcome.md").read_text()
    return cls(
        plan_path, root, cycle, target, learning, trigger, records[-1],
        curriculum_history, notes, latest,
        configs["curriculum"].max_iters,
        configs["curriculum"].wall_clock_secs,
        {}, "", projects=completed, project_records=records,
        completed_curriculum_turns=pending,
        pending_practice=pending_state,
        initial_evolution_memory=trigger_memory,
        curriculum_memory_access=plan["curriculum_memory_access"],
        post_target_learning=True,
        resume_evolution_cycle=True,
        recovery_kind=RECOVERY_KIND,
        evolution_artifact_offset=1,
        failed_evolution_cycle=str(failed_cycle),
        failed_evolution_tree_sha256=plan["failed_evolution_tree_sha256"],
        failed_runtime_log=plan["failed_runtime_log"],
        failed_runtime_log_sha256=plan["failed_runtime_log_sha256"],
        failed_practice_attempt=str(actor_segment),
        failed_practice_attempt_tree_sha256=
        plan["failed_practice_attempt_tree_sha256"],
        recovery_plan_sha256=sha(plan_path),
    )
