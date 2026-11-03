"""Unit tests for BitfunCli."""

import json as _json
import os
import subprocess
from pathlib import Path as _Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch as _patch

import pytest

from benchmarks.deepswe._vendor.bitfun_cli import (
    BitfunCli as _UpstreamBitfunCli,
)
from pier.models.agent.context import AgentContext
from pier.models.trajectories.agent import Agent
from pier.models.trajectories.final_metrics import FinalMetrics
from pier.models.trajectories.trajectory import Trajectory

_DEFAULT_TS_MS = 1_778_000_000_000  # arbitrary fixed epoch ms


def _ts_iso(ms: int) -> str:
    """Convert BitFun millisecond epoch to an ISO-8601 UTC timestamp string."""
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _make_metadata(
    session_id: str,
    *,
    kind: str = "standard",
    model: str = "default",
    workspace: str = "/testbed",
    turn_count: int = 0,
    tool_call_count: int = 0,
    created_at: int = _DEFAULT_TS_MS,
    last_active_at: int | None = None,
) -> dict:
    return {
        "schema_version": 2,
        "sessionId": session_id,
        "sessionName": "test",
        "agentType": "agentic",
        "sessionKind": kind,
        "modelName": model,
        "createdAt": created_at,
        "lastActiveAt": last_active_at or (created_at + 1_000),
        "turnCount": turn_count,
        "messageCount": turn_count * 2,
        "toolCallCount": tool_call_count,
        "status": "completed",
        "tags": [],
        "workspacePath": workspace,
        "workspaceHostname": "localhost",
    }


def _make_text_item(
    item_id: str,
    content: str,
    *,
    order_index: int = 0,
    ts: int = _DEFAULT_TS_MS,
    status: str = "completed",
) -> dict:
    return {
        "id": item_id,
        "content": content,
        "isStreaming": False,
        "timestamp": ts,
        "isMarkdown": True,
        "orderIndex": order_index,
        "status": status,
    }


def _make_thinking_item(
    item_id: str,
    content: str,
    *,
    order_index: int = 0,
    ts: int = _DEFAULT_TS_MS,
) -> dict:
    return {
        "id": item_id,
        "content": content,
        "isStreaming": False,
        "isCollapsed": False,
        "timestamp": ts,
        "orderIndex": order_index,
    }


def _make_tool_item(
    item_id: str,
    tool_name: str,
    input_args: dict,
    *,
    result_text: str | None = None,
    raw_result: object = None,
    success: bool = True,
    error: str | None = None,
    subagent_sid: str | None = None,
    subagent_model_id: str | None = None,
    parent_task_tool_id: str | None = None,
    order_index: int = 0,
    ts: int = _DEFAULT_TS_MS,
    duration_ms: int = 5,
    ai_intent: str | None = None,
) -> dict:
    out: dict = {
        "id": item_id,
        "toolName": tool_name,
        "toolCall": {"id": item_id, "input": input_args},
        "startTime": ts,
        "endTime": ts + duration_ms,
        "durationMs": duration_ms,
        "executionMs": duration_ms,
        "orderIndex": order_index,
        "status": "completed",
    }
    if result_text is not None or raw_result is not None:
        tr: dict = {"success": success}
        tr["result"] = raw_result if raw_result is not None else {"text": result_text}
        if result_text is not None:
            tr["resultForAssistant"] = result_text
        if error is not None:
            tr["error"] = error
            tr["success"] = False
        tr["durationMs"] = duration_ms
        out["toolResult"] = tr
    if ai_intent is not None:
        out["aiIntent"] = ai_intent
    if subagent_sid is not None:
        out["isSubagentItem"] = True
        out["subagentSessionId"] = subagent_sid
    if subagent_model_id is not None:
        out["subagentModelId"] = subagent_model_id
    if parent_task_tool_id is not None:
        out["parentTaskToolId"] = parent_task_tool_id
    return out


def _make_round(
    round_id: str,
    *,
    turn_id: str,
    round_index: int = 0,
    text_items: list | None = None,
    tool_items: list | None = None,
    thinking_items: list | None = None,
    model_id: str | None = "openai/gpt-5",
    model_alias: str | None = None,
    provider_id: str | None = "openai",
    ts: int = _DEFAULT_TS_MS,
    duration_ms: int = 10,
    attempt_count: int = 1,
    status: str = "completed",
    failure_category: str | None = None,
) -> dict:
    return {
        "id": round_id,
        "turnId": turn_id,
        "roundIndex": round_index,
        "timestamp": ts,
        "textItems": text_items or [],
        "toolItems": tool_items or [],
        "thinkingItems": thinking_items or [],
        "startTime": ts,
        "endTime": ts + duration_ms,
        "durationMs": duration_ms,
        "providerId": provider_id,
        "modelId": model_id,
        "modelAlias": model_alias,
        "attemptCount": attempt_count,
        "status": status,
        **({"failureCategory": failure_category} if failure_category else {}),
    }


def _make_turn(
    turn_index: int,
    turn_id: str,
    session_id: str,
    *,
    kind: str = "user_dialog",
    user_text: str = "hello",
    user_content: str | None = None,
    model_rounds: list | None = None,
    ts: int = _DEFAULT_TS_MS,
    duration_ms: int = 100,
    status: str = "completed",
) -> dict:
    return {
        "schema_version": 2,
        "turnId": turn_id,
        "turnIndex": turn_index,
        "sessionId": session_id,
        "timestamp": ts,
        "kind": kind,
        "userMessage": {
            "id": f"{turn_id}-user",
            "content": user_content
            if user_content is not None
            else f"<user_query>\n{user_text}\n</user_query>",
            "timestamp": ts,
            "metadata": {"original_text": user_text} if user_text else {},
        },
        "modelRounds": model_rounds or [],
        "startTime": ts,
        "endTime": ts + duration_ms,
        "durationMs": duration_ms,
        "status": status,
    }


def _make_token_record(
    model_id: str,
    session_id: str,
    turn_id: str,
    in_tok: int,
    out_tok: int,
    *,
    cached: int = 0,
    is_sub: bool = False,
    ts: int = _DEFAULT_TS_MS,
    token_details: dict | None = None,
) -> dict:
    return {
        "model_id": model_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "timestamp": _ts_iso(ts),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cached_tokens": cached,
        "cached_tokens_available": cached > 0,
        "total_tokens": in_tok + out_tok,
        "is_subagent": is_sub,
        "token_details": token_details or {},
    }


def _write_session(
    logs_dir: _Path,
    sid: str,
    *,
    metadata: dict,
    turns: list[dict],
    token_records: list[dict] | None = None,
    token_records_date: str = "2026-01-01",
    snapshot_messages: list[dict] | None = None,
    snapshot_session_id: str | None = None,
) -> _Path:
    """Lay out a minimal BitFun cp-back tree under logs_dir/bitfun/."""
    root = logs_dir / "bitfun" / "sessions" / sid
    (root / "turns").mkdir(parents=True, exist_ok=True)
    (root / "metadata.json").write_text(_json.dumps(metadata))
    for turn in turns:
        (root / "turns" / f"turn-{turn['turnIndex']:04d}.json").write_text(
            _json.dumps(turn)
        )
    if token_records is not None:
        records_dir = logs_dir / "bitfun" / "token_usage" / "records"
        records_dir.mkdir(parents=True, exist_ok=True)
        (records_dir / f"{token_records_date}.json").write_text(
            _json.dumps({"records": list(token_records)})
        )
    if snapshot_messages is not None:
        snaps_dir = root / "snapshots"
        snaps_dir.mkdir(parents=True, exist_ok=True)
        (snaps_dir / "context-0000.json").write_text(
            _json.dumps(
                {
                    "schema_version": 2,
                    "session_id": snapshot_session_id or sid,
                    "turn_index": 0,
                    "messages": list(snapshot_messages),
                }
            )
        )
    return root


class _CaptureEnv:
    def __init__(
        self,
        *,
        raw_config_text: str | None,
        probe_stdout: str,
        existing_manifest: dict | None = None,
        upload_raises: Exception | None = None,
    ) -> None:
        self.raw_config_text = raw_config_text
        self.probe_stdout = probe_stdout
        self.existing_manifest = existing_manifest or {}
        self.upload_raises = upload_raises
        self.exec_calls: list[dict] = []
        self.downloads: list[tuple[str, _Path]] = []
        self.uploads: dict[str, str] = {}

    async def exec(self, **kwargs):
        self.exec_calls.append(kwargs)
        command = kwargs["command"]
        if "APP_CONFIG_SRC" in command:
            return SimpleNamespace(return_code=0, stdout=self.probe_stdout, stderr="")
        return SimpleNamespace(return_code=0, stdout="", stderr="")

    async def download_file(self, source_path, target_path):
        target = _Path(target_path)
        self.downloads.append((source_path, target))
        if source_path == "/logs/agent/bitfun/cp-back-manifest.json":
            target.write_text(_json.dumps(self.existing_manifest))
            return
        if self.raw_config_text is None:
            raise FileNotFoundError(source_path)
        target.write_text(self.raw_config_text)

    async def upload_file(self, source_path, target_path):
        if self.upload_raises is not None:
            raise self.upload_raises
        self.uploads[target_path] = _Path(source_path).read_text()


