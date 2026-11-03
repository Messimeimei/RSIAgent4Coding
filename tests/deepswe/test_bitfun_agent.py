import asyncio
import json
import os
import shlex
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from pier.agents.factory import AgentFactory
from pier.agents.installed.base import NonZeroAgentExitCodeError
from pier.models.agent.context import AgentContext
from pier.models.trial.config import AgentConfig

from benchmarks.deepswe.bitfun_agent import BitfunCli, credential_variables
from benchmarks.deepswe._vendor.bitfun_cli import (
    build_repo_baseline_capture_script,
    build_repo_final_capture_script,
)
from run_deepswe import ROOT, load_job
from benchmarks.deepswe.docker_environment import proxy_script_with_ports
from pier.environments.agent_setup import squid_bootstrap_command


@pytest.fixture
def config():
    return yaml.safe_load((ROOT / "config/deepswe/dateutil-bitfun.yaml").read_text())


@pytest.fixture
def agent(tmp_path, config, monkeypatch):
    monkeypatch.setenv("DEEPSWE_API_KEY", "test-secret-not-a-real-key")
    return BitfunCli(logs_dir=tmp_path, **config["agents"][0]["kwargs"])


def test_external_plugin_works_without_pier_registry_patch(tmp_path, config):
    entry = AgentConfig.model_validate(config["agents"][0])
    obj = AgentFactory.create_agent_from_config(entry, logs_dir=tmp_path)
    assert isinstance(obj, BitfunCli)
    assert obj.name() == "bitfun-cli"
    assert obj.install_spec() is None
    assert obj.network_allowlist().domains == ["113.46.219.251"]


def test_allowlist_ignores_disabled_models(agent):
    agent._bitfun_config["ai"]["models"].append(
        {"enabled": False, "base_url": "https://unrelated.invalid/v1"}
    )
    assert agent.network_allowlist().domains == ["113.46.219.251"]


def test_requires_explicit_model_endpoint(agent):
    agent._bitfun_config["ai"]["models"][0].pop("base_url")
    with pytest.raises(ValueError, match="base_url"):
        agent.network_allowlist()


def test_credentials_stay_as_templates(config):
    cfg = config["agents"][0]["kwargs"]["bitfun_config"]
    assert credential_variables(cfg) == {"DEEPSWE_API_KEY"}
    cfg["ai"]["models"][0]["api_key"] = "do-not-store-this"
    with pytest.raises(ValueError, match="environment variable"):
        credential_variables(cfg)


def test_config_expands_inside_container_without_secret_command(agent, tmp_path):
    command = agent._build_register_config_command()
    assert "test-secret-not-a-real-key" not in command
    env = dict(os.environ, **agent._env_for_run())
    env["BITFUN_USER_ROOT"] = str(tmp_path / "private")
    subprocess.run(["bash", "-c", command], env=env, check=True)
    path = tmp_path / "private/config/app.json"
    assert (
        json.loads(path.read_text())["ai"]["models"][0]["api_key"]
        == env["DEEPSWE_API_KEY"]
    )
    assert path.stat().st_mode & 0o777 == 0o600


def test_bitfun_native_proxy_is_populated(agent, tmp_path):
    env = dict(os.environ, **agent._env_for_run())
    env["BITFUN_USER_ROOT"] = str(tmp_path / "private")
    env["HTTPS_PROXY"] = "http://agent:private-proxy-token@pier-egress-proxy:8080"
    subprocess.run(
        ["bash", "-c", agent._build_register_config_command()], env=env, check=True
    )
    cfg = json.loads((tmp_path / "private/config/app.json").read_text())
    assert cfg["ai"]["proxy"] == {
        "enabled": True,
        "url": "http://pier-egress-proxy:8080",
        "username": "agent",
        "password": "private-proxy-token",
    }
    redacted = agent._redact_config_secrets(cfg)
    assert "private-proxy-token" not in json.dumps(redacted)


def test_no_ambient_bitfun_state_or_credentials(agent, monkeypatch):
    monkeypatch.setenv("BITFUN_HOME", "/host/private-session")
    monkeypatch.setenv("UNRELATED_API_KEY", "unrelated")
    env = agent._env_for_run()
    assert env["BITFUN_HOME"] == "/tmp/rsiagent-bitfun/home"
    assert env["BITFUN_USER_ROOT"] == "/tmp/rsiagent-bitfun/user"
    assert "UNRELATED_API_KEY" not in env


def test_missing_credentials_fail_explicitly(agent, monkeypatch):
    monkeypatch.delenv("DEEPSWE_API_KEY")
    with pytest.raises(ValueError, match="DEEPSWE_API_KEY"):
        agent._env_for_run()


@pytest.mark.asyncio
async def test_pier_egress_proxy_env_is_forwarded(agent):
    environment = SimpleNamespace(
        agent_process_env=lambda env: {**env, "HTTPS_PROXY": "http://proxy:3128"},
        exec=AsyncMock(return_value=SimpleNamespace(return_code=0)),
    )
    await agent.exec_as_agent(environment, "echo ok")
    passed = environment.exec.call_args.kwargs
    assert passed["env"]["HTTPS_PROXY"] == "http://proxy:3128"
    assert passed["env"]["DEEPSWE_API_KEY"] == "test-secret-not-a-real-key"


@pytest.mark.asyncio
async def test_failed_command_does_not_put_secret_into_exception(agent):
    environment = SimpleNamespace(
        agent_process_env=lambda env: env,
        exec=AsyncMock(
            return_value=SimpleNamespace(
                return_code=9, stdout="test-secret-not-a-real-key", stderr=""
            )
        ),
    )
    with pytest.raises(NonZeroAgentExitCodeError) as failure:
        await agent.exec_as_agent(environment, "exit 9")
    assert "test-secret-not-a-real-key" not in str(failure.value)


