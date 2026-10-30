#!/usr/bin/env python3
"""HOST-SIDE ONLY (grader-diagnostic class): build the P2 silent-audit corpus —
8-gram shingles over all locked-release task instructions. The corpus is consumed
exclusively by explore/commit.silent_audit (host process); it never enters any
agent context. Task text is extracted statically so optional task dependencies and
run-generated credentials cannot make the provenance manifest incomplete.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config.runtime_paths import resolve_root, resolve_osworld_root

V2 = str(resolve_osworld_root())
RSIAGENT = str(resolve_root())
for p in (V2, RSIAGENT):
    sys.path.insert(0, p)
os.chdir(V2)

from benchmarks.osworld.task_surface import load_public_task_surface  # noqa: E402
from explore.commit import (CORPUS_NORMALIZATION, build_corpus,    # noqa: E402
                            normalize_instruction_for_corpus)

OUT = os.path.join(RSIAGENT, "results", "explore", "corpus_shingles.json")

lock = json.loads((Path(RSIAGENT) / "config/osworld/baseline.lock.json").read_text())
task_count = int(lock["task_count"])
website_host_suffix = str(lock["website"]["host_suffix"])
instructions, missing = [], []
for i in range(1, task_count + 1):
    tid = f"task_{i:03d}"
    try:
        surface = load_public_task_surface(
            Path(V2), tid, website_host_suffix=website_host_suffix)
        instructions.append(normalize_instruction_for_corpus(surface.instruction))
    except Exception:                                  # noqa: BLE001
        missing.append(tid)

os.makedirs(os.path.dirname(OUT), exist_ok=True)
build_corpus(OUT, instructions)
corpus_bytes = open(OUT, "rb").read()
shingles = json.loads(corpus_bytes)
manifest = {
    "schema_version": 2,
    "benchmark": "OSWorld-V2",
    "evaluation_version": "v2",
    "normalization": CORPUS_NORMALIZATION,
    "instruction_count": len(instructions),
    "absent_task_ids": missing,
    "shingle_count": len(shingles),
    "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
}
with open(OUT + ".MANIFEST.json", "w") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
    f.write("\n")
print(f"corpus built: {len(instructions)} instructions, "
      f"{len(missing)} ids absent, -> {OUT} "
      f"({os.path.getsize(OUT)} bytes, {len(shingles)} shingles, "
      f"sha256={manifest['corpus_sha256']})")
