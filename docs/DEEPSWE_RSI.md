# Coding RSI reproduction

`run_deepswe.py --mode rsi` adds BRS, DRS and frozen-memory evaluation. Native
and baseline keep their own entry points/configurations. Default RSI configuration
is `config/deepswe/rsi-bitfun.yaml`: DeepSeek-V4.1-Flash-1017 Actor, separate
Qwen3.8-Flash Verifier and Curriculum sessions, `parallel: 2`.

## Reference mapping

| Stage | Reference implementation actually called | Coding adaptation |
| --- | --- | --- |
| BRS | `explore.phase1_wave.evolve_parallel_phase1` | Curriculum publishes original wave JSON and fixture directories through BitFun. Each project gets a new Docker environment and native Actor/Verifier sessions. |
| BRS memory | `_commit_branch_memory` | Every branch reads the same pre-wave bank. All branches must finish verification; the same branch Actors then distill and reconcile in project order against the latest canonical bank. Parked branches restore their exact candidates and native conversations. |
| DRS outer loop | `core.self_evolving_loop.run_self_evolving_loop` | New target container and Actor per target cycle. Existing baseline with a training-only FAIL exit supplies grounded reports; UNVERIFIED remains an evidence loop. |
| DRS learning | `explore.target_learning._promote_learning` | Restore canonical memory, resume the actual target Actor (including its actual model after recovery), distill, reconcile, diagnose, audit and atomically promote. |
| DRS curriculum | `explore.unified_evolution.evolve_until_ready` | Original `phase2_outcome_protocol`, `curriculum_review`, read-only disposable memory and persistent Curriculum context. No two-project cap. |
| DRS practice | Original `_run_practice_attempt` / `run_practice_with_evidence` | Independent Actor/Verifier with fresh replayed project trees; PASS/FAIL closes the project. UNVERIFIED preserves role identities and transports only validated Actor Done probes. |
| Phase 3 | Existing `BitfunBaseline.run`, via `MemoryBaseline(training=False)` | Untouched original Pier target container, fresh role sessions, frozen memory inventory for Actor retrieval. FAIL repairs as in baseline. Official Pier grading runs afterward. |

`parallel` is scheduling capacity, not prescribed wave width. Curriculum may
publish more than two projects; the scheduler runs at most two practice branches
at once. BRS retains the reference 8-project safety boundary **between complete
waves**; a wave may cross 8. It is not a convergence rule. Checkpoints are saved
at projects 0, 4 and 8. BRS SATURATED or complete-wave budget exhaustion admits DRS;
BRS STALLED/infrastructure/quarantine does not silently become saturation.

DRS learns from both grounded FAIL and PASS. Curriculum can choose a contrast
after PASS; any practice requires another fresh original-target attempt. A PASS
with zero new projects can finish training. Curriculum STALLED after practice
requires the original state machine's final fresh target test. Missing verdicts,
runtime faults and watchdog exhaustion never become PASS or Curriculum readiness.

## Prompt and practice boundaries

The original Curriculum, Actor, memory-distillation, memory-reconciliation and
diagnosis charters are called directly. Actor evidence/method blocks are shared
with the reference and coding baseline. Practice Verifier uses the complete
`agent_harness_verifier_charter`, replacing only its QEMU/effect-isolation/tool
transport paragraphs with the Docker/BitFun interface. Model messages still go
through `bitfun exec` and its native system/tools, as in the existing baseline;
this does not reproduce the reference system-message priority.

Curriculum receives the public task as a search direction. It creates diverse
prerequisite, variant, contrast or stress projects, not the unchanged target or
an operator-written fixed curriculum. Inputs may be synthetic data, an incomplete
interface, public source or a small repository. It cannot author the requested
solution, project-specific tests, evaluator commands or the desired memory lesson.
The Actor produces code; the Verifier independently chooses its checks.

The task image supplies `/app` public source and installed tools. Practice inputs
and outputs live in `/home/user/evolution_project`; wave fixtures are replayed
there byte-for-byte with manifests. A project requiring source modifications must
place that source in its own tree. Work elsewhere is not a practice deliverable.
Each environment reset creates a fresh compose project. Only immutable BitFun/uv
binaries and that environment's logs are mounted. Host results, official graders,
previous native/baseline patches and other roles' state are not mounted.

