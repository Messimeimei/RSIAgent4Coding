#!/usr/bin/env python3
"""执行 DeepSWE 任务的统一入口，包括原生态 Pier 评测 bitfun；RSIAgent 的 baseline 和 RSIAgent 两种方法"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import signal
import subprocess
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
AGENT_PLUGINS = {
    "native": "benchmarks.deepswe.bitfun_agent:BitfunCli",
    "baseline": "benchmarks.deepswe.baseline:BitfunBaseline",
    "rsi": "benchmarks.deepswe.rsi:BitfunRSI",
}


def select_config(mode, path=None):
    # 默认的配置文件路径，可传入 --config 覆盖
    return path if path is not None else ROOT / "config/deepswe" / f"{mode}.yaml"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_job(path: Path, *, root: Path = ROOT, task=None, name=None, mode=None):
    """读取配置文件并解析得到要运行的任务"""
    import yaml
    from pier.models.job.config import JobConfig

    config = JobConfig.model_validate(yaml.safe_load(path.read_text()))
    if mode is not None:
        expected = AGENT_PLUGINS[mode]
        if any(agent.import_path != expected for agent in config.agents):
            raise ValueError(
                f"--mode {mode} requires the matching {expected} agent config"
            )
    config.jobs_dir = (root / config.jobs_dir).resolve()
    for dataset in config.datasets:
        dataset.path = (root / dataset.path).resolve()
        if task:
            dataset.task_names = [task] if isinstance(task, str) else list(task)
            dataset.exclude_task_names = None
            dataset.n_tasks = None
    for mount in config.environment.mounts or []:
        if mount["type"] == "bind":
            mount["source"] = str((root / mount["source"]).resolve())
    if name:
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("Job name must be a directory name, not a path")
        config.job_name = name
    return config


def integer_at_least(minimum):
    def parse(value):
        number = int(value)
        if number < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}")
        return number
    return parse


def apply_overrides(config, args):
    """Apply experiment overrides to independent role copies, keeping templates intact."""
    config = config.model_copy(deep=True)
    if args.mode != "rsi" and any(value is not None for value in (
        args.curriculum_model, args.practice_parallel, args.brs_project_budget,
    )):
        raise ValueError("Curriculum and practice options require --mode rsi")
    if args.mode == "native" and args.verifier_model is not None:
        raise ValueError("--verifier-model requires baseline or rsi mode")
    if args.tasks_file is not None:
        tasks = [line.partition("#")[0].strip()
                 for line in args.tasks_file.read_text().splitlines()]
        tasks = list(dict.fromkeys(task for task in tasks if task))
        if not tasks:
            raise ValueError("--tasks-file contains no task names")
    else:
        tasks = args.task
    if tasks is not None and any(not task.strip() for task in tasks):
        raise ValueError("Task names must not be empty")
    for dataset in config.datasets:
        if tasks is not None or args.all_tasks:
            dataset.task_names = None if args.all_tasks else list(dict.fromkeys(tasks))
            dataset.exclude_task_names = None
            dataset.n_tasks = None
    if args.concurrency is not None:
        config.n_concurrent_trials = args.concurrency
    if args.attempts is not None:
        config.n_attempts = args.attempts

    ports = set()
    for agent in config.agents:
        kwargs = agent.kwargs
        sources = {"actor": kwargs["bitfun_config"]}
        if args.mode != "native":
            sources["verifier"] = kwargs.get("verifier_bitfun_config") or sources["actor"]
        if args.mode == "rsi":
            sources["curriculum"] = kwargs.get("curriculum_bitfun_config") or sources["verifier"]
        for role, source in sources.items():
            role_config = copy.deepcopy(source)
            ai = role_config["ai"]
            enabled = [model for model in ai["models"] if model.get("enabled", True)]
            primary = ai.get("default_models", {}).get("primary")
            selected = next((model for model in enabled if model["id"] == primary), None)
            if selected is None:
                raise ValueError(f"{role} default model must name an enabled model")
            model_name = getattr(args, f"{role}_model") or args.model
            if model_name is not None:
                if not model_name.strip():
                    raise ValueError("Model names must not be empty")
                old_id = selected["id"]
                selected.update(id=model_name, name=model_name, model_name=model_name)
                for slot, value in ai["default_models"].items():
                    if value == old_id:
                        ai["default_models"][slot] = model_name
                ids = [model["id"] for model in ai["models"]]
                if len(ids) != len(set(ids)):
                    raise ValueError("Model override creates duplicate model IDs")
            for model in enabled:
                if args.base_url is not None:
                    model["base_url"] = args.base_url
                parsed = urlsplit(model.get("base_url", ""))
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or parsed.username or parsed.password or parsed.query or parsed.fragment):
                    raise ValueError("Model base_url must be an HTTP(S) URL without credentials, query or fragment")
                ports.add(parsed.port or (443 if parsed.scheme == "https" else 80))
            key = "bitfun_config" if role == "actor" else f"{role}_bitfun_config"
            kwargs[key] = role_config
            if role == "actor":
                agent.model_name = selected["model_name"]
        if args.practice_parallel is not None:
            kwargs["parallel"] = args.practice_parallel
        if args.brs_project_budget is not None:
            kwargs["brs_project_budget"] = args.brs_project_budget
    config.environment.kwargs["inference_ports"] = sorted(ports)
    return config


async def check_job(config, *, root=ROOT, require_credentials=False):
    from pier.agents.factory import AgentFactory
    from pier.models.task.task import Task
    from benchmarks.deepswe.bitfun_agent import credential_variables

    # 用于检查当前配置和环境是否符合 versions.json 中的 DeepSWE 版本要求
    pins = json.loads((root / "config/deepswe/versions.json").read_text())
    blockers = []
    tasks = []
    for dataset in config.datasets:
        head = subprocess.check_output(
            ["git", "-C", str(dataset.path.parent), "rev-parse", "HEAD"], text=True
        ).strip()

        # 检查 commit 是否匹配和修改
        if head != pins["deep_swe_commit"]:
            blockers.append("DeepSWE commit differs from config/deepswe/versions.json")
        dirty = subprocess.check_output(
            ["git", "-C", str(dataset.path.parent), "status", "--porcelain"], text=True
        ).strip()
        if dirty:
            blockers.append("The pinned DeepSWE checkout has local changes")

        task_configs = await dataset.get_task_configs()
        for pattern in dataset.task_names or []:
            if not any(fnmatchcase(tc.path.name, pattern) for tc in task_configs):
                blockers.append(f"No tasks matched selector: {pattern}")
        for tc in task_configs:
            item = Task(tc.path)
            cfg = item.config

            # 检查 DeepSWE 每个题目的设置是否正确：1. verifier 和 collect 是否分开；2. 是否允许联网
            if (
                not cfg.verifier.collect
                or cfg.verifier.environment_mode.value != "separate"
            ):
                blockers.append(
                    f"{item.name}: separate verifier/collect contract missing"
                )
            if (
                cfg.environment.allow_internet
                or cfg.verifier.environment.allow_internet
            ):
                blockers.append(f"{item.name}: no-network policy was not resolved")

            # 记录题目的关键参数用于跑完后复核
            tasks.append(
                {
                    "task": item.name,
                    "base_commit": cfg.metadata.get("base_commit_hash"),
                    "image": cfg.environment.docker_image,
                    "cpu": cfg.environment.cpus,
                    "memory_mb": cfg.environment.memory_mb,
                    "agent_timeout_sec": cfg.agent.timeout_sec,
                    "has_steps": item.has_steps,
                    "verifier_timeout_sec": cfg.verifier.timeout_sec,
                    "task_sha256": sha256(tc.path / "task.toml"),
                }
            )

    # 检查 uv 和 bitfun 可执行文件是否存在且可执行
    mounts = {m["target"]: Path(m["source"]) for m in config.environment.mounts or []}
    binaries = {}
    for target in ("/usr/local/bin/uv", "/usr/local/bin/bitfun"):
        source = mounts.get(target)
        available = bool(source and source.is_file() and os.access(source, os.X_OK))
        binaries[target] = {"source": str(source), "executable": available}
        if not available:
            blockers.append(f"Missing executable bind source: {target}")
        elif target.endswith("/bitfun"):
            digest = sha256(source)
            binaries[target]["sha256"] = digest
            if digest != pins["bitfun_binary_sha256"]:
                if all(a.kwargs.get("reference_context_policy") is True for a in config.agents):
                    from benchmarks.deepswe.context_policy import verify_build_receipt
                    try:
                        receipt = verify_build_receipt(
                            source, source.with_name(source.name + ".receipt.json"), root
                        )
                        binaries[target]["context_policy_build"] = receipt
                    except (OSError, ValueError, KeyError, TypeError) as error:
                        blockers.append(f"Invalid BitFun context-policy build: {error}")
                else:
                    blockers.append("BitFun binary differs from the pinned release")
            elif any(a.kwargs.get("reference_context_policy") for a in config.agents):
                blockers.append("Reference context policy requires a separately built patched BitFun")


    agents = []
    resume = None
    for agent_config in config.agents:
        agent = AgentFactory.create_agent_from_config(
            agent_config, logs_dir=root / "results/deepswe/preflight-not-created"
        )
        if getattr(agent, "resume_from", None) is not None:
            from benchmarks.deepswe.rsi_resume import inspect_resume, resume_summary
            try:
                resume = resume_summary(inspect_resume(agent.resume_from, owner=agent))
            except Exception as error:
                blockers.append(f"RSI resume validation failed: {error}")
        role_configs = (
            agent.role_configs()
            if hasattr(agent, "role_configs")
            else {"actor": agent_config.kwargs["bitfun_config"]}
        )
        required = set().union(
            *(credential_variables(cfg) for cfg in role_configs.values())
        )
        missing = sorted(name for name in required if not os.environ.get(name))
        roles = {}
        if hasattr(agent, "role_configs"):
            from benchmarks.deepswe.bitfun_agent import BitfunCli

            for role, role_config in role_configs.items():
                probe = BitfunCli(
                    logs_dir=root / "results/deepswe/preflight-not-created",
                    bitfun_config=role_config,
                )
                roles[role] = {
                    "context_policy": role_config.get("ai", {}).get("context_policy", {"mode": "native"}),
                    "model_endpoints": probe.network_allowlist().domains,
                    "default_models": role_config.get("ai", {}).get(
                        "default_models", {}
                    ),
                }

            # baseline 运行要求在 BitfunDockerEnvironment 这套环境上。
            if (
                config.environment.import_path
                != "benchmarks.deepswe.docker_environment:BitfunDockerEnvironment"
            ):
                blockers.append("Baseline requires BitfunDockerEnvironment")

            # Baseline's Pier adapter uses the same working clock, preserving
            # this margin for cleanup while excluding confirmed transport pauses.
            for task in tasks:
                from pier.trial.execution import TrialExecution

                limit = TrialExecution._resolve_agent_timeout(
                    agent_config=agent_config,
                    task_agent_timeout_sec=task["agent_timeout_sec"],
                    timeout_multiplier=config.timeout_multiplier,
                    agent_timeout_multiplier=config.agent_timeout_multiplier,
                )
                if task["has_steps"]:
                    blockers.append("Baseline work-budget adapter requires single-step tasks")
                if agent.name() != "bitfun-rsi" and limit is not None and agent.total_timeout_sec >= limit:
                    blockers.append(
                        f"{task['task']}: baseline total_timeout_sec must be below Pier's agent work timeout"
                    )

        if require_credentials and missing:
            blockers.append("Missing credential variables: " + ", ".join(missing))

        # 记录被评测的 bitfun agent 的信息
        agents.append(
            {
                "name": agent.name(),
                "model_name": agent_config.model_name,
                "import_path": agent_config.import_path,
                "model_endpoints": agent.network_allowlist().domains,
                "missing_credentials": missing,
                **({"roles": roles} if roles else {}),
            }
        )

    job_dir = config.jobs_dir / config.job_name
    if not tasks:
        blockers.append("No tasks selected")
    if job_dir.exists():
        blockers.append(f"Output already exists; use a new --name: {job_dir}")

    return {
        "status": "blocked" if blockers else "ready",
        "model_calls": 0,
        "containers_started": 0,
        "tasks": tasks,
        "agents": agents,
        "binaries": binaries,
        "output": str(job_dir),
        "attempts": config.n_attempts,
        "concurrency": config.n_concurrent_trials,
        "blockers": blockers,
        **({"resume": resume} if resume is not None else {}),
    }


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "run", "smoke"))
    parser.add_argument("--mode", choices=tuple(AGENT_PLUGINS), default="native")
    parser.add_argument(
        "--config", type=Path, help="Defaults to the selected mode's configuration"
    )
    parser.add_argument(
        "--env-file", type=Path, default=ROOT / ".runtime/deepswe/runtime.env"
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--task", action="append", help="Task name or glob; repeat for a batch")
    selection.add_argument("--tasks-file", type=Path, help="One task name or glob per line; # comments allowed")
    selection.add_argument("--all-tasks", action="store_true", help="Explicitly select every dataset task")
    parser.add_argument("--model", help="Override every role's primary model")
    parser.add_argument("--actor-model", help="Override the Actor model after --model")
    parser.add_argument("--verifier-model", help="Override the Verifier model after --model")
    parser.add_argument("--curriculum-model", help="Override the RSI Curriculum model after --model")
    parser.add_argument("--base-url", help="Override all enabled model endpoints")
    parser.add_argument("--concurrency", type=integer_at_least(1), help="Concurrent benchmark tasks")
    parser.add_argument("--attempts", type=integer_at_least(1), help="Attempts per task")
    parser.add_argument("--practice-parallel", type=integer_at_least(1), help="Concurrent RSI practice branches")
    parser.add_argument("--brs-project-budget", type=integer_at_least(0), help="RSI BRS project boundary")
    parser.add_argument("--name", help="New output directory name; generated when omitted")
    parser.add_argument("--resume-from", type=Path,
                        help="Resume a stopped RSI trial at a committed BRS wave in a new job")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.tasks_file is not None:
        args.tasks_file = args.tasks_file.resolve()
    os.chdir(ROOT)
    os.umask(0o077)

    from dotenv import load_dotenv

    # 加载环境变量和配置文件
    if args.env_file.exists():
        load_dotenv(args.env_file, override=False)
    try:
        if args.resume_from is not None:
            overrides = (args.config, args.task, args.tasks_file, args.model, args.actor_model,
                         args.verifier_model, args.curriculum_model, args.base_url, args.concurrency,
                         args.attempts, args.practice_parallel, args.brs_project_budget)
            if (args.mode != "rsi" or not args.name or args.action == "smoke"
                    or args.all_tasks or any(value is not None for value in overrides)):
                parser.error("--resume-from requires --mode rsi and a new --name; "
                             "experiment overrides are not allowed")
            from benchmarks.deepswe.rsi_resume import load_resume_job
            config = load_resume_job(args.resume_from, args.name)
        else:
            label = "smoke" if args.action == "smoke" else args.mode
            name = args.name or (f"bitfun-{label}-"
                                f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}")
            config = apply_overrides(load_job(
                select_config(args.mode, args.config), name=name, mode=args.mode), args)
        if args.action == "smoke":
            if (args.mode != "native" or len(config.agents) != 1
                    or config.n_attempts != 1 or config.n_concurrent_trials != 1):
                parser.error("smoke requires native mode, one agent, one attempt and concurrency 1")
            from benchmarks.deepswe.live_smoke import prepare_smoke
            prepare_smoke(config, root=ROOT)
    except (ValueError, OSError) as error:
        parser.exit(2, f"Configuration failed: {error}\n")

    async def execute():
        report = await check_job(config, require_credentials=args.action != "check")
        if args.action == "smoke" and len(report["tasks"]) != 1:
            report["blockers"].append("smoke requires exactly one selected task")
            report["status"] = "blocked"
        print(json.dumps(report, indent=2), flush=True)
        if report["blockers"]:
            return 2
        if args.action == "check":
            return 0
        from pier.environments.factory import EnvironmentFactory
        from pier.job import Job

        # 起 Docker 容器之前最后一遍检查环境，然后启动 Job
        EnvironmentFactory.run_preflight(
            type=config.environment.type, import_path=config.environment.import_path
        )
        job = await Job.create(config)
        result = await job.run()
        print(f"Job results: {job.job_dir / 'result.json'}", flush=True)
        if args.action == "smoke":
            from benchmarks.deepswe.live_smoke import smoke_receipt
            receipt = smoke_receipt(job.job_dir, result)
            print(json.dumps(receipt, indent=2), flush=True)
            return 0 if receipt["status"] == "passed" else 1
        return 0 if result.stats.n_errored_trials == 0 else 1

    def terminate(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        return asyncio.run(execute())
    except (ValueError, FileNotFoundError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"Preflight failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
