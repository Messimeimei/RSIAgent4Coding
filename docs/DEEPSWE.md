# Native BitFun evaluation on DeepSWE

The default native integration runs BitFun's own coding loop through Pier's external-agent
interface. DeepSWE's pinned tasks, collect hooks and hidden tests are unchanged.
It does not run RSIAgent's process Verifier, BRS, DRS or memory updates.

For the optional Actor-Verifier mode, see [DEEPSWE_BASELINE.md](DEEPSWE_BASELINE.md).
Use `--mode baseline` to select it; existing commands keep their native behavior.

## Pinned runtime

`config/deepswe/versions.json` records DeepSWE, Pier, the upstream adapter and the
BitFun 0.2.15 Linux release with its official archive digest. Runtime binaries and
Python dependencies live in `.runtime/deepswe/` (ignored by Git). The official
dataset stays at the sibling `../DeepSWE` checkout.

The plugin is loaded using
`import_path: benchmarks.deepswe.bitfun_agent:BitfunCli`. No Pier source changes or
built-in agent registry edits are required. The YAML retains `name: bitfun-cli`
and the original `binary_path`, `output_patch_path` and `bitfun_config` arguments.

The model gateway uses port 8080. `BitfunDockerEnvironment` extends only Pier's
generated Squid policy to admit the configured `inference_ports`. The model host
allowlist and proxy authentication remain enforced. The separate official
verifier receives no model allowlist and stays fully offline.
BitFun 0.2.15 ignores system proxy detection, so the plugin also writes Pier's
proxy into BitFun's native `ai.proxy` configuration inside the container. Its
proxy password is removed from the archived app configuration.

## H20 commands

Run from `/data/k8s/yx/RSIAgent4Coding` using the isolated runtime's Python:

```bash
DEEPSWE_UV=/home/djn/.local/bin/uv \
DEEPSWE_PYTHON=/data/k8s/yx/OSWorld-V2/.venv/bin/python \
bash scripts/setup_deepswe.sh
.runtime/deepswe/venv/bin/python run_deepswe.py check
.runtime/deepswe/venv/bin/python run_deepswe.py run --name bitfun-dateutil-001
```

Setup creates a separate virtual environment; it only reads the existing Python
interpreter. The Pier checkout is detached at its recorded commit. Dependencies
come from that checkout's frozen `uv.lock`, with wheel hashes enforced. Set
`DEEPSWE_PACKAGE_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple` to use a mirror
without changing dependency versions or hashes. Setup never starts a task.

`check` reads the pinned task, verifies binary sources, constructs the plugin and
checks network-policy resolution. It makes no model calls and starts no container.
It reports missing credentials separately; `run` requires them. Supply
`DEEPSWE_API_KEY` in the shell or `.runtime/deepswe/runtime.env`, following
`config/deepswe/runtime.env.example`. Job snapshots retain only `${DEEPSWE_API_KEY}`.
The plugin resolves that reference inside the agent container. Never put the
literal key into the job YAML.

The default configuration selects only `dateutil-rfc5545-timezone-interop`.
`--task TASK_ID` selects one other task; editing `datasets[].task_names` selects a
batch. Paths in the YAML are relative to the control checkout, then made absolute
before Pier starts Docker. Always use a new job name.

## Ownership and results

1. Pier starts the task's pinned Linux image with `/app` as its working directory.
2. The host's BitFun/uv files are mounted read-only. The agent gets its own BitFun
   home and configuration, with only enabled model endpoints on Pier's allowlist.
3. BitFun receives `instruction.md`, edits/tests/commits the repository and exits.
4. The official `verifier.collect` exports `base_commit -> HEAD` to
   `artifacts/model.patch`. The adapter never auto-commits unfinished work.
5. Pier stops the agent environment, starts the separate verifier, applies the
   patch and official tests, and saves the official reward. Cleanup is Pier-owned.

Output lives in `results/deepswe/jobs/<name>/<trial>/`: `agent/trajectory.json`,
native `agent/bitfun/sessions` (including session-local `request-traces`),
`agent/bitfun/token_usage`, `artifacts/model.patch`,
`verifier/{reward.json,ctrf.json,test-stdout.txt,run.log}`, and `result.json`.
Full traces are private experiment artifacts. `agent/patch/fix.patch` is a
diagnostic snapshot from the upstream adapter and is NOT the submitted patch.

The task declares 2 CPUs, 8192 MB memory and 20480 MB storage. CPU/memory limits
are passed to Docker; the storage declaration is not an enforced overlay2 quota.
The 10800-second agent timeout remains Pier-owned. `retry.max_retries: 0` disables
job-level retries; the pinned Pier still has its own environment/verifier timeout
retry behavior, so use its timing and exception receipts when counting attempts.

`reward=0` with completed official tests is a valid evaluated failure, not an
adapter failure. A missing patch because the agent did not commit must remain
visible. A runtime test using a mock model validates plumbing, not coding ability.

## Unpaid integration check

```bash
PYTHONPATH=. .runtime/deepswe/venv/bin/python -m pytest -q tests/deepswe
PYTHONPATH=. .runtime/deepswe/venv/bin/python tools/smoke_deepswe_bitfun.py \
  --name bitfun-smoke-001 --bind-host 169.254.30.1
```

The bind address is H20's Docker bridge address; inspect it with
`docker network inspect bridge` on another machine. This test uses the real
BitFun binary, the real dateutil container and the unchanged official tests,
with a local deterministic model stub that creates a small committed file.
Its artifacts go under `results/deepswe/smoke/`, and `smoke-receipt.json` labels
the model as a mock. An official reward of zero is expected and is not a model
benchmark result. No provider credential is needed.

### Verified on H20 (2026-11-02)

- Unit/regression tests: 90 passed with the pinned runtime.
- Complete smoke: `results/deepswe/smoke/bitfun-adapter-smoke-004/smoke-receipt.json`,
  `status=passed`, all eight checks passed, no paid model calls.
- The real BitFun CLI executed `ExecCommand`, committed `adapter-smoke.txt`, and
  the official collect hook exported that commit. The separate grader logged
  `model.patch applied` and completed all tests.
- Expected mock outcome: reward 0; F2P 0/67; P2P 2035/2035.
- Earlier smoke receipts remain: 001 hit temporary ECR rate limiting; 002 exposed
  BitFun's explicit-proxy requirement; 003 exposed the verifier artifact mount
  boundary and the mock's outdated tool name. These are not model evaluations.
- No DeepSeek run was started. `DEEPSWE_API_KEY` is still required for that run.
