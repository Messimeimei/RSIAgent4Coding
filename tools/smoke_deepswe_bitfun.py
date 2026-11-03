#!/usr/bin/env python3
"""Real BitFun + Docker + official tests, with a deterministic local model stub.

This checks evaluation plumbing. It deliberately does not solve the benchmark and
must never be reported as a real model score. No paid provider is contacted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ModelStub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests_seen = 0
    audit = []

    def log_message(self, *args):
        pass

    def json_reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.json_reply(
            200,
            {
                "object": "list",
                "data": [
                    {
                        "id": "bitfun-plumbing-mock",
                        "object": "model",
                        "owned_by": "local",
                    }
                ],
            },
        )

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        type(self).requests_seen += 1
        request_number = type(self).requests_seen
        if request_number > 20:
            self.json_reply(400, {"error": {"message": "Smoke request limit exceeded"}})
            return
        tools = [tool.get("function", tool) for tool in body.get("tools", [])]
        tool = next(
            (
                tool
                for tool in tools
                if tool.get("name", "").lower()
                in {"bash", "shell", "execute_bash", "run_command", "execcommand"}
            ),
            None,
        )
        results = [msg for msg in body.get("messages", []) if msg.get("role") == "tool"]
        type(self).audit.append(
            {
                "request": request_number,
                "path": self.path,
                "tools": [tool.get("name") for tool in tools],
                "tool_results": len(results),
            }
        )
        finish = "stop"
        message = {"role": "assistant", "content": "Smoke commit created and checked."}
        if tool is not None and not results:
            command = (
                "cd /app && printf 'BitFun adapter plumbing smoke\\n' > adapter-smoke.txt "
                "&& git add adapter-smoke.txt "
                "&& git -c user.name=Smoke -c user.email=smoke@example.invalid "
                "commit -m 'BitFun adapter plumbing smoke' && git status --porcelain"
            )
            properties = tool.get("parameters", {}).get("properties", {})
            arguments = {"cmd" if "cmd" in properties else "command": command}
            if "yield_time_ms" in properties:
                arguments["yield_time_ms"] = 10000
            for field in tool.get("parameters", {}).get("required", []):
                if field in arguments:
                    continue
                kind = properties.get(field, {}).get("type")
                arguments[field] = {
                    "boolean": False,
                    "integer": 60,
                    "number": 60,
                    "object": {},
                    "array": [],
                }.get(kind, "Runtime smoke")
            message = {
                "role": "assistant",
                "content": "Run the plumbing smoke command.",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"smoke-{request_number}",
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        usage = {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140}
        envelope = {
            "id": f"smoke-{request_number}",
            "created": int(time.time()),
            "model": body.get("model", "bitfun-plumbing-mock"),
        }
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for delta, stop in ((message, None), ({}, finish)):
                chunk = {
                    **envelope,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": stop}],
                }
                if stop:
                    chunk["usage"] = usage
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
        else:
            self.json_reply(
                200,
                {
                    **envelope,
                    "object": "chat.completion",
                    "choices": [
                        {"index": 0, "message": message, "finish_reason": finish}
                    ],
                    "usage": usage,
                },
            )


async def smoke(args, endpoint):
    from pier.job import Job
    from run_deepswe import load_job

    job_config = load_job(ROOT / "config/deepswe/dateutil-bitfun.yaml", name=args.name)
    job_config.jobs_dir = ROOT / "results/deepswe/smoke"
    agent = job_config.agents[0]
    agent.override_timeout_sec = 180
    agent.model_name = "bitfun-plumbing-mock"
    model_config = agent.kwargs["bitfun_config"]["ai"]
    model = model_config["models"][0]
    model.update(
        id=agent.model_name,
        name=agent.model_name,
        model_name=agent.model_name,
        base_url=endpoint,
        api_key="${DEEPSWE_SMOKE_KEY}",
        max_tokens=4096,
        reasoning_mode="disabled",
    )
    model.pop("reasoning_effort", None)
    model_config["default_models"] = {
        "primary": agent.model_name,
        "fast": agent.model_name,
    }
    job_config.environment.kwargs["inference_ports"] = [args.port]
    destination = job_config.jobs_dir / job_config.job_name
    if destination.exists():
        raise FileExistsError(destination)
    job = await Job.create(job_config)
    result = await job.run()
    trial_roots = [path.parent for path in destination.glob("*/result.json")]
    checks = {}
    if len(trial_roots) == 1:
        trial = trial_roots[0]
        patch = trial / "artifacts/model.patch"
        checks["committed_patch_collected"] = (
            patch.is_file() and "adapter-smoke.txt" in patch.read_text()
        )
        checks["atif_saved"] = (trial / "agent/trajectory.json").is_file()
        checks["native_session_saved"] = any(
            (trial / "agent/bitfun/sessions").glob("*/metadata.json")
        )
        checks["request_traces_saved"] = any(
            (trial / "agent/bitfun").rglob("request-*.json")
        )
        reward = trial / "verifier/reward.json"
        checks["official_reward_saved"] = reward.is_file()
        test_output = trial / "verifier/test-stdout.txt"
        checks["official_verifier_applied_patch"] = (
            test_output.is_file() and "model.patch applied" in test_output.read_text()
        )
        if reward.is_file():
            checks["official_tests_executed"] = (
                json.loads(reward.read_text()).get("p2p_passed", 0) > 0
            )
        checks["model_requests_received"] = ModelStub.requests_seen > 0
    passed = (
        bool(checks) and all(checks.values()) and result.stats.n_errored_trials == 0
    )
    receipt = {
        "status": "passed" if passed else "failed",
        "validation_kind": "real_bitfun_docker_official_tests_with_mock_model",
        "paid_model_calls": 0,
        "checks": checks,
        "mock_requests": ModelStub.audit,
        "job_path": str(destination),
    }
    (destination / "smoke-receipt.json").write_text(
        json.dumps(receipt, indent=2) + "\n"
    )
    print(json.dumps(receipt, indent=2), flush=True)
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument(
        "--bind-host", default="172.17.0.1", help="H20 Docker bridge address"
    )
    parser.add_argument("--port", type=int, default=0, help="0 selects an unused port")
    args = parser.parse_args()
    os.chdir(ROOT)
    os.umask(0o077)
    os.environ["DEEPSWE_SMOKE_KEY"] = "local-smoke-not-a-provider-key"
    with ThreadingHTTPServer((args.bind_host, args.port), ModelStub) as server:
        args.port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            return asyncio.run(smoke(args, f"http://{args.bind_host}:{args.port}/v1"))
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
