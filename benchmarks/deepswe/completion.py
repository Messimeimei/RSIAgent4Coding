"""Execute Actor-authored probes in a credential-free disposable candidate copy."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from core.checks import validate

from .review_environment import review_environment, review_tree_digest


def actor_evidence_leads(completion):
    """Match core.loop's evidence handoff: reproducible probes, never raw output.

    The controller calls this only after the Verifier requested evidence with
    UNVERIFIED. Actor summaries, private logs and the full completion record stay
    outside the Verifier context.
    """
    blocks = []
    for index, check in enumerate(completion["checks"], start=1):
        observed = "PASS" if check["passed"] else "FAIL"
        blocks.append(
            f"LEAD {index}\n"
            f"Actor-stated claim: {check['desc']}\n"
            f"Exact reproducible probe proposed by Actor:\n"
            f"--- BEGIN PROBE ---\n{check['probe']}\n--- END PROBE ---\n"
            f"Harness execution classification: {observed}"
        )
    return "\n\n".join(blocks)


async def run_completion_checks(
    actor_environment,
    candidate,
    handoff,
    instruction,
    destination,
    command_timeout_sec=600,
):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    accepted, rejected, dropped = validate(
        [check.model_dump() for check in handoff.checks], instruction=instruction
    )
    result = {
        "candidate_id": candidate.candidate_id,
        "passed": False,
        "rejections": rejected,
        "dropped": dropped,
        "checks": [],
    }
    if not rejected:
        async with review_environment(
            actor_environment, None, candidate, destination
        ) as environment:
            original = await review_tree_digest(environment, candidate.base_commit)
            for index, check in enumerate(accepted, 1):
                try:
                    observation = await asyncio.wait_for(
                        environment.exec(
                            command=check.probe,
                            cwd=candidate.repo,
                            timeout_sec=command_timeout_sec,
                            env={"PYTHONDONTWRITEBYTECODE": "1"},
                        ),
                        timeout=command_timeout_sec + 5,
                    )
                    output = (observation.stdout or "") + (observation.stderr or "")
                    return_code = observation.return_code
                except TimeoutError:
                    output, return_code = "Completion probe timed out", None
                output_path = destination / f"check-{index:03d}.txt"
                output_path.write_text(output)
                passed = (
                    return_code == 0
                    and bool(re.search(r"\bPASS\b", output))
                    and not re.search(r"\bFAIL\b", output)
                )
                result["checks"].append(
                    {
                        "desc": check.desc,
                        "probe": check.probe,
                        "passed": bool(passed),
                        "return_code": return_code,
                        "output": output[-12000:],
                        "output_path": str(output_path),
                    }
                )
                if (
                    await review_tree_digest(environment, candidate.base_commit)
                    != original
                ):
                    result["rejections"].append(
                        "Completion probe modified candidate tracked state"
                    )
                    break
                if return_code is None:
                    # Remove the container before any timed-out subprocess can
                    # affect a later check. It has no access to the Actor.
                    break
        result["passed"] = (
            not result["rejections"]
            and bool(result["checks"])
            and len(result["checks"]) == len(accepted)
            and all(check["passed"] for check in result["checks"])
        )
    (destination / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