def _snap_ts(ms: int) -> dict:
    secs, ms_part = divmod(ms, 1000)
    return {"secs_since_epoch": secs, "nanos_since_epoch": ms_part * 1_000_000}


def _snap_user_msg(turn_id: str, text: str, *, ts: int = _DEFAULT_TS_MS) -> dict:
    return {
        "id": f"{turn_id}-user",
        "role": "User",
        "content": {"Text": f"<user_query>\n{text}\n</user_query>"},
        "timestamp": _snap_ts(ts),
        "metadata": {
            "turn_id": turn_id,
            "round_id": None,
            "tokens": None,
            "semantic_kind": "actual_user_input",
        },
    }


def _snap_assistant_tool_call_msg(
    turn_id: str,
    round_id: str,
    tool_id: str,
    tool_name: str,
    arguments: dict,
    *,
    text: str = "",
    reasoning: str | None = None,
    ts: int = _DEFAULT_TS_MS,
) -> dict:
    return {
        "id": f"{round_id}-{tool_id}",
        "role": "Assistant",
        "content": {
            "Mixed": {
                "reasoning_content": reasoning,
                "text": text,
                "tool_calls": [
                    {
                        "tool_id": tool_id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "is_error": False,
                    }
                ],
            }
        },
        "timestamp": _snap_ts(ts),
        "metadata": {"turn_id": turn_id, "round_id": round_id, "tokens": None},
    }


def _snap_tool_result_msg(
    turn_id: str,
    round_id: str,
    tool_id: str,
    tool_name: str,
    *,
    result_for_assistant: str = "ok",
    raw_result: dict | None = None,
    is_error: bool = False,
    ts: int = _DEFAULT_TS_MS,
) -> dict:
    return {
        "id": f"{round_id}-{tool_id}-result",
        "role": "Tool",
        "content": {
            "ToolResult": {
                "tool_id": tool_id,
                "tool_name": tool_name,
                "result": raw_result or {"text": result_for_assistant},
                "result_for_assistant": result_for_assistant,
                "is_error": is_error,
            }
        },
        "timestamp": _snap_ts(ts),
        "metadata": {"turn_id": turn_id, "round_id": round_id, "tokens": None},
    }


def _snap_assistant_text_msg(
    turn_id: str,
    round_id: str,
    text: str,
    *,
    ts: int = _DEFAULT_TS_MS,
) -> dict:
    return {
        "id": f"{round_id}-text",
        "role": "Assistant",
        "content": {
            "Mixed": {
                "reasoning_content": None,
                "text": text,
                "tool_calls": [],
            }
        },
        "timestamp": _snap_ts(ts),
        "metadata": {"turn_id": turn_id, "round_id": round_id, "tokens": None},
    }


def _regenerate_golden_fixture(target_root: _Path) -> None:
    """One-shot writer used during local fixture authoring.

    Run via:
      uv run python -c "from pathlib import Path; from tests.unit.agents.installed.test_bitfun_cli import _regenerate_golden_fixture; ..."
    """
    target_root.mkdir(parents=True, exist_ok=True)
    ts = 1_778_000_000_000
    main_sid = "bitfun-golden-001"
    sub_sid = "bitfun-golden-001-sub"

    sub_turn = _make_turn(
        0,
        f"{sub_sid}-turn",
        sub_sid,
        user_text="do thing",
        ts=ts + 200,
        model_rounds=[
            _make_round(
                f"{sub_sid}-round",
                turn_id=f"{sub_sid}-turn",
                ts=ts + 250,
                text_items=[
                    _make_text_item(
                        f"{sub_sid}-ti", "did it", order_index=0, ts=ts + 260
                    )
                ],
                model_id="openai/gpt-5",
            )
        ],
    )
    _write_session(
        target_root,
        sub_sid,
        metadata=_make_metadata(
            sub_sid,
            kind="subagent",
            model="openai/gpt-5",
            workspace="/testbed",
            created_at=ts + 200,
            last_active_at=ts + 280,
            turn_count=1,
        ),
        turns=[sub_turn],
    )

    tool = _make_tool_item(
        "tc-1",
        "Task",
        {"description": "delegate to subagent"},
        result_text="subagent done",
        raw_result={"output": "subagent done"},
        subagent_sid=sub_sid,
        subagent_model_id="openai/gpt-5",
        order_index=2,
        ts=ts + 150,
        duration_ms=40,
        ai_intent="dispatch subagent to do the thing",
    )
    main_turn = _make_turn(
        0,
        "main-turn-0",
        main_sid,
        user_text="please help",
        ts=ts,
        model_rounds=[
            _make_round(
                "main-round-0",
                turn_id="main-turn-0",
                round_index=0,
                ts=ts + 50,
                thinking_items=[
                    _make_thinking_item(
                        "th-0", "I should delegate.", order_index=0, ts=ts + 60
                    )
                ],
                text_items=[
                    _make_text_item(
                        "ti-0", "Delegating now.", order_index=1, ts=ts + 80
                    )
                ],
                tool_items=[tool],
                model_id="openai/gpt-5",
            )
        ],
    )
    compaction_turn = _make_turn(
        1,
        "main-turn-1",
        main_sid,
        user_text="",
        kind="manual_compaction",
        ts=ts + 300,
    )
    _write_session(
        target_root,
        main_sid,
        metadata=_make_metadata(
            main_sid,
            kind="standard",
            model="openai/gpt-5",
            workspace="/testbed",
            created_at=ts,
            last_active_at=ts + 320,
            turn_count=2,
            tool_call_count=1,
        ),
        turns=[main_turn, compaction_turn],
        token_records=[
            _make_token_record(
                "openai/gpt-5",
                main_sid,
                "main-turn-0",
                120,
                80,
                cached=10,
                ts=ts + 100,
            ),
            _make_token_record(
                "openai/gpt-5",
                sub_sid,
                f"{sub_sid}-turn",
                40,
                20,
                cached=0,
                ts=ts + 260,
                is_sub=True,
            ),
        ],
        token_records_date="2026-01-01",
    )


@pytest.fixture
def temp_dir(tmp_path):
    return tmp_path


def _exec_commands(mock_env: AsyncMock) -> list[str]:
    return [call.kwargs["command"] for call in mock_env.exec.call_args_list]


def _first_command_containing(commands: list[str], text: str) -> str:
    return next(command for command in commands if text in command)


def _run_shell(command: str, *, cwd: _Path) -> None:
    subprocess.run(["bash", "-lc", command], cwd=cwd, check=True)


class BitfunCli(_UpstreamBitfunCli):
    def install_spec(self):
        return None


