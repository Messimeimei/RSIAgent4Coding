#!/usr/bin/env python3
"""Launch dateutil Phase-3 frozen-memory evaluation without changing repo source."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

ROOT = Path("/data/k8s/yx/RSIAgent4Coding")
RUNTIME = ROOT / ".runtime/deepswe"
PHASE3 = RUNTIME / "phase3_eval"
DEFAULT_CONFIG = PHASE3 / "dateutil-phase3.yaml"
DEFAULT_NAME = "bitfun-dateutil-rsi-phase3-20261108-01"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "run"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument("--env-file", type=Path, default=RUNTIME / "runtime.env")
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    os.umask(0o077)
    sys.path.insert(0, str(RUNTIME))
    sys.path.insert(0, str(ROOT))

    from dotenv import load_dotenv
    from run_deepswe import check_job, load_job

    if args.env_file.exists():
        load_dotenv(args.env_file, override=False)

    config = load_job(args.config, task="dateutil-rfc5545-timezone-interop",
                      name=args.name, mode=None)

    async def execute() -> int:
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
    return asyncio.run(execute())


if __name__ == "__main__":
    raise SystemExit(main())
