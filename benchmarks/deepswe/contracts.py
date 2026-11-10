"""Versioned, candidate-bound terminal handoffs; absent reports are not verdicts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HandoffError(RuntimeError):
    pass


class RoleInfrastructureError(RuntimeError):
    pass


class TaskBudgetExhausted(RuntimeError):
    pass


class CompletionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    desc: str = Field(min_length=1)
    probe: str = Field(min_length=1)


def read_role_completion(path, handoff_id, *, require_checks=False):
    """Validate RSI completion without including submitted content in diagnostics."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise HandoffError("Completion must be a regular JSON file below 2 MiB")
    try:
        value = json.loads(path.read_text())
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
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise HandoffError("No readable, bounded terminal handoff")
    try:
        value = json.loads(path.read_text())
        report = (ActorHandoff if role == "actor" else ReviewReport).model_validate(
            value
        )
    except (ValueError, OSError) as error:
        raise HandoffError(
            f"Invalid {role} handoff ({type(error).__name__})"
        ) from error
    if report.handoff_id != handoff_id:
        raise HandoffError("Stale handoff identity")
    if role == "verifier" and report.candidate_id != candidate_id:
        raise HandoffError("Report belongs to a different candidate")
    return report
