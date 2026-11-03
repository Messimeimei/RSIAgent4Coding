# BitFun adapter source

- Repository: https://github.com/openBitFun/bitfun_harbor_fork
- Commit: `7ad62874136f489c88ce25dfea40cbca3d8403f3`
- Source: `src/harbor/agents/installed/bitfun_cli.py`
- License: Apache-2.0, reproduced in `LICENSE`.
- Port changes: `harbor.*` imports changed to `pier.*`; the built-in
  `AgentName.BITFUN_CLI` enum reference became the literal `bitfun-cli`.

The public plugin in `../bitfun_agent.py` supplies the Pier-specific install,
network, secret configuration, process lifetime, and artifact collection behavior.
The upstream session/turn/snapshot and ATIF conversion remains here so it can be
compared against the pinned upstream implementation.

`tests/deepswe/test_telemetry.py` ports the upstream test helpers and telemetry
test classes from the same commit's `tests/unit/agents/installed/test_bitfun_cli.py`.
Imports are redirected to Pier and a concrete test subclass supplies its abstract
install hook. Both Python files were formatted with Ruff after porting.

The upstream diagnostic `agent/patch/fix.patch` includes uncommitted work. It is
never submitted to DeepSWE. Only the official task's collect hook creates
`artifacts/model.patch` from the task base commit to the agent's committed HEAD.
