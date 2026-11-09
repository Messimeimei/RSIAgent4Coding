#!/usr/bin/env python3
"""Create an opt-in job config with explicitly confirmed endpoint window sizes."""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.deepswe.context_policy import verify_build_receipt


def configure(config, binary, windows):
    config = copy.deepcopy(config)
    agents = config.get("agents", [])
    if not agents:
        raise ValueError("No agents configured")
    for agent in agents:
        if agent.get("import_path") not in {
            "benchmarks.deepswe.baseline:BitfunBaseline", "benchmarks.deepswe.rsi:BitfunRSI"
        }:
            raise ValueError("Reference policies require BitfunBaseline or BitfunRSI")
        kwargs = agent.setdefault("kwargs", {})
        kwargs["reference_context_policy"] = True
        for key in ("bitfun_config", "verifier_bitfun_config", "curriculum_bitfun_config"):
            for model in kwargs.get(key, {}).get("ai", {}).get("models", []):
                if not model.get("enabled", True):
                    continue
                identity = model["id"]
                if identity not in windows:
                    raise ValueError(f"Pass --model-window {identity}=TOKENS for the endpoint's confirmed window")
                if windows[identity] <= (model.get("max_tokens") or 65536) + 10000:
                    raise ValueError(f"{identity}: window leaves no input budget after output and safety reserve")
                model["context_window"] = windows[identity]
    mounts = config["environment"]["mounts"]
    matches = [m for m in mounts if m["target"] == "/usr/local/bin/bitfun"]
    if len(matches) != 1:
        raise ValueError("Expected exactly one BitFun binary mount")
    matches[0]["source"] = str(binary)
    config["job_name"] = config.get("job_name", "bitfun") + "-paper-context"
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path, default=ROOT / ".runtime/bitfun-paper/bin/bitfun")
    parser.add_argument("--model-window", action="append", required=True, metavar="MODEL_ID=TOKENS")
    args = parser.parse_args()
    binary = args.binary.resolve()
    verify_build_receipt(binary, binary.with_name(binary.name + ".receipt.json"), ROOT)
    windows = {}
    for item in args.model_window:
        identity, value = item.rsplit("=", 1)
        if identity in windows:
            raise ValueError(f"Duplicate model window: {identity}")
        windows[identity] = int(value)
    result = configure(yaml.safe_load(args.base.read_text()), binary, windows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        yaml.safe_dump(result, stream, allow_unicode=True, sort_keys=False)
    print(args.output)


if __name__ == "__main__":
    main()
