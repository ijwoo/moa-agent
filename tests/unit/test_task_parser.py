import json

import pytest
from pydantic import ValidationError

from agent.providers.base import Message, ModelProvider, ModelResponse
from agent.tasks.parser import parse_command
from agent.tasks.schema import TaskRequest, apply_approval_policy


class FakeProvider(ModelProvider):
    name = "fake"

    def __init__(self, text: str):
        self.text = text
        self.last_schema = None

    def generate(self, messages: list[Message], *, json_schema=None, temperature=0.0, max_tokens=None):
        self.last_schema = json_schema
        return ModelResponse(text=self.text, latency_ms=1.0, model="fake", provider="fake")


def task_json(**over) -> str:
    base = {
        "intent": "check_email",
        "target": "회의 자료",
        "time_ref": "지난 회의",
        "details": "회의 자료 메일 도착 여부 확인",
        "needs_approval": False,
        "needs_clarification": False,
    }
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


def test_valid_output_parses_and_schema_is_sent():
    p = FakeProvider(task_json())
    r = parse_command(p, "지난 회의 자료 메일 왔어?")
    assert r.ok
    assert r.task.intent == "check_email"
    assert p.last_schema == TaskRequest.model_json_schema()


def test_invalid_json_is_reported_not_raised():
    r = parse_command(FakeProvider('{"intent": "check_'), "x")
    assert not r.ok
    assert "JSON 파싱 실패" in r.error


def test_unknown_intent_fails_validation():
    r = parse_command(FakeProvider(task_json(intent="run_shell")), "x")
    assert not r.ok
    assert "스키마 검증 실패" in r.error


def test_extra_field_is_rejected():
    r = parse_command(FakeProvider(task_json(command="rm -rf /")), "x")
    assert not r.ok


def test_missing_field_is_rejected():
    data = json.loads(task_json())
    del data["needs_approval"]
    with pytest.raises(ValidationError):
        TaskRequest.model_validate(data)


def test_policy_forces_approval_for_sensitive_intent():
    r = parse_command(FakeProvider(task_json(intent="send_email", needs_approval=False)), "x")
    assert r.ok
    assert r.raw_task.needs_approval is False  # 모델 원 출력은 평가용으로 보존
    assert r.task.needs_approval is True


def test_policy_keeps_conservative_true():
    t = TaskRequest.model_validate(json.loads(task_json(needs_approval=True)))
    assert apply_approval_policy(t).needs_approval is True


@pytest.mark.parametrize("intent", ["send_email", "delete_email", "submit_form", "purchase"])
def test_all_sensitive_intents_require_approval(intent):
    t = TaskRequest.model_validate(json.loads(task_json(intent=intent)))
    assert apply_approval_policy(t).needs_approval is True
