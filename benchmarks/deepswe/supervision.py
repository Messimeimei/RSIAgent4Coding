"""Read BitFun's native event envelope without taking over its coding loop."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path


class EventJournal:
    def __init__(self, path, session_id, text_path=None, *, record_observations=False):
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
        self.subagents = False
        self.errors = []
        self.completed_tools = 0
        self.native_finish_reason = None
        self.observations = [] if record_observations else None

    def feed(self, envelope):
        identity = envelope.get("id")
        if identity in self.seen:
            return
        if identity:
            self.seen.add(identity)
        event = envelope.get("event") or {}
        if (
            event.get("type") == "SubagentSessionLinked"
            and event.get("parent_session_id") == self.session_id
        ):
            self.subagents = True
        if event.get("session_id") not in (None, self.session_id):
            return
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
            self.input_tokens += int(event.get("input_tokens") or 0)
            self.cached_tokens += int(event.get("cached_tokens") or 0)
            if event.get("cached_tokens") is None:
                self.cache_known = False
            if event.get("output_tokens") is None:
                self.output_known = False
            self.output_tokens += int(event.get("output_tokens") or 0)
        elif kind in ("SystemError", "DialogTurnFailed"):
            self.errors.append(str(event.get("error") or "BitFun runtime failure"))
        elif kind == "ToolEvent":
            tool = event.get("tool_event") or {}
            subtype = tool.get("event_type")
            key = tool.get("tool_id")
            name = tool.get("effective_tool_name") or tool.get("tool_name") or "unknown"
            if subtype in ("Queued", "Waiting", "ConfirmationNeeded", "Started"):
                self.active_tools.add(key)
            if subtype == "Started":
                params = tool.get("params") or {}
                if name != tool.get("tool_name") and isinstance(params, dict):
                    params = params.get("arguments", params.get("args", params))
                self.invocations[key] = (name, params)
                self.tools[name] += 1
                rendered = f"\nTool call: {name}\nInput: {json.dumps(params, ensure_ascii=False)}\n"
            elif subtype in ("Completed", "Failed", "Cancelled", "Rejected"):
                self.active_tools.discard(key)
                name, params = self.invocations.get(key, (name, {}))
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
                    # Native exec envelopes carry random chunk IDs and latency.
                    # Preserve actual output/exit state, not delivery metadata.
                    running = (
                        observation.get("completion", {}).get("status") != "exited"
                    )
                    observation = {
                        key: value
                        for key, value in observation.items()
                        if key not in {"chunk_id", "wall_time_seconds"}
                    }
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
                stream.write(rendered)

    def poll(self):
        if not self.path.exists():
            return
        with self.path.open(errors="replace") as stream:
            stream.seek(self.offset)
            chunk = stream.read()
            self.offset = stream.tell()
        lines = (self.pending + chunk).split("\n")
        self.pending = lines.pop()
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
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
            "input_tokens": self.input_tokens if self.usage_events else None,
            "output_tokens": self.output_tokens
            if self.usage_events and self.output_known
            else None,
            "cached_tokens": self.cached_tokens
            if self.usage_events and self.cache_known
            else None,
            "usage_scope": "observed native events; subagent/compression accounting may be incomplete",
            "has_subagents": self.subagents,
        }
