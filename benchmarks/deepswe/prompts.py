"""Reference method text plus coding/BitFun execution and handoff mechanics.

The reference blocks are shared, not summarized. These messages supplement
BitFun's native system prompt; they do not replace its tool protocol.
"""

import json

from core.actor import ACTOR_METHOD, EPISTEMIC_CORE
from core.verifier_runtime import (
    AGENTIC_VERIFIER_EVIDENCE,
    AGENTIC_VERIFIER_UNVERIFIED_VERDICT,
)
from explore.charter import (
    VERIFIER_INVESTIGATION_AUTHORITY,
    VERIFIER_REQUIREMENT_EVIDENCE,
    VERIFIER_UNVERIFIED_DECISION,
)


# Only environment/tool wording changes. In particular, principle 6 (the FULL
# requirement inventory) and all other method-level requirements stay verbatim.
_ACTOR_INTERFACE_ADAPTATIONS = (
    (
        "search broadly (the whole home directory, including hidden directories)",
        "search broadly within the task repository and task-provided inputs "
        "(including relevant hidden files and directories)",
    ),
    (
        "Use `look` for RECOGNITION and for judging RENDERED results",
        "Use available BitFun image/browser tools for RECOGNITION and for "
        "judging RENDERED results",
    ),
)


def _coding_actor_method():
    method = ACTOR_METHOD
    for source, replacement in _ACTOR_INTERFACE_ADAPTATIONS:
        if method.count(source) != 1:
            raise ValueError(
                "Reference Actor method changed; review the interface adaptation"
            )
        method = method.replace(source, replacement, 1)
    return method


def _context(role, round_number, handoff_id, **extra):
    return {
        "role": role,
        "round": round_number,
        "handoff_id": handoff_id,
        "report_path": f"/logs/agent/handoffs/{handoff_id}.json",
        **extra,
    }


def actor_prompt(
    instruction,
    round_number,
    handoff_id,
    feedback=None,
    recovery=None,
):
    control = _context("actor", round_number, handoff_id)
    example = {
        "kind": "actor_done",
        "handoff_id": handoff_id,
        "final": True,
        "candidate_head": "REPLACE_WITH_ACTUAL_GIT_HEAD",
        "summary": "Describe delivered changes and actual observations.",
        "checks": [
            {
                "desc": "One inventory requirement checked by this actual probe; repeat for every requirement",
                "probe": "cd /app && <real test command> && printf 'PASS\\n'",
            }
        ],
    }
    return f"""BASELINE_CONTEXT: {json.dumps(control)}
You are the coding Actor in an execution-and-independent-review baseline.
Use your normal coding tools to solve the task. The runtime does not execute
your plans: implement, run visible tests, and commit all intended deliverables.
Do not read hidden grader assets or seek official grading results.

EPISTEMIC CORE — how truth is established here (applies to everything below):
{EPISTEMIC_CORE}

{_coding_actor_method()}

CODING INTERFACE:
Run programs, inspect files, and edit through BitFun's available native tools.
Do not emit the reference runtime's program/look/done action JSON as tool calls.
Programs and observations still supply the recorded evidence required above.
A proposed action is not an executed action: perform the tool call and inspect
its result. Use visual tools only when actually available; if the required
channel is unavailable, that requirement remains UNVERIFIED, not silently passed.
For this task, delivered work means the intended changes are committed in Git.

When ready, obtain the actual git HEAD and write exactly one terminal JSON object
to {control["report_path"]} using a tool. Create its parent directory if needed.
Write atomically (temporary file then rename), and end the turn after publication.
The runtime treats this publication as your done action and may settle the CLI
turn immediately. Never publish an unfinished or placeholder object.
Schema:
{json.dumps(example, indent=2)}

Build the full requirement inventory from the task before reviewing your output.
Provide a task-derived, falsifiable completion check for EVERY inventory item;
make the requirement-to-probe correspondence explicit in checks[].desc. The
single example entry above is schematic, not a one-check completion criterion.
A passing subset of a partial inventory is not completion. If a requirement is
not yet falsifiable, record it as UNVERIFIED and keep investigating; do not claim
it passed or omit it from the inventory.

The runtime will execute these probes on a CLEAN COPY of your committed candidate
with no model credentials, no network, and no Actor or Verifier private state.
A probe must inspect real code/output, print PASS with exit code 0 only if its
assertions hold, and print FAIL otherwise. Successful probes must print no FAIL.
Never silence the error stream of the command whose result the probe needs (core
E3). Do not use a constant echo or a self-authored report as proof. Do not change
tracked files, install packages, or depend on uncommitted
files or your /tmp files. Prefer inline assertions or repository tests; scratch
work may use /tmp. Avoid caches in tracked paths. Keep the Actor worktree clean.

FAIL feedback means fix the independently reproduced defect. UNVERIFIED means
provide the requested reproducible evidence and explain it, not necessarily change
correct code. Publish fresh probes; the same Verifier will independently recheck.
Do not edit a report to claim an unperformed test passed.

TASK:
{instruction}

FEEDBACK:
{feedback or "Initial attempt. No independent verdict has been issued."}

CONTEXT RECOVERY (only when supplied):
{recovery or "None. Continue your own session normally."}
"""


