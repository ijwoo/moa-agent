import pytest

from agent.providers.base import ToolCall
from agent.tools import Observation, Policy, default_registry, validate_call
from agent.tools.validate import check_url

PAGE = """URL: http://shop.test/cart
제목: 장바구니
[e1] link "홈"
text "합계 64,900원"
[e2] textbox "쿠폰 코드"
[e4] button "결제하기"
"""

REG = default_registry()
POLICY = Policy(allowed_hosts=frozenset({"shop.test", "mail.test"}))
OBS = Observation.parse(PAGE)


def check(name, **args):
    return validate_call(ToolCall(name=name, arguments=args), REG, POLICY, OBS)


def test_observation_parse():
    assert OBS.url == "http://shop.test/cart"
    assert set(OBS.elements) == {"e1", "e2", "e4"}
    assert OBS.elements["e4"].label == "결제하기"
    assert OBS.elements["e2"].role == "textbox"


def test_ok_click():
    c = check("click", ref="e1")
    assert c.status == "ok" and c.executable


def test_unknown_tool():
    c = check("run_shell", cmd="ls")
    assert c.status == "unknown_tool"
    assert "navigate" in c.reason


@pytest.mark.parametrize("args", [{}, {"ref": "3"}, {"ref": "e1", "force": True}, {"ref": 3}])
def test_invalid_click_args(args):
    assert validate_call(ToolCall(name="click", arguments=args), REG, POLICY, OBS).status == "invalid_args"


def test_unknown_ref_is_rejected():
    c = check("click", ref="e99")
    assert c.status == "unknown_ref"
    assert "e99" in c.reason


def test_ref_without_observation():
    c = validate_call(ToolCall(name="click", arguments={"ref": "e1"}), REG, POLICY, None)
    assert c.status == "unknown_ref"


def test_sensitive_click_needs_approval():
    c = check("click", ref="e4")
    assert c.status == "needs_approval"
    assert c.valid and not c.executable
    assert c.approval_detail["label"] == "결제하기"


def test_type_ok_and_extract_optional_ref():
    assert check("type", ref="e2", text="ABC").status == "ok"
    # 검색창이 아닌 입력칸에서 Enter 제출은 양식 제출이라 거절 (Phase 4 보안 수정)
    assert check("type", ref="e2", text="ABC", submit=True).status == "policy_denied"
    assert check("extract_text").status == "ok"
    assert check("extract_text", ref="e1").status == "ok"


@pytest.mark.parametrize("url,ok", [
    ("http://shop.test/cart", True),
    ("https://www.shop.test/", True),
    ("http://evil.example/", False),
    ("http://shop.test.evil.example/", False),
    ("http://shop.test@evil.example/", False),
    ("javascript:alert(1)", False),
    ("file:///etc/passwd", False),
    ("shop.test", False),
    ("http://notshop.test/", False),
    ("http://shop.test/_test/state", False),
    ("http://shop.test//_test/reset", False),
    ("http://shop.test/_test", False),
    ("http://shop.test/%5Ftest/state", False),
    ("http://shop.test/%5ftest/reset", False),
    ("http://shop.test/_testing", True),
    ("http://shop.test/products/_test", True),
])
def test_url_policy(url, ok):
    assert (check_url(url, POLICY) is None) == ok
    status = check("navigate", url=url).status
    assert status == ("ok" if ok else "policy_denied")


def test_empty_allowlist_denies_everything():
    assert check_url("http://shop.test/", Policy(allowed_hosts=frozenset())) is not None


def test_tool_definitions_have_no_titles_and_forbid_extra():
    defs = {d.name: d for d in REG.definitions()}
    assert set(defs) == {"navigate", "observe", "click", "type", "scroll", "extract_text", "search_web", "ask_user"}
    click = defs["click"].parameters
    assert click["additionalProperties"] is False
    assert "title" not in click and "title" not in click["properties"]["ref"]
    assert click["required"] == ["ref"]


def test_type_submit_outside_searchbox_is_denied():
    obs = Observation.parse('URL: http://booking.test/room\n[e1] textbox "날짜"\n[e2] searchbox "검색"')
    pol = Policy(allowed_hosts=frozenset({"booking.test"}))
    c = validate_call(ToolCall(name="type", arguments={"ref": "e1", "text": "2026-11-17", "submit": True}), REG, pol, obs)
    assert c.status == "policy_denied" and "Enter" in c.reason
    assert validate_call(ToolCall(name="type", arguments={"ref": "e1", "text": "x"}), REG, pol, obs).status == "ok"
    assert validate_call(ToolCall(name="type", arguments={"ref": "e2", "text": "x", "submit": True}), REG, pol, obs).status == "ok"


def test_navigate_urls_restriction():
    pol = Policy(allowed_hosts=frozenset({"shop.test"}), navigate_urls=frozenset({"http://shop.test/"}))
    assert validate_call(ToolCall(name="navigate", arguments={"url": "http://shop.test/"}), REG, pol).status == "ok"
    c = validate_call(ToolCall(name="navigate", arguments={"url": "http://shop.test/checkout"}), REG, pol)
    assert c.status == "policy_denied" and "click" in c.reason


