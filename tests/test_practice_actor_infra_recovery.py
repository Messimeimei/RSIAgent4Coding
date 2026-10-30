from pathlib import Path
from types import SimpleNamespace

from benchmarks.osworld.phase2 import _evolution_resume_boundary
from core.self_evolving_loop import EvolutionResult, EvolutionStatus
from explore.post_target_learning_recovery import continue_after_target_learning
from explore.practice_actor_infra_recovery import _heredoc


def test_literal_curriculum_notes_heredoc_is_extracted_losslessly():
    program = """set -e
cat > /home/user/curriculum_notes.md <<'EOF'
line one
line two
EOF
echo done
"""

    assert _heredoc(
        program, "/home/user/curriculum_notes.md") == "line one\nline two\n"


def test_only_admitted_in_cycle_post_target_recovery_resumes_evolution():
    ordinary = SimpleNamespace(post_target_learning=False)
    zero_project = SimpleNamespace(
        post_target_learning=True, resume_evolution_cycle=False)
    in_cycle = SimpleNamespace(
        post_target_learning=True, resume_evolution_cycle=True)

    assert _evolution_resume_boundary(None, 1) is None
    assert _evolution_resume_boundary(ordinary, 1) is ordinary
    assert _evolution_resume_boundary(zero_project, 1) is None
    assert _evolution_resume_boundary(in_cycle, 1) is in_cycle
    assert _evolution_resume_boundary(in_cycle, 2) is None


def test_post_target_continuation_accepts_completed_project_boundary(monkeypatch):
    expected = object()
    starts = []

    def fake_run(_direction, memory, _hooks, *, start):
        starts.append((memory, start))
        return expected

    monkeypatch.setattr(
        "core.self_evolving_loop.run_self_evolving_loop", fake_run)
    recovery = SimpleNamespace(
        post_target_learning=True,
        resume_evolution_cycle=True,
        projects=1,
        trigger={
            "verifier_outcome": "PASS",
            "verifier_report": "VERDICT: PASS",
            "actor_learning_diagnosis": "diagnosis",
        },
        initial_evolution_memory={"before.md": b"before"},
        plan_path=Path("plan.json"),
        evolution_artifact_offset=1,
    )
    hooks = SimpleNamespace(
        evolve=lambda *_args: EvolutionResult(
            EvolutionStatus.READY_FOR_RETRY,
            {"after.md": b"after"},
            2,
            "ready",
        ),
        on_event=lambda *_args: None,
    )

    result = continue_after_target_learning(recovery, "target", hooks)

    assert result is expected
    assert starts[0][0] == {"after.md": b"after"}
    assert starts[0][1].target_cycles == 1
    assert starts[0][1].evolutions == 1
    assert starts[0][1].practice_projects == 2