class TestGetSessionDir:
    def test_picks_unique_standard_session(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        _write_session(
            temp_dir,
            "main",
            metadata=_make_metadata("main", kind="standard"),
            turns=[],
        )
        _write_session(
            temp_dir,
            "sub-1",
            metadata=_make_metadata("sub-1", kind="subagent"),
            turns=[],
        )
        _write_session(
            temp_dir,
            "sub-2",
            metadata=_make_metadata("sub-2", kind="subagent"),
            turns=[],
        )
        result = agent._get_session_dir()
        assert result is not None
        assert result.name == "main"

    def test_no_bitfun_dir_returns_none(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        assert agent._get_session_dir() is None

    def test_falls_back_to_mtime_when_multiple_standards(self, temp_dir):
        import time

        agent = BitfunCli(logs_dir=temp_dir)
        a = _write_session(
            temp_dir,
            "older",
            metadata=_make_metadata("older", kind="standard"),
            turns=[],
        )
        time.sleep(0.02)
        b = _write_session(
            temp_dir,
            "newer",
            metadata=_make_metadata("newer", kind="standard"),
            turns=[],
        )
        now = time.time()
        os.utime(a, (now - 100, now - 100))
        os.utime(b, (now, now))
        result = agent._get_session_dir()
        assert result is not None
        assert result.name == "newer"

    def test_skips_dirs_without_metadata(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        (temp_dir / "bitfun" / "sessions" / "junk").mkdir(parents=True)
        _write_session(
            temp_dir,
            "main",
            metadata=_make_metadata("main", kind="standard"),
            turns=[],
        )
        result = agent._get_session_dir()
        assert result is not None
        assert result.name == "main"


class TestLoadTokenRecords:
    def test_returns_empty_when_no_records_dir(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        assert agent._load_token_records() == []

    def test_loads_records_from_all_date_files(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        records_dir = temp_dir / "bitfun" / "token_usage" / "records"
        records_dir.mkdir(parents=True)
        (records_dir / "2026-01-01.json").write_text(
            _json.dumps(
                {
                    "records": [
                        _make_token_record("m", "s", "t1", 10, 5),
                        _make_token_record("m", "s", "t2", 20, 10),
                    ]
                }
            )
        )
        (records_dir / "2026-01-02.json").write_text(
            _json.dumps({"records": [_make_token_record("m", "s", "t3", 1, 1)]})
        )
        records = agent._load_token_records()
        assert len(records) == 3
        assert {r["turn_id"] for r in records} == {"t1", "t2", "t3"}

    def test_skips_malformed_record_files(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        records_dir = temp_dir / "bitfun" / "token_usage" / "records"
        records_dir.mkdir(parents=True)
        (records_dir / "bad.json").write_text("not json {{{")
        (records_dir / "good.json").write_text(
            _json.dumps({"records": [_make_token_record("m", "s", "t", 1, 1)]})
        )
        records = agent._load_token_records()
        assert len(records) == 1
        assert records[0]["turn_id"] == "t"


class TestLoadStdoutTokenStats:
    def test_returns_none_when_stdout_log_missing(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        assert agent._load_stdout_token_stats() is None

    def test_sums_turn_token_stats_from_stdout_log(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        (temp_dir / "bitfun.txt").write_text(
            "\x1b[32mINFO\x1b[0m Dialog turn completed - Token stats: "
            "turn_id=t1, rounds=2, tools=1, duration=100ms, "
            "prompt_tokens=10, completion_tokens=5, total_tokens=15\n"
            "INFO Dialog turn completed - Token stats: "
            "turn_id=t2, rounds=1, tools=0, duration=50ms, "
            "prompt_tokens=20, completion_tokens=7, total_tokens=27, "
            "cached_tokens=3\n"
        )
        stats = agent._load_stdout_token_stats()
        assert stats == {
            "prompt_tokens": 30,
            "completion_tokens": 12,
            "cached_tokens": 3,
            "total_tokens": 42,
            "record_count": 2,
            "cached_tokens_available": False,
            "cached_tokens_coverage": "partial",
        }

    def test_parses_partial_cache_coverage_from_stdout_log(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        (temp_dir / "bitfun.txt").write_text(
            "INFO Dialog turn completed - Token stats: "
            "turn_id=t1, rounds=5, model_calls=5, tools=4, duration=100ms, "
            "prompt_tokens=99246, completion_tokens=6225, total_tokens=105471, "
            "cached_tokens=12345, cached_tokens_available=partial\n"
        )
        stats = agent._load_stdout_token_stats()
        assert stats == {
            "prompt_tokens": 99246,
            "completion_tokens": 6225,
            "cached_tokens": 12345,
            "total_tokens": 105471,
            "record_count": 1,
            "cached_tokens_available": False,
            "cached_tokens_coverage": "partial",
        }

    def test_parses_complete_cache_tokens_from_stdout_log(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        (temp_dir / "bitfun.txt").write_text(
            "INFO Dialog turn completed - Token stats: "
            "turn_id=t1, rounds=1, tools=0, duration=50ms, "
            "prompt_tokens=20, completion_tokens=7, total_tokens=27, "
            "cached_tokens=3, cached_tokens_available=true\n"
        )
        stats = agent._load_stdout_token_stats()
        assert stats == {
            "prompt_tokens": 20,
            "completion_tokens": 7,
            "cached_tokens": 3,
            "total_tokens": 27,
            "record_count": 1,
            "cached_tokens_available": True,
            "cached_tokens_coverage": "true",
        }


class TestComputeCostViaLitellm:
    def test_returns_none_when_no_model(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        assert agent._compute_cost_via_litellm(None, 100, 0, 50) is None

    def test_returns_none_when_model_unknown(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        with _patch("litellm.model_cost", {}):
            assert (
                agent._compute_cost_via_litellm("totally-fake-model", 100, 0, 50)
                is None
            )

    def test_computes_cost_with_cache_rate(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        fake_pricing = {
            "fake-model": {
                "input_cost_per_token": 1e-6,
                "output_cost_per_token": 2e-6,
                "cache_read_input_token_cost": 1e-7,
            }
        }
        with _patch("litellm.model_cost", fake_pricing):
            cost = agent._compute_cost_via_litellm("fake-model", 100, 10, 50)
        assert cost is not None
        assert abs(cost - (90e-6 + 10e-7 + 100e-6)) < 1e-12

    def test_falls_back_to_input_rate_when_cache_rate_missing(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        fake_pricing = {
            "m": {"input_cost_per_token": 2e-6, "output_cost_per_token": 4e-6}
        }
        with _patch("litellm.model_cost", fake_pricing):
            cost = agent._compute_cost_via_litellm("m", 100, 30, 50)
        assert cost is not None
        assert abs(cost - 4.0e-4) < 1e-12

    def test_strips_provider_prefix(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        fake_pricing = {
            "gpt-5": {"input_cost_per_token": 1e-6, "output_cost_per_token": 1e-6}
        }
        with _patch("litellm.model_cost", fake_pricing):
            cost = agent._compute_cost_via_litellm("openai/gpt-5", 10, 0, 5)
        assert cost is not None
        assert abs(cost - (10e-6 + 5e-6)) < 1e-12


class TestBitfunTpsStepMetrics:
    def test_build_metrics_records_latency_and_tps(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        record = _make_token_record("m", "s", "t", 100, 25)
        record["llm_latency_ms"] = 5000

        metrics = agent._build_metrics_from_record(record)

        assert metrics.extra is not None
        assert metrics.extra["llm_latency_ms"] == 5000
        assert metrics.extra["completion_tokens_per_second"] == 5.0
        assert metrics.extra["tps_completion_tokens"] == 25
        assert metrics.extra["tps_model_call_count"] == 1
        assert metrics.extra["tps_latency_coverage"] == "complete"

    def test_build_metrics_marks_missing_latency(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        record = _make_token_record("m", "s", "t", 100, 25)

        metrics = agent._build_metrics_from_record(record)

        assert metrics.extra is not None
        assert "llm_latency_ms" not in metrics.extra
        assert "completion_tokens_per_second" not in metrics.extra
        assert metrics.extra["tps_unavailable_reason"] == "missing_latency"

    def test_build_metrics_preserves_zero_latency_without_tps(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        record = _make_token_record("m", "s", "t", 100, 25)
        record["llm_latency_ms"] = 0

        metrics = agent._build_metrics_from_record(record)

        assert metrics.extra is not None
        assert metrics.extra["llm_latency_ms"] == 0
        assert "completion_tokens_per_second" not in metrics.extra
        assert metrics.extra["tps_unavailable_reason"] == "zero_latency"

    def test_merge_metrics_computes_weighted_tps(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        a_record = _make_token_record("m", "s", "t", 100, 20)
        b_record = _make_token_record("m", "s", "t", 100, 40)
        a_record["llm_latency_ms"] = 2000
        b_record["llm_latency_ms"] = 8000

        merged = agent._merge_metrics(
            agent._build_metrics_from_record(a_record),
            agent._build_metrics_from_record(b_record),
        )

        assert merged.extra is not None
        assert merged.extra["llm_latency_ms"] == 10000
        assert merged.extra["tps_completion_tokens"] == 60
        assert merged.extra["tps_model_call_count"] == 2
        assert merged.extra["tps_latency_coverage"] == "complete"
        assert merged.extra["completion_tokens_per_second"] == 6.0

    def test_merge_metrics_marks_partial_latency_coverage(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        with_latency = _make_token_record("m", "s", "t", 100, 20)
        without_latency = _make_token_record("m", "s", "t", 100, 40)
        with_latency["llm_latency_ms"] = 2000

        merged = agent._merge_metrics(
            agent._build_metrics_from_record(with_latency),
            agent._build_metrics_from_record(without_latency),
        )

        assert merged.extra is not None
        assert merged.completion_tokens == 60
        assert merged.extra["llm_latency_ms"] == 2000
        assert merged.extra["tps_completion_tokens"] == 20
        assert merged.extra["tps_model_call_count"] == 1
        assert merged.extra["tps_latency_coverage"] == "partial"
        assert merged.extra["completion_tokens_per_second"] == 10.0


class TestConvertEventsToTrajectoryBasic:
    def test_basic_user_assistant_pair(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s1"
        turn = _make_turn(
            0,
            "t1",
            sid,
            user_text="hello",
            model_rounds=[
                _make_round(
                    "r1",
                    turn_id="t1",
                    text_items=[_make_text_item("ti1", "hi there", order_index=0)],
                )
            ],
        )
        _write_session(
            temp_dir, sid, metadata=_make_metadata(sid, turn_count=1), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(
            temp_dir / "bitfun" / "sessions" / sid
        )
        assert traj is not None
        assert traj.schema_version == "ATIF-v1.7"
        assert traj.session_id == sid
        assert traj.agent.name == "bitfun-cli"
        assert len(traj.steps) == 2
        assert traj.steps[0].source == "user"
        assert traj.steps[0].message == "hello"
        assert traj.steps[0].step_id == 1
        assert traj.steps[1].source == "agent"
        assert traj.steps[1].message == "hi there"
        assert traj.steps[1].step_id == 2
        assert traj.steps[1].model_name == "openai/gpt-5"

    def test_stdout_token_stats_fallback_populates_final_metrics(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="fake-model")
        sid = "stdout-stats"
        turn = _make_turn(
            0,
            "t1",
            sid,
            user_text="hello",
            model_rounds=[
                _make_round(
                    "r1",
                    turn_id="t1",
                    text_items=[_make_text_item("ti1", "hi there", order_index=0)],
                )
            ],
        )
        _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid, turn_count=1),
            turns=[turn],
        )
        (temp_dir / "bitfun.txt").write_text(
            "INFO Dialog turn completed - Token stats: "
            "turn_id=t1, rounds=1, tools=0, duration=100ms, "
            "prompt_tokens=100, completion_tokens=20, total_tokens=120\n"
        )
        fake_pricing = {
            "fake-model": {
                "input_cost_per_token": 1e-6,
                "output_cost_per_token": 2e-6,
            }
        }

        with _patch("litellm.model_cost", fake_pricing):
            traj = agent._convert_events_to_trajectory(
                temp_dir / "bitfun" / "sessions" / sid
            )

        assert traj is not None
        assert traj.final_metrics.total_prompt_tokens == 100
        assert traj.final_metrics.total_completion_tokens == 20
        assert traj.final_metrics.total_cached_tokens is None
        assert traj.final_metrics.total_cost_usd is None
        assert traj.final_metrics.extra is not None
        assert traj.final_metrics.extra["token_usage_source"] == "bitfun_stdout"
        assert traj.final_metrics.extra["cached_tokens_available"] is False
        assert traj.final_metrics.extra["cached_tokens_coverage"] == "false"
        assert traj.steps[1].metrics is not None
        assert traj.steps[1].metrics.prompt_tokens == 100
        assert traj.steps[1].metrics.completion_tokens == 20
        assert traj.steps[1].metrics.cached_tokens is None
        assert traj.steps[1].metrics.cost_usd is None
        assert traj.steps[1].metrics.extra is not None
        assert (
            traj.steps[1].metrics.extra["allocation"]
            == "aggregate_attached_to_last_agent_step"
        )

    def test_returns_none_when_metadata_missing(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        bogus = temp_dir / "bitfun" / "sessions" / "x"
        (bogus / "turns").mkdir(parents=True)
        assert agent._convert_events_to_trajectory(bogus) is None

    def test_user_query_wrapper_is_stripped_when_metadata_missing(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s2"
        turn = _make_turn(
            0,
            "t1",
            sid,
            user_content="<user_query>\nplease help\n</user_query>",
            user_text="",
            model_rounds=[
                _make_round(
                    "r1",
                    turn_id="t1",
                    text_items=[_make_text_item("ti1", "ok", order_index=0)],
                )
            ],
        )
        turn["userMessage"]["metadata"] = {}
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        assert traj.steps[0].source == "user"
        assert traj.steps[0].message == "please help"

    def test_step_ids_are_sequential_from_1(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s3"
        turns = [
            _make_turn(
                i,
                f"t{i}",
                sid,
                user_text=f"q{i}",
                model_rounds=[
                    _make_round(
                        f"r{i}",
                        turn_id=f"t{i}",
                        text_items=[_make_text_item(f"ti{i}", f"a{i}")],
                    )
                ],
            )
            for i in range(3)
        ]
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid, turn_count=3), turns=turns
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        assert [s.step_id for s in traj.steps] == list(range(1, len(traj.steps) + 1))

    def test_schema_version_is_atif_v1_7(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s4"
        turn = _make_turn(
            0,
            "t1",
            sid,
            model_rounds=[
                _make_round(
                    "r1",
                    turn_id="t1",
                    text_items=[_make_text_item("ti", "x")],
                )
            ],
        )
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        assert traj.schema_version == "ATIF-v1.7"


class TestBitfunTpsFinalMetrics:
    def test_final_metrics_tps_excludes_subagents(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        main_record = _make_token_record("m", "main", "t1", 100, 20)
        main_record["llm_latency_ms"] = 4000
        sub_record = _make_token_record("m", "sub", "t2", 100, 100)
        sub_record["llm_latency_ms"] = 1000
        sub_record["is_subagent"] = True
        subagent_trajectory = Trajectory.model_construct(
            schema_version="ATIF-v1.7",
            session_id="sub",
            agent=Agent(name="bitfun-cli", version="test"),
            steps=[],
            final_metrics=FinalMetrics(
                total_prompt_tokens=sub_record["input_tokens"],
                total_completion_tokens=sub_record["output_tokens"],
                total_cached_tokens=sub_record["cached_tokens"],
                total_steps=0,
            ),
        )

        final_metrics = agent._build_final_metrics(
            steps=[],
            metadata={},
            records_for_traj=[main_record],
            subagent_trajectories=[subagent_trajectory],
            subagent_count=1,
        )

        assert final_metrics.extra is not None
        assert final_metrics.extra["total_llm_latency_ms"] == 4000
        assert final_metrics.extra["model_call_count"] == 1
        assert final_metrics.extra["tps_completion_tokens"] == 20
        assert final_metrics.extra["completion_tokens_per_second"] == 5.0
        assert final_metrics.extra["tps_latency_coverage"] == "complete"
        assert (
            final_metrics.extra["subagent_total_tokens"] == sub_record["total_tokens"]
        )

    def test_final_metrics_marks_missing_latency(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir)
        main_record = _make_token_record("m", "main", "t1", 100, 20)

        final_metrics = agent._build_final_metrics(
            steps=[],
            metadata={},
            records_for_traj=[main_record],
            subagent_trajectories=[],
            subagent_count=0,
        )

        assert final_metrics.extra is not None
        assert "completion_tokens_per_second" not in final_metrics.extra
        assert final_metrics.extra["tps_unavailable_reason"] == "missing_latency"


class TestThinkingAccumulation:
    def test_thinking_block_attaches_to_next_text_step(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        turn = _make_turn(
            0,
            "t",
            sid,
            model_rounds=[
                _make_round(
                    "r",
                    turn_id="t",
                    thinking_items=[
                        _make_thinking_item("th1", "thinking A", order_index=0)
                    ],
                    text_items=[_make_text_item("ti1", "answer", order_index=1)],
                ),
            ],
        )
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        agent_steps = [s for s in traj.steps if s.source == "agent"]
        assert len(agent_steps) == 1
        assert agent_steps[0].reasoning_content == "thinking A"
        assert agent_steps[0].message == "answer"

    def test_multiple_thinking_blocks_joined_with_double_newlines(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        turn = _make_turn(
            0,
            "t",
            sid,
            model_rounds=[
                _make_round(
                    "r",
                    turn_id="t",
                    thinking_items=[
                        _make_thinking_item("th1", "first", order_index=0),
                        _make_thinking_item("th2", "second", order_index=1),
                    ],
                    text_items=[_make_text_item("ti1", "answer", order_index=2)],
                ),
            ],
        )
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        agent_steps = [s for s in traj.steps if s.source == "agent"]
        assert agent_steps[0].reasoning_content == "first\n\nsecond"

    def test_thinking_after_text_does_not_attach_backwards(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        turn = _make_turn(
            0,
            "t",
            sid,
            model_rounds=[
                _make_round(
                    "r",
                    turn_id="t",
                    text_items=[_make_text_item("ti1", "answer", order_index=0)],
                    thinking_items=[_make_thinking_item("th1", "post", order_index=1)],
                ),
            ],
        )
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        agent_steps = [s for s in traj.steps if s.source == "agent"]
        assert agent_steps[0].reasoning_content is None


class TestToolCallMapping:
    def test_tool_call_uses_result_for_assistant_as_content(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        tool = _make_tool_item(
            "tc1",
            "Read",
            {"file_path": "/x"},
            result_text="file contents",
            raw_result={"text": "file contents", "lines": 1},
        )
        turn = _make_turn(
            0,
            "t",
            sid,
            model_rounds=[_make_round("r", turn_id="t", tool_items=[tool])],
        )
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        tool_steps = [s for s in traj.steps if s.tool_calls]
        assert len(tool_steps) == 1
        step = tool_steps[0]
        assert step.tool_calls[0].function_name == "Read"
        assert step.tool_calls[0].tool_call_id == "tc1"
        assert step.tool_calls[0].arguments == {"file_path": "/x"}
        assert step.observation is not None
        assert step.observation.results[0].source_call_id == "tc1"
        assert step.observation.results[0].content == "file contents"

    def test_tool_call_falls_back_to_json_dumps_when_result_for_assistant_absent(
        self, temp_dir
    ):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        tool = _make_tool_item(
            "tc1",
            "Read",
            {},
            raw_result={"chunks": [1, 2, 3]},
        )
        tool["toolResult"].pop("resultForAssistant", None)
        turn = _make_turn(
            0,
            "t",
            sid,
            model_rounds=[_make_round("r", turn_id="t", tool_items=[tool])],
        )
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        step = [s for s in traj.steps if s.tool_calls][0]
        content = step.observation.results[0].content
        assert content is not None
        assert "chunks" in content

    def test_tool_call_preserves_raw_result_in_observation_extra(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        tool = _make_tool_item(
            "tc1",
            "Read",
            {},
            result_text="ok",
            raw_result={"chunks": [1, 2]},
        )
        turn = _make_turn(
            0,
            "t",
            sid,
            model_rounds=[_make_round("r", turn_id="t", tool_items=[tool])],
        )
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        step = [s for s in traj.steps if s.tool_calls][0]
        extra = step.observation.results[0].extra or {}
        assert extra.get("raw_result") == {"chunks": [1, 2]}
        assert extra.get("success") is True

    def test_tool_error_propagates_to_observation_extra(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        tool = _make_tool_item(
            "tc1",
            "Read",
            {},
            raw_result={"err": "x"},
            success=False,
            error="permission denied",
        )
        turn = _make_turn(
            0,
            "t",
            sid,
            model_rounds=[_make_round("r", turn_id="t", tool_items=[tool])],
        )
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        step = [s for s in traj.steps if s.tool_calls][0]
        extra = step.observation.results[0].extra or {}
        assert extra.get("error") == "permission denied"
        assert extra.get("success") is False

    def test_tool_call_message_uses_ai_intent_when_present(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        tool = _make_tool_item(
            "tc1",
            "Read",
            {},
            result_text="ok",
            ai_intent="read configuration file",
        )
        turn = _make_turn(
            0,
            "t",
            sid,
            model_rounds=[_make_round("r", turn_id="t", tool_items=[tool])],
        )
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        step = [s for s in traj.steps if s.tool_calls][0]
        assert step.message == "read configuration file"

    def test_tool_call_arguments_wraps_non_dict_input(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        tool = _make_tool_item("tc1", "Echo", "not-a-dict", result_text="ok")
        turn = _make_turn(
            0,
            "t",
            sid,
            model_rounds=[_make_round("r", turn_id="t", tool_items=[tool])],
        )
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        step = [s for s in traj.steps if s.tool_calls][0]
        assert step.tool_calls[0].arguments == {"input": "not-a-dict"}

    def test_thinking_attaches_to_tool_call_then_clears(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        tool = _make_tool_item(
            "tc1",
            "Read",
            {},
            result_text="ok",
            order_index=1,
        )
        turn = _make_turn(
            0,
            "t",
            sid,
            model_rounds=[
                _make_round(
                    "r",
                    turn_id="t",
                    thinking_items=[
                        _make_thinking_item("th", "plan to read", order_index=0)
                    ],
                    tool_items=[tool],
                    text_items=[_make_text_item("ti", "done", order_index=2)],
                ),
            ],
        )
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        tool_step = [s for s in traj.steps if s.tool_calls][0]
        text_step = [s for s in traj.steps if s.source == "agent" and not s.tool_calls][
            0
        ]
        assert tool_step.reasoning_content == "plan to read"
        assert text_step.reasoning_content is None


class TestRoundAndTurnEdgeCases:
    def test_empty_round_emits_placeholder_agent_step(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        empty_round = _make_round(
            "r",
            turn_id="t",
            text_items=[],
            tool_items=[],
            thinking_items=[],
            duration_ms=42,
            attempt_count=3,
            failure_category="rate_limit",
            status="failed",
        )
        turn = _make_turn(0, "t", sid, model_rounds=[empty_round])
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        agent_steps = [s for s in traj.steps if s.source == "agent"]
        assert len(agent_steps) == 1
        assert agent_steps[0].message == ""
        extra = agent_steps[0].extra or {}
        assert extra.get("round_status") == "failed"
        assert extra.get("attempt_count") == 3
        assert extra.get("failure_category") == "rate_limit"

    def test_manual_compaction_turn_emits_system_step(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        comp_turn = _make_turn(0, "t-comp", sid, kind="manual_compaction")
        normal_turn = _make_turn(
            1,
            "t-1",
            sid,
            user_text="hi",
            model_rounds=[
                _make_round(
                    "r",
                    turn_id="t-1",
                    text_items=[_make_text_item("ti", "hello")],
                )
            ],
        )
        session_dir = _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid, turn_count=2),
            turns=[comp_turn, normal_turn],
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        comp_steps = [s for s in traj.steps if s.source == "system"]
        assert len(comp_steps) == 1
        assert comp_steps[0].message == "<manual compaction>"
        assert comp_steps[0].is_copied_context is True

    def test_local_command_turn_is_silently_skipped(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        local_turn = _make_turn(0, "t-local", sid, kind="local_command")
        normal_turn = _make_turn(
            1,
            "t-1",
            sid,
            user_text="hi",
            model_rounds=[
                _make_round(
                    "r",
                    turn_id="t-1",
                    text_items=[_make_text_item("ti", "hello")],
                )
            ],
        )
        session_dir = _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid, turn_count=2),
            turns=[local_turn, normal_turn],
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        assert all(
            "t-local" not in (s.extra or {}).get("turn_id", "") for s in traj.steps
        )
        assert any(s.source == "user" for s in traj.steps)

    def test_order_index_orders_mixed_items_within_round(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        tool = _make_tool_item("tc", "Read", {}, result_text="ok", order_index=2)
        turn = _make_turn(
            0,
            "t",
            sid,
            model_rounds=[
                _make_round(
                    "r",
                    turn_id="t",
                    thinking_items=[_make_thinking_item("th", "plan", order_index=0)],
                    text_items=[_make_text_item("ti", "preface", order_index=1)],
                    tool_items=[tool],
                ),
            ],
        )
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[turn]
        )
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        agent_steps = [s for s in traj.steps if s.source == "agent"]
        assert len(agent_steps) == 2
        assert agent_steps[0].message == "preface"
        assert agent_steps[0].reasoning_content == "plan"
        assert agent_steps[1].tool_calls is not None
        assert agent_steps[1].tool_calls[0].function_name == "Read"


class TestTokenAndMetricsAllocation:
    def test_metrics_assigned_to_first_assistant_step_of_round(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        ts = _DEFAULT_TS_MS
        turn = _make_turn(
            0,
            "t",
            sid,
            ts=ts,
            model_rounds=[
                _make_round(
                    "r1",
                    turn_id="t",
                    ts=ts,
                    text_items=[_make_text_item("ti1", "first", order_index=0)],
                )
            ],
        )
        records = [
            _make_token_record("openai/gpt-5", sid, "t", 100, 50, cached=10, ts=ts)
        ]
        session_dir = _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid),
            turns=[turn],
            token_records=records,
        )
        traj = agent._convert_events_to_trajectory(session_dir, token_records=records)
        assert traj is not None
        agent_steps = [s for s in traj.steps if s.source == "agent"]
        assert agent_steps[0].metrics is not None
        m = agent_steps[0].metrics
        assert m.prompt_tokens == 100
        assert m.completion_tokens == 50
        assert m.cached_tokens == 10

    def test_metrics_use_nearest_round_timestamp(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        ts0 = _DEFAULT_TS_MS
        turn = _make_turn(
            0,
            "t",
            sid,
            ts=ts0,
            model_rounds=[
                _make_round(
                    "r1",
                    turn_id="t",
                    round_index=0,
                    ts=ts0 + 100,
                    text_items=[_make_text_item("ti1", "early", order_index=0)],
                ),
                _make_round(
                    "r2",
                    turn_id="t",
                    round_index=1,
                    ts=ts0 + 1000,
                    text_items=[_make_text_item("ti2", "late", order_index=0)],
                ),
            ],
        )
        records = [
            _make_token_record(
                "openai/gpt-5",
                sid,
                "t",
                200,
                80,
                ts=ts0 + 960,
            )
        ]
        session_dir = _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid),
            turns=[turn],
            token_records=records,
        )
        traj = agent._convert_events_to_trajectory(session_dir, token_records=records)
        assert traj is not None
        agent_steps = [s for s in traj.steps if s.source == "agent"]
        assert agent_steps[0].metrics is None
        assert agent_steps[1].metrics is not None
        assert agent_steps[1].metrics.prompt_tokens == 200

    def test_step_metrics_absent_when_no_records_match_turn(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        turn = _make_turn(
            0,
            "t",
            sid,
            model_rounds=[
                _make_round(
                    "r1",
                    turn_id="t",
                    text_items=[_make_text_item("ti1", "x")],
                )
            ],
        )
        session_dir = _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid),
            turns=[turn],
            token_records=[],
        )
        traj = agent._convert_events_to_trajectory(session_dir, token_records=[])
        assert traj is not None
        agent_steps = [s for s in traj.steps if s.source == "agent"]
        assert all(s.metrics is None for s in agent_steps)

    def test_subagent_records_excluded_from_main_trajectory_metrics(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "main"
        ts = _DEFAULT_TS_MS
        turn = _make_turn(
            0,
            "t",
            sid,
            ts=ts,
            model_rounds=[
                _make_round(
                    "r1",
                    turn_id="t",
                    ts=ts,
                    text_items=[_make_text_item("ti1", "x")],
                )
            ],
        )
        records = [
            _make_token_record("openai/gpt-5", sid, "t", 100, 50, ts=ts),
            _make_token_record(
                "openai/gpt-5",
                sid,
                "t",
                999,
                999,
                ts=ts,
                is_sub=True,
            ),
        ]
        session_dir = _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid),
            turns=[turn],
            token_records=records,
        )
        traj = agent._convert_events_to_trajectory(session_dir, token_records=records)
        assert traj is not None
        m = [s for s in traj.steps if s.source == "agent"][0].metrics
        assert m is not None
        assert m.prompt_tokens == 100
        assert m.completion_tokens == 50

    def test_extra_records_attach_to_last_assistant_step_of_turn(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        ts = _DEFAULT_TS_MS
        turn = _make_turn(
            0,
            "t",
            sid,
            ts=ts,
            model_rounds=[
                _make_round(
                    "r1",
                    turn_id="t",
                    ts=ts,
                    text_items=[_make_text_item("ti1", "x", order_index=0)],
                )
            ],
        )
        records = [
            _make_token_record("m", sid, "t", 100, 50, ts=ts),
            _make_token_record("m", sid, "t", 10, 5, ts=ts + 10),
        ]
        session_dir = _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid),
            turns=[turn],
            token_records=records,
        )
        traj = agent._convert_events_to_trajectory(session_dir, token_records=records)
        assert traj is not None
        agent_steps = [s for s in traj.steps if s.source == "agent"]
        assert agent_steps[0].metrics is not None
        assert agent_steps[0].metrics.prompt_tokens in {100, 110}


class TestFinalMetrics:
    def test_final_metrics_sums_step_metrics(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        ts = _DEFAULT_TS_MS
        turns = [
            _make_turn(
                0,
                "t1",
                sid,
                ts=ts,
                model_rounds=[
                    _make_round(
                        "r1",
                        turn_id="t1",
                        ts=ts,
                        text_items=[_make_text_item("ti", "a", order_index=0)],
                    )
                ],
            ),
            _make_turn(
                1,
                "t2",
                sid,
                ts=ts + 100,
                model_rounds=[
                    _make_round(
                        "r2",
                        turn_id="t2",
                        ts=ts + 100,
                        text_items=[_make_text_item("ti", "b", order_index=0)],
                    )
                ],
            ),
        ]
        records = [
            _make_token_record("m", sid, "t1", 100, 50, cached=10, ts=ts),
            _make_token_record("m", sid, "t2", 200, 80, cached=20, ts=ts + 100),
        ]
        session_dir = _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid, turn_count=2),
            turns=turns,
            token_records=records,
        )
        traj = agent._convert_events_to_trajectory(session_dir, token_records=records)
        assert traj is not None
        fm = traj.final_metrics
        assert fm is not None
        assert fm.total_prompt_tokens == 300
        assert fm.total_completion_tokens == 130
        assert fm.total_cached_tokens == 30
        assert fm.total_steps == len(traj.steps)

    def test_final_metrics_cost_is_none_when_any_step_unpriced(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        ts = _DEFAULT_TS_MS
        turn = _make_turn(
            0,
            "t",
            sid,
            ts=ts,
            model_rounds=[
                _make_round(
                    "r1",
                    turn_id="t",
                    ts=ts,
                    text_items=[_make_text_item("ti", "a")],
                )
            ],
        )
        records = [_make_token_record("unknown-model", sid, "t", 100, 50, ts=ts)]
        session_dir = _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid),
            turns=[turn],
            token_records=records,
        )
        with _patch("litellm.model_cost", {}):
            traj = agent._convert_events_to_trajectory(
                session_dir, token_records=records
            )
        assert traj is not None
        assert traj.final_metrics is not None
        assert traj.final_metrics.total_cost_usd is None

    def test_final_metrics_extra_includes_session_summary(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        ts = _DEFAULT_TS_MS
        turn = _make_turn(
            0,
            "t",
            sid,
            ts=ts,
            model_rounds=[
                _make_round(
                    "r1",
                    turn_id="t",
                    ts=ts,
                    text_items=[_make_text_item("ti", "a")],
                )
            ],
        )
        records = [_make_token_record("m", sid, "t", 100, 50, ts=ts)]
        session_dir = _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(
                sid,
                turn_count=1,
                tool_call_count=0,
                created_at=ts,
                last_active_at=ts + 5_000,
            ),
            turns=[turn],
            token_records=records,
        )
        traj = agent._convert_events_to_trajectory(session_dir, token_records=records)
        assert traj is not None
        extra = (traj.final_metrics.extra or {}) if traj.final_metrics else {}
        assert extra.get("main_session_turn_count") == 1
        assert extra.get("main_session_duration_ms") == 5_000
        assert "m" in (extra.get("models_used") or [])


class TestSubagentEmbedding:
    def _build_sessions_with_subagent(
        self, temp_dir, *, sub_sid="sub", main_sid="main"
    ):
        sub_turn = _make_turn(
            0,
            "st1",
            sub_sid,
            user_text="do thing",
            model_rounds=[
                _make_round(
                    "sr1",
                    turn_id="st1",
                    text_items=[_make_text_item("sti", "did it")],
                )
            ],
        )
        _write_session(
            temp_dir,
            sub_sid,
            metadata=_make_metadata(sub_sid, kind="subagent", model="openai/gpt-5"),
            turns=[sub_turn],
        )
        tool = _make_tool_item(
            "tc1",
            "Task",
            {"description": "delegate"},
            result_text="subagent done",
            subagent_sid=sub_sid,
            subagent_model_id="openai/gpt-5",
        )
        main_turn = _make_turn(
            0,
            "mt1",
            main_sid,
            user_text="please",
            model_rounds=[_make_round("mr1", turn_id="mt1", tool_items=[tool])],
        )
        _write_session(
            temp_dir,
            main_sid,
            metadata=_make_metadata(main_sid, kind="standard"),
            turns=[main_turn],
        )
        return temp_dir / "bitfun" / "sessions" / main_sid

    def test_subagent_trajectory_is_embedded(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        session_dir = self._build_sessions_with_subagent(temp_dir)
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        assert traj.subagent_trajectories is not None
        assert len(traj.subagent_trajectories) == 1
        sub = traj.subagent_trajectories[0]
        assert sub.trajectory_id == "sub"
        assert sub.agent.name == "Task"
        assert sub.agent.model_name == "openai/gpt-5"

    def test_populate_context_counts_embedded_subagent_tokens(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        self._build_sessions_with_subagent(temp_dir)
        records_dir = temp_dir / "bitfun" / "token_usage" / "records"
        records_dir.mkdir(parents=True, exist_ok=True)
        (records_dir / "2026-01-01.json").write_text(
            _json.dumps(
                {
                    "records": [
                        _make_token_record(
                            "openai/gpt-5",
                            "main",
                            "mt1",
                            100,
                            40,
                            cached=5,
                        ),
                        _make_token_record(
                            "openai/gpt-5",
                            "sub",
                            "st1",
                            30,
                            5,
                            cached=1,
                            is_sub=True,
                        ),
                        _make_token_record(
                            "openai/gpt-5",
                            "unrelated-sub",
                            "ust1",
                            900,
                            90,
                            is_sub=True,
                        ),
                    ]
                }
            )
        )

        ctx = AgentContext()
        agent.populate_context_post_run(ctx)

        assert ctx.n_input_tokens == 130
        assert ctx.n_output_tokens == 45
        assert ctx.n_cache_tokens == 6

        payload = _json.loads((temp_dir / "trajectory.json").read_text())
        assert payload["final_metrics"]["total_prompt_tokens"] == 100
        assert (
            payload["subagent_trajectories"][0]["final_metrics"]["total_prompt_tokens"]
            == 30
        )
        assert payload["final_metrics"]["extra"]["subagent_total_tokens"] == 35

    def test_token_count_sum_ignores_nested_subagent_tokens(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        nested = Trajectory.model_construct(
            schema_version="ATIF-v1.7",
            session_id="nested",
            agent=Agent(name="Task", version="test"),
            steps=[],
            final_metrics=FinalMetrics(
                total_prompt_tokens=1000,
                total_completion_tokens=100,
                total_cached_tokens=10,
                total_cost_usd=1.0,
                total_steps=0,
            ),
        )
        sub = Trajectory.model_construct(
            schema_version="ATIF-v1.7",
            session_id="sub",
            agent=Agent(name="Task", version="test"),
            steps=[],
            final_metrics=FinalMetrics(
                total_prompt_tokens=30,
                total_completion_tokens=5,
                total_cached_tokens=1,
                total_cost_usd=0.1,
                total_steps=0,
            ),
            subagent_trajectories=[nested],
        )
        main = Trajectory.model_construct(
            schema_version="ATIF-v1.7",
            session_id="main",
            agent=Agent(name="bitfun-cli", version="test"),
            steps=[],
            final_metrics=FinalMetrics(
                total_prompt_tokens=100,
                total_completion_tokens=40,
                total_cached_tokens=5,
                total_cost_usd=0.2,
                total_steps=0,
            ),
            subagent_trajectories=[sub],
        )

        prompt, completion, cached, cost = agent._sum_trajectory_token_counts(main)
        assert (prompt, completion, cached) == (130, 45, 6)
        assert cost == pytest.approx(0.3)

    def test_parent_observation_references_embedded_subagent(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        session_dir = self._build_sessions_with_subagent(temp_dir)
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        tool_step = next(s for s in traj.steps if s.tool_calls)
        refs = tool_step.observation.results[0].subagent_trajectory_ref
        assert refs is not None
        assert any(ref.trajectory_id == "sub" for ref in refs)

    def test_subagent_relationship_metadata_backfills_parent_ref(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sub_sid, main_sid = "sub", "main"
        parent_tool_call_id = "tc1"
        sub_turn = _make_turn(
            0,
            "st1",
            sub_sid,
            user_text="do thing",
            model_rounds=[
                _make_round(
                    "sr1",
                    turn_id="st1",
                    text_items=[_make_text_item("sti", "did it")],
                )
            ],
        )
        sub_metadata = _make_metadata(sub_sid, kind="subagent", model="openai/gpt-5")
        sub_metadata["agentType"] = "Explore"
        sub_metadata["relationship"] = {
            "kind": "subagent",
            "parentSessionId": main_sid,
            "parentDialogTurnId": "mt1",
            "parentToolCallId": parent_tool_call_id,
            "subagentType": "Explore",
        }
        _write_session(
            temp_dir,
            sub_sid,
            metadata=sub_metadata,
            turns=[sub_turn],
        )

        tool = _make_tool_item(
            parent_tool_call_id,
            "Task",
            {"description": "delegate", "subagent_type": "Explore"},
            result_text="subagent done",
        )
        main_turn = _make_turn(
            0,
            "mt1",
            main_sid,
            user_text="please",
            model_rounds=[_make_round("mr1", turn_id="mt1", tool_items=[tool])],
        )
        _write_session(
            temp_dir,
            main_sid,
            metadata=_make_metadata(main_sid, kind="standard"),
            turns=[main_turn],
        )

        session_dir = temp_dir / "bitfun" / "sessions" / main_sid
        traj = agent._convert_events_to_trajectory(session_dir)
        assert traj is not None
        assert traj.subagent_trajectories is not None
        assert len(traj.subagent_trajectories) == 1
        assert traj.subagent_trajectories[0].trajectory_id == sub_sid
        assert traj.subagent_trajectories[0].agent.name == "Explore"

        tool_step = next(s for s in traj.steps if s.tool_calls)
        assert tool_step.extra["is_subagent_dispatch"] is True
        refs = tool_step.observation.results[0].subagent_trajectory_ref
        assert refs is not None
        assert refs[0].trajectory_id == sub_sid
        assert refs[0].extra["relationship_source"] == "metadata"

    def test_duplicate_subagent_session_id_embedded_only_once(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sub_sid, main_sid = "sub", "main"
        sub_turn = _make_turn(
            0,
            "st1",
            sub_sid,
            model_rounds=[
                _make_round(
                    "sr1",
                    turn_id="st1",
                    text_items=[_make_text_item("sti", "ok")],
                )
            ],
        )
        _write_session(
            temp_dir,
            sub_sid,
            metadata=_make_metadata(sub_sid, kind="subagent"),
            turns=[sub_turn],
        )
        tool_a = _make_tool_item(
            "tc1",
            "Task",
            {"a": 1},
            result_text="a-done",
            subagent_sid=sub_sid,
            order_index=0,
        )
        tool_b = _make_tool_item(
            "tc2",
            "Task",
            {"b": 2},
            result_text="b-done",
            subagent_sid=sub_sid,
            order_index=1,
        )
        main_turn = _make_turn(
            0,
            "mt1",
            main_sid,
            model_rounds=[
                _make_round(
                    "mr1",
                    turn_id="mt1",
                    tool_items=[tool_a, tool_b],
                )
            ],
        )
        _write_session(
            temp_dir,
            main_sid,
            metadata=_make_metadata(main_sid, kind="standard"),
            turns=[main_turn],
        )
        traj = agent._convert_events_to_trajectory(
            temp_dir / "bitfun" / "sessions" / main_sid
        )
        assert traj is not None
        assert traj.subagent_trajectories is not None
        assert len(traj.subagent_trajectories) == 1

    def test_missing_subagent_dir_omits_embed_but_keeps_step(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        main_sid = "main"
        tool = _make_tool_item(
            "tc1",
            "Task",
            {"x": 1},
            result_text="ok",
            subagent_sid="missing-sub",
        )
        main_turn = _make_turn(
            0,
            "mt1",
            main_sid,
            model_rounds=[_make_round("mr1", turn_id="mt1", tool_items=[tool])],
        )
        _write_session(
            temp_dir,
            main_sid,
            metadata=_make_metadata(main_sid, kind="standard"),
            turns=[main_turn],
        )
        traj = agent._convert_events_to_trajectory(
            temp_dir / "bitfun" / "sessions" / main_sid
        )
        assert traj is not None
        assert not traj.subagent_trajectories
        tool_step = next(s for s in traj.steps if s.tool_calls)
        refs = tool_step.observation.results[0].subagent_trajectory_ref
        assert refs is None or refs == []
        assert (traj.notes or "").lower().find("missing") >= 0


class TestPopulateContextPostRun:
    def test_writes_trajectory_json_to_logs_dir(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        ts = _DEFAULT_TS_MS
        turn = _make_turn(
            0,
            "t",
            sid,
            ts=ts,
            model_rounds=[
                _make_round(
                    "r",
                    turn_id="t",
                    ts=ts,
                    text_items=[_make_text_item("ti", "hi")],
                )
            ],
        )
        _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid),
            turns=[turn],
            token_records=[_make_token_record("openai/gpt-5", sid, "t", 50, 25, ts=ts)],
        )
        ctx = AgentContext()
        agent.populate_context_post_run(ctx)
        out = temp_dir / "trajectory.json"
        assert out.is_file()
        payload = _json.loads(out.read_text())
        assert payload["schema_version"] == "ATIF-v1.7"
        assert payload["session_id"] == sid

    def test_populates_context_token_counts_from_final_metrics(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        ts = _DEFAULT_TS_MS
        turn = _make_turn(
            0,
            "t",
            sid,
            ts=ts,
            model_rounds=[
                _make_round(
                    "r",
                    turn_id="t",
                    ts=ts,
                    text_items=[_make_text_item("ti", "hi")],
                )
            ],
        )
        _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid),
            turns=[turn],
            token_records=[
                _make_token_record(
                    "openai/gpt-5",
                    sid,
                    "t",
                    100,
                    40,
                    cached=5,
                    ts=ts,
                )
            ],
        )
        ctx = AgentContext()
        agent.populate_context_post_run(ctx)
        assert ctx.n_input_tokens == 100
        assert ctx.n_output_tokens == 40
        assert ctx.n_cache_tokens == 5
        assert ctx.metadata is not None
        assert ctx.metadata["bitfun"]["trajectory_path"] == "agent/trajectory.json"
        assert ctx.metadata["bitfun"]["session_id"] == sid
        assert ctx.metadata["bitfun"]["model_name"] == "default"
        assert ctx.metadata["bitfun"]["total_steps"] == 2

    def test_populates_context_artifact_paths_when_present(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        turn = _make_turn(
            0,
            "t",
            sid,
            model_rounds=[
                _make_round(
                    "r",
                    turn_id="t",
                    text_items=[_make_text_item("ti", "hi")],
                )
            ],
        )
        _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid),
            turns=[turn],
        )
        (temp_dir / "bitfun").mkdir(exist_ok=True)
        (temp_dir / "bitfun" / "cli.log").write_text("cli log\n")
        (temp_dir / "bitfun" / "ai-request-audit.jsonl").write_text(
            '{"thinking":true}\n'
        )
        (temp_dir / "bitfun" / "cli-logs" / "20260604T172854").mkdir(parents=True)
        (temp_dir / "bitfun" / "request-traces" / "trace-0001").mkdir(parents=True)
        (temp_dir / "bitfun" / "cp-back-manifest.json").write_text("{}\n")
        (temp_dir / "bitfun" / "config").mkdir(parents=True)
        (temp_dir / "bitfun" / "config" / "app.redacted.json").write_text("{}")

        ctx = AgentContext()
        agent.populate_context_post_run(ctx)

        assert ctx.metadata is not None
        assert ctx.metadata["bitfun"]["bitfun_data_path"] == "agent/bitfun"
        assert ctx.metadata["bitfun"]["cli_log_path"] == "agent/bitfun/cli.log"
        assert (
            ctx.metadata["bitfun"]["ai_request_audit_path"]
            == "agent/bitfun/ai-request-audit.jsonl"
        )
        assert ctx.metadata["bitfun"]["cli_logs_path"] == "agent/bitfun/cli-logs"
        assert (
            ctx.metadata["bitfun"]["request_traces_path"]
            == "agent/bitfun/request-traces"
        )
        assert (
            ctx.metadata["bitfun"]["cp_back_manifest_path"]
            == "agent/bitfun/cp-back-manifest.json"
        )
        assert (
            ctx.metadata["bitfun"]["final_app_config_path"]
            == "agent/bitfun/config/app.redacted.json"
        )

    def test_swallows_conversion_errors_and_returns_normally(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "s"
        session_dir = temp_dir / "bitfun" / "sessions" / sid
        (session_dir / "turns").mkdir(parents=True)
        (session_dir / "metadata.json").write_text(_json.dumps(_make_metadata(sid)))
        (session_dir / "turns" / "turn-0000.json").write_text("{not json")
        ctx = AgentContext()
        agent.populate_context_post_run(ctx)
        assert ctx.is_empty()
        assert not (temp_dir / "trajectory.json").exists()


class TestSnapshotFallback:
    """Cover BitFun's ``exec``-mode quirk where ``turns/`` is truncated to a
    final-round placeholder but ``snapshots/context-*.json`` still has the
    full conversation."""

    def _make_truncated_turn(self, sid: str, turn_id: str, *, ts: int) -> dict:
        """Mimic the BitFun 0.2.7 ``exec`` artifact: a single ``-final-round``
        with no tool/thinking items and only the final text."""
        return _make_turn(
            0,
            turn_id,
            sid,
            user_text="fix the bug",
            ts=ts,
            model_rounds=[
                _make_round(
                    f"{turn_id}-final-round",
                    turn_id=turn_id,
                    round_index=0,
                    ts=ts + 100,
                    text_items=[
                        _make_text_item(
                            f"{turn_id}-final-text",
                            "all done",
                            order_index=0,
                            ts=ts + 100,
                        )
                    ],
                    model_id=None,
                )
            ],
        )

    def test_snapshot_used_when_turn_files_only_have_final_round(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="default")
        sid = "snap-fallback-1"
        turn_id = "t-1"
        ts = _DEFAULT_TS_MS

        snapshot_messages = [
            _snap_user_msg(turn_id, "fix the bug", ts=ts),
            _snap_assistant_tool_call_msg(
                turn_id,
                "r-1",
                "tool-A",
                "Grep",
                {"pattern": "foo"},
                ts=ts + 10,
            ),
            _snap_tool_result_msg(
                turn_id,
                "r-1",
                "tool-A",
                "Grep",
                result_for_assistant="3 matches",
                ts=ts + 20,
            ),
            _snap_assistant_tool_call_msg(
                turn_id,
                "r-2",
                "tool-B",
                "Edit",
                {"file": "x.py"},
                reasoning="need to edit",
                ts=ts + 30,
            ),
            _snap_tool_result_msg(
                turn_id,
                "r-2",
                "tool-B",
                "Edit",
                result_for_assistant="ok edited",
                ts=ts + 40,
            ),
            _snap_assistant_text_msg(turn_id, "r-3-final", "all done", ts=ts + 50),
        ]

        _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid, turn_count=1, tool_call_count=0),
            turns=[self._make_truncated_turn(sid, turn_id, ts=ts)],
            snapshot_messages=snapshot_messages,
            snapshot_session_id=sid,
        )

        traj = agent._convert_events_to_trajectory(
            temp_dir / "bitfun" / "sessions" / sid
        )
        assert traj is not None
        assert traj.session_id == sid

        sources = [s.source for s in traj.steps]
        assert sources.count("user") == 1
        assert sources.count("agent") >= 3

        tool_steps = [s for s in traj.steps if s.tool_calls]
        assert len(tool_steps) == 2
        names = {s.tool_calls[0].function_name for s in tool_steps}
        assert names == {"Grep", "Edit"}

        edit_step = next(
            s for s in tool_steps if s.tool_calls[0].function_name == "Edit"
        )
        assert edit_step.reasoning_content == "need to edit"
        assert edit_step.observation is not None
        assert edit_step.observation.results[0].content == "ok edited"

        final_text_steps = [
            s
            for s in traj.steps
            if s.source == "agent" and not s.tool_calls and s.message
        ]
        assert any(s.message == "all done" for s in final_text_steps)

    def test_turn_files_used_when_snapshot_missing(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "no-snap"
        turn = _make_turn(
            0,
            "t1",
            sid,
            user_text="hi",
            model_rounds=[
                _make_round(
                    "r1",
                    turn_id="t1",
                    text_items=[_make_text_item("ti", "hello")],
                )
            ],
        )
        _write_session(temp_dir, sid, metadata=_make_metadata(sid), turns=[turn])
        traj = agent._convert_events_to_trajectory(
            temp_dir / "bitfun" / "sessions" / sid
        )
        assert traj is not None
        agent_steps = [s for s in traj.steps if s.source == "agent"]
        assert agent_steps[0].message == "hello"

    def test_turn_files_used_when_snapshot_has_equal_or_fewer_rounds(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="openai/gpt-5")
        sid = "rich-turns"
        turn_id = "t1"
        ts = _DEFAULT_TS_MS

        turn = _make_turn(
            0,
            turn_id,
            sid,
            user_text="hello",
            ts=ts,
            model_rounds=[
                _make_round(
                    "r1",
                    turn_id=turn_id,
                    round_index=0,
                    ts=ts + 100,
                    text_items=[_make_text_item("ti1", "richer answer", order_index=0)],
                    duration_ms=500,
                ),
            ],
        )
        snapshot_messages = [
            _snap_user_msg(turn_id, "hello", ts=ts),
            _snap_assistant_text_msg(turn_id, "r1", "different answer", ts=ts + 100),
        ]
        _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid),
            turns=[turn],
            snapshot_messages=snapshot_messages,
            snapshot_session_id=sid,
        )
        traj = agent._convert_events_to_trajectory(
            temp_dir / "bitfun" / "sessions" / sid
        )
        assert traj is not None
        agent_steps = [s for s in traj.steps if s.source == "agent"]
        assert agent_steps[0].message == "richer answer"

    def test_synthesize_returns_none_when_no_snapshot_dir(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="default")
        sid = "x"
        session_dir = _write_session(
            temp_dir, sid, metadata=_make_metadata(sid), turns=[]
        )
        assert agent._synthesize_turns_from_snapshot(session_dir) is None

    def test_synthesize_strips_user_query_wrapper(self, temp_dir):
        agent = BitfunCli(logs_dir=temp_dir, model_name="default")
        sid = "s"
        turn_id = "t1"
        ts = _DEFAULT_TS_MS
        # Need strictly more rounds than the truncated turn file for snapshot to win.
        snapshot_messages = [
            _snap_user_msg(turn_id, "real question", ts=ts),
            _snap_assistant_tool_call_msg(
                turn_id, "r1", "tc1", "Read", {"file": "x"}, ts=ts + 10
            ),
            _snap_tool_result_msg(turn_id, "r1", "tc1", "Read", ts=ts + 20),
            _snap_assistant_text_msg(turn_id, "r2", "answer", ts=ts + 30),
        ]
        _write_session(
            temp_dir,
            sid,
            metadata=_make_metadata(sid, tool_call_count=0),
            turns=[self._make_truncated_turn(sid, turn_id, ts=ts)],
            snapshot_messages=snapshot_messages,
            snapshot_session_id=sid,
        )
        traj = agent._convert_events_to_trajectory(
            temp_dir / "bitfun" / "sessions" / sid
        )
        assert traj is not None
        user_step = next(s for s in traj.steps if s.source == "user")
        assert user_step.message == "real question"
