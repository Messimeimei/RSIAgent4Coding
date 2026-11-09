"""Opt-in reference context policies for the patched BitFun runtime."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path


IMPLEMENTATION = "rsi-context-policy-v1"
WORK_LOG = {
    "history_keep_pairs": 80,
    "keep_chars": 500000,
    "ctx_high_water": 900000,
    "ctx_low_water": 500000,
    "fold_batch": 10,
    "worklog_max_chars": 500000,
}


def for_role(config: dict, role: str, *, enabled: bool) -> dict:
    """Policy follows the role, including model swaps; never mutate shared YAML aliases."""
    if not enabled:
        return config
    if role not in {"target_actor", "practice_actor", "verifier", "curriculum"}:
        raise ValueError(f"Unknown context policy role: {role}")
    result = copy.deepcopy(config)
    policy = {"mode": "full_history"}
    if role == "target_actor":
        policy = {"mode": "work_log", "work_log": dict(WORK_LOG)}
    result.setdefault("ai", {})["context_policy"] = policy
    return result


def requires_patched_runtime(config: dict) -> bool:
    return config.get("ai", {}).get("context_policy", {}).get("mode", "native") != "native"


def validate_capabilities(output: str) -> dict:
    try:
        capabilities = json.loads(output)
    except (ValueError, TypeError) as exc:
        raise ValueError("BitFun does not expose context-policy capabilities") from exc
    if not isinstance(capabilities, dict) or (
        capabilities.get("schema_version") != 1
        or capabilities.get("implementation") != IMPLEMENTATION
        or capabilities.get("first_turn_window_fix") is not True
        or capabilities.get("strict_capacity") is not True
        or capabilities.get("persisted_policy") is not True
        or not isinstance(capabilities.get("modes"), list)
        or not {"full_history", "work_log"}.issubset(capabilities.get("modes", []))
    ):
        raise ValueError("BitFun does not implement the required reference context policy")
    return capabilities


def verify_build_receipt(binary: Path, receipt_path: Path, root: Path) -> dict:
    """Validate the opt-in binary against a source/patch-bound local build receipt."""
    receipt = json.loads(receipt_path.read_text())
    manifest = json.loads((root / "patches/bitfun/context-policy.json").read_text())
    patch = root / "patches/bitfun/context-policy.patch"
    patch_digest = hashlib.sha256(patch.read_bytes()).hexdigest()
    if patch_digest != manifest["patch_sha256"]:
        raise ValueError("BitFun context-policy patch differs from its manifest")
    expected = {
        "implementation": IMPLEMENTATION,
        "source_commit": manifest["source_commit"],
        "patch_sha256": patch_digest,
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise ValueError(f"BitFun context-policy build receipt mismatch: {key}")
    validate_capabilities(json.dumps(receipt.get("capabilities")))
    return receipt