Verifier sees only the replayed candidate and, for DRS, original fixtures. It
uses private scratch and cannot change submitted bytes without invalidating the
inspection. Its environment is discarded. This is the coding baseline's separate
container approach, not a full QEMU live-process checkpoint. Candidate tests must
work from replayable files. Native session archives are restored only to the
owning role; credentials/configuration are recreated separately.

## Memory, time and artifacts

Only same-experience Actor learning phases promote memory. Work-phase edits and
Curriculum edits are discarded. Verifier receives no memory. BRS and DRS frozen
banks have hash manifests; Phase 3 checks that host frozen memory stayed unchanged.
Only inventory names/sizes enter the Actor opening, never host-selected memory
contents. The Actor chooses retrieval. This run is target-conditioned learning
followed by fresh evaluation of the same target, **not a held-out transfer test**.

Final baseline work budget remains 10200 seconds. DRS target attempts use the
reference 36000-second ceiling; learning roles use 86400 seconds and 2000 model
rounds per phase. These are undisclosed safety bounds, not semantic stop signals.
Native 200-round segments resume the same conversation. Confirmed recoverable
model transport waits are excluded by the existing relay/working clock. The
RSI-only Pier adapter bypasses the original one-task three-hour global timeout;
DRS retains no global project/time cap. Native/baseline clocks are unchanged.

Under each trial, `rsi/` contains:

- `protocol.json`, `events.jsonl`, `result.json`: source hashes, roles, transitions.
- `phase1/`: original wave decisions, fixtures, candidates, outcomes, ordered
  memory journal, checkpoints and `memory_frozen`.
- `phase2/`: fresh target cycles, terminal learning, evolution cycles, journals
  and `memory_frozen`.
- `phase3/`: baseline candidates, independent reviews and final result.
- `runtime/environments/`: native streams, request traces and environment logs.
- `runtime/private-sessions/`: owning-role native conversation archives for
  cross-container continuation. Never mounted into another role.

Native checkpoint failures are recovered in place as described below. A stopped
controller can also resume from a fully committed BRS wave using `--resume-from`
(see below). Arbitrary partial-wave, DRS and Phase 3 process-death recovery is not
implemented. Recovery always creates a new job; the old result remains unchanged.
Usage traces cover all phases; Pier's aggregate token fields are left unknown
instead of incorrectly reporting Phase 3 consumption as total RSI consumption.

The coding evidence adapter reports actual BitFun model-round counts to the
reference practice evidence loop. Confirmed provider pauses are subtracted from
that loop's cumulative role clocks as well as the current CLI invocation. The
optional hook defaults to absent for existing OSWorld/ALE callers.

Validation includes the coding suite, reference BRS/DRS transitions, prompt
preservation, same-Actor memory, freeze boundaries and evidence/memory recovery.
Live Docker/model execution still requires a host shell with Docker access.

## Verifier cache isolation and recovery

Coding practice Verifiers receive `PYTHONDONTWRITEBYTECODE=1`, a scratch
`PYTHONPYCACHEPREFIX`, `PYTEST_ADDOPTS` selecting a scratch cache directory,
`XDG_CACHE_HOME`, and `HYPOTHESIS_STORAGE_DIRECTORY` under `/tmp/verifier-scratch`.
Other build/test outputs must use
scratch copies. These settings apply only to RSI practice Verifier sessions.

Before each inspection, the controller records full candidate and original-fixture
inventories in `inspection_NNN/integrity-before.json`. After the native call has
settled, it compares paths, file bytes and link targets. It may remove only newly
created regular CPython bytecode with a corresponding original source file and
recognized pytest metadata. Hypothesis storage is redirected rather than exempted
from the integrity check. It rechecks the complete tree before and after
cleanup. No existing cache, changed/missing source, unknown addition, symlink or
special file is removed or ignored. Any such change rejects the inspection and
preserves the environment for diagnosis. The original reference snapshot check
still runs afterward without relaxed exclusions.

