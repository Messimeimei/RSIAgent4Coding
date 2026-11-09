"""Broken adapters produce named infrastructure failures, never a task verdict."""

import json
from types import SimpleNamespace

import pytest

from explore import phase1_wave as wave
from explore import practice_loop as practice


@pytest.mark.parametrize("hook,invoke", [
    ("reset_vm", lambda hooks: practice._fresh_vm(hooks, None, "task")),
    ("replay_project", lambda hooks: practice._replay(hooks, None, "snapshot")),
    ("verify_project", lambda hooks: practice._verify_candidate(hooks, None, "snapshot")),
    ("capture_project", lambda hooks: practice._capture_owned_tree(
        hooks, None, "key", "snapshot", "task")),
])
@pytest.mark.parametrize("invalid", [None, [], "unexpected"])
def test_malformed_lifecycle_hook_names_the_failed_boundary(hook, invoke, invalid):
    hooks = practice.PracticeHooks(**{hook: lambda *a, **k: invalid})
    with pytest.raises(practice.PracticeInfrastructureError, match=hook):
        invoke(hooks)


def test_branch_failure_retains_traceback_project_and_stage(tmp_path, monkeypatch):
    calls = []

    def broken(**kwargs):
        calls.append(True)
        error = AttributeError("invalid native completion")
        error.rsi_branch_stage = "verify_agentic"
        raise error

    monkeypatch.setattr(wave, "_execute_branch", broken)
    episode = tmp_path / "ep003"
    with pytest.raises(AttributeError):
        wave._execute_branch_with_infra_retry(
            episode_dir=episode, project_index=3,
            project=SimpleNamespace(project_id="vtz-block"))
    saved = json.loads((episode / "branch_exception.json").read_text())
    assert saved["project_index"] == 3
    assert saved["project_id"] == "vtz-block"
    assert saved["stage"] == "verify_agentic"
    assert 'raise error' in saved["traceback"]
    assert len(calls) == 1  # Unknown software faults must not replay model work.


def test_second_infrastructure_failure_is_also_diagnosed(tmp_path, monkeypatch):
    calls = []

    def broken(**kwargs):
        calls.append(True)
        raise TimeoutError(f"fault {len(calls)}")

    monkeypatch.setattr(wave, "_execute_branch", broken)
    episode = tmp_path / "ep001"
    with pytest.raises(TimeoutError, match="fault 2"):
        wave._execute_branch_with_infra_retry(episode_dir=episode)
    first = json.loads((episode / "infra_attempts/attempt_001/branch_exception.json").read_text())
    last = json.loads((episode / "branch_exception.json").read_text())
    assert first["error"] == "fault 1" and last["error"] == "fault 2"
    assert len(calls) == 2
