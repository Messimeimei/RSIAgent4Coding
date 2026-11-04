"""Method parity and delivery, without invoking a model or starting containers."""

import hashlib
import itertools
import json
import shlex
from types import SimpleNamespace

import pytest

from benchmarks.deepswe.prompts import actor_prompt, verifier_prompt
from benchmarks.deepswe.session import BitfunSession
from core.actor import ACTOR_METHOD, EPISTEMIC_CORE, build_system
from core.verifier_runtime import (
    AGENTIC_VERIFIER_EVIDENCE,
    AGENTIC_VERIFIER_UNVERIFIED_VERDICT,
    agentic_verifier_system_for,
)
from explore.charter import (
    VERIFIER_INVESTIGATION_AUTHORITY,
    VERIFIER_REQUIREMENT_EVIDENCE,
    VERIFIER_UNVERIFIED_DECISION,
    agent_harness_verifier_charter,
)


def prompts():
    return (
        actor_prompt("task", 1, "actor-report"),
        verifier_prompt(
            "task", SimpleNamespace(candidate_id="candidate"), 1, "review-report"
        ),
    )


def test_coding_actor_retains_entire_method_with_only_two_interface_substitutions():
    actor, _ = prompts()
    actual = actor.split("THE METHOD — code as policy:\n", 1)[1].split(
        "\n\nCODING INTERFACE:", 1
    )[0]
    # Reverse ONLY the repository scope and actual vision-tool adaptations. Any
    # deletion, paraphrase, or additional method drift must fail this comparison.
    actual = actual.replace(
        "search broadly within the task repository and task-provided inputs "
        "(including relevant hidden files and directories)",
        "search broadly (the whole home directory, including hidden directories)",
    ).replace(
        "Use available BitFun image/browser tools for RECOGNITION and for "
        "judging RENDERED results",
        "Use `look` for RECOGNITION and for judging RENDERED results",
    )
    assert "THE METHOD — code as policy:\n" + actual == ACTOR_METHOD
    assert "checks[].desc" in actor
    assert "at least one task-derived" not in actor


def test_coding_verifier_keeps_current_agentic_judgment_and_discrimination():
    actor, verifier = prompts()
    assert EPISTEMIC_CORE in actor
    # The current Agentic Verifier has its own method, unlike the legacy
    # inspector. Do not silently import the latter's prompt into this role.
    assert EPISTEMIC_CORE not in verifier
    for block in (
        AGENTIC_VERIFIER_EVIDENCE,
        AGENTIC_VERIFIER_UNVERIFIED_VERDICT,
        VERIFIER_INVESTIGATION_AUTHORITY,
        VERIFIER_REQUIREMENT_EVIDENCE,
        VERIFIER_UNVERIFIED_DECISION,
    ):
        assert block in verifier
    assert "complete investigation report in report" in verifier
    # A known wrong result must not be sent back merely as missing evidence.
    assert "Do not use UNVERIFIED when you already have concrete evidence" in verifier
    assert "keep investigating through a distinguishing channel" in verifier
    assert "Reconcile conflicting channels before PASS" in verifier


@pytest.mark.parametrize("role", [0, 1])
@pytest.mark.parametrize("calls", [0, 1])
def test_full_role_message_survives_bitfun_cli_shell_transport(tmp_path, role, calls):
    instruction = 'Preserve literal `code`, $(text), "quotes", and\nnewlines.'
    prompt = (
        actor_prompt(instruction, 2, "a", feedback="repair", recovery="worklog")
        if role == 0
        else verifier_prompt(instruction, SimpleNamespace(candidate_id="c"), 2, "v")
    )
    session = BitfunSession(logs_dir=tmp_path, bitfun_config={"ai": {}})
    session.calls = calls
    shell = shlex.split(session.command(prompt).split(" &\n")[0])[-1]
    tokens = shlex.split(shell)
    # The full message is one argument, not shell code or a discarded local
    # variable, on both first invocation and --resume.
    assert tokens[tokens.index("--") + 1] == prompt
    assert ("--resume" if calls else "--session-id") in tokens
    assert (EPISTEMIC_CORE if role == 0 else VERIFIER_REQUIREMENT_EVIDENCE) in prompt


def test_shared_text_refactor_preserves_all_reference_prompt_variants():
    # Golden digests captured BEFORE extracting shared constants. These protect
    # the original OSWorld/ALE prompts, including role modes not used by Coding.
    actor_variants = [
        build_system(
            SimpleNamespace(
                vision_model="vision" if blind else "", action_discipline=discipline
            ),
            ask_enabled=ask,
        )
        for blind, discipline, ask in itertools.product([False, True], repeat=3)
    ]
    verifier_variants = [
        agentic_verifier_system_for(
            mode, orienting=orienting, allow_evolve=evolve, allow_unverified=unverified
        )
        for mode, orienting, evolve, unverified in itertools.product(
            ["effect_isolated", "rollback_mirror"],
            [False, True],
            [False, True],
            [False, True],
        )
    ]
    charter_variants = [
        agent_harness_verifier_charter(
            "task",
            "report",
            allow_evolve=evolve,
            allow_unverified=unverified,
            execution_mode=mode,
            actor_evidence_leads=leads,
            curriculum_review=curriculum,
        )
        for mode, evolve, unverified, leads, curriculum in itertools.product(
            ["effect_isolated", "rollback_mirror"],
            [False, True],
            [False, True],
            ["", "lead"],
            ["", "review"],
        )
    ]
    for variants, digest in (
        (
            actor_variants,
            "92c69e21f6d66777ca38613906c124b97b713de8d14813af7b13cbd63ad899cd",
        ),
        (
            verifier_variants,
            "086545f3e90cc1aa797eb1dc581dd053420a94b2dc3ad54cea297efa17ff7776",
        ),
        (
            charter_variants,
            "af8af37a0b4b85e165a98963e3b91b83bf7278c9e94e528c9563a3013c1fed03",
        ),
    ):
        assert (
            hashlib.sha256(
                json.dumps(variants, ensure_ascii=False).encode()
            ).hexdigest()
            == digest
        )
