"""Phase-2 exposes strict recovery without reusing failed artifact cycles."""
import hashlib
from pathlib import Path
from types import SimpleNamespace

from benchmarks.osworld.phase2 import (
    PHASE2_EXECUTE_ACK,
    _evolution_artifact_index,
    _initial_target_learning_updates,
    _parser,
    _recovery_result_record,
    _resolve_recovery_plan_path,
)
from explore.post_target_learning_recovery import _artifact_tree_sha256


def test_recovery_plan_is_an_explicit_cli_input():
    args = _parser().parse_args([
        "task_080",
        "--protocol-run", "recovery-test",
        "--initial-memory", "/tmp/frozen-memory",
        "--recovery-plan", "/tmp/recovery-plan.json",
        "--preflight",
    ])

    assert args.recovery_plan == "/tmp/recovery-plan.json"
    assert args.preflight is True


def test_candidate_plan_print_is_read_only_action():
    args = _parser().parse_args([
        "task_080",
        "--protocol-run", "recovery-test",
        "--initial-memory", "/tmp/frozen-memory",
        "--failed-runtime-log", "/tmp/phase2.log",
        "--print-zero-project-recovery-plan",
    ])

    assert args.print_zero_project_recovery_plan is True
    assert args.failed_runtime_log == "/tmp/phase2.log"
    assert not args.preflight
    assert args.execute == ""


def test_failed_cycle_offsets_only_artifact_numbering():
    recovery = SimpleNamespace(evolution_artifact_offset=1)

    assert _evolution_artifact_index(recovery, 1) == 2
    assert _evolution_artifact_index(recovery, 2) == 3
    assert _evolution_artifact_index(None, 1) == 1


def test_recovery_result_record_discloses_no_replay_and_preserved_failure(
        tmp_path):
    plan = tmp_path / "recovery-plan.json"
    plan.write_text("{}\n")
    failed_cycle = tmp_path / "attempt/evolution_cycles/cycle_001"
    failed_cycle.mkdir(parents=True)
    (failed_cycle / "state.json").write_text("{}\n")
    failed_tree = _artifact_tree_sha256(failed_cycle)
    failed_log = tmp_path / "phase2.log"
    failed_log.write_text("allowlisted failure\n")
    failed_log_sha256 = hashlib.sha256(failed_log.read_bytes()).hexdigest()
    plan_sha256 = hashlib.sha256(plan.read_bytes()).hexdigest()
    recovery = SimpleNamespace(
        recovery_kind="post_target_learning_zero_project_infra",
        plan_path=plan,
        evolution_artifact_offset=1,
        failed_evolution_cycle=str(failed_cycle),
        failed_evolution_tree_sha256=failed_tree,
        failed_runtime_log=str(failed_log),
        failed_runtime_log_sha256=failed_log_sha256,
        recovery_plan_sha256=plan_sha256,
    )

    assert _recovery_result_record(recovery) == {
        "kind": "post_target_learning_zero_project_infra",
        "plan": str(plan),
        "plan_sha256": plan_sha256,
        "completed_target_actor_replayed": False,
        "completed_target_verifier_replayed": False,
        "completed_target_learning_replayed": False,
        "failed_evolution_cycle": str(failed_cycle),
        "failed_evolution_tree_sha256": failed_tree,
        "failed_evolution_cycle_preserved": True,
        "failed_runtime_log": str(failed_log),
        "failed_runtime_log_sha256": failed_log_sha256,
        "failed_practice_attempt": "",
        "failed_practice_attempt_tree_sha256": "",
        "completed_practice_projects_before_recovery": 0,
        "completed_curriculum_turns_before_recovery": 0,
        "first_recovery_evolution_artifact_cycle": 2,
        "target_learning_updates_scope": "lineage_total",
        "pre_recovery_llm_provider_counts": "external_not_aggregated",
    }
    assert _initial_target_learning_updates(recovery) == 1
    assert _initial_target_learning_updates(None) == 0

    plan.write_text('{"changed": true}\n')
    try:
        _recovery_result_record(recovery)
    except RuntimeError as exc:
        assert "plan changed after admission" in str(exc)
    else:
        raise AssertionError("changed recovery plan was accepted")
    plan.write_text("{}\n")
    (failed_cycle / "state.json").write_text('{"changed": true}\n')
    try:
        _recovery_result_record(recovery)
    except RuntimeError as exc:
        assert "evidence changed after admission" in str(exc)
    else:
        raise AssertionError("changed failed-cycle evidence was accepted")


def test_execute_ack_remains_unchanged():
    args = _parser().parse_args([
        "task_080",
        "--protocol-run", "recovery-test",
        "--initial-memory", "/tmp/frozen-memory",
        "--execute", PHASE2_EXECUTE_ACK,
    ])

    assert args.execute == PHASE2_EXECUTE_ACK


def test_relative_recovery_plan_ignores_osworld_working_directory(
        tmp_path, monkeypatch):
    plan = tmp_path / "plans/recovery.json"
    plan.parent.mkdir()
    plan.write_text("{}\n")
    other = tmp_path / "OSWorld-V2"
    other.mkdir()
    monkeypatch.setattr(
        "benchmarks.osworld.phase2.RSIAGENT_ROOT", tmp_path)
    monkeypatch.chdir(other)

    assert _resolve_recovery_plan_path("plans/recovery.json") == plan
