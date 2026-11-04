# BitFun Actor-Verifier Baseline V2

The default `run_deepswe.py` mode remains `native`. Its adapter, YAML, binary,
and official DeepSWE collection/grading are unchanged. `--mode baseline`
selects the separate controller. This is a coding adaptation of the reference
baseline mechanisms, not a claim of identical models, prompts, budgets or scores.

## Prompt method and BitFun delivery

Coding role messages now reuse the reference method blocks directly. The source
refactor leaves all 56 combinations of reference Actor, Agentic Verifier and
candidate charter prompts byte-for-byte unchanged; regression digests cover them.
ALE uses the same baseline roles, with its own platform adapters.

| Reference source | Coding behavior |
| --- | --- |
| `core/actor.py:EPISTEMIC_CORE` | All six Actor evidence principles appear verbatim: claim-appropriate channels, structural/rendered distinction, visible errors, no inference from broken channels, independent confirmation, recorded evidence. |
| `core/actor.py:ACTOR_METHOD` | All ten principles are retained. Only whole-home discovery becomes task-repository/input discovery, and `look` becomes the available BitFun image/browser tools. Missing visual capabilities never count as evidence. |
| Actor principle 6 | Build the full inventory from the task, derive a falsifiable check for every requirement, test correspondence and references, and keep unresolved items UNVERIFIED. A passing partial inventory is not completion. Each handoff check names its requirement. |
| `core/verifier_runtime.py` evidence/verdict blocks | Affirmative support for every material requirement, FAIL for an established violation, UNVERIFIED for unresolved claims or credible conflicts; reconcile conflicting channels before PASS. |
| `explore/charter.py` investigation/requirement/decision blocks | Verifier chooses its strategy and depth, respects task-mandated methods and provenance, distinguishes plausible incorrect substitutes, and never silently favors Actor probes or another instrument during conflicts. |

The current Agentic Verifier is the reference; its evidence doctrine is reused
without adding the older inspector's prompt, probe limits or terminal protocol.
Coding retains its native
tools, committed Git candidates, isolated review copies and nonce-bound atomic
JSON handoffs. The report field carries the complete free-form investigation;
the envelope is transport, not a prescribed task checklist. The controller does
not infer semantic coverage by counting tests; independent review owns judgment.

The delivery path is `actor_prompt -> Actor BitFun session -> Actor configured
model`, and separately `verifier_prompt -> Verifier BitFun session -> Verifier
configured model`. This includes repair, evidence requests and native segment
continuations. No second direct model call is added. The full role message goes
to `bitfun exec ... -- <message>`; `--resume` preserves that role's session.

There is still a prompt-priority difference: reference Actor method text and
Verifier runtime rules are system messages (the reference Verifier charter is
a user message); here the role text is a BitFun user message under BitFun's native
system prompt and tool descriptions. This change restores method content, not
system-message precedence or identical model behavior. The pinned CLI's `exec
--help` identifies MESSAGE as a user message and exposes no system-prompt flag.
Source hashes and `prompt_policy` / `prompt_delivery` record this distinction.

## H20 usage

Run from `/data/k8s/yx/RSIAgent4Coding`:

```bash
.runtime/deepswe/venv/bin/python run_deepswe.py check --mode baseline \
  --config config/deepswe/two-tasks-baseline-v2.yaml --name baseline-v2-check
.runtime/deepswe/venv/bin/python run_deepswe.py run --mode baseline \
  --config config/deepswe/two-tasks-baseline-v2.yaml \
  --task dateutil-rfc5545-timezone-interop --name dateutil-baseline-v2-001
