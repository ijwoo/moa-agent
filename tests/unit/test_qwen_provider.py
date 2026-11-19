import json

import httpx
import pytest

from agent.providers.base import Message, ProviderError
from agent.providers.qwen import QwenProvider


def make_provider(handler, **kw) -> QwenProvider:
    return QwenProvider(transport=httpx.MockTransport(handler), **kw)


def ok_body(content: str, **extra) -> dict:
    body = {
        "model": "qwen3:8b",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 12,
        "eval_count": 34,
    }
    body.update(extra)
    return body


def test_request_payload_includes_schema_and_think_off():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=ok_body("{}"))

    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    make_provider(handler).generate(
        [Message(role="user", content="안녕")], json_schema=schema, max_tokens=50
    )
    body = seen["body"]
    assert seen["path"] == "/api/chat"
    assert body["format"] == schema
    assert body["think"] is False
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0.0, "num_predict": 50}
    assert body["messages"] == [{"role": "user", "content": "안녕"}]


def test_response_is_normalized():
    p = make_provider(lambda r: httpx.Response(200, json=ok_body('{"x": 1}')))
    resp = p.generate([Message(role="user", content="hi")])
    assert resp.text == '{"x": 1}'
    assert resp.finish_reason == "stop"
    assert resp.usage.input_tokens == 12
    assert resp.usage.output_tokens == 34
    assert resp.provider == "qwen-ollama"
    assert resp.latency_ms >= 0


def test_think_block_is_stripped():
    p = make_provider(
        lambda r: httpx.Response(200, json=ok_body("<think>\n생각 중\n</think>\n\n{\"x\": 1}"))
    )
    assert p.generate([Message(role="user", content="hi")]).text == '{"x": 1}'


def test_length_finish_reason():
    p = make_provider(lambda r: httpx.Response(200, json=ok_body('{"x"', done_reason="length")))
    assert p.generate([Message(role="user", content="hi")]).finish_reason == "length"


def test_tool_calls_normalized_including_string_arguments():
    body = ok_body("")
    body["message"]["tool_calls"] = [
        {"function": {"name": "navigate", "arguments": {"url": "http://localhost"}}},
        {"function": {"name": "click", "arguments": '{"ref": "e1"}'}},
        {"function": {"arguments": {}}},  # 이름 없음 → 버림
    ]
    resp = make_provider(lambda r: httpx.Response(200, json=body)).generate(
        [Message(role="user", content="hi")]
    )
    assert [c.name for c in resp.tool_calls] == ["navigate", "click"]
    assert resp.tool_calls[1].arguments == {"ref": "e1"}
    assert resp.finish_reason == "tool_calls"


def test_http_error_raises_provider_error():
    p = make_provider(lambda r: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(ProviderError, match="model not found"):
        p.generate([Message(role="user", content="hi")])


def test_connection_error_raises_provider_error():
    def handler(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderError, match="연결 실패"):
        make_provider(handler).generate([Message(role="user", content="hi")])


def test_non_json_body_raises_provider_error():
    p = make_provider(lambda r: httpx.Response(200, text="not json"))
    with pytest.raises(ProviderError):
        p.generate([Message(role="user", content="hi")])
