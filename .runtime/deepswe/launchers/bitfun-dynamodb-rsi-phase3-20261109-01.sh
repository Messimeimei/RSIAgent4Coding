#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /data/k8s/yx/RSIAgent4Coding
export PYTHONPATH="/data/k8s/yx/RSIAgent4Coding/.runtime/deepswe:${PYTHONPATH:-}"
exec env --unset=API_KEY PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
  .runtime/deepswe/venv/bin/python -u .runtime/deepswe/phase3_eval/run_phase3_frozen.py run \
  --config .runtime/deepswe/phase3_eval/dynamodb-phase3.yaml \
  --task dynamodb-toolbox-lazy-recursive-schemas \
  --name bitfun-dynamodb-rsi-phase3-20261109-01
