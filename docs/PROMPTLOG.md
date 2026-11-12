# Prompt changes

## 2026-11-11 - Free-form Coding Verifier reports

Baseline and RSI target/Phase 3 Verifiers now publish plain report text with the
reference standalone VERDICT line. The runtime binds the private report path to
the invocation and candidate. Separate JSON findings/evidence fields are no longer
requested. Legacy JSON remains readable, including structured object findings;
schema recovery errors identify field paths and error types without echoing values.
The Actor protocol and reference prompts are unchanged.

## 2026-11-03 — Coding baseline method alignment

Extracted `ACTOR_METHOD`, Agentic Verifier evidence/verdict blocks, and Verifier
charter investigation/discrimination/UNVERIFIED blocks as shared constants.
No reference Actor, Verifier or charter output changed: 56 prompt variants were
captured before the refactor and compared afterward, with regression digests in
`tests/deepswe/test_prompts.py`. The no-leakage audit includes the extracted text.

Coding prompts now reuse those method requirements. Repository/input discovery,
available image/browser tools, Git candidates and atomic JSON handoffs are the
interface adaptations. Actor receives the canonical epistemic core. The current
Agentic Verifier's conflict and distinguishing-evidence rules are retained;
legacy inspector probe limits are not mixed into that role. Numeric time limits
remain hidden, while the original BANK PARTIAL PROGRESS principle is preserved.

These prompts remain BitFun user messages routed to separate role sessions and
model configurations, underneath BitFun's native system instructions. This restores
method content without claiming identical instruction precedence. See
`docs/DEEPSWE_BASELINE.md` for the source-to-adaptation mapping.