def test_instruction_is_passed_intact_and_not_shell_interpreted(agent):
    text = 'Task text with `touch BAD` and $(touch BAD) and "quotes"'
    shell = agent._build_run_shell(text)
    outer = shlex.split(shell.split(" &\n", 1)[0])
    inner = outer[-1]
    assert shlex.quote(text) in inner
    assert "--output-patch" not in inner
    assert "--auto --agent agentic" in inner


@pytest.mark.asyncio
async def test_cancel_stops_process_before_final_capture(agent):
    actions = []

    async def execute(environment, command, **kwargs):
        if "setsid" in command:
            actions.append("run")
            raise asyncio.CancelledError()
        actions.append("collect" if "cp-back-manifest" in command else "config")

    async def kill(**kwargs):
        assert "kill -TERM" in kwargs["command"]
        actions.append("kill")

    async def final(environment):
        actions.append("snapshot")

    agent.exec_as_agent = execute
    agent._capture_repo_baseline = AsyncMock()
    agent._capture_repo_final_state = final
    agent._capture_final_app_config = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await agent.run("task", SimpleNamespace(exec=kill), AgentContext())
    assert actions.index("kill") < actions.index("snapshot") < actions.index("collect")


def test_diagnostic_snapshot_does_not_commit_unsubmitted_work(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True
        ).strip()

    git("init", "-q")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.invalid")
    (repo / "example.py").write_text("value = 1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    logs = tmp_path / "logs"
    subprocess.run(
        ["bash", "-c", build_repo_baseline_capture_script(str(logs))],
        cwd=repo,
        check=True,
    )
    (repo / "example.py").write_text("value = 2\n")
    subprocess.run(
        ["bash", "-c", build_repo_final_capture_script(str(logs))], cwd=repo, check=True
    )
    assert git("rev-parse", "HEAD") == base
    assert git("diff", "--binary", base, "HEAD") == ""
    assert "+value = 2" in (logs / "fix.patch").read_text()


def test_job_paths_and_contract(config, tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text(yaml.safe_dump(config))
    job = load_job(path, root=tmp_path, name="smoke")
    assert job.datasets[0].path == (tmp_path.parent / "DeepSWE/tasks").resolve()
    assert job.jobs_dir == tmp_path / "results/deepswe/jobs"
    assert job.agents[0].kwargs["output_patch_path"] is None
    assert job.n_attempts == job.n_concurrent_trials == 1
    assert job.retry.max_retries == 0
    assert "test-secret-not-a-real-key" not in job.model_dump_json()
    assert "${DEEPSWE_API_KEY}" in job.model_dump_json()


def test_no_job_path_traversal(config, tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match="directory name"):
        load_job(path, root=tmp_path, name="../escape")


def test_gateway_port_preserves_domain_and_auth_policy():
    script = proxy_script_with_ports(squid_bootstrap_command(), [8080])
    assert "acl Safe_ports port 80 443 8080" in script
    assert "acl SSL_ports port 443 8080" in script
    assert "http_access allow authenticated allowed_domains" in script
    assert "http_access deny all" in script


@pytest.mark.parametrize("port", [0, 65536, True, "8080\nhttp_access allow all"])
def test_invalid_gateway_port_cannot_inject_proxy_policy(port):
    with pytest.raises(ValueError, match="TCP port"):
        proxy_script_with_ports(squid_bootstrap_command(), [port])


def test_drifted_proxy_template_fails_closed():
    with pytest.raises(RuntimeError, match="template changed"):
        proxy_script_with_ports("new proxy template", [8080])


def test_binary_mounts_keep_log_and_patch_binds(tmp_path):
    from benchmarks.deepswe.docker_environment import BitfunDockerEnvironment
    from pier.models.task.config import EnvironmentConfig
    from pier.models.trial.paths import TrialPaths
    from pier.models.agent.network import NetworkAllowlist

    directory = tmp_path / "env"
    directory.mkdir()
    (directory / "Dockerfile").write_text("FROM python:3.12-slim\n")
    environment = BitfunDockerEnvironment(
        environment_dir=directory,
        environment_name="test",
        session_id="test",
        trial_paths=TrialPaths(tmp_path / "trial"),
        task_env_config=EnvironmentConfig(),
        network_allowlist=NetworkAllowlist(domains=["model.example"]),
        mounts_json=[
            {
                "type": "bind",
                "source": "/host/bitfun",
                "target": "/usr/local/bin/bitfun",
            }
        ],
    )
    targets = {m["target"] for m in environment._mounts_json}
    assert targets == {
        "/usr/local/bin/bitfun",
        "/logs/agent",
        "/logs/verifier",
        "/logs/artifacts",
    }


def test_official_verifier_does_not_share_candidate_artifacts(tmp_path):
    from benchmarks.deepswe.docker_environment import BitfunDockerEnvironment
    from pier.models.task.config import EnvironmentConfig
    from pier.models.trial.paths import TrialPaths

    directory = tmp_path / "tests"
    directory.mkdir()
    (directory / "Dockerfile").write_text("FROM python:3.12-slim\n")
    environment = BitfunDockerEnvironment(
        environment_dir=directory,
        environment_name="test",
        session_id="test-verifier",
        trial_paths=TrialPaths(tmp_path / "trial"),
        task_env_config=EnvironmentConfig(),
        network_allowlist=None,
        mounts_json=[
            {
                "type": "bind",
                "source": str(tmp_path / "verifier"),
                "target": "/logs/verifier",
            }
        ],
    )
    assert [m["target"] for m in environment._mounts_json] == ["/logs/verifier"]
