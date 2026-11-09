#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /data/k8s/yx/RSIAgent4Coding
exec env --unset=API_KEY PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
 .runtime/deepswe/venv/bin/python -u run_deepswe.py run \
 --mode rsi \
 --resume-from results/deepswe/jobs/bitfun-dateutil-rsi-20261106-p2-08/dateutil-rfc5545-timezone-intero__dkSrfxA \
 --name bitfun-dateutil-rsi-resumed-02
