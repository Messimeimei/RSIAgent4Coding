"""Free-form Verifier reports retain the reference verdict and identity rules."""

import json

import pytest

from benchmarks.deepswe.contracts import HandoffError, read_handoff


@pytest.mark.parametrize("verdict", ["PASS", "FAIL", "UNVERIFIED"])
def test_free_report_preserves_text_and_binds_invocation(tmp_path, verdict):
    path = tmp_path / "current.json"
    text = f"Investigation and requirement evidence.\nVERDICT: {verdict}\nFull observations.\n"
    path.write_text(text)
    report = read_handoff(path, "verifier", "current", "candidate")
    assert report.report == text
    assert report.verdict == verdict
    assert report.handoff_id == "current"
    assert report.candidate_id == "candidate"


@pytest.mark.parametrize("text", [
    "No decision published.",
    "VERDICT: PASS\nVERDICT: FAIL",
    "```text\nVERDICT: PASS\n```",
    "    VERDICT: PASS",
    "VERDICT: UNKNOWN",
])
def test_missing_conflicting_or_example_only_verdict_is_rejected(tmp_path, text):
    path = tmp_path / "current.json"
    path.write_text(text)
    with pytest.raises(HandoffError, match="standalone VERDICT"):
        read_handoff(path, "verifier", "current", "candidate")


def test_code_example_does_not_override_real_verdict(tmp_path):
    path = tmp_path / "current.json"
    path.write_text("```text\nVERDICT: PASS\n```\nObserved violation.\nVERDICT: FAIL\n")
    assert read_handoff(path, "verifier", "current", "candidate").verdict == "FAIL"


def test_report_may_begin_with_a_structured_observation(tmp_path):
    path = tmp_path / "current.json"
    text = '{"observed": "build failed"}\nReproduction and analysis.\nVERDICT: FAIL\n'
    path.write_text(text)
    assert read_handoff(path, "verifier", "current", "candidate").report == text


def test_free_report_cannot_be_read_as_another_invocation(tmp_path):
    path = tmp_path / "old.json"
    path.write_text("Observations.\nVERDICT: PASS\n")
    with pytest.raises(HandoffError, match="current invocation"):
        read_handoff(path, "verifier", "new", "candidate")


def legacy_report():
    return {
        "kind": "verifier_report", "handoff_id": "current", "final": True,
        "candidate_id": "candidate", "verdict": "FAIL", "report": "Full investigation",
        "findings": [{"id": "TS9011", "observed": "Build fails", "reproduction": "pnpm build"}],
        "evidence": ["Build exit 1"], "evidence_requests": [],
    }


def test_legacy_object_findings_are_preserved_without_rejecting_report(tmp_path):
    path = tmp_path / "current.json"
    data = legacy_report()
    original = json.dumps(data)
    path.write_text(original)
    report = read_handoff(path, "verifier", "current", "candidate")
    assert report.verdict == "FAIL"
    assert json.loads(report.findings[0]) == data["findings"][0]
    assert path.read_text() == original


@pytest.mark.parametrize("field", ["handoff_id", "candidate_id"])
def test_legacy_identity_checks_still_apply(tmp_path, field):
    path = tmp_path / "current.json"
    data = legacy_report()
    data[field] = "stale"
    path.write_text(json.dumps(data))
    with pytest.raises(HandoffError):
        read_handoff(path, "verifier", "current", "candidate")


def test_legacy_recovery_names_invalid_field_without_echoing_payload(tmp_path):
    path = tmp_path / "current.json"
    data = legacy_report()
    data["findings"] = [123]
    path.write_text(json.dumps(data))
    with pytest.raises(HandoffError, match=r"findings\.0: string_type"):
        read_handoff(path, "verifier", "current", "candidate")


def test_legacy_error_does_not_echo_unrecognized_field_names(tmp_path):
    path = tmp_path / "current.json"
    data = legacy_report()
    data["PRIVATE_PAYLOAD"] = "PRIVATE_VALUE"
    path.write_text(json.dumps(data))
    with pytest.raises(HandoffError) as error:
        read_handoff(path, "verifier", "current", "candidate")
    assert "extra_forbidden" in str(error.value)
    assert "PRIVATE" not in str(error.value)


def test_free_report_does_not_change_actor_protocol(tmp_path):
    path = tmp_path / "current.json"
    path.write_text("VERDICT: PASS\n")
    with pytest.raises(HandoffError):
        read_handoff(path, "actor", "current")


def test_handoff_published_with_restricted_mode_is_still_read(tmp_path):
    """A container-published 0600 handoff must not read as an invalid report."""
    path = tmp_path / "current.json"
    path.write_text("Observed evidence.\nVERDICT: FAIL\n")
    path.chmod(0o000)
    report = read_handoff(path, "verifier", "current", "candidate")
    assert report.verdict == "FAIL"
    assert report.report == "Observed evidence.\nVERDICT: FAIL\n"
