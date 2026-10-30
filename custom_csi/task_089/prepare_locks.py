#!/usr/bin/env python3
"""Refresh task-089 lock templates using the shared CSI model configuration."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
PROFILE = Path(__file__).resolve().parent
ROUTE_REL = "custom_csi/route.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from custom_csi.run import _resolved_role_routes

ROUTE = json.loads((ROOT / ROUTE_REL).read_text(encoding="utf-8"))
ROLES = _resolved_role_routes(ROUTE)
ACTOR_MODEL = ROUTE["model_map"]["z-ai/glm-5.3"]
VERIFIER_MODEL = ROUTE["model_map"]["moonshotai/kimi-k3"]
ACTOR_CONFIG = ROOT / "config/osworld/baseline.yaml"
VERIFIER_CONFIG = ROOT / "config/roles/verifier.yaml"
ESCALATED_VERIFIER_CONFIG = ROOT / "config/roles/verifier_escalation.yaml"


def read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def update_common(lock: dict) -> None:
    lock["actor_agent"].update({
        "model": "z-ai/glm-5.3",
        "transport_model": ACTOR_MODEL,
        "transport_route": ROUTE_REL,
        "config": "config/osworld/baseline.yaml",
        "sha256": sha(ACTOR_CONFIG),
    })
    lock["verifier_agent"].update({
        "model": "moonshotai/kimi-k3",
        "transport_model": VERIFIER_MODEL,
        "transport_route": ROUTE_REL,
        "config": "config/roles/verifier.yaml",
        "sha256": sha(VERIFIER_CONFIG),
    })
    escalated = lock.get("escalated_verifier_agent")
    if isinstance(escalated, dict):
        escalated.update({
            "model": "z-ai/glm-5.3",
            "transport_model": ACTOR_MODEL,
            "config": "config/roles/verifier_escalation.yaml",
            "sha256": sha(ESCALATED_VERIFIER_CONFIG),
        })
    lock["evaluator"].update({
        "provider": ROUTE["transport"],
        "model": ROLES["sealed_evaluator"]["wire_model"],
        "base_url": ROUTE["base_url"],
        "api_key_env": ROUTE["api_key_env"],
        "website_host_suffix": "site.hku.icu",
    })
    lock["user_simulator"].update({
        "provider": ROUTE["transport"],
        "model": ROLES["user_simulator"]["wire_model"],
        "base_url": ROUTE["base_url"],
        "api_key_env": ROUTE["api_key_env"],
    })


baseline = read(ROOT / "custom_csi/baseline.lock.json")
baseline["experiment"] = "baseline_custom_csi_task089_multimodel"
baseline["tag"] = "baseline-csi-task089-multimodel"
update_common(baseline)
baseline["escalation"].update({
    "actor_transport_model": VERIFIER_MODEL,
    "actor_provider_routing": "custom_openai_compatible_recorded",
    "verifier_transport_model": ACTOR_MODEL,
})
baseline["visual_eye"].update({
    "transport_model": VERIFIER_MODEL,
    "provider_routing": "custom_openai_compatible_recorded",
})

evaluation = read(ROOT / "custom_csi/evaluation.lock.json")
evaluation["experiment"] = "frozen_memory_task089_multimodel"
update_common(evaluation)

write(PROFILE / "baseline.lock.json", baseline)
write(PROFILE / "evaluation.lock.json", evaluation)
print("generated", PROFILE / "baseline.lock.json")
print("generated", PROFILE / "evaluation.lock.json")
