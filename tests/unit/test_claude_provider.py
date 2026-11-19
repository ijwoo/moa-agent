from types import SimpleNamespace as NS

import pytest

from agent.providers.base import Message, ToolCall, ToolDefinition
from agent.providers.claude import ClaudeProvider, cost_usd, strict_schema


class FakeMessages:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


def reply(content, stop="tool_use", inp=100, out=20, cr=0, cw=0):
    return NS(content=content, stop_reason=stop, model="claude-sonnet-5-5",
              usage=NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=cr, cache_creation_input_tokens=cw))


def provider(replies, **kw):
    fm = FakeMessages(replies)
    return ClaudeProvider(client=NS(messages=fm), **kw), fm


TOOLS = [ToolDefinition(name="click", description="클릭", parameters={"type": "object", "properties": {"ref": {"type": "string", "pattern": "^e\\d+$"}}, "required": ["ref"]})]


def test_tool_call_is_normalized_and_request_shape():
    tu = NS(type="tool_use", id="toolu_abc", name="click", input={"ref": "e3"})
    p, fm = provider([reply([NS(type="thinking", thinking=""), tu], cr=50)])
    r = p.generate([Message(role="system", content="S"), Message(role="user", content="장바구니")], tools=TOOLS, temperature=0.0)
    assert r.tool_calls == [ToolCall(name="click", arguments={"ref": "e3"}, id="toolu_abc")]
    assert r.finish_reason == "tool_calls" and r.usage.input_tokens == 150 and r.provider == "claude"
    kw = fm.calls[0]
    assert kw["system"] == "S" and kw["messages"] == [{"role": "user", "content": "장바구니"}]
    assert kw["tools"][0] == {"name": "click", "description": "클릭", "input_schema": TOOLS[0].parameters}
    assert kw["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert "temperature" not in kw and "fallbacks" not in kw
    assert kw["thinking"] == {"type": "between_tools"} and kw["output_config"] == {"effort": "medium"}
    assert p.totals == {"calls": 1, "input": 100, "output": 20, "cache_read": 50, "cache_write": 0}


def test_history_replays_original_content_and_pairs_results():
    tu = NS(type="tool_use", id="toolu_abc", name="click", input={"ref": "e3"})
    original = [NS(type="thinking", thinking=""), tu]
    p, fm = provider([reply(original), reply([NS(type="text", text="끝")], stop="end_turn")])
    first = p.generate([Message(role="user", content="u")], tools=TOOLS)
    hist = [Message(role="user", content="u"),
            Message(role="assistant", tool_calls=[first.tool_calls[0]]),
            Message(role="tool", tool_name="click", content="click 완료"),
            Message(role="assistant", tool_calls=[ToolCall(name="observe")]),   # 코드가 만든 호출(id 없음)
            Message(role="tool", tool_name="observe", content="화면"),
            Message(role="assistant", tool_calls=[ToolCall(name="finish", arguments={"summary": "x"})]),
            Message(role="user", content="도구를 호출해야 한다")]            # 결과 없는 tool_use 뒤의 사용자 메시지
    r = p.generate(hist, tools=TOOLS)
    m = fm.calls[1]["messages"]
    assert m[1]["content"] is original                       # 원래 블록 그대로 (thinking 포함)
    assert m[2]["content"][0] == {"type": "tool_result", "tool_use_id": "toolu_abc", "content": "click 완료"}
    assert m[3]["content"][0]["id"] == m[4]["content"][0]["tool_use_id"] == "toolu_local_0002"
    assert m[6]["content"][0]["type"] == "tool_result" and m[7] == {"role": "user", "content": "도구를 호출해야 한다"}
    assert r.text == "끝" and r.finish_reason == "stop" and r.tool_calls == []


def test_json_schema_and_thinking_settings_per_model():
    p, fm = provider([reply([NS(type="text", text='{"completed": true, "reason": "r"}')], stop="end_turn")],
                     model="claude-haiku-5-5")
    schema = {"type": "object", "title": "V", "properties": {"completed": {"type": "boolean", "title": "C"},
              "reason": {"type": "string", "maxLength": 5}}, "required": ["completed", "reason"]}
    r = p.generate([Message(role="user", content="q")], json_schema=schema)
    fmt = fm.calls[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False
    assert "title" not in fmt["schema"] and "maxLength" not in fmt["schema"]["properties"]["reason"]
    assert fm.calls[0]["thinking"] == {"type": "disabled"} and "tools" not in fm.calls[0]
    assert r.text.startswith("{")
    p2, fm2 = provider([reply([NS(type="text", text="x")], stop="end_turn")], think=True)
    p2.generate([Message(role="user", content="q")])
    assert "thinking" not in fm2.calls[0]
    with pytest.raises(ValueError):
        p.generate([Message(role="user", content="q")], tools=TOOLS, json_schema=schema)


def test_strict_schema_keeps_property_named_like_keyword():
    s = strict_schema({"type": "object", "properties": {"title": {"type": "string"}, "format": {"type": "string"}}})
    assert set(s["properties"]) == {"title", "format"} and s["required"] == ["title", "format"]


def test_refusal_is_error_not_fallback_and_cost():
    p, _ = provider([reply([NS(type="text", text="...")], stop="refusal")])
    r = p.generate([Message(role="user", content="q")], tools=TOOLS)
    assert r.finish_reason == "error" and r.tool_calls == [] and r.text == ""
    c = cost_usd("claude-sonnet-5-5", {"input": 1_000_000, "output": 100_000, "cache_read": 1_000_000, "cache_write": 0})
    assert abs(c - (2.0 + 1.0 + 0.10)) < 1e-9