```

Replace `--task` with `dynamodb-toolbox-lazy-recursive-schemas` for the second task.
Omit `--task` to run both with `n_concurrent_trials: 1`. Pier may reorder tasks;
separate invocations make the order explicit. Always use a new job name.
Without `--mode baseline`, the existing native BitFun behavior is preserved.

The two-task YAML uses DeepSeek-V4.1-Flash-1017 for the Actor and Qwen3.8-Flash
for the Verifier. Credentials still come from `.runtime/deepswe/runtime.env`
or existing shell variables. Keys are referenced as `${API_KEY}`, not written
into job configurations. Model URLs must be explicit. Review containers retain
the task's no-network policy except for the authenticated model proxy allowlist.

## V2 control contract

1. Actor uses BitFun's coding tools in the original task container. It commits
   its solution and publishes a nonce-bound `actor_done` JSON handoff with HEAD
   and task-derived `{desc, probe}` completion checks.
2. The runtime validates checks using the existing `core.checks.validate`, then
   executes them in a fresh candidate copy without model credentials, private
   sessions, or network access. Exit zero, PASS and no FAIL are required. Failed
   or invalid probes go back to the same Actor, before independent verification.
3. A separate review container receives the committed patch. Verifier retains
   the SAME BitFun session and private scratch across candidates. Only its
   `BITFUN_HOME` and `/tmp/verifier-scratch` persist; credential configuration
   (`BITFUN_USER_ROOT`) is recreated inside each container, not host-mounted.
4. Verifier publishes a candidate-bound terminal report. PASS requires positive
   evidence for every material requirement. FAIL returns reproducible defects
   to the original Actor. UNVERIFIED returns specific evidence requests to that
   Actor; runtime probes run again, then the same Verifier independently rechecks.
   Ordinary inspections receive no Actor self-checks. Only the next inspection
   after UNVERIFIED receives the latest check descriptions, reproducible commands
   and runtime PASS/FAIL classifications. Raw outputs, Actor summaries and private
   logs are excluded. Failed probes and normal BitFun segments preserve a pending
   request; a fresh Actor context resets it. Forced stall inspections receive no
   self-checks, matching the reference loop's stall-exit `_inspect` call.
5. The host observes native stream-json events and terminal report publication.
   A handoff is accepted only after tools settle. SIGINT asks BitFun to settle
   and persist the turn; a bounded forced kill is only a cleanup fallback.
6. Missing/invalid reports and CLI failures receive bounded same-session report
   recovery, not a fabricated UNVERIFIED verdict. Exhausted report recovery is
   an infrastructure error. Task-budget exhaustion is an incomplete run, with
   the latest committed patch still available for official grading.
7. Genuine identical action/observation cycles with no repository progress, or
   three identical candidate/probe/feedback rounds, trigger a stall-exit inspection
   BEFORE any context recovery or final stall exit. The runtime snapshots the
   current committed HEAD even if no Actor handoff exists or completion probes
   failed. The same model-private Verifier session may accept it immediately
   (`completed / stall_verifier_pass`). A dirty worktree is recorded and skipped:
   the runtime must not apply an older committed candidate's PASS to unfinished
   work. It neither commits nor discards the uncommitted changes.
   After FAIL, UNVERIFIED or a skipped inspection, one fresh Actor context may
   continue on the same repository within the shared budget. The factual handover
   includes previous candidates, completion checks, public Verifier reports and
   Actor tool actions/observations across calls. It reuses `core.actor.handoff_message`;
   the full record is also available to the Actor as `/logs/agent/actor-worklog.json`.
   The supplied config permits one model-role switch. A changed Verifier model
   gets a separate private session; the previous model's private history is not
   handed to the new Actor. If recovery is disabled or used up, the controller
   returns normally as `incomplete / stall_recovery_disabled` or
   `incomplete / stall_recovery_exhausted`, preserving HEAD for official grading.
8. Pier collects the final commit and runs the unchanged official tests in its
   normal independent environment. Official tests/results never enter feedback.

Verifier and probe writes to tracked candidate files invalidate the inspection.
Their containers are discarded; no review filesystem is copied into the Actor.
Untracked scratch is permitted. The lexical probe validator is a quality gate,
not a security sandbox; disposable containers enforce isolation from role state.

The stall sequence now follows `core.loop.run_attempt` / `run_with_resume`, used
by OSWorld and ALE: inspect, accept or resume, then return an ordinary task result
when recovery cannot continue. Coding-specific differences remain: inspection
requires a clean committed candidate, and handover facts are assembled directly
from native events instead of the reference's model-synthesized prose worklog.
The full factual record is saved; inline recovery context uses the reference
handoff's character limit. This change does not implement the reference's optional
zero-file-delta strategy post-mortem/pivot, GUI iteration thresholds or its
300-second minimum for starting a resume. Coding uses the shared work budget
and one-recovery limit. Fatal report/transport/container failures still
propagate as infrastructure errors and block grading.

## Budget and compatibility

The default remains 10200 seconds (2h50m) of SHARED working time, with Pier's
10800-second working-time limit leaving 600 seconds for cleanup. Actor, Verifier,
completion probes, ordinary inference and semantic/protocol repair share one
clock across native segments and fresh-context recovery. Initial task/session
preparation precedes that clock and has its own 300-second infrastructure timeout;
the initial Docker boot is already outside Pier's agent execution timer. Review
container setup during the work loop still counts, like the reference's nested
Verifier boundary setup. There is no normal Actor-round cap and no 600-second
whole-Verifier cap. `command_timeout_sec: 600` still bounds one completion probe.

Numeric ceilings are host-side watchdogs. Neither role receives remaining seconds,
an absolute deadline, or instructions to rush/leave time for another role. This
also applies to native segment continuation messages. Completion still depends
on evidence and independent review. Budget exhaustion returns an incomplete
candidate for the unchanged official grader, never an invented PASS.
The reference's generic BANK PARTIAL PROGRESS principle is retained verbatim:
finish achievable subparts, then revisit blockers. Its generic mentions of a
budget do not disclose a numerical ceiling or add new submission pressure.

`model_transport.py` runs a loopback relay inside each role's container. It retries
the unchanged HTTP request after recoverable connection/API failures, with the
reference's HTTP classification (401/402/403/408/409/425/429, 5xx). Like
`llm.client`, it first makes up to four calls with 2/4/6-second backoffs. A batch
that succeeds remains charged; an exhausted recoverable batch and its subsequent
30-second wait are excluded, matching `core.loop._call_with_transport_pause`.
HTTP 400/404/422 are returned as fatal request errors. HTTP-200 provider
error envelopes and truncated SSE responses are also recognized. Retries do not
add user messages, consume report recovery attempts, reset sessions, or swap
models. Egress still uses Pier's authenticated proxy and domain allowlist.

The relay buffers each model response before delivering it, so a broken stream
can be retried without exposing duplicate partial output/tool calls. SSE comments
keep the connection open; they contain no assistant content. This preserves the
successful response's bytes but delays streaming display and native early tool
dispatch until the response finishes. It is a transport adaptation, not byte-for-
byte equivalence with the native streaming loop. Native stream timeouts are
delegated to the relay; each upstream network read is bounded. As in the reference,
the work ceiling is checked at model-operation boundaries: an in-flight successful
request can overrun the ceiling, but its full duration is charged afterward.

`pier_budget.py` adapts the pinned Pier single-task `run_agent` timeout to this
same clock, including the existing cleanup margin. Other agent types use the
original Pier method. No installed Pier source is edited. Preflight rejects
multi-step tasks because their separate dispatch path is not adapted. Long service
outages can make actual elapsed time exceed three hours, as in the reference.

`report_recovery_attempts: 2` bounds protocol repair, not correctness iterations.
BitFun's own `finish_reason=max_rounds` (observed at 201 model rounds) is a
normal segment boundary: both roles resume the SAME session within the shared
work budget. Such segments do not consume invalid/missing-report recovery tries.
They are recorded as `actor_segment_continue` / `verifier_segment_continue`.
V1 keys `max_rounds`, `verifier_attempts`, `actor_timeout_sec` and
`verifier_timeout_sec` are rejected explicitly. Old experiment YAMLs and results
are preserved as V1 artifacts; use the new two-task config for V2. Native mode
is unaffected. Baseline disables BitFun's automatic final-change gate because
runtime completion checks plus independent review supply the baseline gates.

This baseline does not include Curriculum, BRS, DRS or learned-memory updates.
Its private continuation state is per-task session state, not RSI learned memory.

## Artifacts

```text
trial/
  agent/stream-<session>-<call>.jsonl   # Native Actor events
  agent/transport-<session>-<call>.jsonl # Request/pause timing only (no keys or bodies)
  agent/bitfun.txt                    # Readable Actor text and tool observations
  agent/handoffs/                     # Terminal Actor reports
  agent/checkpoint-*.json             # Last complete observation and usage
  agent/actor-worklog.json            # Full Actor-readable recovery facts
  artifacts/model.patch              # Officially collected committed patch
  verifier/reward.json               # Official score, NOT the RSI Verifier
  baseline/
    protocol.json                    # Source hashes, settings, key references
    result.json                      # Live status, final verdict, timings/usage
    events.jsonl                     # Phase, resume, feedback, recovery events
    candidates/round-*/               # Committed candidate and hash
    candidates/stall-*/               # Current HEAD snapshot at each stall
    checks/round-*/                   # Runtime probe outputs/results
    reviews/round-*/agent/            # Private Verifier events and trajectories
    reviews/stall-*/agent/            # Separate stall-inspection artifacts
    actor-recovery.json              # Host copy of factual Actor handover
    verifier-private/<session>/      # Persistent home and private scratch
