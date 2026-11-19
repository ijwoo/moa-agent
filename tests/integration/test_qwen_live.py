"""실제 Ollama + qwen3:8b 호출. RUN_INTEGRATION=1 일 때만 실행한다."""

import os

import pytest

from agent.config import get_provider
from agent.tasks.parser import parse_command

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="RUN_INTEGRATION=1 필요"),
]


def test_parse_simple_korean_command():
    r = parse_command(get_provider("qwen"), "지난 회의에서 요청한 자료가 메일로 왔는지 확인해줘")
    assert r.ok, r.error
    assert r.task.intent == "check_email"
    assert r.task.needs_approval is False


def test_parse_send_requires_approval():
    r = parse_command(get_provider("qwen"), "김팀장님께 회의록 메일 보내줘")
    assert r.ok, r.error
    assert r.task.intent == "send_email"
    assert r.task.needs_approval is True
