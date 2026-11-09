#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /data/k8s/yx/RSIAgent4Coding
export PYTHONPATH="/data/k8s/yx/RSIAgent4Coding/.runtime/deepswe:${PYTHONPATH:-}"
exec env --unset=API_KEY PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
  .runtime/deepswe/venv/bin/python -u .runtime/deepswe/phase3_eval/run_dateutil_phase3.py run \
  --name bitfun-dateutil-rsi-phase3-20261108-01