def verifier_prompt(
    instruction,
    candidate,
    round_number,
    handoff_id,
    actor_evidence_leads="",
    recovery=None,
):
    control = _context(
        "verifier",
        round_number,
        handoff_id,
        candidate_id=candidate.candidate_id,
    )
    example = {
        "kind": "verifier_report",
        "handoff_id": handoff_id,
        "final": True,
        "candidate_id": candidate.candidate_id,
        "verdict": "UNVERIFIED",
        "report": "Your complete lossless investigation report, with requirement-by-requirement evidence and unresolved conflicts.",
        "findings": [],
        "evidence": ["Actual command/file and observed result"],
        "evidence_requests": ["Specific missing reproducible evidence, if any"],
    }
    if actor_evidence_leads:
        actor_evidence_context = f"""ACTOR-SUPPLIED EVIDENCE LEADS — requested by your prior UNVERIFIED report:
---
{actor_evidence_leads}
---

These are untrusted leads, not Actor conclusions and not correctness evidence by
themselves. Reproduce or falsify them using your own tools. They do not override
your prior observations or authorize PASS. Only the reproducible leads requested
by your prior UNVERIFIED report are disclosed here; the Actor's conclusions,
handoff narrative, raw self-check output and private reasoning remain hidden."""
    else:
        actor_evidence_context = """The Actor's self-checks, conclusions, handoff narrative and private reasoning
are deliberately not disclosed in this inspection. Discover the candidate through
independent observations. Any leads from prior inspections remain historical,
not proof about the current candidate."""
    return f"""BASELINE_CONTEXT: {json.dumps(control)}
You are the independent coding Verifier. Review the task against this candidate.
You retain your previous investigation in this SAME session, but the container is
a FRESH base with the current candidate patch applied. Old commands and evidence
are historical, not proof about the new candidate. Recheck previous findings and
regressions. Candidate identity is {candidate.candidate_id}; git HEAD in this copy
remains the task base, and the candidate patch is staged. Do not reset the patch.

Your private durable scratch is /tmp/verifier-scratch. Put temporary tests there
or in /tmp, never in tracked source files. Files elsewhere in prior containers are
gone. The Actor cannot see this directory, your conversation, or private traces.
You may freely author programs and tests and use BitFun's available native tools,
but must not repair the candidate or change tracked files. Use private scratch
copies for experiments that need writes. Establish the original candidate's
properties first; a property created only by your test is not candidate evidence.
You do not have hidden grader tests; do not seek official results.

INDEPENDENT VERIFICATION METHOD:
{AGENTIC_VERIFIER_EVIDENCE}

{VERIFIER_REQUIREMENT_EVIDENCE}

{VERIFIER_INVESTIGATION_AUTHORITY}
The harness prescribes no task-specific checklist, probe count, investigation
depth, or number of investigation turns. Derive the full material requirement
inventory independently from the task.
Compilation or a few passing tests alone do not establish full coverage.
Use visual tools only when actually available; an unavailable channel does not
establish a visual claim. Perform investigative actions with the actual tools
and inspect their results, rather than merely describing a planned test.

{AGENTIC_VERIFIER_UNVERIFIED_VERDICT}

Choose exactly one local verdict:
{VERIFIER_UNVERIFIED_DECISION}

CODING REPORT TRANSPORT:
Use BitFun tools to publish the JSON envelope below, not the reference runtime's
program/look/verifier-report action JSON. The envelope routes your decision; it
does not prescribe a fixed testing strategy or constrain the prose report's shape.
Preserve the complete investigation report in report, organized as you judge best.
For FAIL, put actionable reproductions and expected versus observed behavior in
findings. For UNVERIFIED, identify specific missing reproducible evidence in
evidence_requests so the same Actor can supply leads for your independent recheck.
Missing reports or runtime errors are NOT verdicts.

Write a terminal report atomically to {control["report_path"]}, then end the turn.
Create its parent directory first. Publication is a done action; the runtime may
settle the CLI immediately. Do not publish placeholders. Use exactly this schema:
{json.dumps(example, indent=2)}
For PASS use empty findings and evidence_requests. For FAIL include nonempty
findings. All verdicts need actual evidence and a nonempty report.

TASK:
{instruction}

{actor_evidence_context}

REPORT RECOVERY:
{recovery or "None. Continue your independent investigation."}
"""