Git inspection can refresh `.git/index` stat-cache metadata even when it leaves
the staged tree unchanged. The pre-inspection inventory saves bounded regular
index files. Recovery permits index drift only when all other protected files,
fixtures, HEAD, refs and objects are unchanged (apart from the recognized new
caches above), both old and new indexes have no staged diff against HEAD, and
their staged entries, modes and assume-unchanged/skip-worktree flags match.
Git checks use temporary index copies with optional locks and fsmonitor disabled.
Recovery restores the original index bytes before the strict reference snapshot
check. Missing snapshots, Git errors, staged changes and unknown `.git` changes
fail closed. `git_index_metadata_drift` and `restored_git_indexes` record the
repair, and `VERIFIER_GIT_INDEX_RECOVERED` records the event.

`integrity.json` records the before/after inventories, changed/missing/unexpected
paths, removed caches and final equality. Cache recovery does not call the model
again or alter its PASS/FAIL/UNVERIFIED report. `tools/rsi_recovery.py inspect`
includes these records. This recovery operates on a live inspection; old stopped
jobs are not automatically resumed or retroactively accepted.

An unrecoverable BRS branch failure now aborts other coding branches and cancels
queued work, then settles their native processes before environment cleanup.
It records `RSI_BRANCHES_ABORTED`, never commits a partial wave, and leaves the
original VM runtime's scheduling policy unchanged.

## Native checkpoint diagnostics and recovery

The role's `outcome-NNN.json` (accepted handoff, report, identity and usage) and
transcript are written **before** copying or archiving native state. Archive
recovery never reruns the completed model invocation, changes a verdict or
promotes memory. Native 200-round exhaustion still continues the same session
under the cumulative 2000-round/86400-second role limits; missing-envelope
retries are a separate limit.

Each checkpoint first tries GNU tar twice, then a Python copy of stable regular
files into a private temporary snapshot. Changing or unsafe source trees fail
closed. Each failure records the operation, exit code, bounded stderr/stdout or
exception in `rsi/runtime/private-sessions/<session-id>.events.jsonl`, with known
credentials redacted. Disk/inode availability and process names are collected
after a failed batch. Log copy failures are recorded and retried separately;
native session state remains authoritative.

Downloads go to temporary files and must pass gzip CRC, safe tar member and
native session metadata checks. Each valid archive has a unique filename; an
fsynced, atomically replaced `<session-id>.json` manifest is the commit point.
The prior checkpoint is not overwritten by a failed transfer. Restore validates
hash, size, session, role, call number and working directory before upload and
extracts into a staged directory before installing native home. Only the live
target Actor's first learning call can adopt its existing unarchived session.

After three failed attempts, the live controller holds the exact same container,
workspace and accepted completion. It retries every 60 seconds for up to
`checkpoint_repair_timeout_sec` (default 3600), without spending role work time
or calling the model again. Operators can inspect and request an earlier retry:

```bash
.runtime/deepswe/venv/bin/python tools/rsi_recovery.py inspect <trial-directory>
.runtime/deepswe/venv/bin/python tools/rsi_recovery.py retry <trial-directory> \
  --session <session-id>
```

`inspect` shows the exact environment/compose project, heartbeat, failed stage
and whether the last good archive covers the current call. Fix the reported
storage or transfer problem, then use `retry` only for a live
`waiting_for_checkpoint_repair` controller. This command signals the controller;
it never executes a shell command or starts a new model turn.

If the repair window expires, the run ends as infrastructure failure and retains
the affected experiment container for inspection, instead of deleting the only
copy of the workspace. Such preserved containers need explicit per-project
cleanup after investigation; never use global Docker cleanup. A dead controller
cannot be resumed by the retry command. This recovery scope covers checkpoint
faults during a live job, not arbitrary host/controller loss.

Validation with a local mock endpoint, real BitFun and real Docker (no paid model):

```bash
.runtime/deepswe/venv/bin/python tools/smoke_deepswe_rsi_recovery.py \
  --name rsi-recovery-smoke-unique
```

