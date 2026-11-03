#!/usr/bin/env python3
"""执行 DeepSWE 任务的统一入口，包括 baseline 和 RSIAgent 两种方法"""

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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_job(path: Path, *, root: Path = ROOT, task=None, name=None):
    import yaml
    from pier.models.job.config import JobConfig

    config = JobConfig.model_validate(yaml.safe_load(path.read_text()))
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

    pins = json.loads((root / "config/deepswe/versions.json").read_text())
    blockers = []
    tasks = []
    for dataset in config.datasets:
        head = subprocess.check_output(
            ["git", "-C", str(dataset.path.parent), "rev-parse", "HEAD"], text=True
        ).strip()
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
            tasks.append(
                {
                    "task": item.name,
                    "base_commit": cfg.metadata.get("base_commit_hash"),
                    "image": cfg.environment.docker_image,
                    "cpu": cfg.environment.cpus,
                    "memory_mb": cfg.environment.memory_mb,
                    "agent_timeout_sec": cfg.agent.timeout_sec,
                    "verifier_timeout_sec": cfg.verifier.timeout_sec,
                    "task_sha256": sha256(tc.path / "task.toml"),
                }
            )
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
                blockers.append("BitFun binary differs from the pinned release")
    agents = []
    for agent_config in config.agents:
        agent = AgentFactory.create_agent_from_config(
            agent_config, logs_dir=root / "results/deepswe/preflight-not-created"
        )
        missing = sorted(
            name
            for name in credential_variables(agent_config.kwargs["bitfun_config"])
            if not os.environ.get(name)
        )
        if require_credentials and missing:
            blockers.append("Missing credential variables: " + ", ".join(missing))
        agents.append(
            {
                "name": agent.name(),
                "import_path": agent_config.import_path,
                "model_endpoints": agent.network_allowlist().domains,
                "missing_credentials": missing,
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
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "run"))
    parser.add_argument(
        "--config", type=Path, default=ROOT / "config/deepswe/dateutil-bitfun.yaml"
    )
    parser.add_argument(
        "--env-file", type=Path, default=ROOT / ".runtime/deepswe/runtime.env"
    )
    parser.add_argument("--task", help="Run just this task from the configured dataset")
    parser.add_argument("--name", help="New output directory name")
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    os.umask(0o077)
    from dotenv import load_dotenv

    if args.env_file.exists():
        load_dotenv(args.env_file, override=False)
    config = load_job(args.config, task=args.task, name=args.name)

    async def execute():
        report = await check_job(config, require_credentials=args.action == "run")
        print(json.dumps(report, indent=2), flush=True)
        if report["blockers"]:
            return 2
        if args.action == "check":
            return 0
        from pier.environments.factory import EnvironmentFactory
        from pier.job import Job

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
