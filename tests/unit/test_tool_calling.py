import json

import httpx
import pytest

from agent.providers.base import Message, ModelProvider, ModelResponse, ToolCall
from agent.providers.qwen import QwenProvider, json_tool_schema
from agent.tools import Observation, Policy, build_messages, default_registry, select_tool

REG = default_registry()
POLICY = Policy(allowed_hosts=frozenset({"shop.test"}))
OBS = Observation.parse('URL: http://shop.test/\n[e1] link "홈"\n[e3] link "장바구니"')


def capture_provider(reply: dict, **kw):
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={
            "model": "qwen3:8b", "message": reply, "done": True, "done_reason": "stop",
        })

    return QwenProvider(transport=httpx.MockTransport(handler), **kw), seen


# --- Provider: native 모드 ---

def test_native_mode_sends_tools_and_history():
    p, seen = capture_provider({"role": "assistant", "content": "",
                                "tool_calls": [{"function": {"name": "click", "arguments": {"ref": "e3"}}}]})
    msgs = build_messages("장바구니 열어줘", POLICY, OBS)
    resp = p.generate(msgs, tools=REG.definitions())
    body = seen["body"]
    assert [t["function"]["name"] for t in body["tools"]][:3] == ["navigate", "observe", "click"]
    assert body["tools"][0]["type"] == "function"
    assert "format" not in body
    assert body["messages"][2] == {"role": "assistant", "content": "",
                                   "tool_calls": [{"function": {"name": "observe", "arguments": {}}}]}
    assert body["messages"][3]["role"] == "tool" and body["messages"][3]["tool_name"] == "observe"
    assert resp.tool_calls == [ToolCall(name="click", arguments={"ref": "e3"})]


def test_plain_messages_serialize_without_tool_fields():
    p, seen = capture_provider({"role": "assistant", "content": "x"})
    p.generate([Message(role="user", content="안녕")])
    assert seen["body"]["messages"] == [{"role": "user", "content": "안녕"}]


def test_tools_and_json_schema_are_exclusive():
    p, _ = capture_provider({"role": "assistant", "content": "x"})
    with pytest.raises(ValueError):
        p.generate([Message(role="user", content="a")], tools=REG.definitions(), json_schema={})


# --- Provider: json 모드 ---

def test_json_mode_uses_format_and_flattens_history():
    p, seen = capture_provider({"role": "assistant", "content": '{"tool": "click", "arguments": {"ref": "e3"}}'},
                               tool_mode="json")
    resp = p.generate(build_messages("장바구니 열어줘", POLICY, OBS), tools=REG.definitions())
    body = seen["body"]
    assert "tools" not in body
    assert body["format"] == json_tool_schema(REG.definitions())
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert "사용 가능한 도구" in body["messages"][0]["content"]
    assert json.loads(body["messages"][2]["content"]) == {"tool": "observe", "arguments": {}}
    assert body["messages"][3]["content"].startswith("[observe 결과]")
    assert all("tool_calls" not in m for m in body["messages"])
    assert resp.tool_calls == [ToolCall(name="click", arguments={"ref": "e3"})]
    assert resp.finish_reason == "tool_calls"


def test_json_tool_schema_shape():
    s = json_tool_schema(REG.definitions())
    first = s["anyOf"][0]
    assert first["properties"]["tool"] == {"const": "navigate"}
    assert first["required"] == ["tool", "arguments"]


def test_json_tool_schema_strips_grammar_unsafe_keywords():
    text = json.dumps(json_tool_schema(REG.definitions()))
    assert '"pattern"' not in text and '"maxLength"' not in text and '"minLength"' not in text
    # 원래 도구 정의(검증용)는 그대로 유지
    click = next(d for d in REG.definitions() if d.name == "click")
    assert click.parameters["properties"]["ref"]["pattern"] == r"^e\d+$"


@pytest.mark.parametrize("content", ["not json", '{"arguments": {}}', "[1,2]"])
def test_json_mode_bad_output_gives_no_tool_calls(content):
    p, _ = capture_provider({"role": "assistant", "content": content}, tool_mode="json")
    resp = p.generate([Message(role="user", content="a")], tools=REG.definitions())
    assert resp.tool_calls == []


# --- select_tool: 잘못된 호출 처리 ---

class ScriptedProvider(ModelProvider):
    name = "scripted"

    def __init__(self, replies: list[list[ToolCall] | str]):
        self.replies = list(replies)
        self.seen: list[list[Message]] = []

    def generate(self, messages, *, tools=None, json_schema=None, temperature=0.0, max_tokens=None):
        self.seen.append(list(messages))
        r = self.replies.pop(0)
        if isinstance(r, str):
            return ModelResponse(text=r, latency_ms=1, model="s", provider="s")
        return ModelResponse(text="", tool_calls=r, latency_ms=1, model="s", provider="s")


def test_valid_first_call_no_retry():
    p = ScriptedProvider([[ToolCall(name="click", arguments={"ref": "e3"})]])
    sel = select_tool(p, build_messages("장바구니", POLICY, OBS), REG, POLICY, OBS)
    assert len(sel.attempts) == 1 and sel.valid
    assert sel.final.check.status == "ok"


def test_invalid_call_is_fed_back_and_retried():
    p = ScriptedProvider([
        [ToolCall(name="navigate", arguments={"url": "http://evil.example/"})],
        [ToolCall(name="click", arguments={"ref": "e3"})],
    ])
    sel = select_tool(p, build_messages("장바구니", POLICY, OBS), REG, POLICY, OBS)
    assert [a.check.status for a in sel.attempts] == ["policy_denied", "ok"]
    feedback = p.seen[1][-1]
    assert feedback.role == "tool" and "policy_denied" in feedback.content and "evil.example" in feedback.content


def test_text_reply_is_retried_then_gives_up():
    p = ScriptedProvider(["그냥 답", "또 답"])
    sel = select_tool(p, build_messages("x", POLICY), REG, POLICY, None, max_attempts=2)
    assert len(sel.attempts) == 2 and not sel.valid
    assert sel.final.call is None


def test_multiple_calls_take_first_and_count_extra():
    p = ScriptedProvider([[ToolCall(name="observe"), ToolCall(name="scroll", arguments={"direction": "down"})]])
    sel = select_tool(p, build_messages("x", POLICY), REG, POLICY, None)
    assert sel.final.call.name == "observe" and sel.final.extra_calls == 1


def test_needs_approval_is_not_retried():
    obs = Observation.parse('URL: http://shop.test/cart\n[e4] button "결제하기"')
    p = ScriptedProvider([[ToolCall(name="click", arguments={"ref": "e4"})]])
    sel = select_tool(p, build_messages("결제해줘", POLICY, obs), REG, POLICY, obs)
    assert len(sel.attempts) == 1 and sel.final.check.status == "needs_approval"