It sends a prompt larger than Linux's per-argument limit, injects two tar failures,
validates the fallback archive, destroys the first test container and constructs
a new controller with no in-memory sessions. It restores the same native
conversation in a fresh container and checks that previous context survived
without replaying the completed turn. All endpoints use the local model stub.

## Stopped BRS recovery and long prompts

The dateutil job `bitfun-dateutil-rsi-20261106-p2-08` completed six projects and
their ordered memory commits in wave 1. Wave 2's 126667-byte prompt became an
oversized shell argument after nested quoting. Docker could not even start the
native invocation: `OSError: [Errno 7] Argument list too long: 'docker'`.
This failure was unrelated to the native model-round limit.

Native, baseline and RSI now upload UTF-8 prompt files and invoke
`bitfun exec ... < /tmp/rsiagent-bitfun/prompt-....txt`. Prompt text is never a
shell argument or a `$(cat ...)` substitution. Quotation marks, newlines and
Unicode reach stdin unchanged; increasing memory/feedback size no longer grows
the Docker command. This removes the OS argument limit, not the model's context
window. Launch errors are also retained in role outcomes and higher-level faults;
`inspect` includes legacy environment checkpoint errors.

Check eligibility without models, containers or source writes:

```bash
trial=results/deepswe/jobs/bitfun-dateutil-rsi-20261106-p2-08/dateutil-rfc5545-timezone-intero__dkSrfxA
.runtime/deepswe/venv/bin/python tools/rsi_recovery.py resume-check "$trial"
.runtime/deepswe/venv/bin/python run_deepswe.py check --mode rsi \
  --resume-from "$trial" --name bitfun-dateutil-rsi-resumed-01
```

To start the reviewed recovery, replace `check` with `run`:

```bash
.runtime/deepswe/venv/bin/python run_deepswe.py run --mode rsi \
  --resume-from "$trial" --name bitfun-dateutil-rsi-resumed-01
```

The source trial must be finished with a BRS infrastructure error. The validator
checks task checksum, protocol/budgets, complete-wave events, ordered PASS/FAIL
outcomes, the memory commit chain, candidate/fixture manifests and the accepted
Curriculum native archive (gzip CRC, members, hash, identity, role, cwd and call).
If a later Curriculum invocation exists, this recovery version accepts only a
proven pre-launch Docker E2BIG fault with no native events or executed tools.
Zero observed usage alone is insufficient. Partial project learning, a started
next wave, quarantine, missing/corrupt archives and later phases are rejected.

Recovery copies the saved BRS artifacts and native archives into the new trial,
selects the checkpoint belonging to the last accepted Curriculum handoff, and
archives the failed pending wave inside the new copy. It writes `rsi/resume.json`
with provenance. Models, mounts, budgets and context policy come from the old
job's recorded configuration; `--task` and `--config` overrides are rejected.
The old failure and all its files are retained.

For p2-08 this preserves six completed projects and eight memory files (120222
bytes), restores Curriculum checkpoint call 2 instead of the failed call 3, and
continues with wave 2. The total project boundary stays 8; it is not reset to 8
additional projects. The original containers are unnecessary: the next wave
already starts in a clean environment by design, and all required state at this
boundary is stored on the host. A conversation log alone would not reconstruct
unarchived workspace edits, running processes or an incomplete memory commit.
This is explicit boundary recovery, not general restoration from any failure.

Native `ExecCommand` can finish its tool invocation while its command continues
in the background. Its result then has `completion: null`, `exit_code: null`
and a resumable command `session_id`. The event journal treats this as running
and clears stagnation signatures; it does not interpret it as command success,
task completion or an infrastructure error. This fixes the p2-06 `NoneType.get`
crash, reproduced by replaying the saved Verifier stream offline.

BRS branch exceptions are saved in `phase1/episodes/epXXX/branch_exception.json`
with project identity, stage, error type and traceback. A bounded infrastructure
retry archives its first receipt under `infra_attempts/attempt_001`; a failed
retry gets its own receipt. Malformed lifecycle-hook returns are infrastructure
errors naming the hook. Ordinary project PASS/FAIL remains learning evidence.
An unrecoverable branch exception emits `RSI_BRANCHES_ABORTED`, cancels sibling
native calls and prevents partial-wave memory promotion. No software exception
is turned into a candidate FAIL or a successful phase transition.