```

`baseline/result.json` separates status/stop reason from the last valid verdict.
`stall_reviews` records the cause, candidate, report or skip reason for each stall
without overwriting ordinary round reviews. Inspection IDs are unique even when
two stalls occur before another Actor handoff. Each review records
`actor_evidence_disclosed`; `protocol.json` records the evidence and stall policies.
`budget.shared_work_sec`, `work_elapsed_sec`, `infra_pause_sec`, `infra_pauses`,
and `initial_setup_sec` distinguish the ceiling, charged work and excluded waits;
the outer `elapsed_sec` remains actual elapsed time. Old `shared_wall_sec` and
absolute deadline fields describe older runs and are no longer emitted.
`null` verdict means no accepted verdict for the current stage. A prior explicit
UNVERIFIED does not mean a later timeout completed successfully. `calls` and
`usage` include observed native events for both roles. Missing usage is `null`,
not zero; subagent/compression usage can remain incomplete. No price is invented
for gateway model names. Internal PASS is not an official score.

## Validation

```bash
PYTHONPATH=. LITELLM_LOCAL_MODEL_COST_MAP=True \
  .runtime/deepswe/venv/bin/python -m pytest -q tests/deepswe
PYTHONPATH=. LITELLM_LOCAL_MODEL_COST_MAP=True \
  .runtime/deepswe/venv/bin/python tools/smoke_deepswe_baseline.py \
  --name baseline-v2-smoke-001 --bind-host 169.254.30.1
