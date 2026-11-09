"""读取 bitfun 的运行日志，把日志转换成程序能够使用的运行状态"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path


class EventJournal:
    def __init__(self, path, session_id, text_path=None, *, record_observations=False):
        """准备好返回给 session 使用的保存变量，包括记录文件读取情况，工具运行状态，动作和观察结果以及统计模型运行情况"""
        self.path = Path(path)
        self.session_id = session_id
        self.text_path = Path(text_path) if text_path else None
        self.offset = 0
        self.pending = ""
        self.seen = set()
        self.active_tools = set()
        self.invocations = {}
        self.signatures = []
        self.tools = Counter()
        self.last_result = ""
        self.last_action = None
        self.model_calls = 0
        self.compressions = 0
        self.usage_events = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.output_known = True
        self.cache_known = True
        self.input_known = True
        self.subagents = False
        self.errors = []
        self.completed_tools = 0
        self.native_finish_reason = None
        self.observations = [] if record_observations else None

    def feed(self, envelope):
        """将读取到的新增事件内容，转换成不同的 session 可利用的"""
        if not isinstance(envelope, dict):
            return
        
        # 事件编号，按编号去重
        identity = envelope.get("id")
        if not isinstance(identity, (str, int)):
            identity = None
        if identity in self.seen:
            return
        if identity:
            self.seen.add(identity)

        # 事件内容，只处理属于当前会话内容的事件
        event = envelope.get("event") or {}
        if not isinstance(event, dict):
            self.errors.append("Malformed native event object")
            return
        if (
            event.get("type") == "SubagentSessionLinked"
            and event.get("parent_session_id") == self.session_id
        ):
            self.subagents = True
        if event.get("session_id") not in (None, self.session_id):
            return

        # 根据事件类型进行处理
        kind = event.get("type")
        rendered = ""
        if kind in ("TextChunk", "ThinkingChunk"):
            rendered = event.get("text") or event.get("content") or ""
        elif kind == "ModelRoundStarted":
            self.model_calls += 1
        elif kind == "ContextCompressionCompleted":
            self.compressions += 1
        elif kind == "DialogTurnCompleted":
            self.native_finish_reason = event.get("finish_reason")
        elif kind == "SubagentSessionLinked":
            self.subagents = True
        elif kind == "TokenUsageUpdated":
            self.usage_events += 1
            for field, known in (("input_tokens", "input_known"),
                                 ("cached_tokens", "cache_known"),
                                 ("output_tokens", "output_known")):
                value = event.get(field)
                if type(value) is int and value >= 0:
                    setattr(self, field, getattr(self, field) + value)
                else:
                    setattr(self, known, False)
                    if value is not None:
                        self.errors.append("Invalid native usage field: " + field)
        elif kind in ("SystemError", "DialogTurnFailed"):
            self.errors.append(str(event.get("error") or "BitFun runtime failure"))

        # 如果是工具事件，跟踪“开始了什么、结束了什么、观察到了什么”
        elif kind == "ToolEvent":
            tool = event.get("tool_event") or {}
            if not isinstance(tool, dict):
                self.errors.append("Malformed native tool event")
                return
            subtype = tool.get("event_type")
            key = tool.get("tool_id")   # 此次工具调用的编号
            if not isinstance(key, (str, int, type(None))):
                self.errors.append("Malformed native tool identity")
                return

            # 工具名称
            name = tool.get("effective_tool_name") or tool.get("tool_name") or "unknown"
            if not isinstance(name, str):
                name = "unknown"

            # 工具调用所处阶段
            if subtype in ("Queued", "Waiting", "ConfirmationNeeded", "Started"):
                self.active_tools.add(key)

            # 工具开始调用，则记录工具名、参数和调用累计次数
            if subtype == "Started":
                params = tool.get("params") or {}
                if name != tool.get("tool_name") and isinstance(params, dict):
                    params = params.get("arguments", params.get("args", params))
                self.invocations[key] = (name, params)
                self.tools[name] += 1
                rendered = f"\nTool call: {name}\nInput: {json.dumps(params, ensure_ascii=False)}\n"

            # 工具结束时，从活跃的工具集合移除，并找回调用信息
            elif subtype in ("Completed", "Failed", "Cancelled", "Rejected"):
                self.active_tools.discard(key)
                name, params = self.invocations.get(key, (name, {}))

                # 提取工具调用的结果
                result = (
                    tool.get("result_for_assistant")
                    or tool.get("result")
                    or tool.get("error")
                    or tool.get("reason")
                    or ""
                )
                result = (
                    result
                    if isinstance(result, str)
                    else json.dumps(result, ensure_ascii=False)
                )
                observation = tool.get("result", result)
                running = False
                if (
                    isinstance(observation, dict)
                    and "completion" in observation
                    and "output" in observation
                ):
                    # 记录工具的完成情况
                    completion = observation.get("completion")
                    running = not isinstance(completion, dict) or (
                        completion.get("status") != "exited")
                    observation = {
                        key: value
                        for key, value in observation.items()
                        if key not in {"chunk_id", "wall_time_seconds"}
                    }
                
                # 工具调用的指纹，通过工具名称，调用参数和观测结果来区分
                signature = hashlib.sha256(
                    json.dumps([name, params, observation], sort_keys=True).encode()
                ).hexdigest()
                if not running and name not in {"TodoWrite", "get_goal", "update_goal"}:
                    self.signatures.append(signature)
                elif running:
                    self.signatures.clear()
                self.completed_tools += 1
                self.last_action = {"tool": name, "arguments": params}
                self.last_result = result
                if self.observations is not None:
                    self.observations.append(
                        {
                            "action": self.last_action,
                            "status": subtype,
                            "observation": result,
                        }
                    )
                rendered = f"\nTool {subtype}: {name}\n{result}\n"
        if rendered and self.text_path:
            self.text_path.parent.mkdir(parents=True, exist_ok=True)
            with self.text_path.open("a") as stream:
                stream.write(rendered if isinstance(rendered, str) else json.dumps(rendered))

    def poll(self):
        """从 bitfun 日志文件中读取新增的内容"""
        if not self.path.exists():
            return

        # 根据日志创建情况，只读新增日志内容
        with self.path.open(errors="replace") as stream:
            stream.seek(self.offset)
            chunk = stream.read()
            self.offset = stream.tell()
        lines = (self.pending + chunk).split("\n")
        self.pending = lines.pop()

        # 逐行解析 JSONL 文件内容
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            
            # 最后把读取的新增内容交给 feed 进行转换，成 session 需要的格式
            if isinstance(record, dict):
                self.feed(record)

    def repeated_actions(self, repeats=3):
        # Require identical requests AND observations, not merely repeated reads.
        for width in range(1, 5):
            size = width * repeats
            if len(self.signatures) >= size:
                block = self.signatures[-width:]
                if self.signatures[-size:] == block * repeats:
                    return True
        return False

    def summary(self):
        return {
            "model_calls": self.model_calls,
            "tools": dict(self.tools),
            "completed_tools": self.completed_tools,
            "context_compressions": self.compressions,
            "native_finish_reason": self.native_finish_reason,
            "usage_events": self.usage_events,
            "input_tokens": self.input_tokens if self.usage_events and self.input_known else None,
            "output_tokens": self.output_tokens
            if self.usage_events and self.output_known
            else None,
            "cached_tokens": self.cached_tokens
            if self.usage_events and self.cache_known
            else None,
            "usage_scope": "observed native events; subagent/compression accounting may be incomplete",
            "has_subagents": self.subagents,
            "native_error_count": len(self.errors),
        }