## Run

```bash
.runtime/deepswe/venv/bin/python run_deepswe.py check --mode rsi \
  --task dateutil-rfc5545-timezone-interop --name bitfun-dateutil-rsi-20261104-p2-01

nohup bash .runtime/deepswe/launchers/bitfun-dateutil-rsi-20261104-p2-01.sh \
  > .runtime/deepswe/logs/bitfun-dateutil-rsi-20261104-p2-01.console.log 2>&1 < /dev/null &
```

The launcher uses existing runtime credentials without printing them. It creates
only this new job. No GPU is requested and no production service is restarted.

## Infrastructure audit, 2026-11-06

The p2-07 Curriculum published six projects and archived its native session.
The first parallel Actor environment startups then failed with Docker's
`networks have overlapping IPv4`. No Actor verdict or official score existed.
The initial fix serialized network creation/removal across its controllers with
an advisory lock, and set `COMPOSE_PARALLEL_LIMIT=1` so a project's two proxy
networks were also created sequentially. This changes environment setup only;
Actor work still runs with `parallel: 2`.

That was insufficient: `bitfun-dateutil-rsi-resumed-01` later failed at
`reset_for_verifier` after three overlap errors. All three attempts delegated
subnet selection to Docker's automatic IPAM. Serializing and repeating the same
definition did not guarantee a different, conflict-free subnet. The two network
IDs from that failure had already been removed; their historical subnets cannot
be reconstructed from the old logs. Do not attribute this to model round limits
or claim a proven Docker daemon corruption.

The adapter now assigns explicit IPAM for used, project-owned automatic bridge
networks. Under the same lifecycle lock it reads existing Docker subnets, every
IPv4 host routing table and interface addresses, then selects disjoint `/24`s
from private `10.0.0.0/8`. Each network has up to 253 container addresses. A
run-local `docker-compose-network-ipam.json` overrides only subnet allocation;
it preserves Pier's internal bridge, egress proxy, authentication and allowlist.
Actor/Curriculum/Verifier environments all use this adapter. The official grader
and completion probes with `network_mode: none` allocate no network. Explicit
task IPAM, externally named/shared networks and non-bridge drivers are retained.

An overlap caused by another Docker client between inventory and creation gets
scoped teardown, a fresh inventory and **different subnets**: failed allocations
are excluded from the next attempt. Existing networks belonging to the same
project are reused for an idempotent `up`. Inventory failure or exhausted safe
address space stops before `up`; it never falls back to automatic allocation.
This automatic allocation currently requires a local Unix-socket Docker daemon
and host `ip -j` support, because remote host routes cannot be verified locally.

`docker-lifecycle.jsonl` now records each plan, occupied subnets, network IDs,
names and owners, exclusions on retry and collision IDs when Docker reports
them. Resolved Compose config and proxy credentials are not logged. No global
network pruning, daemon restart or production service modification is involved.

An `up` network-overlap error gets at most three attempts. Every failed attempt
first tears down only its unique compose project. Other startup errors are not
blindly retried. Cancellation/timeout terminates and reaps the Docker CLI process
group before scoped cleanup. No daemon configuration, unrelated network, shared
image or production service is changed. Ordinary BitFun `exec` retains the role's
working clock rather than inheriting the lifecycle command's timeout.

Cleanup attempts `down` even if host-log permission adjustment fails. It retries
idempotent teardown once, retains environment/config handles on failure, attempts
all owned environments, and preserves the original exception alongside cleanup
errors. Preserved checkpoint/integrity workspaces cannot be overwritten by reset.
Final Pier Actor/grader cleanup follows the same policy and never uses `--rmi`.
Failure while parking a completed BRS branch also aborts siblings without promoting
partial-wave memory. Ordinary project FAIL continues into learning.

