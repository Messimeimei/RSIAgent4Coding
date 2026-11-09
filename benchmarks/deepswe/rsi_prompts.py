"""Original RSI charters with explicit coding/BitFun transport adaptations."""

from core.actor import EPISTEMIC_CORE, PLAIN_JSON_TRANSPORT_NOTE
from core.verifier_runtime import (
    AGENTIC_VERIFIER_EVIDENCE, AGENTIC_VERIFIER_BINARY_VERDICT,
    AGENTIC_VERIFIER_UNVERIFIED_VERDICT,
)
from explore.charter import agent_harness_verifier_charter
from .prompts import _coding_actor_method


CODING_PRACTICE = """CODING ENVIRONMENT ADAPTATION:
This is an isolated coding environment with the public, pinned repository at /app
and its installed development tools. /app is reference material for practice;
only /home/user/evolution_project is captured as the practice deliverable. If a
practice needs to modify source, place the needed public source in that project
root. Include required local inputs/dependencies so it replays in the same clean
image. Use BitFun's native file, program and inspection tools. No desktop control
API is assumed. Keep temporary probes and caches outside the published tree.

Curriculum chooses the coding capability and precise natural-language behavior
to practice. Inputs can include synthetic data, an incomplete interface, a small
repository, or public source excerpts. Do not supply the requested implementation,
project-specific tests, grading commands, a worked solution, or a prescribed
memory lesson. Project-specific verification belongs to the independent Verifier.
Do not copy the unchanged target into a learning project. No previous benchmark
run, candidate patch, hidden tests or official scoring feedback is available.
"""


def practice_prompt(charter, *, curriculum, terminal_path, handoff_id,
                    evidence_requested=False, target_learning=False):
    # The original role charter and all its method requirements remain intact.
    # Only the native action transport and guest path spelling change.
    charter = charter.replace(PLAIN_JSON_TRANSPORT_NOTE, "")
    charter = charter.replace("~/.memory", "/home/user/.memory")
    charter = charter.replace(
        "every action\nyou take is a submitted program",
        "your actions use BitFun's native tools and executed programs",
    )
    method = "\n" + _coding_actor_method()
    evidence_contract = "" if not evidence_requested else """
This is the independent Verifier's evidence request. Include a checks array in
the completion envelope: [{"desc":"claim being tested","probe":"exact reproducible
shell command"}]. Give only probes the Verifier can independently rerun against
the submitted project in a clean environment. This array is a transport for the
reference Done checks; private reasoning, conclusions and raw logs are not shared.
"""
    environment = CODING_PRACTICE if not target_learning else """CODING TARGET LEARNING:
This is the terminal learning phase of your actual target attempt in /app, in
your same native BitFun conversation. Use the live repository to investigate
remaining questions if useful. Only /home/user/.memory is promoted; target code,
tests, scratch and session state do not cross into a fresh target attempt.
"""
    return f"""{EPISTEMIC_CORE}
{method}

{charter}

{environment}

BITFUN COMPLETION TRANSPORT:
Use native BitFun tools rather than the reference program/look/done action JSON.
Never describe an intended operation without executing it through a tool and
inspecting its result. When the phase is finished, publish its completion.
When the charter's work and any required handoff files are complete, atomically
write {{"kind":"rsi_done","handoff_id":"{handoff_id}","final":true}}
to {terminal_path}. Create its parent directory first and end the turn. This is
only the completion envelope; the charter's wave/project/diagnosis publication
remains authoritative. Do not publish this envelope before that work is complete.
{evidence_contract}
"""


def practice_verifier_prompt(project, context, report_path, *, allow_unverified,
                             generation, evidence_leads=""):
    # Preserve the entire original charter, replacing only execution mechanics.
    reference = agent_harness_verifier_charter(
        project, report_path, mechanical_context=context,
        candidate_generation=generation, allow_unverified=allow_unverified,
        actor_evidence_leads=evidence_leads)
    start = reference.index("You are a full investigation Agent with code-as-policy")
    end = reference.index("You decide the investigation strategy", start)
    reference = reference[:start] + """CODING ISOLATION:
You have a fresh disposable container with an exact replay of the submitted tree
at /home/user/evolution_project and, when supplied, original input fixtures at
/home/user/.practice_original_project. Use BitFun's native tools and installed
development environment. /app contains pristine public reference source only.
Do not change the candidate or original fixtures; their full inventories and bytes
are checked after inspection. Put experimental copies, test programs and outputs
under /tmp/verifier-scratch. Original properties must be established before tests;
a property created only by an experiment is not evidence about the candidate.
Python bytecode, pytest caches, and Hypothesis storage are redirected to /tmp/verifier-scratch by the
harness. Keep these environment settings. Run tools that create other build or
test outputs on a copy under /tmp/verifier-scratch. Do not install packages into,
rewrite, or delete files in the submitted tree, including existing cache files.
Your native conversation survives an evidence request, but this container and its
scratch are fresh; recreate probes as needed. Never consult official evaluations.

""" + reference[end:]
    transport_start = reference.index("When ready, publish your complete lossless report with:")
    transport_end = reference.index("Include exactly one standalone line", transport_start)
    reference = (reference[:transport_start] +
        f"When ready, use a native BitFun tool to write your complete report atomically to {report_path}. "
        + reference[transport_end:])
    doctrine = (AGENTIC_VERIFIER_UNVERIFIED_VERDICT if allow_unverified
                else AGENTIC_VERIFIER_BINARY_VERDICT)
    return AGENTIC_VERIFIER_EVIDENCE + "\n" + doctrine + "\n\n" + reference
