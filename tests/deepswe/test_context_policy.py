import copy
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from benchmarks.deepswe.bitfun_agent import BitfunCli
from benchmarks.deepswe.context_policy import (
    IMPLEMENTATION, WORK_LOG, for_role, requires_patched_runtime, validate_capabilities,
    verify_build_receipt,
)
from benchmarks.deepswe.rsi import BitfunRSI


@pytest.fixture
def model():
    return {"ai": {"models": [{"id": "test", "model_name": "test",
        "base_url": "http://model.example/v1", "api_key": "${TEST_KEY}"}]}}


def test_role_policy_is_independent_of_model_and_yaml_alias(model):
    original = copy.deepcopy(model)
    actor = for_role(model, "target_actor", enabled=True)
    assert actor["ai"]["context_policy"] == {"mode": "work_log", "work_log": WORK_LOG}
    assert for_role(actor, "verifier", enabled=True)["ai"]["context_policy"] == {"mode": "full_history"}
    assert model == original
    assert for_role(model, "target_actor", enabled=False) is model


def test_rsi_practice_and_target_use_distinct_policies(model, tmp_path):
    owner = BitfunRSI(logs_dir=tmp_path / "rsi", bitfun_config=model,
        reference_context_policy=True)
    assert all(c["ai"]["context_policy"]["mode"] == "full_history"
               for c in owner.role_configs().values())
    target = owner.baseline(tmp_path / "target", {}, training=True)
    assert target.reference_context_policy is True
    # The target seat applies its policy after choosing the model, including swaps.
    cfg = for_role(target._bitfun_config, "target_actor", enabled=target.reference_context_policy)
    assert cfg["ai"]["context_policy"]["mode"] == "work_log"


def test_unpatched_binary_is_rejected_before_any_model_request(model, tmp_path):
    agent = BitfunCli(logs_dir=tmp_path, bitfun_config=for_role(model, "verifier", enabled=True))
    env = SimpleNamespace(exec=AsyncMock(side_effect=[
        SimpleNamespace(return_code=0, stdout="bitfun 0.2.15"),
        SimpleNamespace(return_code=2, stdout=""),
    ]))
    import asyncio
    with pytest.raises(RuntimeError, match="patched BitFun"):
        asyncio.run(agent.setup(env))
    assert env.exec.await_count == 2


def test_capability_contract_rejects_partial_support():
    proof = {"schema_version": 1, "implementation": IMPLEMENTATION,
        "modes": ["native", "full_history", "work_log"], "first_turn_window_fix": True,
        "strict_capacity": True, "persisted_policy": True}
    assert validate_capabilities(json.dumps(proof)) == proof
    proof["strict_capacity"] = False
    with pytest.raises(ValueError):
        validate_capabilities(json.dumps(proof))
    with pytest.raises(ValueError):
        validate_capabilities("bitfun 0.2.15")
    assert not requires_patched_runtime({})


def test_build_receipt_binds_binary_patch_and_source(tmp_path):
    source = Path(__file__).resolve().parents[2]
    patch_root = tmp_path / "patches/bitfun"
    patch_root.mkdir(parents=True)
    for name in ("context-policy.json", "context-policy.patch"):
        (patch_root / name).write_bytes((source / "patches/bitfun" / name).read_bytes())
    manifest = json.loads((patch_root / "context-policy.json").read_text())
    binary = tmp_path / "bitfun"
    binary.write_bytes(b"mock executable")
    receipt = {"implementation": IMPLEMENTATION, "source_commit": manifest["source_commit"],
        "patch_sha256": manifest["patch_sha256"],
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "capabilities": {"schema_version": 1, "implementation": IMPLEMENTATION,
            "modes": ["native", "full_history", "work_log"], "first_turn_window_fix": True,
            "strict_capacity": True, "persisted_policy": True}}
    proof = tmp_path / "receipt.json"
    proof.write_text(json.dumps(receipt))
    assert verify_build_receipt(binary, proof, tmp_path) == receipt
    binary.write_bytes(b"changed executable")
    with pytest.raises(ValueError, match="binary_sha256"):
        verify_build_receipt(binary, proof, tmp_path)
    (patch_root / "context-policy.patch").write_text("changed patch")
    with pytest.raises(ValueError, match="manifest"):
        verify_build_receipt(binary, proof, tmp_path)


def test_config_generation_requires_confirmed_capacity_and_preserves_source(model):
    from tools.configure_bitfun_context_policy import configure
    original = {"agents": [{"import_path": "benchmarks.deepswe.baseline:BitfunBaseline",
        "kwargs": {"bitfun_config": model, "verifier_bitfun_config": model}}],
        "environment": {"mounts": [{"target": "/usr/local/bin/bitfun", "source": "old"}]}}
    before = copy.deepcopy(original)
    with pytest.raises(ValueError, match="confirmed window"):
        configure(original, Path("new"), {})
    with pytest.raises(ValueError, match="no input budget"):
        configure(original, Path("new"), {"test": 65536})
    configured = configure(original, Path("new"), {"test": 262144})
    assert original == before
    kwargs = configured["agents"][0]["kwargs"]
    assert kwargs["reference_context_policy"] is True
    assert kwargs["verifier_bitfun_config"]["ai"]["models"][0]["context_window"] == 262144
    assert configured["environment"]["mounts"][0]["source"] == "new"
