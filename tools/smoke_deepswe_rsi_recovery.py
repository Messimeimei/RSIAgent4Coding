#!/usr/bin/env python3
"""Unpaid real Docker/BitFun archive-fault recovery and cross-container resume."""

import argparse
import asyncio
import json
import os
import re
import shlex
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.smoke_deepswe_bitfun import ModelStub


class RecoveryStub(ModelStub):
    steps = []
    resumed_context = False
    large_prompt_seen = False

    def do_POST(self):
        try:
            self.respond()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def respond(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        messages = body.get("messages", [])
        latest = next((i for i in range(len(messages) - 1, -1, -1)
                       if messages[i].get("role") == "user"
                       and "RSI_SMOKE_STEP:" in str(messages[i].get("content", ""))), None)
        if latest is None:
            self.json_reply(400, {"error": {"message": "Missing smoke control"}})
            return
        prompt = str(messages[latest]["content"])
        step = int(re.search(r"RSI_SMOKE_STEP: (\d)", prompt)[1])
        if step == 1:
            payload = 'LARGE_PROMPT_START\n' + ('quoted \' \" $() `literal` 中文\n' * 6000) + 'LARGE_PROMPT_END'
            type(self).large_prompt_seen = payload in prompt
        control = (json.loads(re.search(r'\{"kind":"rsi_done"[^\n]+?\}', prompt)[0])
                   if step != 3 else None)
        report_path = re.search(r"/logs/agent/handoffs/[a-f0-9]+\.json", prompt)[0]
        results = [m for m in messages[latest + 1:] if m.get("role") == "tool"]
        if step == 2:
            type(self).resumed_context = "first-context-canary-19873" in json.dumps(messages[:latest])
        message = {"role": "assistant", "content": "Smoke complete."}
        finish = "stop"
        if not results:
            type(self).steps.append(step)
            script = (
                "from pathlib import Path; import json; "
                "marker=Path('/home/user/practice-marker'); "
                "assert not marker.exists(); "
                f"marker.write_text('step {step}'); "
                f"path=Path({report_path!r}); path.parent.mkdir(parents=True,exist_ok=True); "
                f"temp=path.with_suffix('.tmp'); temp.write_text(json.dumps({control!r})); temp.replace(path)"
            )
            if step == 3:
                # First test normal isolation, then deliberately bypass it to
                # exercise host recovery of real pytest/CPython cache files.
                script = (
                    "from pathlib import Path; import os, subprocess; "
                    "root=Path('/home/user/evolution_project'); "
                    "index_before=(root/'.git/index').read_bytes(); "
                    "source=root/'answer.py'; st=source.stat(); "
                    "os.utime(source,ns=(st.st_atime_ns,st.st_mtime_ns+2000000000)); "
                    "subprocess.run(['git','update-index','--really-refresh'],cwd=root,check=True); "
                    "assert (root/'.git/index').read_bytes()!=index_before; "
                    "subprocess.run(['python3','-m','pytest','-q'],cwd=root,check=True); "
                    "assert not (root/'.pytest_cache').exists(); "
                    "assert not list(root.rglob('__pycache__')); "
                    "assert not (root/'.hypothesis').exists(); "
                    "assert Path(os.environ['HYPOTHESIS_STORAGE_DIRECTORY']).is_dir(); "
                    "env=dict(os.environ); "
                    "[env.pop(key,None) for key in ['PYTHONDONTWRITEBYTECODE','PYTHONPYCACHEPREFIX','PYTEST_ADDOPTS']]; "
                    "subprocess.run(['python3','-m','pytest','-q'],cwd=root,env=env,check=True); "
                    "assert (root/'.pytest_cache').is_dir(); "
                    "assert list(root.rglob('*.pyc')); "
                    f"path=Path({report_path!r}); path.parent.mkdir(parents=True,exist_ok=True); "
                    "temp=path.with_suffix('.tmp'); "
                    "temp.write_text('Independent smoke test passed.\\nVERDICT: PASS\\n'); temp.replace(path)"
                )
            message = {"role": "assistant", "content": "Publish the smoke completion.",
                "tool_calls": [{"index": 0, "id": f"recovery-smoke-{step}", "type": "function",
                    "function": {"name": "ExecCommand", "arguments": json.dumps({
                        "cmd": "python3 -c " + shlex.quote(script), "yield_time_ms": 10000})}}]}
            finish = "tool_calls"
        else:
            time.sleep(5)  # Host must settle the native process after accepting the file.
        envelope = {"id": f"recovery-{step}", "created": int(time.time()), "model": body.get("model")}
        usage = {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140}
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for delta, stop in ((message, None), ({}, finish)):
                chunk = {**envelope, "object": "chat.completion.chunk",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": stop}]}
                if stop:
                    chunk["usage"] = usage
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
        else:
            self.json_reply(200, {**envelope, "object": "chat.completion", "usage": usage,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}]})


