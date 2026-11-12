"""启动并监督一个可以多次续跑的 BitFun 会话，把运行结果整理后交给上层控制器。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path

from .bitfun_agent import BitfunCli, credential_variables
from .contracts import HandoffError, RoleInfrastructureError, read_handoff
from .supervision import EventJournal
from .work_budget import WorkBudget


@dataclass
class SessionOutcome:
    """返回一段会话的执行结果"""
    status: str
    report: object = None
    metrics: dict = None
    last_action: dict = None
    last_result: str = ""
    error: str | None = None
    handoff_error: str | None = None


class BitfunSession(BitfunCli):
    """对 BitfunCli 类的扩展，增加了会话相关的功能，本质也是 Pier 插件"""

    def __init__(self, *args, session_id=None, **kwargs):
        # 继承父类获取模型配置，bitfun 相关属性，同时维护四个会话属性
        super().__init__(*args, **kwargs)
        self.session_id = (
            str(uuid.UUID(session_id)) if session_id else str(uuid.uuid4())
        )
        self.calls = 0
        self.last_outcome = None
        self._pid_file = f"/tmp/rsiagent-bitfun/{self.session_id}.pid"

    def _find_main_session_dir(self):
        # 定位当前会话的日志目录
        path = self.logs_dir / "bitfun/sessions" / self.session_id
        return path if path.is_dir() else None

    async def prepare(self, environment):
        # 调用父类的环境配置方法，同时在容器内生成 bitfun 配置文件
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        await self.setup(environment)
        await self.exec_as_agent(environment, self._build_register_config_command())

    def command(self, prompt_path, *, bootstrap=""):
        # 拼装 bitfun-cli 的启动参数，可能是新建会话或者续跑会话
        args = [
            self._binary_path,
            "exec",
            "--auto",
            "--agent",
            self._exec_agent,
            "--output-format",
            "stream-json",
            "--no-verify-final-changes",    # 禁止 bitfun 自己验证，外部另行组织。
        ]
        args += ["--resume" if self.calls else "--session-id", self.session_id] # 决定是新建还是续跑
        stream_path = f"/logs/agent/stream-{self.session_id}-{self.calls + 1:03d}.jsonl"
        inner = (
            # A role publishes its handoff from inside the container; keep it
            # host-readable instead of inheriting a strict container umask.
            "umask 022\nset -o pipefail\n" + bootstrap
            + shlex.join(args) + " < " + shlex.quote(prompt_path)
            + f" 2>&1 | tee {stream_path}\nexit ${{PIPESTATUS[0]}}"
        )
        return (
            f"setsid bash -c {shlex.quote(inner)} &\n"
            "session_pid=$!\n"
            f'printf "%s" "$session_pid" > {self._pid_file}\n'
            'wait "$session_pid"\nsession_rc=$?\n'
            f"rm -f {self._pid_file}\n"
            'exit "$session_rc"'
        )

    async def stop(self, environment, *, force=False):
        # 负责发送停止信号给 bitfun-cli 进程
        signal = "KILL" if force else "INT"
        result = await asyncio.wait_for(
            environment.exec(
                command=(
                    f"if test -f {self._pid_file}; then read -r session_pid < {self._pid_file}; "
                    'case "$session_pid" in *[!0-9]*|"") exit 1;; esac; '
                    f'kill -{signal} -- "-$session_pid" 2>/dev/null || true; fi'
                )
            ),
            timeout=15,
        )
        if result.return_code:
            raise RoleInfrastructureError("Unable to signal the active BitFun session")

    async def execute(
        self,
        environment,
        prompt,
        timeout_sec,
        *,
        role,
        handoff_id,
        candidate_id=None,
        progress_probe=None,
        detect_stall=True,  # 是否启动停滞检测，看会话是否卡住了
        work_budget=None,   # 控制每个角色的时间预算，共用同一个时间，不会每个角色全新的时间
        terminal_reader=None,
    ):
        """启动或继续角色会话，监督完成、停滞、错误和超时。"""
        prompt_path = await self.upload_prompt(environment, prompt,
            f"prompt-{self.session_id}-{self.calls + 1:03d}.txt")
        stream_path = (
            self.logs_dir / f"stream-{self.session_id}-{self.calls + 1:03d}.jsonl"
        )

        # 需要日志、报告的路径和时间预算
        journal = EventJournal(
            stream_path, self.session_id, self.logs_dir / "bitfun.txt"
        )   # 运行日志
        self.last_outcome = None
        handoff_path = self.logs_dir / "handoffs" / f"{handoff_id}.json"    # 角色发布的报告
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        # Container root may own the published file, so the directory must not
        # block the host-side reader that validates it.
        try:
            os.chmod(handoff_path.parent, 0o777)
        except OSError:
            pass
        budget = work_budget or WorkBudget(timeout_sec) # baseline 的共享时间预算

        # 传入共享时间预算后，再启动模型中继
        transport_path = None
        if work_budget is not None:
            transport_id = f"{self.session_id}-{self.calls + 1:03d}"
            transport_path = self.logs_dir / f"transport-{transport_id}.jsonl"
            budget.watch(transport_path)

            # model_transport.py 是一个中继程序，负责 bitfun 和 模型服务之间的通信，该文件会被上传到容器中运行
            # 中继的目的是处理模型服务故障，并精确计算时间
            source = Path(__file__).with_name("model_transport.py")
            remote = f"/tmp/rsiagent-bitfun/transport-{transport_id}.py"
            ready = f"/tmp/rsiagent-bitfun/transport-{transport_id}.ready"
            await environment.upload_file(source, remote)

            bootstrap = (
                f"python3 {shlex.quote(remote)} "
                '"$BITFUN_USER_ROOT/config/app.json" '
                f"/logs/agent/{transport_path.name} {ready} >/dev/null 2>&1 &\n"
                "transport_pid=$!\n"
                'trap \'kill "$transport_pid" 2>/dev/null || true; '
                'wait "$transport_pid" 2>/dev/null || true\' EXIT\n'
                f"for attempt in {{1..200}}; do test -f {ready} && break; "
                'kill -0 "$transport_pid" 2>/dev/null || exit 1; sleep 0.05; done\n'
                f"test -f {ready} || exit 1\n"
            )
            # Configuration may still point at a relay from a previous segment.
            # Restore original endpoints before starting this invocation's relay.
            await self.exec_as_agent(environment, self._build_register_config_command())

            # 同时运行 bitfun 和监控程序
            command = self.command(prompt_path, bootstrap=bootstrap)
        else:
            command = self.command(prompt_path)

        self.calls += 1
        before = await progress_probe() if progress_probe else None
        observed_tools = 0
        task = asyncio.create_task(self.exec_as_agent(environment, command))    # 异步启动监控循环
        status = None
        report = None
        error = None
        handoff_failure = None
        unsettled = False

        # python 关注 bitfun 的主循环
        try:
            while True:
                journal.poll()  # 读取并更新时间日志内容

                # 当没有 bitfun 工具运行，工具完成或者任务完成，会尝试读取 handoff 报告
                if not journal.active_tools and (
                    journal.completed_tools or task.done()
                ):
                    try:
                        report = (
                            terminal_reader(handoff_path)
                            if terminal_reader is not None
                            else read_handoff(handoff_path, role, handoff_id, candidate_id)
                        )
                    except HandoffError as handoff_error:
                        report = None
                        handoff_failure = self.failure_details(handoff_error, journal)

                    if report is not None:
                        handoff_failure = None
                        status = "handoff"
                        break

                    # 没有读取到报告，检查是否有错误或者停滞
                    if (
                        detect_stall
                        and progress_probe
                        and journal.completed_tools != observed_tools
                    ):  # 如果工具调用反复相同的动作，说明模型陷入循环
                        current = await progress_probe()
                        observed_tools = journal.completed_tools
                        if current != before:
                            journal.signatures.clear()
                            before = current
                        elif journal.repeated_actions():
                            status = "stalled"
                            break

                # bitfun 进程结束，读取执行结果
                if task.done():
                    try:
                        task.result()
                    except Exception as error:
                        status = "execution_error"
                        failure = self.failure_details(error, journal)

                    # 检查任务完成得情况是否正常，如报告格式；模型轮数是否超出
                    else:
                        journal.poll()
                        failure = None
                        # The process has exited: make one real read attempt.
                        # A handoff file can land on the host before the stream
                        # event that clears active_tools, so the in-loop guard
                        # may never fire. Existence alone says nothing about
                        # validity, so validate here instead of labelling it.
                        try:
                            report = (
                                terminal_reader(handoff_path)
                                if terminal_reader is not None
                                else read_handoff(
                                    handoff_path, role, handoff_id, candidate_id
                                )
                            )
                        except HandoffError as handoff_error:
                            report = None
                            handoff_failure = self.failure_details(handoff_error, journal)
                        if report is not None:
                            handoff_failure = None
                            status = "handoff"
                        else:
                            status = (
                                "invalid_handoff"
                                if handoff_path.exists()
                                else "missing_handoff"
                            )
                        if (
                            status == "missing_handoff"
                            and journal.native_finish_reason == "max_rounds"
                        ):
                            status = "segment_exhausted"
                    error = failure
                    break

                # 任务没结束，检查时间预算是否超出
                if budget.remaining() <= 0 and not budget.requests:
                    status = "budget_exhausted"
                    break
                await asyncio.sleep(0.25)

        # 无论 bitfun 进程执行如何，结束了后要清理资源
        finally:
            # 请求正常停止
            if not task.done():
                try:
                    await self.stop(environment)
                    await asyncio.wait_for(asyncio.shield(task), timeout=20)
                except (Exception, asyncio.CancelledError):
                    try:
                        await self.stop(environment, force=True)
                        await asyncio.wait_for(asyncio.shield(task), timeout=10)
                    except (Exception, asyncio.CancelledError):
                        unsettled = not task.done()
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

            # 实在不行，汇报基础设施错误
            else:
                await asyncio.gather(task, return_exceptions=True)

            journal.poll()
            if transport_path is not None:
                budget.finish(transport_path)

            # 保存一次 execute 是怎么结束的
            checkpoint = {
                "status": status or "cancelled",
                "session_id": self.session_id,
                "call": self.calls,
                "handoff_id": handoff_id,
                "last_action": journal.last_action,
                "last_result": journal.last_result,
                "metrics": journal.summary(),
                "error": error,
                "handoff_error": handoff_failure,
            }
            (
                self.logs_dir / f"checkpoint-{self.session_id}-{self.calls:03d}.json"
            ).write_text(json.dumps(checkpoint, indent=2) + "\n")

            # 构造最后返回值，是否有报告，运行的统计等
            self.last_outcome = SessionOutcome(
                status or "cancelled",
                report,
                journal.summary(),
                journal.last_action,
                journal.last_result,
                error,
                handoff_failure,
            )
        if unsettled:
            raise RoleInfrastructureError(
                "BitFun did not settle after bounded process cleanup"
            )
        return self.last_outcome

    async def collect(self, environment):
        await asyncio.wait_for(
            self.exec_as_agent(environment, self._cp_back_command()), timeout=60
        )
        await asyncio.wait_for(self._capture_final_app_config(environment), timeout=30)

    def failure_details(self, error, journal):
        text = type(error).__name__ + ': ' + str(error)
        if journal.errors:
            text += '\nNative errors: ' + '\n'.join(journal.errors[-3:])
        for key in credential_variables(self._bitfun_config):
            value = self._extra_env.get(key) or os.environ.get(key)
            if value:
                text = text.replace(value, '[REDACTED]')
        return re.sub(r'(?i)(bearer\s+)[^\s\"\']+', r'\1[REDACTED]', text)[-8192:]
