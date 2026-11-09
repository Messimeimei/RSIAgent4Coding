#!/usr/bin/env python3
"""执行 DeepSWE 任务的统一入口，包括原生态 Pier 评测 bitfun；RSIAgent 的 baseline 和 RSIAgent 两种方法"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AGENT_PLUGINS = {
    "native": "benchmarks.deepswe.bitfun_agent:BitfunCli",
    "baseline": "benchmarks.deepswe.baseline:BitfunBaseline",
    "rsi": "benchmarks.deepswe.rsi:BitfunRSI",
}


def select_config(mode, path=None):
    # 默认的配置文件路径，可传入 --config 覆盖
    filename = {"baseline": "baseline-bitfun.yaml", "rsi": "rsi-bitfun.yaml"}.get(
        mode, "dateutil-bitfun.yaml")
    return path if path is not None else ROOT / "config/deepswe" / filename


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
            dataset.task_names = [task]
    for mount in config.environment.mounts or []:
        if mount["type"] == "bind":
            mount["source"] = str((root / mount["source"]).resolve())
    if name:
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("Job name must be a directory name, not a path")
        config.job_name = name
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

        for tc in await dataset.get_task_configs():
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
                "import_path": agent_config.import_path,
                "model_endpoints": agent.network_allowlist().domains,
                "missing_credentials": missing,
                **({"roles": roles} if roles else {}),
            }
        )

    job_dir = config.jobs_dir / config.job_name
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
        "blockers": blockers,
        **({"resume": resume} if resume is not None else {}),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "run"))
    parser.add_argument("--mode", choices=tuple(AGENT_PLUGINS), default="native")
    parser.add_argument(
        "--config", type=Path, help="Defaults to the selected mode's configuration"
    )
    parser.add_argument(
        "--env-file", type=Path, default=ROOT / ".runtime/deepswe/runtime.env"
    )
    parser.add_argument("--task", help="Run just this task from the configured dataset")
    parser.add_argument("--name", help="New output directory name")
    parser.add_argument("--resume-from", type=Path,
                        help="Resume a stopped RSI trial at a committed BRS wave in a new job")
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    os.umask(0o077)

    from dotenv import load_dotenv

    # 加载环境变量和配置文件
    if args.env_file.exists():
        load_dotenv(args.env_file, override=False)
    if args.resume_from is not None:
        if args.mode != "rsi" or not args.name or args.config is not None or args.task is not None:
            parser.error("--resume-from requires --mode rsi and a new --name; "
                         "--config/--task cannot override the saved experiment")
        from benchmarks.deepswe.rsi_resume import load_resume_job
        try:
            config = load_resume_job(args.resume_from, args.name)
        except Exception as error:
            parser.exit(2, f"RSI resume validation failed: {error}\n")
    else:
        config = load_job(
            select_config(args.mode, args.config), task=args.task, name=args.name, mode=args.mode)

    async def execute():
        report = await check_job(config, require_credentials=args.action == "run")
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
