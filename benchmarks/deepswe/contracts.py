"""Versioned, candidate-bound terminal handoffs; absent reports are not verdicts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class HandoffError(RuntimeError):
    pass


class RoleInfrastructureError(RuntimeError):
    pass


class TaskBudgetExhausted(RuntimeError):
    pass


def _read_text(path):
    """Read a role handoff published from inside the container.

    The handoff lands in the bind-mounted logs directory, so it can be owned by
    container root with mode 0600 and a host-side read then raises
    PermissionError. That used to be reported as an invalid report, which spent
    the whole handoff recovery budget on a cause the role could not act on.
    Retry once after a best-effort chmod, otherwise state the problem plainly.
    """
    try:
        return path.read_text()
    except PermissionError as error:
        try:
            os.chmod(path, 0o644)
        except OSError:
            raise HandoffError(
                f"Handoff file {path.name} is not readable by the runner "
                "(PermissionError); publish it world-readable"
            ) from error
        try:
            return path.read_text()
        except OSError as retry_error:
            raise HandoffError(
                f"Handoff file {path.name} is still unreadable after chmod "
                f"({type(retry_error).__name__})"
            ) from retry_error


class CompletionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    desc: str = Field(min_length=1)
    probe: str = Field(min_length=1)


def read_role_completion(path, handoff_id, *, require_checks=False):
    """Validate RSI completion without including submitted content in diagnostics."""
    path = Path(path)
    try:
        bounded = (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 2 * 1024 * 1024
        )
    except OSError as error:
        raise HandoffError(
            f"Completion is not accessible ({type(error).__name__})"
        ) from error
    if bounded:
        raise HandoffError("Completion must be a regular JSON file below 2 MiB")
    try:
        value = json.loads(_read_text(path))
    except json.JSONDecodeError as error:
        raise HandoffError(f"Invalid JSON at line {error.lineno}, column {error.colno}") from error
    except (OSError, UnicodeError) as error:
        raise HandoffError("Completion must be readable UTF-8") from error
    if not isinstance(value, dict):
        raise HandoffError("Completion must be a JSON object")
    if set(value) - {"kind", "handoff_id", "final", "checks"}:
        raise HandoffError("Only kind, handoff_id, final and optional checks are allowed; put other results in the charter's files")
    if value.get("kind") != "rsi_done":
        raise HandoffError('kind must equal "rsi_done"')
    if value.get("handoff_id") != handoff_id:
        raise HandoffError("handoff_id must match the current invocation, not an earlier attempt")
    if value.get("final") is not True:
        raise HandoffError("final must be the JSON boolean true")
    if not isinstance(value.get("checks", []), list):
        raise HandoffError("checks must be an array of desc/probe objects")
    try:
        checks = [CompletionCheck.model_validate(item) for item in value.get("checks", [])]
    except ValueError as error:
        raise HandoffError("Each check must contain only nonempty desc and probe strings") from error
    if require_checks and not checks:
        raise HandoffError("This evidence response requires at least one reproducible check")
    return value, checks


class ActorHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["actor_done"]
    handoff_id: str
    final: Literal[True]
    candidate_head: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    summary: str = Field(min_length=1)
    checks: list[CompletionCheck] = Field(min_length=1)


class ReviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["verifier_report"]
    handoff_id: str
    final: Literal[True]
    candidate_id: str = Field(min_length=1)
    verdict: Literal["PASS", "FAIL", "UNVERIFIED"]
    report: str = Field(min_length=1)
    findings: list[str]
    evidence: list[str] = Field(min_length=1)
    evidence_requests: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        if not self.report.strip() or not all(x.strip() for x in self.evidence):
            raise ValueError("Report and evidence must not be blank")
        if self.verdict == "FAIL" and not any(x.strip() for x in self.findings):
            raise ValueError("FAIL requires concrete violating evidence")
        if self.verdict == "PASS" and (self.findings or self.evidence_requests):
            raise ValueError("PASS cannot contain unresolved requirements")
        if self.verdict == "UNVERIFIED" and not any(
            x.strip() for x in self.evidence_requests
        ):
            raise ValueError("UNVERIFIED must identify reproducible evidence needed")
        return self


def read_handoff(path, role, handoff_id, candidate_id=None):
    path = Path(path)
    try:
        bounded = (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 2 * 1024 * 1024
        )
    except OSError as error:
        raise HandoffError(
            f"No readable, bounded terminal handoff ({type(error).__name__})"
        ) from error
    if bounded:
        raise HandoffError("No readable, bounded terminal handoff")
    try:
        text = _read_text(path)
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            if role == "verifier":
                return _read_verifier_text(path, text, handoff_id, candidate_id)
            raise
        if role == "verifier" and not (
            isinstance(value, dict) and value.get("kind") == "verifier_report"
        ):
            return _read_verifier_text(path, text, handoff_id, candidate_id)
        # Older sessions may publish structured findings inside the JSON envelope.
        if role == "verifier" and isinstance(value, dict):
            findings = value.get("findings")
            if isinstance(findings, list):
                value["findings"] = [
                    json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else item
                    for item in findings
                ]
        report = (ActorHandoff if role == "actor" else ReviewReport).model_validate(
            value
        )
    except HandoffError:
        raise
    except ValidationError as error:
        fields = ActorHandoff.model_fields.keys() | ReviewReport.model_fields.keys() | {"desc", "probe"}
        details = []
        for item in error.errors(include_input=False, include_url=False)[:8]:
            location = ".".join(
                str(part) if isinstance(part, int) or part in fields else "<field>"
                for part in item["loc"]
            ) or "report"
            details.append(f"{location}: {item['type']}")
        raise HandoffError(f"Invalid {role} handoff: {'; '.join(details)}") from error
    except (ValueError, OSError) as error:
        raise HandoffError(
            f"Invalid {role} handoff ({type(error).__name__})"
        ) from error
    if report.handoff_id != handoff_id:
        raise HandoffError("Stale handoff identity")
    if role == "verifier" and report.candidate_id != candidate_id:
        raise HandoffError("Report belongs to a different candidate")
    return report


def _read_verifier_text(path, text, handoff_id, candidate_id):
    from core.verifier import _parse_agentic_verifier_report

    if path.name != f"{handoff_id}.json" or not candidate_id:
        raise HandoffError("Verifier report must use the current invocation's report path and candidate")
    decision = _parse_agentic_verifier_report(text, allow_unverified=True)
    if decision is None:
        raise HandoffError(
            "Report must contain an unambiguous standalone VERDICT: PASS, "
            "VERDICT: FAIL, or VERDICT: UNVERIFIED outside quoted examples or code fences"
        )
    verdict = {"pass": "PASS", "wrong": "FAIL", "unverified": "UNVERIFIED"}[decision]
    # Keep the existing controller record shape without asking the model to split
    # its report into sections. Identity comes from the invocation's private path.
    return ReviewReport(
        kind="verifier_report",
        handoff_id=handoff_id,
        final=True,
        candidate_id=candidate_id,
        verdict=verdict,
        report=text,
        findings=[text] if verdict == "FAIL" else [],
        evidence=[text],
        evidence_requests=[text] if verdict == "UNVERIFIED" else [],
    )
