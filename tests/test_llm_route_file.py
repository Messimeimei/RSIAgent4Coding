"""Custom OpenAI-compatible routes preserve the reviewed OpenRouter default."""
from types import SimpleNamespace
import json
import pytest

from llm import client as C


def write_route(tmp_path):
    route = tmp_path / "route.json"
    route.write_text("""{
  "name": "csi-test",
  "transport": "openai_compatible",
  "base_url": "http://router.invalid/v1",
  "api_key_env": "CSI_TEST_KEY",
  "model_map": {
    "z-ai/glm-5.3": "GLM-5.3-Flash",
    "moonshotai/kimi-k3": "Qwen3.7-Plus"
  },
  "reasoning_effort_map": {
    "z-ai/glm-5.3": "low"
  },
  "supports_openrouter_provider_routing": false,
  "supports_reasoning_controls": false,
  "supports_reasoning_history": false,
  "max_output_tokens_cap": 12288,
  "max_output_tokens_map": {
    "z-ai/glm-5.3": 4096,
    "moonshotai/kimi-k3": 8192
  }
}\n""", encoding="utf-8")
    return route


def test_custom_route_resolves_named_key_without_changing_default(monkeypatch, tmp_path):
    route = write_route(tmp_path)
    env_file = tmp_path / "runtime.env"
    env_file.write_text("CSI_TEST_KEY=private-test-value\n", encoding="utf-8")
    created = []
    marker = object()

    def openai(**kwargs):
        created.append(kwargs)
        return marker

    monkeypatch.setenv("RSIAGENT_LLM_ROUTE_FILE", str(route))
    monkeypatch.setenv("RSIAGENT_ENV_FILE", str(env_file))
    monkeypatch.delenv("CSI_TEST_KEY", raising=False)
    monkeypatch.setattr(C, "OpenAI", openai)
    monkeypatch.setattr(C, "_client", None)

    assert C._c() is marker
    assert created == [{
        "base_url": "http://router.invalid/v1",
        "api_key": "private-test-value",
    }]
    monkeypatch.delenv("RSIAGENT_LLM_ROUTE_FILE")
    assert C._transport_route() == C.DEFAULT_ROUTE


def test_custom_route_maps_wire_model_and_strips_openrouter_fields(
        monkeypatch, tmp_path):
    route = write_route(tmp_path)
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(
            provider=None, model_extra={}, usage=None,
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content="done", reasoning=None,
                    reasoning_content=None, tool_calls=[]))])

    fake = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setenv("RSIAGENT_LLM_ROUTE_FILE", str(route))
    monkeypatch.setattr(C, "_client", fake)
    monkeypatch.setattr(C, "_PROVIDER_COUNTS", {})

    answer = C.chat(
        "moonshotai/kimi-k3", "system", "user",
        history=[{"role": "assistant", "content": "prior",
                  "reasoning": "private reasoning"}],
        max_tokens=65536, reasoning_effort="max",
        provider_order=["MoonshotAI"])

    assert answer == "done"
    assert requests[0]["model"] == "Qwen3.7-Plus"
    assert requests[0]["max_tokens"] == 8192
    assert "extra_body" not in requests[0]
    assert "reasoning" not in requests[0]["messages"][1]
    assert C.chat(
        "z-ai/glm-5.3", "system", "user",
        reasoning_effort="max") == "done"
    assert requests[1]["model"] == "GLM-5.3-Flash"
    assert requests[1]["max_tokens"] == 4096
    assert requests[1]["extra_body"]["reasoning"] == {"effort": "low"}
    assert C.provider_counts() == {
        "moonshotai/kimi-k3": {"csi-test:Qwen3.7-Plus": 1},
        "z-ai/glm-5.3": {"csi-test:GLM-5.3-Flash": 1},
    }


