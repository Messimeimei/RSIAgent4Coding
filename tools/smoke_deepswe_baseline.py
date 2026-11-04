#!/usr/bin/env python3
"""Unpaid real-CLI test of persistent review, evidence, repair and grading."""

import argparse
import asyncio
import copy
import json
import os
import re
import shlex
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.smoke_deepswe_bitfun import ModelStub  # noqa: E402


class BaselineStub(ModelStub):
    audit = []
    requests_seen = 0

    def do_POST(self):
        try:
            self.handle_model_post()
        except (BrokenPipeError, ConnectionResetError):
            # Terminal handoff intentionally cancels an in-flight next model call.
            self.close_connection = True

    def handle_model_post(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        type(self).requests_seen += 1
        number = type(self).requests_seen
        if number > 100:
            self.json_reply(
                400, {"error": {"message": "Baseline smoke request limit exceeded"}}
            )
            return
        control = None
        last_user = -1
        for index, message in enumerate(body.get("messages", [])):
            if message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            matches = re.findall(r"BASELINE_CONTEXT: (\{[^\n]+\})", str(content))
            if matches:
                control = json.loads(matches[-1])
                last_user = index
        tools = [tool.get("function", tool) for tool in body.get("tools", [])]
        tool = next((t for t in tools if t.get("name") == "ExecCommand"), None)
        results = [
            m
            for m in body.get("messages", [])[last_user + 1 :]
            if m.get("role") == "tool"
        ]
        self.audit.append(
            {"request": number, "control": control, "tool_results": len(results)}
        )
        message = {
            "role": "assistant",
            "content": "The requested smoke work is complete.",
        }
        finish = "stop"
        if control and tool and not results:
            if control["role"] == "actor":
                content = "initial" if control["round"] < 3 else "revised"
                probe_script = (
                    "from pathlib import Path; "
                    f"assert Path('/app/baseline-smoke.txt').read_text().strip() == {content!r}; "
                    "print('PASS')"
                )
                handoff = {
                    "kind": "actor_done",
                    "handoff_id": control["handoff_id"],
                    "final": True,
                    "candidate_head": None,
                    "summary": "Controlled implementation/evidence smoke",
                    "checks": [
                        {
                            "desc": "Marker matches committed candidate",
                            "probe": "cd /app && python3 -c "
                            + shlex.quote(probe_script),
                        }
                    ],
                }
                script = (
                    "from pathlib import Path; import json, subprocess; "
                    "assert not Path('/tmp/verifier-scratch/canary.txt').exists(); "
                    f"Path('/app/baseline-smoke.txt').write_text({content!r} + '\\n'); "
                    "subprocess.run(['git','add','baseline-smoke.txt'],check=True); "
                    "subprocess.run(['git','-c','user.name=Smoke','-c','user.email=smoke@example.invalid','commit','--allow-empty','-m','baseline smoke'],check=True); "
                    f"data = {handoff!r}; "
                    "data['candidate_head'] = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(); "
                    f"path = Path({control['report_path']!r}); path.parent.mkdir(parents=True,exist_ok=True); "
                    "temp=path.with_suffix('.tmp'); temp.write_text(json.dumps(data)); temp.replace(path)"
                )
                command = "cd /app && python3 -c " + shlex.quote(script)
            else:
                round_number = control["round"]
                verdict = {1: "UNVERIFIED", 2: "FAIL", 3: "PASS"}[round_number]
                report = {
                    "kind": "verifier_report",
                    "handoff_id": control["handoff_id"],
                    "final": True,
                    "candidate_id": control["candidate_id"],
                    "verdict": verdict,
                    "report": "Controlled independent review " + str(round_number),
                    "findings": [
                        "Replace the initial marker with revised and commit it"
                    ]
                    if verdict == "FAIL"
                    else [],
                    "evidence": ["Read baseline-smoke.txt and checked its contents"],
                    "evidence_requests": ["Provide a reproducible marker probe"]
                    if verdict == "UNVERIFIED"
                    else [],
                }
                script = (
                    "from pathlib import Path; import json; "
                    f"assert Path('/app/baseline-smoke.txt').read_text().strip() == {('initial' if round_number < 3 else 'revised')!r}; "
                    "scratch=Path('/tmp/verifier-scratch/canary.txt'); "
                    + (
                        f"assert scratch.read_text() == '{round_number - 1}'; "
                        if round_number > 1
                        else ""
                    )
                    + f"scratch.write_text('{round_number}'); "
                    + f"path=Path({control['report_path']!r}); path.parent.mkdir(parents=True,exist_ok=True); "
                    + f"temp=path.with_suffix('.tmp'); temp.write_text(json.dumps({report!r})); temp.replace(path)"
                )
                command = "python3 -c " + shlex.quote(script)
            message = {
                "role": "assistant",
                "content": "Execute the controlled smoke step.",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"baseline-smoke-{number}",
                        "type": "function",
                        "function": {
                            "name": "ExecCommand",
                            "arguments": json.dumps(
                                {"cmd": command, "yield_time_ms": 10000}
                            ),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        elif control and results:
            # Keep the natural CLI turn open so host-side terminal-publication
            # supervision, not an immediate text-only exit, ends the invocation.
            time.sleep(5)
        envelope = {
            "id": f"baseline-smoke-{number}",
            "created": int(time.time()),
            "model": body.get("model"),
        }
        usage = {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140}
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

    config = load_job(
        ROOT / "config/deepswe/baseline-bitfun.yaml", name=args.name, mode="baseline"
    )
    config.jobs_dir = ROOT / "results/deepswe/smoke"
    agent = config.agents[0]
    agent.override_timeout_sec = 600
    agent.model_name = "baseline-mock"
    agent.kwargs.update(
        report_recovery_attempts=1,
        command_timeout_sec=30,
        total_timeout_sec=480,
    )
    for key in ("bitfun_config", "verifier_bitfun_config"):
        value = copy.deepcopy(agent.kwargs[key])
        ai = value["ai"]
        model = ai["models"][0]
        model.update(
            id="baseline-mock",
            name="baseline-mock",
            model_name="baseline-mock",
            base_url=endpoint,
            api_key="${DEEPSWE_SMOKE_KEY}",
            max_tokens=4096,
            reasoning_mode="disabled",
        )
        model.pop("reasoning_effort", None)
        ai["default_models"] = {"primary": "baseline-mock", "fast": "baseline-mock"}
        agent.kwargs[key] = value
    config.environment.kwargs["inference_ports"] = [args.port]
    destination = config.jobs_dir / config.job_name
    if destination.exists():
        raise FileExistsError(destination)
    job = await Job.create(config)
    result = await job.run()
    trials = [p.parent for p in destination.glob("*/result.json")]
    checks = {}
    if len(trials) == 1:
        trial = trials[0]
        summary_path = trial / "baseline/result.json"
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        rounds = summary.get("rounds", [])
        checks["evidence_then_repair_then_pass"] = [
            r.get("verdict") for r in rounds
        ] == ["UNVERIFIED", "FAIL", "PASS"]
        checks["runtime_completion_checks_passed"] = len(rounds) == 3 and all(
            r.get("completion", {}).get("passed") for r in rounds
        )
        checks["review_did_not_mutate_actor"] = bool(rounds) and all(
            r.get("actor_unchanged_after_review") for r in rounds
        )
        events_path = trial / "baseline/events.jsonl"
        events = (
            [json.loads(line) for line in events_path.read_text().splitlines()]
            if events_path.exists()
            else []
        )
        starts = [e for e in events if e["event"] == "actor_start"]
        checks["actor_session_resumed"] = (
            len(starts) == 3
            and len({e["session_id"] for e in starts}) == 1
            and not starts[0]["resume"]
            and all(e["resume"] for e in starts[1:])
        )
        patch = trial / "artifacts/model.patch"
        text = patch.read_text() if patch.exists() else ""
        checks["revised_commit_submitted"] = (
            "baseline-smoke.txt" in text and "+revised" in text
        )
        checks["private_verifier_changes_not_submitted"] = "canary" not in text
        checks["actor_trajectory_saved"] = (trial / "agent/trajectory.json").is_file()
        reviews = list((trial / "baseline/reviews").glob("*/agent/trajectory.json"))
        checks["all_verifier_trajectories_saved"] = len(reviews) == 3
        private_ids = [json.loads(p.read_text())["session_id"] for p in reviews]
        checks["same_verifier_across_candidates"] = (
            len(private_ids) == 3 and len(set(private_ids)) == 1
        )
        checks["role_sessions_are_distinct"] = (
            len(set(private_ids + [summary.get("actor_session_id")])) == 2
        )
        review_starts = [e for e in events if e["event"] == "verifier_start"]
        checks["verifier_resumed_across_candidates"] = len(review_starts) == 3 and all(
            e["resume"] for e in review_starts[1:]
        )
        scratch = list(
            (trial / "baseline/verifier-private").glob("*/scratch/canary.txt")
        )
        checks["private_scratch_survived"] = (
            len(scratch) == 1 and scratch[0].read_text() == "3"
        )
        calls = summary.get("calls", [])
        checks["terminal_handoffs_detected"] = len(calls) == 6 and all(
            c["status"] == "handoff" for c in calls
        )
        checks["both_roles_have_observed_usage"] = all(
            summary.get("usage", {}).get(role, {}).get("input_tokens", 0)
            for role in ["actor", "verifier"]
        )
        reward = trial / "verifier/reward.json"
        checks["official_tests_executed"] = (
            reward.is_file() and json.loads(reward.read_text()).get("p2p_passed", 0) > 0
        )
        stdout = trial / "verifier/test-stdout.txt"
        checks["official_patch_applied"] = (
            stdout.is_file() and "model.patch applied" in stdout.read_text()
        )
    passed = (
        bool(checks) and all(checks.values()) and result.stats.n_errored_trials == 0
    )
    receipt = {
        "status": "passed" if passed else "failed",
        "validation_kind": "real_bitfun_actor_resume_isolated_review_official_tests_with_mock_model",
        "paid_model_calls": 0,
        "checks": checks,
        "requests": BaselineStub.audit,
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
    parser.add_argument("--bind-host", default="169.254.30.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    os.chdir(ROOT)
    os.umask(0o077)
    os.environ["DEEPSWE_SMOKE_KEY"] = "local-smoke-not-a-provider-key"
    with ThreadingHTTPServer((args.bind_host, args.port), BaselineStub) as server:
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
