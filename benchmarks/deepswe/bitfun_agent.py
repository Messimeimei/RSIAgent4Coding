"""Pier 连接 BitFun CLI 的插件实现，对应 BitFun 0.2.15。"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shlex
import textwrap
from typing import Any
from urllib.parse import urlsplit

from pier.agents.installed.base import NonZeroAgentExitCodeError
from pier.agents.network import allowlist_from_urls
from pier.models.agent.network import NetworkAllowlist

# 这里的 _vendor.bitfun_cli 是从 BitFun 官方仓库的 bitfun_cli.py 复制过来的，经过了少量修改以适配 Pier 的环境。
from ._vendor.bitfun_cli import BitfunCli as UpstreamBitfunCli

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ROOT = "/tmp/rsiagent-bitfun"


def credential_variables(config: dict) -> set[str]:
    """要求配置文件里面的 key 要用 模板变量，否则报错。"""
    names: set[str] = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if UpstreamBitfunCli._is_sensitive_config_key(key) and item:
                    if not isinstance(item, str) or not _VARIABLE.fullmatch(item):
                        raise ValueError(
                            f"bitfun_config.{key} must reference an environment variable"
                        )
                    names.add(_VARIABLE.fullmatch(item).group(1))
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(config)
    return names


class BitfunCli(UpstreamBitfunCli):
    """Use BitFun's own coding loop; Pier owns collection and official grading."""

    def __init__(
        self,
        *args,
        binary_path: str = "/usr/local/bin/bitfun",
        output_patch_path: str | None = None,
        bitfun_config: dict[str, Any] | None = None,
        **kwargs,
    ):
        if not binary_path.startswith("/"):
            raise ValueError("binary_path must be an absolute in-container path")
        if not isinstance(bitfun_config, dict):
            raise ValueError("bitfun_config is required")
        self._credential_vars = credential_variables(bitfun_config)
        super().__init__(
            *args,
            binary_path=binary_path,
            output_patch_path=output_patch_path,
            bitfun_config=copy.deepcopy(bitfun_config),
            **kwargs,
        )

    def install_spec(self):
        # bitfun 是挂载进容器的，而不是装进镜像，所以这里表示跳过在容器安装 bitfun
        return None

    def network_allowlist(self) -> NetworkAllowlist:
        # 阅读配置获取可以访问网络的端口
        models = self._bitfun_config.get("ai", {}).get("models", [])
        urls = []
        for model in models:
            if not model.get("enabled", True):
                continue
            url = model.get("base_url", "")
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError(
                    "Every enabled BitFun model needs an explicit base_url"
                )
            if parsed.username or parsed.password:
                raise ValueError("Model base_url must not contain credentials")
            urls.append(url)
        if not urls:
            raise ValueError("No enabled BitFun model endpoint is configured")
        return allowlist_from_urls(urls)

    def _env_for_run(self) -> dict[str, str]:
        # 固定 bitfun 运行路径在 tmp 下，同时检查模型 key 是否设置正确
        result = dict(self._extra_env)
        result.update(
            BITFUN_HOME=f"{_ROOT}/home",
            BITFUN_USER_ROOT=f"{_ROOT}/user",
            GIT_AUTHOR_NAME="BitFun",
            GIT_AUTHOR_EMAIL="bitfun@evaluation.invalid",
            GIT_COMMITTER_NAME="BitFun",
            GIT_COMMITTER_EMAIL="bitfun@evaluation.invalid",
        )
        for name in self._credential_vars:
            value = self._extra_env.get(name) or os.environ.get(name)
            if not value:
                raise ValueError(
                    f"Required credential environment variable is unset: {name}"
                )
            result[name] = value
        return result

    async def setup(self, environment) -> None:
        """检查 bitfun binary 是否存在和版本"""
        result = await environment.exec(
            command=f"test -x {shlex.quote(self._binary_path)} && "
            f"{shlex.quote(self._binary_path)} --version"
        )
        if result.return_code != 0:
            raise RuntimeError("Mounted BitFun binary is missing or cannot execute")
        self._version = self.parse_version(result.stdout or "")
        if self._version != "bitfun 0.2.15":
            raise RuntimeError(
                f"Expected pinned bitfun 0.2.15, found {self._version!r}"
            )

        await self.exec_as_agent(
            environment,
            command=f"mkdir -p {shlex.quote(_ROOT)} /logs/agent",
        )

    def _build_register_config_command(self) -> str:
        # 读取配置里的模型 key 的时候，防止 Pier 把 key 写进日志里；解决代理问题
        payload = json.dumps(self._bitfun_config)
        script = rf"""
            import json, os, re
            from pathlib import Path
            from urllib.parse import urlsplit, unquote
            config = json.loads({payload!r})
            pattern = re.compile(r'\$\{{([A-Za-z_][A-Za-z0-9_]*)\}}')
            def expand(value):
                if isinstance(value, dict):
                    return {{key: expand(item) for key, item in value.items()}}
                if isinstance(value, list):
                    return [expand(item) for item in value]
                if isinstance(value, str):
                    match = pattern.fullmatch(value)
                    if match:
                        return os.environ[match.group(1)]
                return value
            config = expand(config)
            # BitFun 0.2.15 explicitly disables reqwest's system proxy detection.
            # Populate its native proxy config from Pier's authenticated proxy environment.
            proxy_url = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY')
            if proxy_url:
                proxy = urlsplit(proxy_url)
                config.setdefault('ai', {{}})['proxy'] = {{
                    'enabled': True,
                    'url': proxy.scheme + '://' + proxy.hostname + ':' + str(proxy.port or 80),
                    'username': unquote(proxy.username or ''),
                    'password': unquote(proxy.password or ''),
                }}
            path = Path(os.environ['BITFUN_USER_ROOT']) / 'config' / 'app.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(config, stream)
            os.chmod(path, 0o600)
            """
        return "python3 -c " + shlex.quote(textwrap.dedent(script).lstrip())

    async def _exec(
        self, environment, command, user=None, env=None, cwd=None, timeout_sec=None
    ):
        """通用执行任务函数"""
        process_env = environment.agent_process_env(env or self._env_for_run())
        result = await environment.exec(
            command="set -o pipefail; " + command,
            user=user,
            env=process_env,
            cwd=cwd,
            timeout_sec=timeout_sec,
        )
        if result.return_code != 0:
            # Avoid embedding credentials from commands or environment in errors.
            raise NonZeroAgentExitCodeError(
                f"BitFun command exited {result.return_code}; see agent/bitfun.txt"
            )
        return result

    def _build_run_shell(self, instruction: str) -> str:
        # 构建 bitfun 运行命令，解决指定的题目
        command = super()._build_run_shell(instruction)
        command = command.replace(" exec --agent ", " exec --auto --agent ", 1)

        return (
            f"setsid bash -c {shlex.quote(command)} &\n"
            "bitfun_pid=$!\n"
            f'printf "%s" "$bitfun_pid" > {_ROOT}/pid\n'
            'wait "$bitfun_pid"\n'
            "bitfun_rc=$?\n"
            f"rm -f {_ROOT}/pid\n"
            'exit "$bitfun_rc"'
        )

    def _cp_back_command(self) -> str:
        # 记录容器里面的信息，如对话记录，token 情况，运行日志等
        script = r"""
            import json, os, shutil
            from pathlib import Path
            home = Path(os.environ['BITFUN_HOME'])
            user = Path(os.environ['BITFUN_USER_ROOT'])
            dest = Path('/logs/agent/bitfun')
            dest.mkdir(parents=True, exist_ok=True)
            manifest = {}
            for kind in ('sessions', 'request-traces'):
                sources = sorted((home / 'projects').glob('*/' + kind))
                manifest[kind] = [str(source) for source in sources]
                for source in sources:
                    shutil.copytree(source, dest / kind, dirs_exist_ok=True)
            for relative, target in (
                ('data/token_usage', 'token_usage'), ('cli-logs', 'cli-logs'),
                ('logs/bitfun-cli.log', 'cli.log'),
                ('logs/ai-request-audit.jsonl', 'ai-request-audit.jsonl'),
            ):
                source = user / relative
                manifest[target] = {'source': str(source), 'exists': source.exists()}
                if source.is_dir():
                    shutil.copytree(source, dest / target, dirs_exist_ok=True)
                elif source.is_file():
                    shutil.copy2(source, dest / target)
            (dest / 'cp-back-manifest.json').write_text(json.dumps(manifest, indent=2))
            """
        return "python3 -c " + shlex.quote(textwrap.dedent(script).lstrip())

    async def run(self, instruction, environment, context) -> None:
        baseline_captured = False

        # 分别执行注册模型配置、捕获快照信息以及运行解决题目的 bitfun 命令
        try:
            await self.exec_as_agent(environment, self._build_register_config_command())
            await self._capture_repo_baseline(environment)
            baseline_captured = True
            await self.exec_as_agent(
                environment, self._build_run_shell(self.render_instruction(instruction))
            )
        except asyncio.CancelledError:
            try:
                await environment.exec(
                    command=(
                        f"if test -f {_ROOT}/pid; then "
                        f"read -r bitfun_pid < {_ROOT}/pid || true; "
                        'case "$bitfun_pid" in *[!0-9]*|"") exit 1;; esac; '
                        'kill -TERM -- "-$bitfun_pid" 2>/dev/null || true; '
                        'sleep 1; kill -KILL -- "-$bitfun_pid" 2>/dev/null || true; fi'
                    ),
                    timeout_sec=10,
                )
            except Exception:
                self.logger.warning(
                    "BitFun process cleanup failed; Pier must remove the container"
                )
            raise

        # 完成后，再次捕获快照信息，用于构造 patch 补丁交给官方测试
        finally:
            if baseline_captured:
                try:
                    await self._capture_repo_final_state(environment)
                except Exception:
                    self.logger.warning("Could not capture diagnostic repository patch")
            try:
                await self.exec_as_agent(
                    environment, self._cp_back_command(), timeout_sec=60
                )
                await self._capture_final_app_config(environment)
            except Exception:
                self.logger.warning("BitFun artifact collection incomplete")
