#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /data/k8s/yx/RSIAgent4Coding

.runtime/deepswe/venv/bin/python -u - <<'PY'
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

predecessor = Path('results/deepswe/jobs/bitfun-dateutil-baseline-qwen38-001/result.json')
print(f'{datetime.now().isoformat()} Waiting for {predecessor.parent.name}', flush=True)
dead_polls = 0
while True:
    try:
        result = json.loads(predecessor.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        result = {}
    if result.get('finished_at'):
        print(f'{datetime.now().isoformat()} Predecessor finished; starting remaining 19 tasks', flush=True)
        break
    pane = subprocess.run(
        ['tmux', 'list-panes', '-t', 'deepswe-dateutil-baseline-qwen38', '-F', '#{pane_dead}'],
        capture_output=True, text=True,
    )
    dead = pane.returncode != 0 or pane.stdout.strip() == '1'
    dead_polls = dead_polls + 1 if dead else 0
    if dead_polls >= 3:
        raise SystemExit('Predecessor stopped without a final result; batch not started')
    time.sleep(15)
PY

exec env --unset=API_KEY LITELLM_LOCAL_MODEL_COST_MAP=True \
  .runtime/deepswe/venv/bin/python -u run_deepswe.py run \
  --mode baseline \
  --config config/deepswe/remaining19-baseline-qwen38.yaml \
  --name bitfun-remaining19-baseline-qwen38-20261103-01
