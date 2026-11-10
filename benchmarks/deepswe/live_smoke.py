"""Real-provider infrastructure smoke, explicitly separate from model evaluation."""

from __future__ import annotations

import json
import re
from uuid import uuid4

from .bitfun_agent import BitfunCli


class LiveSmokeBitfun(BitfunCli):
    def __init__(self, *args, smoke_token, **kwargs):
        if not re.fullmatch(r"[a-f0-9]{32}", smoke_token):
            raise ValueError("Invalid live smoke token")
        self.smoke_token = smoke_token
        super().__init__(*args, **kwargs)

    async def run(self, instruction, environment, context):
        prompt = (
            "This is a real-model infrastructure smoke test, not a benchmark attempt. "
            "Ignore the benchmark feature request. In /app, create exactly one new file "
            "named rsiagent-live-smoke.txt containing exactly this line and a final newline:\n"
            f"RSIAgent live model smoke: {self.smoke_token}\n"
            "Read the file back and verify its exact contents using Python. "
            "Do not modify any other file. Configure a local Git identity if needed, "
            "git add only this file, and commit it with message 'RSIAgent live model smoke'. "
            "Check git status is clean and finish. Use your execution tool now; "
            "do not merely describe these steps. Do not attempt the benchmark solution."
        )
        await super().run(prompt, environment, context)


def prepare_smoke(config, *, root):
    config.jobs_dir = root / "results/deepswe/smoke"
    for agent in config.agents:
        agent.import_path = "benchmarks.deepswe.live_smoke:LiveSmokeBitfun"
        agent.override_timeout_sec = 300
        agent.kwargs["smoke_token"] = uuid4().hex
        for model in agent.kwargs["bitfun_config"]["ai"]["models"]:
            model["max_tokens"] = 4096
            model["reasoning_mode"] = "disabled"
            model.pop("reasoning_effort", None)


def smoke_receipt(destination, result):
    trials = [path.parent for path in destination.glob("*/result.json")]
    checks = {"one_trial": len(trials) == 1, "no_infrastructure_errors": result.stats.n_errored_trials == 0}
    reward = None
    if len(trials) == 1:
        trial = trials[0]
        config = json.loads((destination / "config.json").read_text())
        token = config["agents"][0]["kwargs"]["smoke_token"]
        patch = trial / "artifacts/model.patch"
        patch_text = patch.read_text() if patch.is_file() else ""
        checks["committed_smoke_patch_collected"] = (
            "rsiagent-live-smoke.txt" in patch_text
            and f"+RSIAgent live model smoke: {token}" in patch_text
        )
        checks["trajectory_saved"] = (trial / "agent/trajectory.json").is_file()
        checks["native_session_saved"] = any((trial / "agent/bitfun/sessions").glob("*/metadata.json"))
        checks["model_request_traces_saved"] = any((trial / "agent/bitfun").rglob("request-*.json"))
        reward_path = trial / "verifier/reward.json"
        if reward_path.is_file():
            reward = json.loads(reward_path.read_text())
        checks["official_tests_executed"] = bool(reward and reward.get("p2p_total", 0) > 0)
        stdout = trial / "verifier/test-stdout.txt"
        checks["official_patch_applied"] = stdout.is_file() and "model.patch applied" in stdout.read_text()
    receipt = {
        "status": "passed" if all(checks.values()) else "failed",
        "validation_kind": "real_provider_bitfun_docker_official_tests",
        "is_benchmark_score": False,
        "expected_benchmark_reward": 0,
        "checks": checks,
        "official_reward": reward,
        "job_path": str(destination),
    }
    (destination / "smoke-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt
