import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import run_deepswe as runner
from benchmarks.deepswe.live_smoke import LiveSmokeBitfun, prepare_smoke, smoke_receipt


def configured(*argv):
    args = runner.build_parser().parse_args(["check", *argv])
    source = runner.load_job(runner.select_config(args.mode), mode=args.mode)
    return source, runner.apply_overrides(source, args)


def primary(config, role="actor"):
    key = "bitfun_config" if role == "actor" else f"{role}_bitfun_config"
    return config.agents[0].kwargs[key]["ai"]


def test_templates_are_only_three_modes():
    assert {p.stem for p in (runner.ROOT / "config/deepswe").glob("*.yaml")} == {
        "native", "baseline", "rsi",
    }
    config = runner.load_job(runner.select_config("rsi"), mode="rsi")
    assert config.agents[0].kwargs["parallel"] == 1
    assert config.agents[0].kwargs["isolate_failed_projects"] is True
    assert (runner.ROOT / "config/roles/rsi-practice-verifier.yaml").is_file()


@pytest.mark.parametrize("mode", ["baseline", "rsi"])
def test_five_hour_work_budget_preserves_learning_and_cleanup_limits(mode):
    from pier.trial.execution import TrialExecution
    _, job = configured("--mode", mode)
    agent = job.agents[0]
    assert agent.kwargs["total_timeout_sec"] == 18000
    if mode == "baseline":
        outer = TrialExecution._resolve_agent_timeout(
            agent_config=agent, task_agent_timeout_sec=10800,
            timeout_multiplier=job.timeout_multiplier,
            agent_timeout_multiplier=job.agent_timeout_multiplier)
        assert outer == 18600
    else:
        assert agent.kwargs["learning_timeout_sec"] == 86400
        assert agent.kwargs["target_learning_timeout_sec"] == 36000
        assert agent.kwargs["parallel"] == 1


def test_model_precedence_and_alias_independence():
    original, job = configured("--mode", "rsi", "--model", "shared",
        "--actor-model", "actor-new", "--verifier-model", "review-new")
    assert primary(job)["default_models"] == {"primary": "actor-new", "fast": "actor-new"}
    assert primary(job, "verifier")["models"][0]["model_name"] == "review-new"
    assert primary(job, "curriculum")["models"][0]["id"] == "shared"
    assert job.agents[0].model_name == "actor-new"
    assert primary(original)["models"][0]["id"] == "DeepSeek-V4.1-Flash-1017"
    primary(job, "verifier")["models"][0]["base_url"] = "https://review.example/v1"
    assert primary(job, "curriculum")["models"][0]["base_url"] != "https://review.example/v1"
    assert primary(job)["models"][0]["api_key"] == "${API_KEY}"


def test_default_baseline_alias_is_not_mutated_by_actor_override():
    _, job = configured("--mode", "baseline", "--actor-model", "different")
    assert primary(job, "verifier")["models"][0]["id"] == "DeepSeek-V4.1-Flash-1017"


@pytest.mark.parametrize("url,port", [("https://model.example/v1", 443),
    ("http://model.example:9000/v1", 9000)])
def test_endpoint_updates_all_roles_and_proxy_ports(url, port):
    _, job = configured("--mode", "rsi", "--base-url", url)
    assert job.environment.kwargs["inference_ports"] == [port]
    for role in ("actor", "verifier", "curriculum"):
        assert primary(job, role)["models"][0]["base_url"] == url


@pytest.mark.parametrize("url", ["model.example", "ftp://model.example", "https://user:pass@model.example",
    "https://model.example?api_key=secret", "https://model.example:99999/v1"])
def test_invalid_or_credential_bearing_url_rejected(url):
    with pytest.raises(ValueError):
        configured("--base-url", url)


def test_task_lists_clear_template_filters_and_deduplicate(tmp_path):
    listing = tmp_path / "tasks.txt"
    listing.write_text("# Batch\nabs-*\n\nexpr-try-catch-errors # one task\nabs-*\n")
    _, job = configured("--tasks-file", str(listing))
    assert job.datasets[0].task_names == ["abs-*", "expr-try-catch-errors"]
    assert job.datasets[0].exclude_task_names is None
    _, job = configured("--task", "abs-*", "--task", "expr-try-catch-errors")
    assert len(job.datasets[0].task_names) == 2
    _, job = configured("--all-tasks")
    assert job.datasets[0].task_names is None
    listing.write_text("# nothing\n")
    with pytest.raises(ValueError, match="no task names"):
        configured("--tasks-file", str(listing))


def test_rsi_concurrency_and_practice_are_separate():
    _, job = configured("--mode", "rsi", "--concurrency", "1", "--practice-parallel", "3",
        "--attempts", "2", "--brs-project-budget", "0")
    assert job.n_concurrent_trials == 1
    assert job.n_attempts == 2
    assert job.agents[0].kwargs["parallel"] == 3
    assert job.agents[0].kwargs["brs_project_budget"] == 0


@pytest.mark.parametrize("flags", [["--concurrency", "0"], ["--attempts", "-1"],
    ["--practice-parallel", "0"], ["--brs-project-budget", "-1"],
    ["--all-tasks", "--task", "anything"]])
def test_invalid_flags_rejected(flags):
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(["check", *flags])


@pytest.mark.parametrize("flags", [["--verifier-model", "x"], ["--curriculum-model", "x"],
    ["--practice-parallel", "1"], ["--mode", "baseline", "--brs-project-budget", "1"]])
def test_options_for_wrong_mode_rejected(flags):
    with pytest.raises(ValueError):
        configured(*flags)


@pytest.mark.parametrize("flags", [["--model", "x"], ["--all-tasks"], ["--task", "x"],
    ["--base-url", "https://model.example/v1"], ["--concurrency", "1"]])
def test_resume_rejects_experiment_overrides(flags, tmp_path):
    with pytest.raises(SystemExit) as error:
        runner.main(["check", "--env-file", str(tmp_path / "absent.env"), "--mode", "rsi",
            "--resume-from", str(tmp_path), "--name", "new-job", *flags])
    assert error.value.code == 2


def test_generated_names_are_unique_and_checks_do_not_start_jobs(monkeypatch, tmp_path):
    names = []
    async def check(job, **kwargs):
        names.append(job.job_name)
        return {"tasks": [{}], "blockers": [], "status": "ready"}
    monkeypatch.setattr(runner, "check_job", check)
    for _ in range(2):
        assert runner.main(["check", "--env-file", str(tmp_path / "absent.env")]) == 0
    assert names[0] != names[1]
    assert all(name.startswith("bitfun-native-") for name in names)


def test_each_task_selector_is_checked():
    _, job = configured("--task", "dateutil-rfc5545-timezone-interop", "--task", "typo-task")
    report = asyncio.run(runner.check_job(job))
    assert "No tasks matched selector: typo-task" in report["blockers"]


def test_live_smoke_uses_real_transport_but_diagnostic_instruction(tmp_path):
    _, job = configured()
    prepare_smoke(job, root=tmp_path)
    assert job.agents[0].override_timeout_sec == 300
    assert job.jobs_dir == tmp_path / "results/deepswe/smoke"
    assert primary(job)["models"][0]["api_key"] == "${API_KEY}"
    from benchmarks.deepswe.bitfun_agent import BitfunCli
    original = BitfunCli.run
    BitfunCli.run = AsyncMock()
    try:
        obj = LiveSmokeBitfun(logs_dir=tmp_path / "logs", **job.agents[0].kwargs)
        asyncio.run(obj.run("DO NOT USE ORIGINAL TASK", object(), object()))
        prompt = BitfunCli.run.call_args.args[0]
        assert "DO NOT USE ORIGINAL TASK" not in prompt
        assert job.agents[0].kwargs["smoke_token"] in prompt
        assert "commit" in prompt
    finally:
        BitfunCli.run = original


def test_live_receipt_fails_without_collected_patch(tmp_path):
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "result.json").write_text("{}")
    (tmp_path / "config.json").write_text(json.dumps({"agents": [{"kwargs": {"smoke_token": "a" * 32}}]}))
    receipt = smoke_receipt(tmp_path, SimpleNamespace(stats=SimpleNamespace(n_errored_trials=0)))
    assert receipt["status"] == "failed"
    assert not receipt["checks"]["committed_smoke_patch_collected"]
    assert receipt["is_benchmark_score"] is False


def test_live_receipt_accepts_completed_pipeline_with_expected_zero_reward(tmp_path):
    trial = tmp_path / "trial"
    for directory in ("artifacts", "verifier", "agent/bitfun/sessions/one"):
        (trial / directory).mkdir(parents=True, exist_ok=True)
    (trial / "result.json").write_text("{}")
    (tmp_path / "config.json").write_text(json.dumps({"agents": [{"kwargs": {"smoke_token": "a" * 32}}]}))
    (trial / "artifacts/model.patch").write_text("rsiagent-live-smoke.txt\n+RSIAgent live model smoke: " + "a" * 32)
    (trial / "agent/trajectory.json").write_text("{}")
    (trial / "agent/bitfun/sessions/one/metadata.json").write_text("{}")
    (trial / "agent/bitfun/sessions/one/request-001.json").write_text("{}")
    (trial / "verifier/test-stdout.txt").write_text("model.patch applied")
    (trial / "verifier/reward.json").write_text(json.dumps({"reward": 0, "p2p_total": 2035}))
    receipt = smoke_receipt(tmp_path, SimpleNamespace(stats=SimpleNamespace(n_errored_trials=0)))
    assert receipt["status"] == "passed"
    assert receipt["official_reward"]["reward"] == 0
    assert receipt["is_benchmark_score"] is False
