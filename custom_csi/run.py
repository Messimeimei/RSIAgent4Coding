#!/usr/bin/env python3
"""
RSI 实验总入口：
1. 准备配置
2. 检查实验条件
3. 检查模型接口
4. 启动任务并保存日志
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent

# OSWorld-V2 任务路径
OSWORLD_ROOT = ROOT.parent / "OSWorld-V2"
PYTHON = OSWORLD_ROOT / ".venv/bin/python"

# 实验配置目录
PROFILE = Path(
    os.environ.get("RSIAGENT_CSI_PROFILE_DIR", str(HERE))
).expanduser().resolve()

# route.json: 所有任务默认共用这一份模型配置；--route-file 可显式选择其他路由。
ROUTE = HERE / "route.json"

# runtime.json：定义使用的相关镜像和宿主机要加载的内核模块
_runtime_candidate = PROFILE / "runtime.json"
RUNTIME = (
    _runtime_candidate if _runtime_candidate.is_file()
    else HERE / "runtime.json"
)

BASELINE_LOCK = PROFILE / "baseline.lock.json"  # 锁定 baseline 的运行配置
EVALUATION_LOCK = PROFILE / "evaluation.lock.json"  # 锁定 RSI 最终评测的运行配置

# _protocol_candidate 只是协议模板，实际运行时会根据任务和预算
# 在 yx/RSIAgent/results/custom_csi 下面的文件夹生成新的 protocol.json
_protocol_candidate = PROFILE / "protocol.json"
PROTOCOL_TEMPLATE = (
    _protocol_candidate if _protocol_candidate.is_file()
    else HERE / "protocols/task_080.json"
)

# 
_baseline_candidate = PROFILE / "baseline.yaml"
BASELINE_CONFIG = (
    _baseline_candidate if _baseline_candidate.is_file()
    else ROOT / "config/osworld/baseline.yaml"
)

RESULTS = ROOT / "results"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ==================== 工具函数 ====================
def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object: {path}")
    return value


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(_json_bytes(value))
    os.replace(temporary, path)


def _resolved_role_routes(route: dict) -> dict:
    """检查模型路由文件 route.json 的模型信息并返回字典保存的信息"""
    if not isinstance(route, dict):
        raise RuntimeError("LLM route must be a JSON object")
    if route.get("transport") != "openai_compatible":
        raise RuntimeError("CSI route transport must be openai_compatible")
    for key in ("base_url", "api_key_env"):
        if not isinstance(route.get(key), str) or not route[key].strip():
            raise RuntimeError(f"CSI route requires a nonempty {key}")
            
    model_map = route.get("model_map")
    if not isinstance(model_map, dict) or not all(
            isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
            for k, v in model_map.items()):
        raise RuntimeError("CSI route model_map must contain nonempty string pairs")
    roles = route.get("role_routes")
    if not isinstance(roles, dict):
        raise RuntimeError("CSI route requires role_routes")
    resolved = {}
    for role, canonical in (
            ("actor_memory_target", "z-ai/glm-5.3"),
            ("verifier_visual_escalation", "moonshotai/kimi-k3"),
            ("curriculum", "moonshotai/kimi-k3")):
        record = roles.get(role, {})
        if (not isinstance(record, dict)
                or record.get("canonical_model", canonical) != canonical):
            raise RuntimeError(f"{role}.canonical_model must remain {canonical}")
        if canonical not in model_map:
            raise RuntimeError(f"CSI route model_map is missing {canonical}")
        # Older files also recorded wire_model here. Derive it from model_map
        # so a model change never requires editing the same value twice.
        resolved[role] = {
            "canonical_model": canonical, "wire_model": model_map[canonical]}
    for role in ("sealed_evaluator", "user_simulator"):
        record = roles.get(role)
        if (not isinstance(record, dict)
                or not isinstance(record.get("wire_model"), str)
                or not record["wire_model"].strip()):
            raise RuntimeError(f"CSI route requires role_routes.{role}.wire_model")
        resolved[role] = {"wire_model": record["wire_model"]}
    return resolved


def _resolved_locks(route: dict, *, route_path: Path | None = None) -> tuple[dict, dict]:
    """Bind new run locks to the selected route and current local role configs."""
    roles = _resolved_role_routes(route)
    baseline = _read_json(BASELINE_LOCK)
    evaluation = _read_json(EVALUATION_LOCK)
    # Templates must still declare matching experimental conditions. Refresh only
    # their shared config identity; never hide a disagreement between the arms.
    for role in ("actor_agent", "verifier_agent"):
        for key in ("config", "sha256"):
            if baseline[role].get(key) != evaluation[role].get(key):
                raise RuntimeError(f"baseline/RSI mismatch: {role}.{key}")
    escalation = baseline["escalation"]
    escalated = evaluation["escalated_verifier_agent"]
    if (escalation["verifier_config"] != escalated["config"]
            or escalation["verifier_config_sha256"] != escalated["sha256"]):
        raise RuntimeError("baseline/RSI escalation mismatch")

    selected_route = str((route_path or ROUTE).resolve())
    model_map = route["model_map"]
    config_hashes = {}
    for lock in (baseline, evaluation):
        for role in ("actor_agent", "verifier_agent", "escalated_verifier_agent"):
            if role in lock:
                record = lock[role]
                config = ROOT / record["config"]
                if config not in config_hashes:
                    config_hashes[config] = hashlib.sha256(config.read_bytes()).hexdigest()
                record["sha256"] = config_hashes[config]
                record["transport_model"] = model_map.get(record["model"], record["model"])
                record["transport_route"] = selected_route
        for role, route_role in (("evaluator", "sealed_evaluator"),
                                 ("user_simulator", "user_simulator")):
            lock[role].update({
                "provider": route["transport"],
                "model": roles[route_role]["wire_model"],
                "base_url": route["base_url"],
                "api_key_env": route["api_key_env"],
            })
    escalation["verifier_config_sha256"] = escalated["sha256"]
    for role in ("actor", "verifier"):
        canonical = baseline["escalation"][f"{role}_model"]
        baseline["escalation"][f"{role}_transport_model"] = model_map.get(canonical, canonical)
    eye = baseline["visual_eye"]
    eye["transport_model"] = model_map.get(eye["model"], eye["model"])
    return baseline, evaluation


def _load_credentials(env_file: Path, *, api_key_env: str = "CSI_API_KEY") -> None:
    # 读取环境变量文件并写入 os.environ 
    if env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if separator and name.strip() and name.strip() not in os.environ:
                os.environ[name.strip()] = value.strip().strip("'\"")
    if not os.environ.get(api_key_env, "").strip():
        raise RuntimeError(
            f"缺少模型 key： {api_key_env}，请创建对应文件： {env_file}")


def _environment(env_file: Path, *, route_path: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    selected_route = route_path or ROUTE
    route = _read_json(selected_route)
    endpoint_host = urlparse(route["base_url"]).hostname
    runtime = _read_json(RUNTIME)
    env.update({
        "RSIAGENT_ROOT": str(ROOT),
        "OSWORLD_ROOT": str(OSWORLD_ROOT),
        "RSIAGENT_ENV_FILE": str(env_file),
        "RSIAGENT_LLM_ROUTE_FILE": str(selected_route),
        "OSWORLD_FILE_BASE_URL": str(
            OSWORLD_ROOT / "cache/osworld_v2_assets_0808"),
        "WEBSITE_HOST_SUFFIX": "site.hku.icu",
        "RSIAGENT_EVAL_MIN_OUTPUT_TOKENS": str(
            route["evaluator_min_output_tokens"]),
        "RSIAGENT_DISABLE_DIRECT_DOCKER_PUSH": "1",
        "RSIAGENT_DOCKER_IMAGE": str(runtime["outer_image"]),
    })
    if endpoint_host:
        for name in ("NO_PROXY", "no_proxy"):
            values = [part.strip() for part in env.get(name, "").split(",")
                      if part.strip()]
            if endpoint_host not in values:
                values.append(endpoint_host)
            env[name] = ",".join(values)
    if route["api_key_env"] != "OPENROUTER_API_KEY":
        env.pop("OPENROUTER_API_KEY", None)
    return env


def _task_ids(values: list[str]) -> list[str]:
    if values == ["all"]:
        return [f"task_{index:03d}" for index in range(1, 109)]
    tasks = []
    for value in values:
        for task in value.split(","):
            task = task.strip()
            if not re.fullmatch(r"task_\d{3}", task):
                raise ValueError(f"invalid task id: {task!r}")
            if task not in tasks:
                tasks.append(task)
    return tasks


def _direction(task: str) -> str:
    curated = ROOT / "config/target_queries" / f"{task}.md"
    if curated.is_file():
        return curated.read_text(encoding="utf-8")
    from benchmarks.osworld.task_surface import load_public_task_surface
    from explore.commit import normalize_instruction_for_corpus

    surface = load_public_task_surface(
        OSWORLD_ROOT, task, website_host_suffix="site.hku.icu")
    return normalize_instruction_for_corpus(surface.instruction)


def _protocol(task: str, run_name: str, direction_path: Path,
              project_budget: int, parallelism: int | None = None, *,
              evaluation_lock: Path | None = None) -> dict:
    value = _read_json(PROTOCOL_TEMPLATE)
    value["run_name"] = run_name
    value["phase1"].update({
        "distribution_file": str(direction_path),
        "project_budget": project_budget,
        "checkpoints": sorted(set((0, project_budget // 2, project_budget))),
        "parallel_waves": True,
        "park_verified_branches": True,
    })
    if parallelism is not None:
        value["phase1"]["parallelism"] = parallelism
    numeric = int(task.rsplit("_", 1)[1])
    value["phase2"]["development_tasks"] = [task]
    value["phase2"]["seed"] = 83000 + numeric
    value["phase3"]["held_out_tasks"] = [task]
    value["phase3"]["seed"] = 93000 + numeric
    value["phase3"]["benchmark_lock"] = str(evaluation_lock or EVALUATION_LOCK)
    return value


def _comparison_contract(route: dict, *, locks: tuple[dict, dict] | None = None) -> dict:
    """Reject unmatched baseline/RSI conditions before any paid call or VM."""
    # Callers that only validate an existing profile use its declared locks;
    # the main runner passes generated snapshots of the route and config hashes.
    baseline, evaluation = locks if locks is not None else (
        _read_json(BASELINE_LOCK), _read_json(EVALUATION_LOCK))
    for role in ("actor_agent", "verifier_agent"):
        for key in ("model", "transport_model", "config", "sha256"):
            if baseline[role].get(key) != evaluation[role].get(key):
                raise RuntimeError(f"baseline/RSI mismatch: {role}.{key}")
        record = baseline[role]
        wire_model = route["model_map"].get(record["model"], record["model"])
        if wire_model != record["transport_model"]:
            raise RuntimeError(f"route/lock model mismatch: {role}")
        config = ROOT / record["config"]
        if hashlib.sha256(config.read_bytes()).hexdigest() != record["sha256"]:
            raise RuntimeError(f"config hash mismatch: {role}")
    if baseline["actor_agent"]["transport_model"] == baseline["verifier_agent"]["transport_model"]:
        raise RuntimeError("CSI reproduction requires different Actor and Verifier models")
    escalation = baseline["escalation"]
    escalated = evaluation["escalated_verifier_agent"]
    if (escalation["actor_transport_model"] != baseline["verifier_agent"]["transport_model"]
            or escalation["verifier_transport_model"] != baseline["actor_agent"]["transport_model"]
            or escalated["transport_model"] != escalation["verifier_transport_model"]
            or escalated["config"] != escalation["verifier_config"]
            or escalated["sha256"] != escalation["verifier_config_sha256"]
            or hashlib.sha256((ROOT / escalated["config"]).read_bytes()).hexdigest()
            != escalated["sha256"]):
        raise RuntimeError("baseline/RSI escalation mismatch")
    template = _read_json(PROTOCOL_TEMPLATE)
    if (BASELINE_CONFIG.resolve() != (ROOT / baseline["actor_agent"]["config"]).resolve()
            or (ROOT / template["phase3"]["config"]).resolve() != BASELINE_CONFIG.resolve()):
        raise RuntimeError("baseline and Phase-3 must use the same target config")
    for role in ("evaluator", "user_simulator", "task_assets"):
        if baseline[role] != evaluation[role]:
            raise RuntimeError(f"baseline/RSI mismatch: {role}")
    if baseline["benchmark_release"] != evaluation["task_release"]:
        raise RuntimeError("baseline/RSI task release mismatch")
    if (baseline["memory"].get("enabled") is not False
            or baseline["memory"].get("host_writeback") is not False
            or evaluation["memory"].get("host_writeback") is not False
            or evaluation["memory"].get("mode") != "frozen_host_snapshot_copy_in_only"):
        raise RuntimeError("baseline must disable memory; RSI must use frozen memory")
    return {
        "baseline_lock_sha256": hashlib.sha256(_json_bytes(baseline)).hexdigest(),
        "evaluation_lock_sha256": hashlib.sha256(_json_bytes(evaluation)).hexdigest(),
        "actor": baseline["actor_agent"]["transport_model"],
        "verifier": baseline["verifier_agent"]["transport_model"],
        "evaluator": baseline["evaluator"]["model"],
        "user_simulator": baseline["user_simulator"]["model"],
    }


def _validated_probe(path: Path, *, route_sha256: str | None = None) -> dict:
    receipt = _read_json(path)
    required = {"long_context_actor", "long_context_json_verifier", "image_input",
                "primary_output_budget"}
    expected_hash = route_sha256 or hashlib.sha256(ROUTE.read_bytes()).hexdigest()
    if (receipt.get("status") != "passed"
            or receipt.get("route_sha256") != expected_hash
            or not required.issubset(receipt.get("checks", []))):
        raise RuntimeError("probe receipt does not validate the current route and output budget")
    return receipt


def _run_logged(command: list[str], *, env: dict[str, str], log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists():
        raise FileExistsError(f"refusing to overwrite log: {log}")
    print("+ " + " ".join(shlex.quote(part) for part in command), flush=True)
    with log.open("x", encoding="utf-8") as stream:
        process = subprocess.Popen(
            command, cwd=ROOT, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1)
        try:
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                stream.write(line)
                stream.flush()
            return process.wait()
        except KeyboardInterrupt:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise


def _probe(output_root: Path, *, route_path: Path | None = None) -> dict:
    """测试模型接口是否能正常返回，包括文字和图片测试两种，并写入 probe.json 文件"""
    from llm import client

    selected_route = route_path or ROUTE
    client._client = None
    client.reset_provider_counts()
    # Probe the real primary ceiling; a 64-token request cannot validate it.
    context = "\n".join(
        f"Record {i:04d}: alpha beta gamma delta epsilon zeta eta theta iota kappa."
        for i in range(2200))
    actor = client.chat(
        "z-ai/glm-5.3",
        "Follow the user's exact response format.",
        context + "\nReply exactly CSI_TEXT_READY.",
        max_tokens=65536, temperature=1.0, top_p=1.0, reasoning_effort="max")
    structured = client.chat(
        "moonshotai/kimi-k3",
        "Return one JSON object and no prose.",
        context + '\nReturn {"status":"CSI_JSON_READY"}.',
        max_tokens=65536, temperature=1.0, top_p=1.0,
        reasoning_effort="max", json_object=True)
    parsed = client.parse_object(structured)
    if (actor.strip() != "CSI_TEXT_READY" or not isinstance(parsed, dict)
            or parsed.get("status") != "CSI_JSON_READY"):
        raise RuntimeError("CSI text/JSON capability probe returned unusable output")

    # DashScope requires both image dimensions to exceed ten pixels. The content
    # question is deliberately trivial; this is a transport gate, not a vision
    # benchmark.
    from io import BytesIO
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (16, 16), (220, 30, 30)).save(buffer, format="PNG")
    image = buffer.getvalue()
    vision = client.chat(
        "moonshotai/kimi-k3",
        "Acknowledge valid image input using the requested token only.",
        "An image is attached. Reply exactly CSI_IMAGE_READY.",
        image=image, max_tokens=32768, temperature=1.0, top_p=1.0,
        reasoning_effort="max")
    if vision.strip() != "CSI_IMAGE_READY":
        raise RuntimeError("CSI image capability probe returned an empty output")
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "route_path": str(selected_route),
        "route_sha256": hashlib.sha256(selected_route.read_bytes()).hexdigest(),
        "checks": ["long_context_actor", "long_context_json_verifier",
                   "image_input", "primary_output_budget"],
        "context_chars": len(context),
        "requested_primary_max_tokens": 65536,
        "scope": "request_acceptance_and_response_format_not_completed_agent_runs",
        "response_sha256": {
            "text": hashlib.sha256(actor.encode()).hexdigest(),
            "json": hashlib.sha256(structured.encode()).hexdigest(),
            "image": hashlib.sha256(vision.encode()).hexdigest(),
        },
        "providers": client.provider_counts(),
    }
    _write_json(output_root / "probe.json", receipt)
    print(json.dumps(receipt, indent=2), flush=True)
    return receipt


def _run_name(name: str, task: str, arm: str) -> str:
    return f"{name}_{task}_{arm}"


def _check_outputs(tasks: list[str], name: str, arm: str) -> None:
    targets = []
    arms = ("baseline", "rsi") if arm == "both" else (arm,)
    for task in tasks:
        for selected_arm in arms:
            run_name = _run_name(name, task, selected_arm)
            targets.append(RESULTS / "custom_csi" / run_name)
            if selected_arm == "baseline":
                targets.append(RESULTS / task / f"seed5400_{run_name}")
            elif selected_arm == "rsi":
                targets.append(RESULTS / "recursive_improvement" / run_name)
    existing = [path for path in targets if path.exists()]
    if existing:
        raise FileExistsError(
            "use a new --name; outputs already exist: "
            + ", ".join(map(str, existing)))


def _baseline(task: str, name: str, env: dict[str, str], work: Path, *,
              baseline_lock: Path | None = None) -> None:
    child = dict(env)
    child["RSIAGENT_BENCHMARK_LOCK"] = str(baseline_lock or BASELINE_LOCK)
    child["RSIAGENT_OSWORLD_CACHE_DIR"] = str(work / "cache/baseline")
    command = [
        str(PYTHON), "-u", "-m", "benchmarks.osworld.task", task,
        "--config", str(BASELINE_CONFIG),
        "--seed", "5400", "--tag", _run_name(name, task, "baseline"),
    ]
    if _run_logged(command, env=child, log=work / "baseline.log"):
        raise RuntimeError(f"baseline failed for {task}")


def _rsi(task: str, name: str, env: dict[str, str], work: Path,
         project_budget: int, parallelism: int | None = None, *,
         evaluation_lock: Path | None = None) -> None:
    run_name = _run_name(name, task, "rsi")
    if len(run_name) > 64:
        raise ValueError(f"generated RSI run_name exceeds 64 characters: {run_name}")
    direction = work / "direction.md"
    direction.write_text(_direction(task), encoding="utf-8")
    protocol = work / "protocol.json"
    _write_json(protocol, _protocol(
        task, run_name, direction, project_budget, parallelism,
        evaluation_lock=evaluation_lock))
    child = dict(env)
    child.pop("RSIAGENT_BENCHMARK_LOCK", None)
    for number, phase in enumerate(("phase1", "phase2", "phase3"), 1):
        child["RSIAGENT_OSWORLD_CACHE_DIR"] = str(
            work / "cache" / phase)
        preflight = [
            str(PYTHON), "-u", "-m", "benchmarks.osworld.pipeline",
            "--protocol", str(protocol), "--phase", phase, "--preflight",
        ]
        if _run_logged(
                preflight, env=child,
                log=work / f"{number}_{phase}_preflight.log"):
            raise RuntimeError(f"{phase} preflight failed for {task}")
        execute = [
            str(PYTHON), "-u", "-m", "benchmarks.osworld.pipeline",
            "--protocol", str(protocol), "--phase", phase,
            "--execute", "RUN-RECURSIVE-IMPROVEMENT-" + phase.upper(),
        ]
        if _run_logged(
                execute, env=child,
                log=work / f"{number}_{phase}.log"):
            raise RuntimeError(f"{phase} failed for {task}")


def main(argv: list[str] | None = None, *, default_task: str = "task_080") -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--arm", choices=("probe", "baseline", "rsi", "both"),
        default="both")
    parser.add_argument("--tasks", nargs="+", default=[default_task])
    parser.add_argument(
        "--name",
        default=datetime.now(timezone.utc).strftime("csi_%Y%m%d_%H%M%S"),
        help="run prefix; output names are <name>_<task>_<baseline|rsi|probe>")
    parser.add_argument("--project-budget", type=int, default=8)
    parser.add_argument(
        "--parallelism", type=int, choices=range(1, 5), default=None,
        help="BRS execution capacity (1-4); preserves wave snapshots and commits")
    parser.add_argument("--env-file", type=Path, default=HERE / ".env")
    parser.add_argument(
        "--route-file", type=Path, default=ROUTE,
        help="model configuration (shared custom_csi/route.json by default)")
    parser.add_argument("--skip-probe", action="store_true")
    parser.add_argument(
        "--probe-receipt", type=Path,
        help="reuse a successful probe of this exact route with --skip-probe")
    parser.add_argument(
        "--allow-unstable-route", action="store_true",
        help="permit long Agent runs despite route validation failure")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", args.name):
        parser.error("--name must contain 1-40 letters, numbers, '_' or '-'")
    if args.project_budget < 1:
        parser.error("--project-budget must be positive")
    tasks = _task_ids(args.tasks)
    route_path = args.route_file.expanduser().resolve()
    route_bytes = route_path.read_bytes()
    route = json.loads(route_bytes)
    role_routes = _resolved_role_routes(route)
    arms = ("baseline", "rsi") if args.arm == "both" else (args.arm,)
    runs = []
    run_locks = []
    for task in tasks:
        for arm in arms:
            run_name = _run_name(args.name, task, arm)
            output_root = (RESULTS / "custom_csi" / run_name).resolve()
            config_root = output_root / "config"
            runtime_route = config_root / "route.json"
            locks = _resolved_locks(route, route_path=runtime_route)
            runs.append({
                "run_name": run_name,
                "task": task,
                "arm": arm,
                "output_root": str(output_root),
                "comparison": _comparison_contract(route, locks=locks),
                "runtime_config": {
                    "route": str(runtime_route),
                    "baseline_lock": str(config_root / "baseline.lock.json"),
                    "evaluation_lock": str(config_root / "evaluation.lock.json"),
                },
            })
            run_locks.append(locks)
    runtime = _read_json(RUNTIME)
    phase1 = _protocol(tasks[0], "plan", Path("direction.md"),
                       args.project_budget, args.parallelism)["phase1"]
    plan = {
        "schema_version": 2,
        "name": args.name,
        "arm": args.arm,
        "tasks": tasks,
        "project_budget": args.project_budget,
        "parallelism": phase1["parallelism"],
        "parallel_waves": phase1["parallel_waves"],
        "park_verified_branches": phase1["park_verified_branches"],
        "profile_dir": str(PROFILE),
        "runs": runs,
        "route": {
            "path": str(route_path),
            "sha256": hashlib.sha256(route_bytes).hexdigest(),
            "name": route["name"],
            "base_url": route["base_url"],
            "api_key_env": route["api_key_env"],
            "model_map": route["model_map"],
            "role_routes": role_routes,
            "reasoning_effort_map": route["reasoning_effort_map"],
            "supports_reasoning_controls": route["supports_reasoning_controls"],
            "supports_reasoning_history": route["supports_reasoning_history"],
            "strict_request_parameters": route.get("strict_request_parameters", False),
            "fail_on_truncation": route.get("fail_on_truncation", False),
            "stream": route["stream"],
            "max_output_tokens_cap": route["max_output_tokens_cap"],
            "max_output_tokens_map": route["max_output_tokens_map"],
            "evaluator_min_output_tokens":
                route["evaluator_min_output_tokens"],
            "validation": route["validation"],
        },
        "runtime": {
            "path": str(RUNTIME),
            "sha256": hashlib.sha256(RUNTIME.read_bytes()).hexdigest(),
            "base_outer_image": runtime["base_outer_image"],
            "outer_image": runtime["outer_image"],
            "host_kernel_modules": runtime["host_kernel_modules"],
        },
        "classification": "custom_model_structural_reproduction",
    }
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return 0
    if not PYTHON.is_file():
        raise RuntimeError(f"OSWorld Python environment is missing: {PYTHON}")
    probe_receipt = None
    probe_source = None
    if args.arm != "probe" and args.skip_probe:
        if args.probe_receipt:
            probe_receipt = _validated_probe(
                args.probe_receipt, route_sha256=plan["route"]["sha256"])
            probe_source = args.probe_receipt.expanduser().resolve()
        elif not args.allow_unstable_route:
            raise RuntimeError("--skip-probe requires --probe-receipt for the current route")

    env_file = args.env_file.expanduser().resolve(strict=False)
    _load_credentials(env_file, api_key_env=route["api_key_env"])
    _check_outputs(tasks, args.name, args.arm)
    # Capture every task/arm before the first probe or child process. A source
    # route edit during execution cannot change a later task's route or locks.
    for execution, locks in zip(runs, run_locks):
        output_root = Path(execution["output_root"])
        output_root.mkdir(parents=True)
        paths = {key: Path(value) for key, value in execution["runtime_config"].items()}
        paths["route"].parent.mkdir()
        paths["route"].write_bytes(route_bytes)
        _write_json(paths["baseline_lock"], locks[0])
        _write_json(paths["evaluation_lock"], locks[1])
        _write_json(output_root / "plan.json", {
            **plan, **execution, "tasks": [execution["task"]],
            "requested_arm": args.arm, "requested_tasks": tasks,
        })

    for index, execution in enumerate(runs):
        task, arm = execution["task"], execution["arm"]
        output_root = Path(execution["output_root"])
        paths = {key: Path(value) for key, value in execution["runtime_config"].items()}
        env = _environment(env_file, route_path=paths["route"])
        for name in (
                "RSIAGENT_ROOT", "OSWORLD_ROOT", "RSIAGENT_ENV_FILE",
                "RSIAGENT_LLM_ROUTE_FILE", "OSWORLD_FILE_BASE_URL",
                "WEBSITE_HOST_SUFFIX", "RSIAGENT_EVAL_MIN_OUTPUT_TOKENS",
                "RSIAGENT_DISABLE_DIRECT_DOCKER_PUSH",
                "RSIAGENT_DOCKER_IMAGE", "NO_PROXY", "no_proxy"):
            os.environ[name] = env[name]
        if route["api_key_env"] != "OPENROUTER_API_KEY":
            os.environ.pop("OPENROUTER_API_KEY", None)

        if index == 0 and (not args.skip_probe or arm == "probe"):
            probe_receipt = _probe(output_root, route_path=paths["route"])
            probe_source = output_root / "probe.json"
        elif probe_receipt is not None:
            # All snapshots contain the exact same route bytes. Reuse one probe
            # per invocation and retain its provenance in every output directory.
            _write_json(output_root / "probe.json", {
                **probe_receipt, "reused_from": str(probe_source),
            })

        if arm == "baseline":
            _baseline(task, args.name, env, output_root,
                      baseline_lock=paths["baseline_lock"])
        elif arm == "rsi":
            _rsi(task, args.name, env, output_root, args.project_budget,
                 args.parallelism, evaluation_lock=paths["evaluation_lock"])
        _write_json(output_root / "status.json", {
            "schema_version": 1,
            "status": "complete",
            "run_name": execution["run_name"],
            "tasks": [task],
            "arm": arm,
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
