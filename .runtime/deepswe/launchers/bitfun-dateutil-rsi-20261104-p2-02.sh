#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /data/k8s/yx/RSIAgent4Coding

exec env --unset=API_KEY PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
  .runtime/deepswe/venv/bin/python -u run_deepswe.py run \
  --mode rsi \
  --config config/deepswe/rsi-bitfun.yaml \
  --task dateutil-rfc5545-timezone-interop \
  --name bitfun-dateutil-rsi-20261104-p2-02
