import json

import pytest

from benchmarks.deepswe.supervision import EventJournal


def test_exec_event_with_null_completion_is_still_running(tmp_path):
    path = tmp_path / "stream.jsonl"
    event = {
        "id": "event-1",
        "event": {
            "type": "ToolEvent",
            "session_id": "session-1",
            "tool_event": {
                "event_type": "Completed",
                "tool_id": "tool-1",
                "tool_name": "ExecCommand",
                "result": {
                    "completion": None,
                    "exit_code": None,
                    "output": "",
                    "session_id": 29967,
                },
                "result_for_assistant": "<status>\nProcess is still running.\n</status>",
            },
        },
    }
    path.write_text(json.dumps(event) + "\n")
    journal = EventJournal(path, "session-1", record_observations=True)
    journal.signatures = ["previous-read"] * 3
    journal.poll()
    assert journal.completed_tools == 1
    assert journal.signatures == []
    assert not journal.repeated_actions()
    assert "still running" in journal.observations[0]["observation"]
    journal.poll()
    assert journal.completed_tools == 1


def test_exec_event_with_exited_completion_is_recorded(tmp_path):
    path = tmp_path / "stream.jsonl"
    event = {
        "id": "event-1",
        "event": {
            "type": "ToolEvent",
            "session_id": "session-1",
            "tool_event": {
                "event_type": "Completed",
                "tool_id": "tool-1",
                "tool_name": "ExecCommand",
                "result": {
                    "completion": {"status": "exited"},
                    "exit_code": 0,
                    "output": "PASS",
                },
                "result_for_assistant": "PASS",
            },
        },
    }
    path.write_text(json.dumps(event) + "\n")
    journal = EventJournal(path, "session-1")
    journal.poll()
    assert journal.completed_tools == 1
    assert len(journal.signatures) == 1


@pytest.mark.parametrize('event', [None, [], 'unexpected',
    {'type': 'ToolEvent', 'tool_event': ['unexpected']},
    {'type': 'ToolEvent', 'tool_event': {'tool_id': [], 'event_type': 'Started'}},
    {'type': 'TokenUsageUpdated', 'input_tokens': 'unknown', 'output_tokens': None},
    {'type': 'TextChunk', 'text': {'unexpected': 'shape'}},
])
def test_optional_native_telemetry_cannot_crash_role_controller(tmp_path, event):
    journal = EventJournal(tmp_path/'stream', 'session', tmp_path/'text')
    journal.feed({'id': [], 'event': event})
    journal.feed({'event': {'type': 'ModelRoundStarted'}})
    assert journal.model_calls == 1
    if isinstance(event, dict) and event.get('type') == 'TokenUsageUpdated':
        assert journal.summary()['input_tokens'] is None
        assert journal.errors
