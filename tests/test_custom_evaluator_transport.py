"""Sealed OSWorld evaluation may use a lock-selected named credential."""
import hashlib
import json
import os
from types import SimpleNamespace

from benchmarks.osworld.runtime import (
    _configure_sealed_evaluator,
    _install_evaluator_token_floor,
)
import benchmarks.osworld.pipeline as pipeline
from config.settings import load


def test_sealed_evaluator_uses_lock_api_key_env(monkeypatch, tmp_path):
    config = tmp_path / "actor.yaml"
    config.write_text("model: z-ai/glm-5.3\n", encoding="utf-8")
    lock = tmp_path / "evaluation.lock.json"
    lock.write_text(json.dumps({
        "actor_agent": {
            "model": "z-ai/glm-5.3",
            "sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        },
        "evaluator": {
            "provider": "openai_compatible",
            "model": "Qwen3.7-Plus",
            "base_url": "http://router.invalid/v1",
            "api_key_env": "CSI_TEST_KEY",
            "website_host_suffix": "site.invalid",
            "retry_attempts": 2,
            "retry_delay_seconds": 0,
            "visibility": "sealed_after_actor_verifier_loop",
        },
    }), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("CSI_TEST_KEY=private-test-value\n", encoding="utf-8")
    monkeypatch.setenv("RSIAGENT_ENV_FILE", str(env_file))
    monkeypatch.delenv("CSI_TEST_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    receipt = _configure_sealed_evaluator(lock, load(str(config)), config)

    assert receipt["api_key_env"] == "CSI_TEST_KEY"
    assert receipt["model"] == "Qwen3.7-Plus"
    assert receipt["credential"] == "configured"
    assert "private-test-value" not in json.dumps(receipt)
    assert os.environ["CSI_TEST_KEY"] == "private-test-value"
    assert "OPENROUTER_API_KEY" not in os.environ


def test_custom_evaluator_token_floor_is_opt_in(monkeypatch):
    from benchmarks.osworld import runtime

    monkeypatch.syspath_prepend(str(runtime.OSWORLD_ROOT))
    from desktop_env.evaluators import model_client

    seen = []

    def fake(prompt, image_paths=None, options=None, **kwargs):
        seen.append(dict(options or {}))
        return "READY"

    monkeypatch.setattr(model_client, "generate_text", fake)
    monkeypatch.delattr(
        model_client, "_rsiagent_original_generate_text", raising=False)
    monkeypatch.setenv("RSIAGENT_EVAL_MIN_OUTPUT_TOKENS", "512")

    assert _install_evaluator_token_floor() == 512
    assert model_client.generate_text(
        "short", options={"max_tokens": 10}) == "READY"
    assert model_client.generate_text(
        "long", options={"max_tokens": 1024}) == "READY"
    assert seen == [{"max_tokens": 512}, {"max_tokens": 1024}]


def test_phase3_child_inherits_protocol_selected_evaluator_lock(
        monkeypatch, tmp_path):
    run_root = tmp_path / "study"
    source = run_root / "phase2/memory_frozen"
    source.mkdir(parents=True)
    (source / "lesson.md").write_text("grounded lesson", encoding="utf-8")
    source_record = pipeline._memory_record(source)
    pipeline._write_json_atomic(run_root / "phase2/result.json", {
        "status": "complete", "memory_frozen": source_record})
    config = tmp_path / "baseline.yaml"
    config.write_text("model: z-ai/glm-5.3\n", encoding="utf-8")
    selected_lock = tmp_path / "csi-evaluation.lock.json"
    selected_lock.write_text("{}\n", encoding="utf-8")
    spec = {
        "run_name": "custom_lock_test",
        "task_release": "osworld-v2-2026.08.08",
        "phase3": {"seed": 93080},
        "_resolved": {
            "study_design": "target_conditioned_adaptation",
            "run_root": run_root,
            "held_out_tasks": ("task_080",),
            "configs": {"phase3": {
                "config": config, "benchmark_lock": selected_lock}},
        },
    }
    monkeypatch.setattr(pipeline, "RSIAGENT_ROOT", tmp_path)
    monkeypatch.setattr(
        pipeline, "_configure_phase3_evaluator", lambda *_: {})
    monkeypatch.setattr(
        pipeline, "_configure_release_environment", lambda *_, **__: {})
    seen = []

    def launch(_command, **kwargs):
        environment = kwargs["environment"]
        seen.append(environment["RSIAGENT_BENCHMARK_LOCK"])
        frozen = run_root / "phase3/memory_frozen"
        memory = pipeline._memory_record(frozen)
        result = (tmp_path / "results/task_080"
                  / "seed93080_custom_lock_test_phase3/result.json")
        pipeline._write_json_atomic(result, {
            "score": 0.5, "status": "done",
            "official_evaluator_attempts": 1,
            "official_evaluator_feedback_entered_agent": False,
            "memory": {**memory, "host_writeback": False},
        })
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(pipeline, "_run", launch)
    result = pipeline.execute_phase3(spec)

    assert result["mean_score"] == 0.5
    assert seen == [str(selected_lock)]
