"""Read-only eligibility checks and new-job recovery at a committed BRS wave.

No guest disk is required at this boundary: the next wave intentionally starts
in a fresh container. Only its owning Curriculum receives the native context.
Partial-wave/DRS recovery is deliberately rejected, never silently replayed.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import stat
import uuid
from pathlib import Path

from explore.phase1_boundary_recovery import validate_boundary
from explore.practice_loop import PracticeInfrastructureError
from explore.provisioning import _manifest_from_tgz, read_manifest
from .rsi_checkpoint import archive_info, atomic_json


def require(condition, message):
    if not condition:
        raise PracticeInfrastructureError("RSI resume: " + message)


def read(path):
    return json.loads(Path(path).read_text())


def regular_tree(root):
    require(root.is_dir() and not root.is_symlink(), "missing/linked directory: " + str(root))
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        require(stat.S_ISREG(mode) or stat.S_ISDIR(mode), "non-regular artifact: " + str(path))


def curriculum_checkpoint(transcript, private):
    """Select the last accepted handoff, not a later failed launch checkpoint."""
    transcript = Path(transcript)
    trace = read(transcript)
    identity = str(uuid.UUID(trace["bitfun_session_id"]))
    require(trace.get("completion") == "handoff", "Curriculum handoff was not accepted")
    outcomes = [read(p) for p in transcript.parent.glob("outcome-*.json")]
    matches = [o for o in outcomes if o.get("status") == "handoff"
               and o.get("checkpoint_status") == "ready"
               and o.get("session_id") == identity]
    require(bool(matches), "missing committed Curriculum handoff")
    outcome = max(matches, key=lambda o: o["call"])
    record = outcome["checkpoint"]
    require(record.get("session_id") == identity and record.get("role") == "curriculum"
            and record.get("working_dir") == "/home/user"
            and type(record.get("call")) is int and record["call"] == outcome["call"]
            and record["call"] > 0, "Curriculum checkpoint identity/role/call mismatch")
    name = record["archive"]
    require(Path(name).name == name and name.endswith(".tgz"), "invalid archive path")
    require(Path(trace.get("native_archive", "")).name == name,
            "Curriculum transcript and checkpoint disagree")
    path = private / name
    require(path.is_file() and not path.is_symlink(), "Curriculum archive missing or linked")
    info = archive_info(path, identity)
    require(all(record.get(k) == v for k, v in info.items()), "Curriculum archive integrity mismatch")
    return record


def launch_diagnostics(root, outcome):
    """Legacy runs stored the launch exception only in the environment journal."""
    identity = str(uuid.UUID(outcome["session_id"]))
    call = outcome["call"]
    require(type(call) is int and call > 0, "invalid failed invocation number")
    paths = list((root / "runtime/environments").glob(
        f"*/agent/checkpoint-{identity}-{call:03d}.json"))
    for path in paths:
        require(path.is_file() and not path.is_symlink(), "invalid launch diagnostic")
    records = [outcome, *[read(path) for path in paths]]
    # Zero observed events alone cannot prove that the model never started.
    # E2BIG at exec is a positive pre-launch failure, safe to repeat in a new VM.
    require(any(r.get("session_id") == identity and r.get("call") == call
        and r.get("status") == "execution_error"
        and "[Errno 7] Argument list too long" in str(r.get("error", ""))
        and "'docker'" in str(r.get("error", "")) for r in records),
        "no proven pre-launch E2BIG failure; zero usage alone is insufficient")
    return paths


def validate_coding_boundary(lineage, target, **kwargs):
    lineage = Path(lineage)
    selected = {}

    def history(transcript):
        selected.update(curriculum_checkpoint(transcript, lineage.parent / "runtime/private-sessions"))
        return [{"role": "user", "content": "Native BitFun session continuity"},
                {"role": "assistant", "content": "Native session archive is authoritative",
                 "bitfun_session_id": selected["session_id"]}]

    plan = validate_boundary(lineage, target, **kwargs, history_loader=history,
                             events_path=lineage.parent / "events.jsonl", require_frozen=False)
    require(plan["projects"] > 0, "no completed wave to resume")
    diagnostics = []
    # A partially executed wave needs its own recovery protocol. In this first
    # supported case the next invocation failed before any native work began.
    for path in plan["pending"]:
        require(path.parent.name == "curriculum", "partial wave/project recovery is unsupported")
        outcomes = list(path.rglob("outcome-*.json"))
        require(bool(outcomes), "pending Curriculum has no diagnostic outcome")
        for f in outcomes:
            o = read(f)
            require(o.get("session_id") == selected["session_id"]
                    and o.get("status") == "execution_error"
                    and o.get("metrics", {}).get("model_calls") == 0
                    and o.get("metrics", {}).get("completed_tools") == 0,
                    "pending Curriculum already executed; cannot discard its work")
            diagnostics.extend(launch_diagnostics(lineage.parent, o))
        require(not list(path.rglob("stream-*.jsonl")), "pending native events require separate recovery")
    for index in range(1, plan["projects"] + 1):
        episode = lineage / "episodes" / f"ep{index:03d}"
        for directory in (episode / "fixtures", episode / "candidates/cycle_001"):
            manifest = read_manifest(str(directory))
            require(bool(manifest) and _manifest_from_tgz((directory / "materials.tgz").read_bytes()) == manifest,
                    "project archive mismatch: " + str(directory))
    plan["checkpoint"] = selected
    plan["diagnostics"] = diagnostics
    return plan


def inspect_resume(trial, *, owner=None, instruction=None):
    """No model calls, containers, writes, or source modifications."""
    trial = Path(trial).resolve()
    root = trial / "rsi"
    result = read(root / "result.json")
    require(result.get("status") == "infrastructure_error" and result.get("phase") == "phase1",
            "only a stopped BRS infrastructure failure can resume")
    trial_result = read(trial / "result.json")
    require(trial_result.get("finished_at"), "source controller has not finished")
    regular_tree(root / "phase1")
    regular_tree(root / "runtime/private-sessions")
    config = read(trial / "config.json")
    from pier.models.task.task import Task
    task = Task(config["task"]["path"])
    require(trial_result.get("task_checksum") == task.checksum,
            "task files/base environment differ from the saved trial")
    require(instruction is None or instruction == task.instruction, "target instruction changed")
    protocol = read(root / "protocol.json")
    if owner is not None:
        expected = {"parallel": owner.parallel, "brs_project_budget": owner.brs_project_budget,
            "phase3_work_sec": owner.total_timeout_sec, "learning_role_work_sec": owner.learning_timeout_sec,
            "training_target_work_sec": owner.target_learning_timeout_sec, "roles": owner.role_configs()}
        require(all(protocol.get(k) == v for k, v in expected.items()), "saved roles/budgets differ from requested run")
        require(protocol.get("reference_context_policy", False) == owner.reference_context_policy,
                "context policy differs from source run")
    plan = validate_coding_boundary(root / "phase1", task.instruction,
        project_budget=protocol["brs_project_budget"], checkpoint_projects=(0, 4, 8),
        max_parallel=protocol["parallel"], target_query_conditioned=True)
    plan.update(source_trial=str(trial), target_sha256=hashlib.sha256(task.instruction.encode()).hexdigest())
    return plan


def resume_summary(plan):
    return {"eligible": True, "source_trial": plan["source_trial"],
        "completed_wave": plan["wave"], "committed_projects": plan["projects"],
        "next_wave": plan["wave"] + 1, "memory_files": len(plan["state"]["memory_manifest"]),
        "memory_bytes": sum(x["bytes"] for x in plan["state"]["memory_manifest"].values()),
        "curriculum_session_id": plan["checkpoint"]["session_id"],
        "curriculum_checkpoint_call": plan["checkpoint"]["call"],
        "source_unchanged": True, "requires_original_container": False}


def prepare_resume(trial, destination, *, owner, instruction):
    plan = inspect_resume(trial, owner=owner, instruction=instruction)
    source = Path(plan["source_trial"]) / "rsi"
    destination = Path(destination)
    require(not (destination / "phase1").exists() and not (destination / "runtime").exists(),
            "resume destination already contains work")
    shutil.copytree(source / "phase1", destination / "phase1")
    shutil.copytree(source / "runtime/private-sessions", destination / "runtime/private-sessions")
    for diagnostic in plan["diagnostics"]:
        target = destination / diagnostic.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(diagnostic, target)
    shutil.copy2(source / "events.jsonl", destination / "events.jsonl")
    record = plan["checkpoint"]
    private = destination / "runtime/private-sessions"
    atomic_json(private / (record["session_id"] + ".json"), record)
    # Keep the failed launch's diagnostics in the original trial / archived wave,
    # and do not advertise its later call number as the selected checkpoint.
    (private / (record["session_id"] + ".recovery.json")).unlink(missing_ok=True)
    copied = validate_coding_boundary(destination / "phase1", instruction,
        project_budget=owner.brs_project_budget, checkpoint_projects=(0, 4, 8),
        max_parallel=owner.parallel, target_query_conditioned=True)
    require(copied["state"] == plan["state"] and copied["checkpoint"] == record,
            "source changed while copying")
    receipt = {**resume_summary(plan), "target_sha256": plan["target_sha256"],
        "source_protocol": read(source / "protocol.json")}
    atomic_json(destination / "resume.json", receipt)
    return receipt


def load_resume_job(trial, name):
    """Use the recorded experiment configuration, not today's default YAML."""
    require(bool(name), "a new --name is required")
    trial = Path(trial).resolve()
    inspect_resume(trial)
    from run_deepswe import load_job
    config = load_job(trial.parent / "config.json", name=name, mode="rsi")
    require(len(config.agents) == 1 and config.n_attempts == 1, "resume needs one agent/attempt")
    task_path = Path(read(trial / "config.json")["task"]["path"]).resolve()
    require(len(config.datasets) == 1 and not config.tasks
            and config.datasets[0].path.resolve() == task_path.parent, "unsupported task selection")
    config.datasets[0].task_names = [task_path.name]
    config.datasets[0].exclude_task_names = None
    config.datasets[0].n_tasks = None
    config.agents[0].kwargs["resume_from"] = str(trial)
    require(not (config.jobs_dir / name).exists(), "use a new job name")
    return config
