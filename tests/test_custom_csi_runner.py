"""The CSI runner keeps task lineages independent and pins local runtime inputs."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from benchmarks.osworld.provider import _validated_docker_image_override
from custom_csi import run


def test_environment_uses_route_token_floor_and_pinned_outer_image(
        tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-leak-to-child")

    environment = run._environment(tmp_path / "private.env")

    assert environment["RSIAGENT_EVAL_MIN_OUTPUT_TOKENS"] == "512"
    assert environment["RSIAGENT_DOCKER_IMAGE"].startswith("sha256:")
    assert _validated_docker_image_override(
        environment["RSIAGENT_DOCKER_IMAGE"]) == \
        environment["RSIAGENT_DOCKER_IMAGE"]
    assert "OPENROUTER_API_KEY" not in environment


def test_all_tasks_receive_independent_single_task_protocols(tmp_path):
    tasks = run._task_ids(["all"])

    assert len(tasks) == 108
    assert tasks[0] == "task_001"
    assert tasks[-1] == "task_108"
    for task in tasks:
        numeric = int(task.rsplit("_", 1)[1])
        protocol = run._protocol(
            task, f"batch_{task}_rsi", Path(tmp_path / f"{task}.md"), 8)
        assert protocol["phase2"]["development_tasks"] == [task]
        assert protocol["phase2"]["seed"] == 83000 + numeric
        assert protocol["phase3"]["held_out_tasks"] == [task]
        assert protocol["phase3"]["seed"] == 93000 + numeric
        assert protocol["phase1"]["parallelism"] == 1
        assert protocol["phase1"]["parallel_waves"] is True
        assert protocol["phase1"]["park_verified_branches"] is True


def test_default_profile_preserves_original_csi_paths():
    assert run.PROFILE == run.HERE
    assert run.ROUTE == run.HERE / "route.json"
    assert run.BASELINE_LOCK == run.HERE / "baseline.lock.json"
    assert run.EVALUATION_LOCK == run.HERE / "evaluation.lock.json"
    assert run.PROTOCOL_TEMPLATE == run.HERE / "protocols/task_080.json"
    assert run.BASELINE_CONFIG == run.ROOT / "config/osworld/baseline.yaml"


@pytest.mark.parametrize("profile", ["custom_csi", "custom_csi/task_089"])
def test_profiles_share_comparable_dual_model_conditions(profile, monkeypatch):
    folder = run.ROOT / profile
    monkeypatch.setattr(run, "BASELINE_LOCK", folder / "baseline.lock.json")
    monkeypatch.setattr(run, "EVALUATION_LOCK", folder / "evaluation.lock.json")
    route = run._read_json(folder / "route.json")
    contract = run._comparison_contract(route, locks=run._resolved_locks(route))
    assert contract["actor"] != contract["verifier"]
    assert all(cap == 65536 for cap in route["max_output_tokens_map"].values())


@pytest.mark.parametrize("role,key", [("evaluator", "model"), ("user_simulator", "max_tokens"),
                                     ("actor_agent", "sha256")])
def test_comparison_rejects_changed_evaluation_conditions(role, key):
    route = run._read_json(run.ROUTE)
    baseline, evaluation = run._resolved_locks(route)
    evaluation[role][key] = "drift"
    with pytest.raises(RuntimeError, match="baseline/RSI mismatch"):
        run._comparison_contract(route, locks=(baseline, evaluation))


def test_protocol_honors_capacity_without_disabling_waves(tmp_path):
    value = run._protocol("task_089", "test", tmp_path / "direction.md", 8, 4)
    assert value["phase1"]["parallel_waves"] is True
    assert value["phase1"]["parallelism"] == 4
    assert value["phase1"]["park_verified_branches"] is True


@pytest.mark.parametrize("entry,task", [("custom_csi/run.py", "task_080"),
                                       ("custom_csi/task_089/run.py", "task_089")])
def test_dry_run_requires_no_osworld_venv_or_credentials(entry, task):
    result = subprocess.run([sys.executable, str(run.ROOT / entry), "--dry-run"],
                            capture_output=True, text=True, check=True)
    plan = json.loads(result.stdout)
    assert plan["tasks"] == [task]
    assert plan["parallel_waves"] is True
    assert plan["parallelism"] == 1
    assert [item["arm"] for item in plan["runs"]] == ["baseline", "rsi"]
    assert all(item["comparison"]["actor"] != item["comparison"]["verifier"]
               for item in plan["runs"])
    assert plan["route"]["path"] == str(run.HERE / "route.json")


@pytest.mark.parametrize("entry", ["custom_csi/run.py", "custom_csi/task_089/run.py"])
def test_dry_run_changes_all_models_from_route_only(entry, tmp_path):
    route = run._read_json(run.ROUTE)
    route["model_map"] = {
        "z-ai/glm-5.3": "actor-new", "moonshotai/kimi-k3": "verifier-new"}
    # Older files may still contain descriptive Agent wire names. They must
    # never override the new model_map or require a second manual edit.
    route["role_routes"]["actor_memory_target"]["wire_model"] = "actor-stale"
    route["role_routes"]["sealed_evaluator"]["wire_model"] = "evaluator-new"
    route["role_routes"]["user_simulator"]["wire_model"] = "user-new"
    source = tmp_path / "route.json"
    source.write_text(json.dumps(route))
    result = subprocess.run(
        [sys.executable, str(run.ROOT / entry), "--dry-run", "--route-file", str(source)],
        capture_output=True, text=True, check=True)
    plan = json.loads(result.stdout)
    assert {key: plan["runs"][0]["comparison"][key]
            for key in ("actor", "verifier", "evaluator", "user_simulator")} == {
                "actor": "actor-new", "verifier": "verifier-new",
                "evaluator": "evaluator-new", "user_simulator": "user-new"}
    assert plan["route"]["role_routes"]["actor_memory_target"]["wire_model"] == "actor-new"
    assert plan["route"]["role_routes"]["curriculum"]["wire_model"] == "verifier-new"
    assert all(not Path(item["runtime_config"]["route"]).exists()
               for item in plan["runs"])


@pytest.mark.parametrize("profile", ["custom_csi", "custom_csi/task_089"])
def test_launch_uses_generated_locks_and_frozen_route(profile, tmp_path, monkeypatch):
    from config.benchmark_runtime import configure_associated_benchmark_lock
    from llm import client

    folder = run.ROOT / profile
    for name, file in (("BASELINE_LOCK", "baseline.lock.json"),
                       ("EVALUATION_LOCK", "evaluation.lock.json")):
        monkeypatch.setattr(run, name, folder / file)
    monkeypatch.setattr(run, "PROTOCOL_TEMPLATE", folder / (
        "protocol.json" if profile.endswith("task_089") else "protocols/task_080.json"))
    templates = {path: path.read_bytes() for path in (run.BASELINE_LOCK, run.EVALUATION_LOCK)}
    route = run._read_json(run.ROUTE)
    route["model_map"] = {
        "z-ai/glm-5.3": "actor-new", "moonshotai/kimi-k3": "verifier-new"}
    route["role_routes"]["sealed_evaluator"]["wire_model"] = "evaluator-new"
    route["role_routes"]["user_simulator"]["wire_model"] = "user-new"
    route.update(base_url="https://models.invalid/v1", api_key_env="CSI_TEST_KEY", stream=False)
    route_bytes = json.dumps(route).encode()
    source = tmp_path / "route.json"
    source.write_bytes(route_bytes)
    monkeypatch.setattr(run, "ROUTE", source)
    monkeypatch.setattr(run, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(run, "PYTHON", Path(sys.executable))
    monkeypatch.setattr(os, "environ", {"CSI_TEST_KEY": "test-secret"})

    def probe(output, *, route_path):
        assert route_path.read_bytes() == route_bytes
        assert os.environ["RSIAGENT_LLM_ROUTE_FILE"] == str(route_path)
        # Editing the source after launch must not affect either experimental arm.
        source.write_text("{}")
        receipt = {"status": "passed", "route_path": str(route_path),
                   "route_sha256": hashlib.sha256(route_bytes).hexdigest()}
        run._write_json(output / "probe.json", receipt)
        return receipt

    monkeypatch.setattr(run, "_probe", probe)
    launched = []

    def launch(command, *, env, log):
        launched.append((command, dict(env)))
        return 0

    monkeypatch.setattr(run, "_run_logged", launch)
    assert run.main(["--name", "route_snapshot", "--arm", "both", "--tasks", "task_080",
                     "--env-file", str(tmp_path / "absent.env")]) == 0
    assert len(launched) == 7  # baseline plus preflight/execute for all three RSI phases
    output = run.RESULTS / "custom_csi/route_snapshot_task_080_baseline"
    plan = run._read_json(output / "plan.json")
    paths = {key: Path(value) for key, value in plan["runtime_config"].items()}
    assert paths["route"].read_bytes() == route_bytes
    baseline = run._read_json(paths["baseline_lock"])
    evaluation = run._read_json(paths["evaluation_lock"])
    assert baseline["escalation"]["actor_transport_model"] == "verifier-new"
    assert baseline["escalation"]["verifier_transport_model"] == "actor-new"
    assert baseline["visual_eye"]["transport_model"] == "verifier-new"
    assert evaluation["escalated_verifier_agent"]["transport_model"] == "actor-new"
    for kind in ("baseline", "evaluation"):
        lock_path = paths[kind + "_lock"]
        assert plan["comparison"][kind + "_lock_sha256"] == hashlib.sha256(lock_path.read_bytes()).hexdigest()
        bound = {"RSIAGENT_BENCHMARK_LOCK": str(lock_path), "CSI_TEST_KEY": "test-secret"}
        configure_associated_benchmark_lock(run.BASELINE_CONFIG, repo_root=run.ROOT, environment=bound)
        assert bound["OSWORLD_EVAL_MODEL_NAME"] == "evaluator-new"
        assert bound["OSWORLD_USER_SIM_MODEL"] == "user-new"
        assert bound["OSWORLD_EVAL_MODEL_BASE_URL"] == "https://models.invalid/v1"
        assert bound["OSWORLD_USER_SIM_BASE_URL"] == "https://models.invalid/v1"
        assert bound["OSWORLD_EVAL_MODEL_API_KEY_ENV"] == "CSI_TEST_KEY"
        assert bound["OSWORLD_USER_SIM_API_KEY_ENV"] == "CSI_TEST_KEY"
    rsi_output = run.RESULTS / "custom_csi/route_snapshot_task_080_rsi"
    rsi_plan = run._read_json(rsi_output / "plan.json")
    rsi_paths = {key: Path(value) for key, value in rsi_plan["runtime_config"].items()}
    assert rsi_paths["route"].read_bytes() == route_bytes
    assert run._read_json(rsi_output / "probe.json")["reused_from"] == str(output / "probe.json")
    assert rsi_plan["run_name"] == "route_snapshot_task_080_rsi"
    for index, (command, env) in enumerate(launched):
        selected_paths = paths if index == 0 else rsi_paths
        assert env["RSIAGENT_LLM_ROUTE_FILE"] == str(selected_paths["route"])
        if index == 0:
            assert env["RSIAGENT_BENCHMARK_LOCK"] == str(paths["baseline_lock"])
            assert command[command.index("--tag") + 1] == "route_snapshot_task_080_baseline"
        else:
            protocol = run._read_json(Path(command[command.index("--protocol") + 1]))
            assert protocol["phase3"]["benchmark_lock"] == str(rsi_paths["evaluation_lock"])
            assert protocol["run_name"] == rsi_output.name

    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            provider=None, model_extra={}, usage=None,
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content="ready", reasoning=None, reasoning_content=None, tool_calls=[]))])

    monkeypatch.setattr(client, "_client", SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(client, "_PROVIDER_COUNTS", {})
    assert client.chat("z-ai/glm-5.3", "system", "user") == "ready"
    assert client.chat("moonshotai/kimi-k3", "system", "user") == "ready"
    assert [request["model"] for request in requests] == ["actor-new", "verifier-new"]
    assert all(path.read_bytes() == original for path, original in templates.items())
    assert "test-secret" not in (output / "plan.json").read_text()


def test_multi_task_both_splits_outputs_and_checks_all_collisions_before_launch(
        tmp_path, monkeypatch):
    route = run._read_json(run.ROUTE)
    monkeypatch.setattr(run, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(run, "PYTHON", Path(sys.executable))
    monkeypatch.setattr(os, "environ", {route["api_key_env"]: "test-secret"})
    calls, probes = [], []
    monkeypatch.setattr(run, "_run_logged", lambda command, **kwargs: calls.append(command) or 0)

    def probe(output, *, route_path):
        probes.append(output)
        receipt = {"status": "passed", "route_path": str(route_path),
                   "route_sha256": hashlib.sha256(route_path.read_bytes()).hexdigest()}
        run._write_json(output / "probe.json", receipt)
        return receipt

    monkeypatch.setattr(run, "_probe", probe)
    monkeypatch.setattr(run, "_direction", lambda task: "public instruction " + task)
    args = ["--name", "batch", "--arm", "both", "--tasks", "task_085", "task_106",
            "--env-file", str(tmp_path / "absent.env")]
    assert run.main(args) == 0
    assert len(calls) == 14
    assert len(probes) == 1
    custom = run.RESULTS / "custom_csi"
    assert {p.name for p in custom.iterdir()} == {
        "batch_task_085_baseline", "batch_task_085_rsi",
        "batch_task_106_baseline", "batch_task_106_rsi"}
    for task in ("task_085", "task_106"):
        for arm in ("baseline", "rsi"):
            output = custom / f"batch_{task}_{arm}"
            plan = run._read_json(output / "plan.json")
            assert plan["tasks"] == [task]
            assert plan["arm"] == arm
            assert plan["requested_arm"] == "both"
            assert run._read_json(output / "status.json")["run_name"] == output.name
            assert (output / "probe.json").is_file()
            assert not (output / task).exists()
            if arm == "rsi":
                protocol = run._read_json(output / "protocol.json")
                assert protocol["run_name"] == output.name
                assert Path(protocol["phase1"]["distribution_file"]).parent == output

    before = len(calls)
    with pytest.raises(FileExistsError, match="outputs already exist"):
        run.main(args)
    assert len(calls) == before
    assert len(probes) == 1


@pytest.mark.parametrize("location", ["custom", "baseline", "rsi"])
def test_output_collision_on_later_task_stops_before_creating_any_run(
        location, tmp_path, monkeypatch):
    monkeypatch.setattr(run, "RESULTS", tmp_path / "results")
    targets = {
        "custom": run.RESULTS / "custom_csi/batch_task_106_rsi",
        "baseline": run.RESULTS / "task_106/seed5400_batch_task_106_baseline",
        "rsi": run.RESULTS / "recursive_improvement/batch_task_106_rsi",
    }
    targets[location].mkdir(parents=True)
    with pytest.raises(FileExistsError, match="outputs already exist"):
        run._check_outputs(["task_085", "task_106"], "batch", "both")
    assert not (run.RESULTS / "custom_csi/batch_task_085_baseline").exists()


def test_same_prefix_can_launch_baseline_then_rsi(tmp_path, monkeypatch):
    route = run._read_json(run.ROUTE)
    monkeypatch.setattr(run, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(run, "PYTHON", Path(sys.executable))
    monkeypatch.setattr(os, "environ", {route["api_key_env"]: "test-secret"})
    monkeypatch.setattr(run, "_direction", lambda _: "public instruction")
    monkeypatch.setattr(run, "_run_logged", lambda *args, **kwargs: 0)
    for arm in ("baseline", "rsi"):
        assert run.main(["--name", "separate", "--tasks", "task_085", "--arm", arm,
                         "--skip-probe", "--allow-unstable-route",
                         "--env-file", str(tmp_path / "absent.env")]) == 0
        assert (run.RESULTS / f"custom_csi/separate_task_085_{arm}/status.json").is_file()


def test_reused_probe_is_recorded_in_each_arm_without_new_calls(tmp_path, monkeypatch):
    route = run._read_json(run.ROUTE)
    monkeypatch.setattr(run, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(run, "PYTHON", Path(sys.executable))
    monkeypatch.setattr(os, "environ", {route["api_key_env"]: "test-secret"})
    receipt = tmp_path / "probe.json"
    run._write_json(receipt, {
        "status": "passed", "route_sha256": hashlib.sha256(run.ROUTE.read_bytes()).hexdigest(),
        "checks": ["long_context_actor", "long_context_json_verifier",
                   "image_input", "primary_output_budget"],
    })

    def unexpected_probe(*args, **kwargs):
        pytest.fail("a valid --probe-receipt should not trigger another probe")

    monkeypatch.setattr(run, "_probe", unexpected_probe)
    monkeypatch.setattr(run, "_direction", lambda _: "public instruction")
    monkeypatch.setattr(run, "_run_logged", lambda *args, **kwargs: 0)
    assert run.main(["--name", "reuse", "--tasks", "task_085", "--arm", "both",
                     "--skip-probe", "--probe-receipt", str(receipt),
                     "--env-file", str(tmp_path / "absent.env")]) == 0
    for arm in ("baseline", "rsi"):
        output = run.RESULTS / f"custom_csi/reuse_task_085_{arm}"
        assert run._read_json(output / "probe.json")["reused_from"] == str(receipt)


def test_failed_baseline_does_not_start_rsi_or_mark_it_complete(tmp_path, monkeypatch):
    route = run._read_json(run.ROUTE)
    monkeypatch.setattr(run, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(run, "PYTHON", Path(sys.executable))
    monkeypatch.setattr(os, "environ", {route["api_key_env"]: "test-secret"})
    calls = []
    monkeypatch.setattr(run, "_run_logged", lambda command, **kwargs: calls.append(command) or 75)
    with pytest.raises(RuntimeError, match="baseline failed"):
        run.main(["--name", "failure", "--tasks", "task_085", "--arm", "both",
                  "--skip-probe", "--allow-unstable-route",
                  "--env-file", str(tmp_path / "absent.env")])
    assert len(calls) == 1
    for arm in ("baseline", "rsi"):
        output = run.RESULTS / f"custom_csi/failure_task_085_{arm}"
        assert (output / "plan.json").is_file()
        assert not (output / "status.json").exists()


@pytest.mark.parametrize("role,key", [("evaluator", "retry_attempts"),
                                     ("user_simulator", "max_tokens"),
                                     ("actor_agent", "config"),
                                     ("actor_agent", "sha256"),
                                     ("actor_agent", "model"),
                                     ("verifier_agent", "config"),
                                     ("verifier_agent", "sha256")])
def test_generated_locks_preserve_non_routing_checks(role, key, tmp_path, monkeypatch):
    value = run._read_json(run.EVALUATION_LOCK)
    value[role][key] = "drift"
    path = tmp_path / "evaluation.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(run, "EVALUATION_LOCK", path)
    route = run._read_json(run.ROUTE)
    with pytest.raises(RuntimeError, match="baseline/RSI mismatch"):
        run._comparison_contract(route, locks=run._resolved_locks(route))


@pytest.mark.parametrize("role", ["actor_agent", "verifier_agent", "escalated_verifier_agent"])
def test_new_locks_accept_config_edits_but_saved_locks_detect_drift(
        role, tmp_path, monkeypatch):
    route = run._read_json(run.ROUTE)
    templates = {path: path.read_bytes() for path in (run.BASELINE_LOCK, run.EVALUATION_LOCK)}
    evaluation = run._read_json(run.EVALUATION_LOCK)
    for name in ("actor_agent", "verifier_agent", "escalated_verifier_agent"):
        relative = evaluation[name]["config"]
        copy = tmp_path / relative
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes((run.ROOT / relative).read_bytes())
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(run, "BASELINE_CONFIG", tmp_path / evaluation["actor_agent"]["config"])
    saved = run._resolved_locks(route)
    run._comparison_contract(route, locks=saved)

    edited = tmp_path / evaluation[role]["config"]
    edited.write_text(edited.read_text().replace("max_tokens: 65536", "max_tokens: 32768", 1))
    expected_hash = hashlib.sha256(edited.read_bytes()).hexdigest()
    assert expected_hash != saved[1][role]["sha256"]
    with pytest.raises(RuntimeError, match="config hash mismatch|escalation mismatch"):
        run._comparison_contract(route, locks=saved)

    baseline, current = run._resolved_locks(route)
    run._comparison_contract(route, locks=(baseline, current))
    assert current[role]["sha256"] == expected_hash
    if role == "escalated_verifier_agent":
        assert baseline["escalation"]["verifier_config_sha256"] == expected_hash
    else:
        assert baseline[role]["sha256"] == expected_hash
    assert all(path.read_bytes() == original for path, original in templates.items())


@pytest.mark.parametrize("key", ["config", "sha256"])
def test_new_locks_reject_disagreeing_escalation_templates(key, tmp_path, monkeypatch):
    evaluation = run._read_json(run.EVALUATION_LOCK)
    evaluation["escalated_verifier_agent"][key] = "drift"
    path = tmp_path / "evaluation.json"
    path.write_text(json.dumps(evaluation))
    monkeypatch.setattr(run, "EVALUATION_LOCK", path)
    with pytest.raises(RuntimeError, match="baseline/RSI escalation mismatch"):
        run._resolved_locks(run._read_json(run.ROUTE))


def test_generated_locks_reject_identical_actor_and_verifier():
    route = run._read_json(run.ROUTE)
    route["model_map"]["z-ai/glm-5.3"] = route["model_map"]["moonshotai/kimi-k3"]
    with pytest.raises(RuntimeError, match="different Actor and Verifier"):
        run._comparison_contract(route, locks=run._resolved_locks(route))


@pytest.mark.parametrize("role", ["sealed_evaluator", "user_simulator"])
@pytest.mark.parametrize("value", [None, "", "  ", 7])
def test_empty_direct_model_fails_before_launch(role, value):
    route = run._read_json(run.ROUTE)
    route["role_routes"][role]["wire_model"] = value
    with pytest.raises(RuntimeError, match=role + r"\.wire_model"):
        run._resolved_locks(route)


def test_probe_exercises_primary_budget_and_invalidates_old_receipts(tmp_path, monkeypatch):
    from llm import client
    calls = []
    replies = iter(["CSI_TEXT_READY", '{"status":"CSI_JSON_READY"}', "CSI_IMAGE_READY"])

    def chat(*args, **kwargs):
        calls.append((args, kwargs))
        return next(replies)

    monkeypatch.setattr(client, "chat", chat)
    monkeypatch.setattr(client, "_client", None)
    monkeypatch.setattr(client, "_PROVIDER_COUNTS", {})
    receipt = run._probe(tmp_path)
    assert len(calls[0][0][2]) > 150000
    assert [c[1]["max_tokens"] for c in calls] == [65536, 65536, 32768]
    assert all(c[1]["temperature"] == 1.0 and c[1]["top_p"] == 1.0 for c in calls)
    assert run._validated_probe(tmp_path / "probe.json") == receipt
    receipt["route_sha256"] = "old-route"
    (tmp_path / "probe.json").write_text(json.dumps(receipt))
    with pytest.raises(RuntimeError, match="current route"):
        run._validated_probe(tmp_path / "probe.json")


def test_old_small_budget_probe_cannot_authorize_run(tmp_path):
    receipt = {"status": "passed", "route_sha256": hashlib.sha256(run.ROUTE.read_bytes()).hexdigest(),
               "checks": ["text_completion", "json_object", "image_input"]}
    path = tmp_path / "probe.json"
    path.write_text(json.dumps(receipt))
    with pytest.raises(RuntimeError, match="output budget"):
        run._validated_probe(path)


def test_probe_rejects_wrong_nonempty_response(tmp_path, monkeypatch):
    from llm import client
    replies = iter(["wrong", '{"status":"CSI_JSON_READY"}'])
    monkeypatch.setattr(client, "chat", lambda *a, **k: next(replies))
    monkeypatch.setattr(client, "_client", None)
    monkeypatch.setattr(client, "_PROVIDER_COUNTS", {})
    with pytest.raises(RuntimeError, match="unusable"):
        run._probe(tmp_path)
    assert not (tmp_path / "probe.json").exists()