`docker-lifecycle.jsonl`, `cleanup-errors.jsonl`, `runtime/operation-errors.jsonl`,
`phase1/phase_exception.json`, branch receipts and top-level `rsi/result.json`
provide the operation and traceback. `tools/rsi_recovery.py inspect <trial>`
aggregates these with checkpoint/integrity incidents and the official result, if
one exists. Optional null/malformed telemetry no longer crashes orchestration;
unavailable usage stays unknown. Native execution errors retain redacted details.
Malformed provider responses enter transport recovery rather than raising a
parser exception. Publication retries share each phase's emergency work allowance;
a new learning phase receives its own allowance.

Validation:

- 482 related tests passed; one host Hypothesis test skipped because that package
  is absent from the host venv. Real Hypothesis ran in the Docker recovery smoke.
- `results/deepswe/smoke/rsi-recovery-audit-20261106/smoke-receipt.json`: two injected
  tar failures, stable fallback, native context restoration in a fresh container,
  pytest/Hypothesis cache isolation, Git index metadata repair, no verdict replay.
- `results/deepswe/smoke/rsi-full-lifecycle-20261106-c/smoke-receipt.json`: three BRS
  projects with concurrency two (PASS/FAIL/PASS), ordered same-Actor memory,
  DRS target FAIL, one practice with UNVERIFIED evidence recovery, fresh target
  PASS, post-PASS learning/Curriculum, frozen memory in a fresh final Actor,
  committed patch collection and official tests. All model endpoints were local
  stubs; this verifies plumbing and is not a dateutil model score.

The full smoke created 33 compose environments. One actual network-overlap error
occurred during a DRS review startup; scoped teardown and the second startup
attempt recovered it, and the remaining phases completed. The official test
container applied the final smoke patch and ran 67 F2P plus 2035 P2P tests (all
P2P passed; the marker-only smoke intentionally does not implement the F2P task).
This earlier successful retry did not establish that automatic IPAM would
recover every time; the resumed job above demonstrated that limitation.

The dedicated IPAM smoke injects a real Docker conflict by creating its own
competing network after subnet selection, verifies a new subnet is selected,
then repeatedly creates and removes parallel proxy environments. It checks
proxy HTTP access, blocked direct access, unchanged `none` isolation, idempotent
startup and scoped cleanup. It uses no model calls:

```bash
PYTHONPATH=. LITELLM_LOCAL_MODEL_COST_MAP=True \
  .runtime/deepswe/venv/bin/python tools/smoke_deepswe_network.py \
  --name rsi-network-unique --rounds 12 --parallel 3
```

IPAM validation on 2026-11-06:

- 475 related tests passed, one host Hypothesis test skipped; real Hypothesis
  passed inside the recovery smoke's Docker container.
- `results/deepswe/smoke/rsi-network-ipam-20261106-01/smoke-receipt.json`:
  real collision recovered on attempt two, 12 rounds at concurrency three,
  38 environments total; all isolation/cleanup checks passed. Only the injected
  collision occurred. Preexisting networks and the protected production
  container's identity, start time and running state were unchanged.
- `results/deepswe/smoke/rsi-network-ipam-final-20261106-01/smoke-receipt.json`:
  final implementation rechecked with collision injection and concurrency two,
  after separating JSON stdout from CLI warnings.
- `results/deepswe/smoke/rsi-ipam-recovery-20261106-01/smoke-receipt.json`:
  large prompt, two archive failures, new-controller/new-container native
  session resume and Verifier integrity/cache recovery all passed with the
  explicit IPAM adapter. All model responses came from a local stub.

These checks address the repeated subnet-selection failure, not every possible
Docker/host failure. The failed resumed job remains stopped. Its partial second
wave is not eligible for the existing complete-wave `--resume-from` protocol;
do not silently replay it or discard already completed Actor work.

Reproduce the full lifecycle smoke with a new name:

```bash
PYTHONPATH=. LITELLM_LOCAL_MODEL_COST_MAP=True \
  .runtime/deepswe/venv/bin/python tools/smoke_deepswe_rsi.py \
  --name rsi-full-lifecycle-unique
```

These recoveries do not implement arbitrary host-process crash continuation.
Restart a dead controller into a new job directory. Native BitFun context
compression still occurs; archive recovery preserves the native session but does
not undo its semantic compression. Emergency budget exhaustion remains an
infrastructure stop, not a fabricated PASS or convergence decision.