def test_stream_collector_preserves_visible_and_reasoning_channels():
    events = [
        SimpleNamespace(
            id="stream-1", provider="CSI", model_extra={}, usage=None,
            choices=[SimpleNamespace(
                finish_reason=None,
                delta=SimpleNamespace(
                    content=None, reasoning_content="think ",
                    reasoning=None, tool_calls=[]))]),
        SimpleNamespace(
            id="stream-1", provider=None, model_extra={}, usage="usage",
            choices=[SimpleNamespace(
                finish_reason="stop",
                delta=SimpleNamespace(
                    content="done", reasoning_content=None,
                    reasoning=None, tool_calls=[]))]),
    ]

    response = C._consume_stream(iter(events))

    assert response.provider == "CSI"
    assert response.usage == "usage"
    assert response.choices[0].finish_reason == "stop"
    assert response.choices[0].message.content == "done"
    assert response.choices[0].message.reasoning == "think "


def _completion(content, finish_reason):
    return SimpleNamespace(
        provider=None, model_extra={}, usage=None,
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(
                content=content, reasoning=None,
                reasoning_content=None, tool_calls=[]))])


def _install_capped_client(monkeypatch, tmp_path, replies):
    route = write_route(tmp_path)
    requests = []
    responses = iter(replies)

    def create(**kwargs):
        requests.append(kwargs)
        return next(responses)

    fake = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setenv("RSIAGENT_LLM_ROUTE_FILE", str(route))
    monkeypatch.setattr(C, "_client", fake)
    monkeypatch.setattr(C, "_PROVIDER_COUNTS", {})
    return requests


def test_token_cap_prevents_identical_truncation_retry(monkeypatch, tmp_path):
    requests = _install_capped_client(
        monkeypatch, tmp_path,
        [_completion('{"program":', "length")])

    answer = C.chat(
        "moonshotai/kimi-k3", "system", "user", max_tokens=65536)

    assert answer == '{"program":'
    assert [request["max_tokens"] for request in requests] == [8192]


def test_truncation_retry_can_grow_until_route_cap(monkeypatch, tmp_path):
    requests = _install_capped_client(
        monkeypatch, tmp_path,
        [_completion('{"program":', "length"),
         _completion("complete", "stop")])

    assert C.chat(
        "moonshotai/kimi-k3", "system", "user",
        max_tokens=4096) == "complete"
    assert [request["max_tokens"] for request in requests] == [4096, 8192]


def test_truncation_retry_stops_after_reaching_route_cap(
        monkeypatch, tmp_path):
    requests = _install_capped_client(
        monkeypatch, tmp_path,
        [_completion('{"program":', "length"),
         _completion('{"program":', "length")])

    answer = C.chat(
        "moonshotai/kimi-k3", "system", "user", max_tokens=4096)

    assert answer == '{"program":'
    assert [request["max_tokens"] for request in requests] == [4096, 8192]


def test_strict_route_never_drops_request_parameters(monkeypatch, tmp_path):
    path = write_route(tmp_path)
    route = json.loads(path.read_text())
    route["strict_request_parameters"] = True
    path.write_text(json.dumps(route))
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        raise ValueError("unsupported request field")

    monkeypatch.setenv("RSIAGENT_LLM_ROUTE_FILE", str(path))
    monkeypatch.setattr(C, "_client", SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(C.time, "sleep", lambda *_: None)
    with pytest.raises(C.LLMTransportError):
        C.chat("moonshotai/kimi-k3", "system", "user", max_tokens=65536,
               temperature=1.0, top_p=1.0, json_object=True)
    assert len(seen) == 4
    assert all(r["top_p"] == 1.0 and r["response_format"] == {"type": "json_object"}
               for r in seen)


def test_strict_truncation_never_returns_partial_agent_output(monkeypatch, tmp_path):
    requests = _install_capped_client(monkeypatch, tmp_path,
                                     [_completion('{"program":', "length")])
    path = tmp_path / "route.json"
    route = json.loads(path.read_text())
    route["fail_on_truncation"] = True
    path.write_text(json.dumps(route))
    with pytest.raises(C.LLMTransportError, match="no agent reply accepted") as caught:
        C.chat("moonshotai/kimi-k3", "system", "user", max_tokens=65536)
    assert caught.value.recoverable is False
    assert len(requests) == 1
