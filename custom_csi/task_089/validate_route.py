#!/usr/bin/env python3
"""Validate the shared CSI route at its actual primary request budget."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
PROFILE = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=PROFILE.parent / ".env")
    parser.add_argument("--route-file", type=Path, default=PROFILE.parent / "route.json")
    parser.add_argument("--output", type=Path, required=True,
                        help="new directory for probe.json")
    args = parser.parse_args()
    os.environ["RSIAGENT_CSI_PROFILE_DIR"] = str(PROFILE)
    from custom_csi import run

    route_bytes = args.route_file.expanduser().resolve().read_bytes()
    route = json.loads(route_bytes)
    run._comparison_contract(route, locks=run._resolved_locks(route))
    output = args.output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    env_file = args.env_file.expanduser().resolve()
    run._load_credentials(env_file, api_key_env=route["api_key_env"])
    output.mkdir(parents=True)
    route_path = output / "route.json"
    route_path.write_bytes(route_bytes)
    environment = run._environment(env_file, route_path=route_path)
    for key, value in environment.items():
        os.environ[key] = value
    if route["api_key_env"] != "OPENROUTER_API_KEY":
        os.environ.pop("OPENROUTER_API_KEY", None)
    run._probe(output, route_path=route_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