def test_select_option_only_on_combobox():
    from agent.tools.browser import agent_registry
    obs = Observation.parse('URL: http://booking.test/room\n[e1] combobox "회의실"\n[e2] textbox "날짜"')
    pol = Policy(allowed_hosts=frozenset({"booking.test"}))
    reg = agent_registry()
    ok = validate_call(ToolCall(name="select_option", arguments={"ref": "e1", "option": "소회의실 A"}), reg, pol, obs)
    bad = validate_call(ToolCall(name="select_option", arguments={"ref": "e2", "option": "x"}), reg, pol, obs)
    assert ok.status == "ok" and bad.status == "invalid_args"
    assert "select_option" not in REG.names()  # Phase 2 평가 도구 세트는 그대로


def test_read_draft_mode_forbids_actions_and_gates_draft():
    from agent.tools.browser import agent_registry
    obs = Observation.parse('URL: http://mail.test/compose\n[e1] link "받은편지함"\n[e2] button "검색"\n'
                            '[e3] button "임시 저장"\n[e4] button "보내기"\n[e5] button "장바구니 담기"\n[e6] link "삭제"\n'
                            '[e7] textbox "본문"')
    pol = Policy(allowed_hosts=frozenset({"mail.test"}), mode="read_draft")
    reg = agent_registry()
    st = lambda ref: validate_call(ToolCall(name="click", arguments={"ref": ref}), reg, pol, obs)
    assert st("e1").status == "ok" and st("e2").status == "ok"
    d = st("e3")
    assert d.status == "needs_approval" and d.approval_detail["kind"] == "draft"
    for ref in ("e4", "e5", "e6"):
        assert st(ref).status == "policy_denied", ref
    assert "발송" in st("e4").reason
    assert validate_call(ToolCall(name="type", arguments={"ref": "e7", "text": "초안"}), reg, pol, obs).status == "ok"


# ---- D120: 화면을 여는 메뉴·문제 보기, 사용자가 하지 말라고 한 동작 (실기기 2026-11-18 교육 사이트) ----
from dataclasses import replace as _rep  # noqa: E402

from agent.sites.inside import SITE_BUTTON_ONLY, SITE_SENSITIVE  # noqa: E402

EDU = """URL: http://shop.test/class
[e1] link "과제제출"
[e2] link "수업신청 내역"
[e3] radio "1. 제거소화-산소공급을 차단하여"
[e4] button "제출하기"
[e5] link "결제하러 가기"
[e6] link "제출"
[e7] button "과제제출"
[e8] link "게시물 삭제"
"""
SITE_POLICY = Policy(allowed_hosts=frozenset({"shop.test"}), sensitive_keywords=SITE_SENSITIVE, button_keywords=SITE_BUTTON_ONLY, nav_relax=True)


def edu(ref, policy=SITE_POLICY):
    return validate_call(ToolCall(name="click", arguments={"ref": ref}), REG, policy, Observation.parse(EDU)).status


def test_menus_and_quiz_choices_do_not_ask_but_real_actions_do():
    assert edu("e1") == "ok" and edu("e2") == "ok" and edu("e3") == "ok"          # 화면을 여는 메뉴·문제 보기
    assert edu("e4") == "needs_approval" and edu("e6") == "needs_approval"        # 동작 그 자체
    assert edu("e7") == "needs_approval"                                          # 단추는 그대로 묻는다
    assert edu("e5") == "needs_approval" and edu("e8") == "needs_approval"        # 링크라도 돈·삭제는 묻는다
    off = _rep(SITE_POLICY, nav_relax=False)
    assert edu("e1", off) == "needs_approval"                                     # 켜지 않은 길은 예전대로


def test_actions_the_user_ruled_out_are_refused_without_asking():
    no_submit = _rep(SITE_POLICY, forbid_words=("제출",))
    assert edu("e4", no_submit) == "policy_denied" and edu("e1", no_submit) == "ok"   # 보는 메뉴는 그대로 연다
    assert edu("e7", no_submit) == "needs_approval"     # '과제제출' 단추: 작성 화면을 여는 단추일 수 있다 — 막지 않고 카드로 묻는다(실기기 2026-11-18)
    look = _rep(SITE_POLICY, read_only=True)
    assert edu("e4", look) == "policy_denied" and edu("e5", look) == "policy_denied" and edu("e2", look) == "ok"
    assert edu("e7", look) == "needs_approval"


def test_forbid_words_come_from_the_users_words():
    from agent.sites.inside import forbid_from
    assert forbid_from("과제 화면 읽어줘. 제출은 하지 마") == (("제출",), False)
    assert forbid_from("제출하기·저장 버튼은 누르지 말고 보기만 해") == (("제출", "저장"), True)
    assert forbid_from("과제를 제출하고 설문은 하지 마") == ((), False)
    assert forbid_from("평가 풀어서 제출해줘") == ((), False)
    assert forbid_from("과제 화면을 열어서 읽어서 알려줘. 제출은 하지 않는다.") == (("제출",), False)   # 대화 판단이 옮겨 적은 꼴
