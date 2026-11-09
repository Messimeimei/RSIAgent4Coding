#!/usr/bin/env python3
"""Exercise the patched CLI against a local mock model, without Docker or GPUs."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply({"object": "list", "data": [{"id": "policy-mock", "object": "model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        state = self.server.state
        state["requests"].append(body)
        messages = body.get("messages", [])
        summary = any("You maintain the WORK LOG" in str(m.get("content", "")) for m in messages)
        message = {"role": "assistant", "content": "OK"}
        finish = "stop"
        tools = [t.get("function", t) for t in body.get("tools", [])]
        shell = next((t for t in tools if t.get("name", "").lower() in
                      {"execcommand", "bash", "shell", "run_command", "execute_bash"}), None)
        if summary:
            state["summary_requests"].append(body)
            if state.get("summary_error"):
                self.reply({"error": {"message": "maximum context length exceeded", "code": "context_length_exceeded"}}, 400)
                return
            message["content"] = "" if state.get("empty_summary") else "CURRENT STATE: inspected evidence. KEY VALUES: SENTINEL_000. NEXT STEPS: continue."
        elif shell:
            state["main_requests"].append(body)
            if state.get("provider_overflow"):
                self.reply({"error": {"message": "maximum context length exceeded", "code": "context_length_exceeded"}}, 400)
                return
            if state["tools_issued"] < state["rounds"]:
                number = state["tools_issued"]
                state["tools_issued"] += 1
                properties = shell.get("parameters", {}).get("properties", {})
                args = {"cmd" if "cmd" in properties else "command":
                        "printf '%%s\\n' 'SENTINEL_%03d %s'" % (number, "evidence " * 35)}
                # The only executed action prints a fixed synthetic observation.
                if "yield_time_ms" in properties:
                    args["yield_time_ms"] = 10000
                for key in shell.get("parameters", {}).get("required", []):
                    if key not in args:
                        args[key] = {"integer": 10, "number": 10, "boolean": False,
                                     "object": {}, "array": []}.get(properties[key].get("type"), "probe")
                message = {"role": "assistant", "content": f"Inspect sample {number}",
                    "tool_calls": [{"index": 0, "id": f"probe-{number}", "type": "function",
                        "function": {"name": shell["name"], "arguments": json.dumps(args)}}]}
                finish = "tool_calls"
        usage = {"prompt_tokens": state.get("usage_tokens", 15000), "completion_tokens": 20,
                 "total_tokens": state.get("usage_tokens", 15000) + 20}
        envelope = {"id": "mock-" + uuid.uuid4().hex, "created": int(time.time()), "model": "policy-mock"}
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for delta, stop in [(message, None), ({}, finish)]:
                chunk = {**envelope, "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": stop}]}
                if stop:
                    chunk["usage"] = usage
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
        else:
            self.reply({**envelope, "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})


def events(output):
    result = []
    for line in output.splitlines():
        try:
            event = json.loads(line).get("event")
            if event:
                result.append(event)
        except (ValueError, AttributeError):
            pass
    return result


def run_case(binary, root, mode, rounds, *, usage_tokens=15000, window=1048576,
             resume_mode=None, empty_summary=False, summary_error=False, provider_overflow=False):
    root.mkdir(parents=True)
    work = root / "work"
    work.mkdir()
    user = root / "user"
    (user / "config").mkdir(parents=True)
    state = {"requests": [], "summary_requests": [], "main_requests": [],
             "rounds": rounds, "tools_issued": 0, "usage_tokens": usage_tokens,
             "empty_summary": empty_summary, "summary_error": summary_error,
             "provider_overflow": provider_overflow}
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    policy = {"mode": mode}
    if mode == "work_log":
        policy["work_log"] = {"history_keep_pairs": 6, "keep_chars": 2000,
            "ctx_high_water": 4000, "ctx_low_water": 2000,
            "fold_batch": 10, "worklog_max_chars": 2000}
    config = {"ai": {"context_policy": policy, "models": [{
        "id": "policy-mock", "name": "policy-mock", "model_name": "policy-mock",
        "provider": "openai", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
        "api_key": "local-mock-only", "enabled": True,
        "context_window": window, "max_tokens": 65536}],
        "default_models": {"primary": "policy-mock", "fast": "policy-mock"}}}
    (user / "config/app.json").write_text(json.dumps(config))
    env = dict(os.environ, BITFUN_HOME=str(root / "home"), BITFUN_USER_ROOT=str(user))
    sid = str(uuid.uuid4())
    def invoke(resume=False):
        command = [str(binary), "exec", "--auto", "--no-verify-final-changes", "--output-format",
                   "stream-json", "--resume" if resume else "--session-id", sid, "--",
                   "CONTEXT_POLICY_TASK_SENTINEL: inspect synthetic observations using the shell tool, then reply OK."]
        result = subprocess.run(command, cwd=work, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
        (root / ("resume.log" if resume else "new.log")).write_text(result.stdout)
        return result, events(result.stdout)
    try:
        first, first_events = invoke()
        if resume_mode:
            config["ai"]["context_policy"] = {"mode": resume_mode}
            (user / "config/app.json").write_text(json.dumps(config))
        second, second_events = invoke(True) if first.returncode == 0 else (None, [])
        for path in (root / "home").rglob("state.json"):
            session_state = json.loads(path.read_text())
            state.setdefault("persisted_states", []).append(session_state)
        (root / "requests.json").write_text(json.dumps(state, indent=2))
        return state, first, first_events, second, second_events
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    binary, root = args.binary.resolve(), args.output.resolve()
    checks = {}
    for mode, rounds in [("native", 0), ("full_history", 20), ("work_log", 28)]:
        state, first, ev, resumed, resumed_ev = run_case(binary, root / mode, mode, rounds,
            resume_mode="native" if mode != "native" else None)
        assert first.returncode == 0 and resumed.returncode == 0, first.stdout[-3000:]
        windows = [e["max_context_tokens"] for e in ev + resumed_ev if e["type"] == "TokenUsageUpdated"]
        assert windows and set(windows) == {1048576}, windows
        checks[mode + "_first_and_resume_window"] = True
        if mode == "full_history":
            assert state["tools_issued"] == rounds, "Shell tool was not exposed to the mock"
            assert not state["summary_requests"]
            last = json.dumps(state["main_requests"][-1])
            assert all(f"SENTINEL_{i:03d}" in last for i in range(rounds))
            assert not any(e["type"] == "ContextCompressionStarted" for e in ev + resumed_ev)
            checks["full_history_verbatim_observations"] = True
        if mode == "work_log":
            assert state["tools_issued"] == rounds
            assert len(state["summary_requests"]) >= 2
            assert all(r.get("temperature") == 0 and not r.get("tools") for r in state["summary_requests"])
            last = json.dumps(state["main_requests"][-1])
            assert "CONTEXT_POLICY_TASK_SENTINEL" in last and "WORK LOG" in last
            assert all(f"SENTINEL_{i:03d}" in last for i in range(rounds - 6, rounds))
            assert "model_work_log" in json.dumps(ev)
            checks["work_log_batches_task_tail_and_resume"] = True
        if mode != "native":
            assert any(s.get("config", {}).get("context_policy", {}).get("mode") == mode
                       for s in state["persisted_states"]), "Persisted policy was changed on resume"
            checks[mode + "_policy_persists_after_app_config_change"] = True
    state, first, ev, _, _ = run_case(binary, root / "capacity", "full_history", 3,
                                     usage_tokens=110000, window=128128)
    assert first.returncode != 0 and "context_capacity_exceeded" in first.stdout
    assert state["tools_issued"] == 1 and not state["summary_requests"]
    assert not any(e["type"] == "ContextCompressionStarted" for e in ev)
    checks["capacity_stops_without_lossy_recovery"] = True
    state, first, ev, _, _ = run_case(binary, root / "empty_summary", "work_log", 16,
                                     empty_summary=True)
    assert first.returncode == 0 and state["summary_requests"]
    assert "reference_digest" in json.dumps(ev)
    # The deliberately tiny log cap keeps the newest digest lines, as in the reference.
    assert "[you] Inspect sample 9" in json.dumps(state["main_requests"][-1])
    assert "SENTINEL_009" in json.dumps(state["main_requests"][-1])
    checks["empty_summary_reference_digest"] = True
    for mode, options in [("full_history", {"provider_overflow": True}),
                          ("work_log", {"summary_error": True})]:
        state, first, ev, _, _ = run_case(binary, root / (mode + "_error"), mode, 16, **options)
        assert first.returncode != 0
        assert not any(e["type"] == "ContextCompressionCompleted" for e in ev)
        assert all(e.get("trigger") == "work_log_high_water" for e in ev
                   if e["type"] == "ContextCompressionStarted")
    checks["provider_errors_do_not_trigger_native_compression"] = True
    state, first, ev, resumed, resumed_ev = run_case(binary, root / "legacy_resume", "native", 0,
                                                   resume_mode="full_history")
    assert first.returncode == 0 and resumed.returncode != 0
    assert "context_policy_migration_required" in resumed.stdout
    checks["legacy_resume_requires_fresh_session"] = True
    receipt = {"checks": checks, "passed": all(checks.values()), "binary": str(binary)}
    (root / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
