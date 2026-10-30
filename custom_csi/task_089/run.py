#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import sys


PROFILE = Path(__file__).resolve().parent
ROOT = PROFILE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["RSIAGENT_CSI_PROFILE_DIR"] = str(PROFILE)

from custom_csi.run import main


if __name__ == "__main__":
    raise SystemExit(main(default_task="task_089"))