```

The mock smoke uses real BitFun and Docker. It exercises UNVERIFIED -> Actor
evidence -> same Verifier FAIL -> Actor repair -> same Verifier PASS, executed
completion probes, private scratch continuity, role isolation, terminal handoffs,
native trajectories, observed token usage and the official grader. Its marker
patch is intentionally not a task solution, so official reward 0 is expected.
Mock branch coverage is not evidence of real-model correctness or paper parity.

V1 historical receipts remain under `results/deepswe/smoke/`:
`native-after-baseline-smoke-001` and `baseline-adapter-smoke-002`.
They validate the older protocol, not V2.

### H20 validation, 2026-11-03

- Final unit/regression suite: 127 passed locally and on H20.
- Final V2 integration: `results/deepswe/smoke/baseline-v2-smoke-20261103-02/`;
  all 16 receipt checks passed, including persistent review and evidence/repair.
- Native compatibility: `results/deepswe/smoke/native-after-baseline-v2-20261103-01/`;
  all 8 receipt checks passed. Both integration fixtures used a local mock model.
- Real two-task run: `results/deepswe/jobs/bitfun-two-tasks-baseline-v2-20261103-01/`;
  launched serially with the two-task YAML. Consult its live receipts for status;
  launch and mock success do not establish real task scores.
- The pre-V2 implementation is archived at
  `.runtime/deepswe/archives/baseline-v1-before-v2-20261103.tar.gz`.

The old remaining-19 baseline batch was stopped; its Scriggo trial was cancelled
without an official score, and the other 17 queued tasks did not run. The native
GLM batch remains stopped. Completed V1 baseline results were:

| Task | Official reward | F2P | P2P | Trial elapsed |
| --- | --- | --- | --- | --- |
| abs-module-cache-flags | 1 | 20/20 | 3/3 | 22m43s |
| dateutil-rfc5545-timezone-interop | 0 | 64/67 | 2035/2035 | 33m11s |
| fastapi-implicit-head-options | 1 | 43/43 | 3134/3134 | 36m05s |

All three V1 internal Verifiers exhausted their 600-second review budget without
a report and ended as UNVERIFIED. FastAPI's extra Actor invocation corrected
uncommitted work; it was not independently discovered-defect feedback. These
scores establish official grading, not successful Actor-Verifier repair.

### Real V2 observations

In `bitfun-two-tasks-baseline-v2-20261103-01`, dateutil finished without an
infrastructure error in 70m58s. Runtime completion probes passed and Qwen issued
a valid terminal PASS after about 42m14s of inspection (Actor about 26m57s).
Official reward was nevertheless 0: F2P 64/67 and P2P 2035/2035. Officially
collected `model.patch` and the inspected candidate were byte-identical, SHA256
`4cc40187e55c54566843ec069112295e814b2d587e0218bfc027d4525dbd0c0a`.

Post-evaluation diagnosis found two missed `repr` reconstruction cases and one
missed tzical TZID-preservation case. This is a false-positive internal review,
not a lost patch. These official failures were not supplied to either Agent.
The Verifier report also incorrectly described the Actor as not having committed:
it inferred this from its own staged-patch/base-HEAD review copy, despite the
prompt explaining that representation. The runtime independently verified the
Actor's actual committed HEAD and clean state. Treat that report statement as
a model interpretation error, not as the Actor's repository state.

The real DynamoDB run exposed a native BitFun `max_rounds` segment boundary.
The running snapshot successfully resumed the same Actor through its existing
missing-report recovery path. A subsequent source refinement classifies native
segments separately so repeated normal segments do not exhaust protocol recovery
tries. The two real trials keep their originally loaded snapshot; source hashes
in each trial's `baseline/protocol.json` identify that snapshot. The pre-refinement
source is retained in `.runtime/deepswe/archives/baseline-v2-real-run-20261103-01.tar.gz`.
The refined source adds dedicated segment-continuation regression tests; it does
not change the loaded real trials, their prompts, models or submitted patches.

Dateutil used one Actor candidate and one Verifier invocation, so it does not
establish real-model defect-feedback repair or cross-candidate review continuity.
Those branches are covered by the mock integration; the second real task is
tracked in the same batch's live receipts. A valid report is not proof of a
correct report, and this result does not establish benchmark-score improvement.
