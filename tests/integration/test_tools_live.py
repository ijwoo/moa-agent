"""실제 Ollama + qwen3:8b 도구 호출. RUN_INTEGRATION=1 일 때만 실행한다."""

import os

import pytest

from agent.providers.qwen import QwenProvider
from agent.tools import Observation, Policy, build_messages, default_registry, select_tool

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="RUN_INTEGRATION=1 필요"),
]

POLICY = Policy(allowed_hosts=frozenset({"shop.test"}))
CART = Observation.parse(
    'URL: http://shop.test/cart\n[e1] link "홈"\n[e5] text "합계 64,900원"\n[e4] button "결제하기"'
)


@pytest.mark.parametrize("mode", ["native", "json"])
def test_click_payment_is_gated_by_approval(mode):
    p = QwenProvider(tool_mode=mode)
    sel = select_tool(p, build_messages("결제해줘", POLICY, CART), default_registry(), POLICY, CART)
    assert sel.final.call is not None
    assert sel.final.call.name == "click" and sel.final.call.arguments.get("ref") == "e4"
    assert sel.final.check.status == "needs_approval"


@pytest.mark.parametrize("mode", ["native", "json"])
def test_navigate_to_named_site(mode):
    p = QwenProvider(tool_mode=mode)
    sel = select_tool(p, build_messages("shop.test 사이트 열어줘", POLICY), default_registry(), POLICY)
    assert sel.valid
    assert sel.final.call.name == "navigate"
    assert "shop.test" in sel.final.call.arguments["url"]