async def smoke(args, endpoint):
    from pier.models.task.task import Task
    from pier.models.trial.paths import TrialPaths
    from benchmarks.deepswe.docker_environment import BitfunDockerEnvironment
    from benchmarks.deepswe.rsi import BitfunRSI
    from benchmarks.deepswe.rsi_runtime import CodingRuntime, CodingVM
    from run_deepswe import load_job

    config = load_job(args.config, mode="rsi")
    destination = ROOT / "results/deepswe/smoke" / args.name
    destination.mkdir(parents=True, exist_ok=False)
    kwargs = config.agents[0].kwargs
    for key in ("bitfun_config", "curriculum_bitfun_config", "verifier_bitfun_config"):
        ai = kwargs[key]["ai"]
        model = ai["models"][0]
        model.update(id="recovery-mock", name="recovery-mock", model_name="recovery-mock",
            base_url=endpoint, api_key="${DEEPSWE_SMOKE_KEY}", max_tokens=4096,
            reasoning_mode="disabled")
        model.pop("reasoning_effort", None)
        ai["default_models"] = {"primary": "recovery-mock", "fast": "recovery-mock"}
    kwargs["checkpoint_repair_timeout_sec"] = 5
    owner = BitfunRSI(logs_dir=destination / "agent", **kwargs)
    owner.rsi_dir.mkdir(parents=True, exist_ok=True)
    task = Task(config.datasets[0].path / "dateutil-rfc5545-timezone-interop")
    template = BitfunDockerEnvironment(environment_dir=task.paths.environment_dir,
        environment_name=task.name, session_id=args.name,
        trial_paths=TrialPaths(destination / "template"), task_env_config=task.config.environment,
        network_allowlist=owner.network_allowlist(), inference_ports=[args.port],
        mounts_json=config.environment.mounts)
    audit = SimpleNamespace(instruction="smoke", validate=lambda *a: {}, text=lambda *a, **k: [],
        transcripts=lambda *a, **k: [], captured=lambda *a, **k: [], memory=lambda *a, **k: {})
    runtime = CodingRuntime(owner, template, destination / "rsi/runtime", asyncio.get_running_loop(), audit)
    vm = CodingVM(runtime)
    vm.leased = True
    runtime.vms.append(vm)
    checks = {}
    try:
        await runtime.reset_async(vm)
        original_exec = vm.environment.exec
        failures = 0
        async def fail_tar_twice(command, **kw):
            nonlocal failures
            if command.startswith("tar czf") and failures < 2:
                failures += 1
                return SimpleNamespace(return_code=2, stdout="", stderr="Injected archive I/O failure")
            return await original_exec(command=command, **kw)
        vm.environment.exec = fail_tar_twice
        long_prompt = ('RSI_SMOKE_STEP: 1\nfirst-context-canary-19873\nLARGE_PROMPT_START\n'
            + ('quoted \' \" $() `literal` 中文\n' * 6000) + 'LARGE_PROMPT_END')
        first, session = await runtime.execute(vm, long_prompt,
            "curriculum", destination / "step1", seconds=90)
        first_checkpoint = json.loads((destination / "step1/outcome-001.json").read_text())
        checks["fallback_archive_after_two_faults"] = (
            first_checkpoint["checkpoint"]["method"] == "stable_python_snapshot" and failures == 2)
        checks["no_model_replay_during_recovery"] = session.calls == 1
        checks["large_prompt_intact"] = RecoveryStub.large_prompt_seen
        first_environment = vm.environment.session_id
        # Simulate losing the entire controller and deleting its containers.
        # Only files in the runtime directory survive to this new instance.
        await runtime.close()
        runtime = CodingRuntime(owner, template, destination / "rsi/runtime", asyncio.get_running_loop(), audit)
        vm = CodingVM(runtime)
        vm.leased = True
        runtime.vms.append(vm)
        checks["new_controller_has_no_sessions"] = not runtime.sessions
        await runtime.reset_async(vm)
        second, resumed = await runtime.execute(vm, "RSI_SMOKE_STEP: 2", "curriculum",
            destination / "step2", session_id=session.session_id, seconds=90)
        second_checkpoint = json.loads((destination / "step2/outcome-002.json").read_text())
        checks.update(
            two_valid_handoffs=first.status == second.status == "done",
            same_session=resumed is not session and resumed.session_id == session.session_id and resumed.calls == 2,
            fresh_container=first_environment != vm.environment.session_id,
            native_context_survived=RecoveryStub.resumed_context,
            two_native_invocations=(resumed.calls == 2
                and len(list((destination / "step1").glob("outcome-*.json"))) == 1
                and len(list((destination / "step2").glob("outcome-*.json"))) == 1
                and first_checkpoint["metrics"]["completed_tools"] == 1
                and second_checkpoint["metrics"]["completed_tools"] == 1),
        )
        await runtime.reset_async(vm)
        setup = (
            "from pathlib import Path; p=Path('/home/user/evolution_project'); p.mkdir(); "
            "(p/'answer.py').write_text('def answer(): return 42\\n'); "
            "(p/'test_answer.py').write_text('from answer import answer\\ndef test_answer(): assert answer() == 42\\n'); "
            "(p/'test_property.py').write_text('from hypothesis import given, settings, strategies as st\\n"
            "@settings(max_examples=10, deadline=None)\\n@given(st.text())\\n"
            "def test_round_trip(s): assert s == s[::-1][::-1]\\n')"
        )
        result = await vm.environment.exec(command="python3 -c " + shlex.quote(setup))
        assert result.return_code == 0
        result = await vm.environment.exec(command=(
            "git init -q /home/user/evolution_project && "
            "cd /home/user/evolution_project && git add answer.py test_answer.py test_property.py && "
            "git -c user.email=rsi@test -c user.name=RSI commit -qm smoke"))
        assert result.return_code == 0
        capture = await asyncio.to_thread(runtime.hooks.capture_project, vm, "smoke-candidate",
            str(destination / "candidate"), guest_dirs=["evolution_project"])
        assert capture["ok"]
        verdict, report = await runtime.verify_async("RSI_SMOKE_STEP: 3", vm,
            SimpleNamespace(wall_clock_secs=120, max_iters=30, verifier_unverified_evidence=False),
            sink=SimpleNamespace(root=destination / "verifier"), session=SimpleNamespace(inspections=0))
        integrity = json.loads((destination / "verifier/inspection_001/integrity.json").read_text())
        exact = await asyncio.to_thread(runtime.hooks.verify_project, vm, str(destination / "candidate"))
        outcome = json.loads((destination / "verifier/inspection_001/outcome-001.json").read_text())
        checks.update(verifier_pass_preserved=verdict == "pass" and "VERDICT: PASS" in report,
            hypothesis_storage_redirected=verdict == "pass" and exact["ok"],
            real_pytest_caches_recovered=integrity["status"] == "cache_recovered" and bool(integrity["removed"]),
            git_index_metadata_recovered=bool(integrity["restored_git_indexes"]) and integrity["final_matches"],
            strict_reference_snapshot_check_passes=exact["ok"],
            verifier_not_replayed=outcome["call"] == 1)
    finally:
        await runtime.close()
    receipt = {"status": "passed" if all(checks.values()) else "failed",
        "paid_model_calls": 0, "checks": checks, "stub_steps": RecoveryStub.steps,
        "path": str(destination)}
    (destination / "smoke-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2), flush=True)
    return 0 if all(checks.values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "config/deepswe/rsi-bitfun.yaml",
                        help="Experiment config (all model endpoints are replaced with the local stub)")
    parser.add_argument("--bind-host", default="169.254.30.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if Path(args.name).name != args.name or args.name in {".", ".."}:
        parser.error("name must be a single directory name")
    os.chdir(ROOT)
    os.umask(0o077)
    os.environ["DEEPSWE_SMOKE_KEY"] = "local-unpaid-model"
    with ThreadingHTTPServer((args.bind_host, args.port), RecoveryStub) as server:
        args.port = server.server_port
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            return asyncio.run(smoke(args, f"http://{args.bind_host}:{args.port}/v1"))
        finally:
            server.shutdown()
            worker.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
