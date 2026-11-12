#!/usr/bin/env bash
set -euo pipefail
cd /home/messi/pyprojects/RSIAgent4Coding
umask 077
mkdir -p .runtime/deepswe/logs
export LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONUNBUFFERED=1
trap 'status=$?; printf "%s\n" "$status" > .runtime/deepswe/logs/rsi-tomlkit-20261110-5h-01.exit-code' EXIT
.runtime/deepswe/venv/bin/python run_deepswe.py run \
  --mode rsi \
  --task tomlkit-toml-table-converters \
  --concurrency 1 \
  --practice-parallel 1 \
  --name rsi-tomlkit-20261110-5h-01 \
  2>&1 | tee .runtime/deepswe/logs/rsi-tomlkit-20261110-5h-01.console.log
