#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /data/k8s/yx/RSIAgent4Coding
exec env --unset=API_KEY PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True COMPOSE_PARALLEL_LIMIT=1 \
 .runtime/deepswe/venv/bin/python -u run_deepswe.py run \
 --mode rsi --config config/deepswe/dynamodb-expr-rsi-p1.yaml \
 --name bitfun-dynamodb-expr-rsi-20261106-p1-01
