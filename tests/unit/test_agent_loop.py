from agent.core.loop import AgentLoop, Limits, _compact
from agent.providers.base import Message, ModelProvider, ModelResponse, ToolCall
from agent.tools import Policy
from agent.tools.observation import Observation
from agent.tools.browser import agent_registry

POLICY = Policy(allowed_hosts=frozenset({"shop.test", "mail.test"}))
SITES = {"쇼핑몰": "http://shop.test/"}
CART = 'URL: http://shop.test/cart\n제목: 장바구니\n[e1] link "홈"\n[e2] button "결제하기"'
COMPOSE = ('URL: http://mail.test/compose\n제목: 메일 쓰기\n[e1] textbox "받는 사람" = "a@mail.test"\n'
           '[e2] textbox "본문" = "안녕"\n[e3] button "보내기"')


class Scripted(ModelProvider):
    name = "scripted"

    def __init__(self, calls):
        self.calls = list(calls)
        self.seen = []

    def generate(self, messages, *, tools=None, json_schema=None, temperature=0.0, max_tokens=None):
        self.seen.append(messages)
        c = self.calls.pop(0) if self.calls else ToolCall(name="observe")
        if isinstance(c, str):
            return ModelResponse(text=c, latency_ms=1, model="s", provider="s")
        return ModelResponse(text="", tool_calls=[c], latency_ms=1, model="s", provider="s")


def clicks(b, ref: str) -> int:
    """그 요소를 실제로 누른 횟수. 승인된 누르기에는 accept_dialog 표시가 붙는다(그 동작이 띄우는 확인 창도 '확인'으로 닫는다)."""
    return sum(1 for op, args in b.ops if op == "click" and args.get("ref") == ref)


class FakeBrowser:
    def __init__(self, page, after_observes=None, raw=None, raw_after=None, fail_fields=False):
        self.page = page
        self.ops = []
        self.after_observes = after_observes or {}  # n번째 observe 이후 페이지 교체 (승인 중 변경 흉내)
        self.n_observe = 0
        # 입력칸 원문 {ref: 값}. 없으면 관찰 결과의 값을 그대로 돌려준다(짧은 값은 요약과 원문이 같다)
        self.raw, self.raw_after, self.fail_fields = raw or {}, raw_after or {}, fail_fields

    def request(self, op, args=None):
        self.ops.append((op, args or {}))
        if op == "field_values":
            if self.fail_fields:
                raise RuntimeError("field_values 실패")
            obs = Observation.parse(self.page)
            raw = {**self.raw, **self.raw_after.get(self.n_observe, {})}
            return {"values": {r: raw.get(r, obs.elements[r].value or "") for r in args["refs"]}}
        if op == "observe":
            self.n_observe += 1
            if self.n_observe in self.after_observes:
                self.page = self.after_observes[self.n_observe]
            return {"url": "", "title": "", "text": self.page}
        if op == "extract_text":
            return {"url": "http://shop.test/", "title": "t", "text": "본문", "truncated": False}
        return {"url": "http://shop.test/", "title": "t"}


def loop(calls, page=CART, approver=lambda r: True, **kw):
    browser = kw.pop("browser", None) or FakeBrowser(page)
    a = AgentLoop(Scripted(calls), browser, agent_registry(), POLICY, SITES, approver, **kw)
    return a, browser


def C(name, **args):
    return ToolCall(name=name, arguments=args)


def test_finish_completes_with_summary():
    a, _ = loop([C("observe"), C("finish", summary="합계 64,900원")])
    r = a.run("합계 알려줘")
    assert r.status == "completed" and r.summary == "합계 64,900원" and r.llm_calls == 2


def test_verifier_failure_is_fed_back_then_gives_up():
    a, _ = loop([C("finish", summary="처리했습니다"), C("finish", summary="처리했습니다")],
                verifier=lambda t, s: (False, "보낸편지함에 없음"))
    r = a.run("메일 보내줘")
    assert r.status == "verify_failed" and "보낸편지함에 없음" in r.summary and r.verify_failures == 2


def test_verifier_failure_then_success():
    verdicts = iter([(False, "장바구니에 K200 없음"), (True, "장바구니에 K200 있음")])
    prov = Scripted([C("finish", summary="담았습니다"), C("observe"), C("finish", summary="담았습니다")])
    a = AgentLoop(prov, FakeBrowser(CART), agent_registry(), POLICY, SITES, lambda r: True,
                  verifier=lambda t, s: next(verdicts))
    r = a.run("K200 담아줘")
    assert r.status == "completed" and r.verify_failures == 1
    fed_back = [m for m in prov.seen[1] if m.role == "tool" and m.tool_name == "finish"]
    assert "장바구니에 K200 없음" in fed_back[-1].content


def test_approved_action_adds_stop_hint():
    prov = Scripted([C("observe"), C("click", ref="e2"), C("finish", summary="ok")])
    a = AgentLoop(prov, FakeBrowser(CART), agent_registry(), POLICY, SITES, lambda r: True)
    a.run("결제해줘")
    last_tool = [m for m in prov.seen[2] if m.role == "tool"][-1]
    assert "다른 동작을 하지 말고" in last_tool.content


def test_approval_denied_stops_without_clicking():
    a, b = loop([C("observe"), C("click", ref="e2")], approver=lambda r: False)
    r = a.run("결제해줘")
    assert r.status == "approval_denied"
    assert clicks(b, "e2") == 0
    req, ok = r.approvals[0]
    assert not ok and req.element_label == "결제하기" and req.url == "http://shop.test/cart"


def test_approved_click_executes_after_fingerprint_check():
    a, b = loop([C("observe"), C("click", ref="e2"), C("finish", summary="결제 완료")])
    r = a.run("결제해줘")
    assert r.status == "completed"
    assert clicks(b, "e2") >= 1
    assert any(s.note == "승인 후 실행" for s in r.steps)


def test_page_changed_after_approval_is_not_executed():
    changed = COMPOSE.replace('"a@mail.test"', '"attacker@evil.example"')
    b = FakeBrowser(COMPOSE, after_observes={2: changed})  # 1: 모델용 관찰, 2: 승인 후 재관찰
    seen = []
    a, _ = loop([C("observe"), C("click", ref="e3")], browser=b, approver=lambda r: seen.append(r) or True)
    r = a.run("메일 보내줘")
    assert r.status == "approval_mismatch"
    assert clicks(b, "e3") == 0
    assert seen[0].form_values == {"받는 사람": "a@mail.test", "본문": "안녕"}


def test_observe_mode_does_not_click():
    a, b = loop([C("observe"), C("click", ref="e1"), C("finish", summary="계획만")], mode="observe")
    r = a.run("홈으로")
    assert r.status == "completed" and clicks(b, "e1") == 0


def test_loop_detection():
    a, _ = loop([C("scroll", direction="down")] * 5)
    assert a.run("x").status == "loop_detected"


def test_too_many_invalid_calls():
    a, _ = loop([C("navigate", url="http://evil.example/")] * 10)
    r = a.run("x")
    assert r.status == "too_many_failures"


def test_max_steps():
    calls = [C("observe"), C("extract_text")] * 10
    a, _ = loop(calls, limits=Limits(max_steps=4, max_repeats=10))
    assert a.run("x").status == "max_steps"


def test_ask_user_without_answer_stops():
    a, _ = loop([C("ask_user", question="누구에게 보낼까요?")])
    r = a.run("메일 보내줘")
    assert r.status == "needs_user" and r.question == "누구에게 보낼까요?"


def test_ask_user_with_answer_continues():
    a, _ = loop([C("ask_user", question="누구?"), C("finish", summary="ok")], user_answer=lambda q: "김팀장")
    assert a.run("x").status == "completed"


def test_compact_keeps_latest_tool_result_only():
    msgs = [Message(role="system", content="s"), Message(role="user", content="u")]
    for i in range(3):
        msgs.append(Message(role="assistant", tool_calls=[C("observe")]))
        msgs.append(Message(role="tool", tool_name="observe", content=f"첫 줄 {i}\n긴 내용 {i}"))
    out = _compact(msgs, keep_full=1)
    tools = [m.content for m in out if m.role == "tool"]
    assert tools[0] == "첫 줄 0 / 긴 내용 0 (이전 결과 요약)"
    assert tools[2] == "첫 줄 2\n긴 내용 2"
    long = Message(role="tool", tool_name="observe", content="[관찰 결과 — 머리말]\nURL: http://mail.test/inbox\n" + "가" * 900)
    s = _compact([long, long], keep_full=1)[0].content
    assert s.startswith("URL: http://mail.test/inbox") and "머리말" not in s and len(s) < 450


def test_escalation_replaces_ask_user_step():
    fast = Scripted([C("ask_user", question="몰라요")])
    slow = Scripted([C("observe"), C("finish", summary="찾음")])
    a = AgentLoop(fast, FakeBrowser(CART), agent_registry(), POLICY, SITES, lambda r: True, escalate_provider=slow)
    fast.calls += [C("ask_user", question="또 몰라요")]
    r = a.run("x")
    assert r.status == "completed" and r.summary == "찾음" and r.escalations == 2


def test_escalation_failure_falls_back_to_fast_choice():
    class Broken(Scripted):
        def generate(self, *a, **k):
            raise TimeoutError("timed out")
    fast = Scripted([C("ask_user", question="누구?")])
    a = AgentLoop(fast, FakeBrowser(CART), agent_registry(), POLICY, SITES, lambda r: True, escalate_provider=Broken([]))
    r = a.run("x")
    assert r.status == "needs_user" and r.escalations == 1


def test_approval_request_shows_empty_fields():
    page = ('URL: http://booking.test/room\n[e1] combobox "회의실" = "대회의실"\n[e2] textbox "날짜" = "2026-11-17"\n'
            '[e3] textbox "시간"\n[e4] button "예약 신청"')
    seen = []
    a, _ = loop([C("observe"), C("click", ref="e4")], page=page, approver=lambda r: seen.append(r) or False)
    a.run("예약해줘")
    assert seen[0].form_values == {"회의실": "대회의실", "날짜": "2026-11-17", "시간": ""}
    assert "시간: (비어 있음)" in seen[0].describe()


def test_approval_feedback_lets_agent_fix_and_retry():
    answers = iter(["'시간'이(가) 비어 있다", True])
    prov = Scripted([C("observe"), C("click", ref="e2"), C("click", ref="e2"), C("finish", summary="ok")])
    b = FakeBrowser(CART)
    a = AgentLoop(prov, b, agent_registry(), POLICY, SITES, lambda r: next(answers))
    r = a.run("결제해줘")
    assert r.status == "completed" and [ok for _, ok in r.approvals] == [False, True]
    assert clicks(b, "e2") == 1  # 첫 시도는 실행되지 않음
    fb = [m for m in prov.seen[2] if m.role == "tool"][-1]
    assert "비어 있다" in fb.content and "실행되지 않았다" in fb.content


def test_approval_feedback_is_limited():
    a, b = loop([C("observe")] + [C("click", ref="e2"), C("observe")] * 5, approver=lambda r: "틀렸다",
                limits=Limits(max_repeats=10))
    r = a.run("결제해줘")
    assert r.status == "approval_denied" and clicks(b, "e2") == 0
    assert len(r.approvals) == 3


def test_screenshots_saved_per_step_and_attached_to_approval(tmp_path):
    import base64, os, stat

    class ShotBrowser(FakeBrowser):
        def request(self, op, args=None):
            if op == "screenshot":
                self.ops.append((op, {}))
                return {"jpeg_b64": base64.b64encode(b"\xff\xd8fake").decode()}
            return super().request(op, args)

    seen = []
    prov = Scripted([C("navigate", url="http://shop.test/"), C("click", ref="e2"), C("finish", summary="ok")])
    b = ShotBrowser(CART)
    a = AgentLoop(prov, b, agent_registry(), POLICY, SITES, lambda r: seen.append(r) or True,
                  screenshot_dir=str(tmp_path / "shots"))
    r = a.run("결제해줘")
    files = sorted(p.name for p in (tmp_path / "shots").iterdir())
    assert files == ["step-01-navigate.jpg", "step-02-approval.jpg", "step-02-click.jpg", "step-03-finish.jpg"]
    assert seen[0].screenshot_path.endswith("step-02-approval.jpg")
    assert stat.S_IMODE(os.stat(tmp_path / "shots" / files[0]).st_mode) == 0o600
    assert r.steps[0].screenshot.endswith("step-01-navigate.jpg")
    # 모델에게 간 메시지에는 이미지 데이터가 없다
    assert all("jpeg_b64" not in m.content and "/9j/" not in m.content for msgs in prov.seen for m in msgs)


def test_screenshot_failure_does_not_break_run(tmp_path):
    a, _ = loop([C("navigate", url="http://shop.test/"), C("finish", summary="ok")], screenshot_dir=str(tmp_path / "s"))
    assert a.run("x").status == "completed"  # FakeBrowser는 screenshot 응답에 jpeg_b64가 없다


def test_plan_is_appended_and_ask_about_ref_is_rejected():
    from agent.core.plan import format_plan, make_plan
    from agent.tools.validate import validate_call
    import json as _json

    class P(Scripted):
        def generate(self, messages, **kw):
            self.seen.append(messages)
            return ModelResponse(text=_json.dumps({"steps": ["받은편지함을 연다", " 박대리 메일을   찾는다 ", ""]}),
                                 latency_ms=1, model="s", provider="s")
    steps = make_plan(P([]), "자료 왔는지 확인", SITES, "[기억] ...")
    assert steps == ["받은편지함을 연다", "박대리 메일을 찾는다"]
    plan = format_plan(steps)
    prov = Scripted([C("finish", summary="ok")])
    AgentLoop(prov, FakeBrowser(CART), agent_registry(), POLICY, SITES, lambda r: True).run("t", context="[기억] x", plan=plan)
    user = prov.seen[0][1].content
    assert user.startswith("t\n\n[기억] x\n\n[계획") and "2. 박대리 메일을 찾는다" in user
    assert format_plan([]) is None

    class Bad(Scripted):
        def generate(self, messages, **kw):
            return ModelResponse(text="nope", latency_ms=1, model="s", provider="s")
    assert make_plan(Bad([]), "t", SITES) == []
    c = validate_call(C("ask_user", question="'메일 쓰기' 버튼의 ref는 무엇인가요?"), agent_registry(), POLICY, None)
    assert c.status == "policy_denied"
    assert validate_call(C("ask_user", question="누구에게 보낼까요?"), agent_registry(), POLICY, None).status == "ok"


def test_alternating_loop_is_detected_and_visit_note_added():
    from agent.core.loop import _cycle
    A, B, C_ = ("a",), ("b",), ("c",)
    assert _cycle([A, A, A], 3, 3) == 1 and _cycle([A, B, A, B, A, B], 3, 3) == 2
    assert _cycle([A, B, C_] * 3, 3, 3) == 3 and _cycle([A, B, A, B, A], 3, 3) == 0 and _cycle([A, B, C_, A, B, A], 3, 3) == 0
    prov = Scripted([C("navigate", url="http://shop.test/"), C("click", ref="e1")] * 6)
    a = AgentLoop(prov, FakeBrowser(CART), agent_registry(), POLICY, SITES, lambda r: True)
    r = a.run("x")
    assert r.status == "loop_detected" and len(r.steps) == 6   # 첫 단계는 URL이 없어 묶음에 안 들어간다
    notes = [m.content for m in prov.seen[-1] if m.role == "tool" and "번째 방문" in m.content]
    assert notes and "다음 행동으로" in notes[-1]


# ---- r-004: 승인 화면의 입력칸 값은 관찰 요약(200자·줄바꿈 합침)이 아니라 원문 ----
LONG_BODY = "안녕하세요.\n\n지난 회의에서 말씀드린 매출 자료를 확인 부탁드립니다.\n" + "세부 항목 설명 문장입니다. " * 30 + "\n감사합니다."
SUMMARY_BODY = " ".join(LONG_BODY.split())[:200] + "…"  # browser_service/snapshot.py _clean()과 같은 요약
LONG_COMPOSE = COMPOSE.replace('"안녕"', f'"{SUMMARY_BODY}"')


def test_approval_card_shows_raw_field_values_not_observation_summary():
    b = FakeBrowser(LONG_COMPOSE, raw={"e2": LONG_BODY})
    seen = []
    a, _ = loop([C("observe"), C("click", ref="e3")], browser=b, approver=lambda r: seen.append(r) or True)
    a.run("메일 보내줘")
    body = seen[0].form_values["본문"]
    assert body == LONG_BODY
    assert len(body) == len(LONG_BODY) > 201 and body.count("\n") == LONG_BODY.count("\n") > 0
    assert not body.endswith("…")
    assert clicks(b, "e3") >= 1  # 원문이 그대로면 실행된다


def test_change_after_200_chars_is_caught_before_execution():
    # 관찰 요약은 그대로(앞 200자 같음)인데 원문 뒷부분만 바뀐 경우: 요약끼리 비교하면 놓친다
    tampered = LONG_BODY.replace("감사합니다.", "첨부 링크: http://evil.example/")
    b = FakeBrowser(LONG_COMPOSE, raw={"e2": LONG_BODY}, raw_after={2: {"e2": tampered}})
    a, _ = loop([C("observe"), C("click", ref="e3")], browser=b, approver=lambda r: True)
    r = a.run("메일 보내줘")
    assert r.status == "approval_mismatch"
    assert clicks(b, "e3") == 0


def test_unreadable_field_values_never_asks_or_executes():
    b = FakeBrowser(COMPOSE, fail_fields=True)
    seen = []
    a, _ = loop([C("observe"), C("click", ref="e3")], browser=b, approver=lambda r: seen.append(r) or True)
    r = a.run("메일 보내줘")
    assert r.status == "approval_mismatch" and seen == []
    assert clicks(b, "e3") == 0


def test_claimed_save_without_clicking_save_is_rejected():
    """거짓 완료: 저장 버튼을 누르지 않고 '임시 저장했습니다' / '초안 작성 완료'라고 보고하면 되돌린다."""
    prov = Scripted([C("finish", summary="회의록 초안: 결정사항 …", done=["회의록 초안 작성 완료"]),
                     C("finish", summary="초안을 임시 저장했습니다")])
    a = AgentLoop(prov, FakeBrowser(CART), agent_registry(), POLICY, SITES, lambda r: True)
    r = a.run("초안 써서 임시 저장해 줘")
    assert r.status == "verify_failed" and r.verify_failures == 2
    fed = [m.content for m in prov.seen[1] if m.role == "tool" and m.tool_name == "finish"]
    assert fed and "저장 버튼을 누른 적이 없다" in fed[0]


def test_reading_saved_drafts_is_not_a_claim():
    a, _ = loop([C("observe"), C("finish", summary="임시 저장된 초안은 2개입니다")])
    assert a.run("초안 몇 개야?").status == "completed"


def test_claim_after_real_save_click_is_accepted():
    page = 'URL: http://mail.test/compose\n제목: 메일 쓰기\n[e1] textbox "본문" = "안녕"\n[e2] button "임시 저장"'
    a, b = loop([C("observe"), C("click", ref="e2"), C("finish", summary="초안을 임시 저장했습니다")], page=page)
    assert a.run("초안 저장").status == "completed" and clicks(b, "e2") >= 1


class TypingBrowser(FakeBrowser):
    """type 하면 입력칸 원문이 바뀌는 가짜 브라우저."""
    def request(self, op, args=None):
        if op == "type":
            self.raw[args["ref"]] = args["text"]
        return super().request(op, args)


def test_empty_body_is_sent_back_before_asking_approval():
    """실사용 측정 r-005: 본문 없이 '임시 저장'을 눌러 빈 초안이 저장됐다 → 승인 요청 전에 되돌린다."""
    b = TypingBrowser(COMPOSE.replace('"안녕"', '""'), raw={"e2": ""})
    seen = []
    a, _ = loop([C("observe"), C("click", ref="e3"), C("type", ref="e2", text="회의록 본문"), C("click", ref="e3"),
                 C("finish", summary="임시 저장했습니다")], browser=b, approver=lambda r: seen.append(r) or True)
    r = a.run("회의록 초안 저장해 줘")
    assert [s.result_status for s in r.steps if s.tool == "click"][0] == "empty_field"
    assert len(seen) == 1 and seen[0].form_values["본문"] == "회의록 본문"
    assert r.status == "completed"


def test_empty_body_goes_to_approval_after_repeated_nudges():
    """고집하면 결국 승인 화면에 빈 본문을 그대로 보여 준다(사람이 판단)."""
    seen = []
    a, b = loop([C("observe"), C("click", ref="e3"), C("observe"), C("click", ref="e3"), C("observe"),
                 C("click", ref="e3")],
                browser=FakeBrowser(COMPOSE.replace('"안녕"', '""'), raw={"e2": ""}),
                approver=lambda r: seen.append(r) or False)
    a.run("저장해 줘")
    assert len(seen) == 1 and seen[0].form_values["본문"] == ""


def test_claim_wording_from_phase7_w1_is_caught():
    """Phase 7 재측정 w1: 저장 버튼 없이 '메일을 작성했으며 … 초안은 완료되었습니다'로 끝냄."""
    s = "매출 자료 요청 메일을 작성했으며, 아직 해당 자료가 도착하지 않은 것으로 확인되었습니다. 확인 요청 메일 초안은 완료되었습니다."
    a, _ = loop([C("observe"), C("finish", summary=s), C("finish", summary=s)])
    assert a.run("자료 안 왔으면 확인 메일 초안 저장해 줘").status == "verify_failed"
    for ok in ("박대리가 보낸 메일이 도착했습니다", "김팀장이 작성한 메일 제목은 '견적'입니다"):
        a, _ = loop([C("observe"), C("finish", summary=ok)])
        assert a.run("메일 확인해 줘").status == "completed"


def test_undecided_note_only_when_task_has_undecided_items():
    """r-005 대응 주의문은 미정 표시가 있을 때만 붙는다(모든 작업에 넣으면 Phase 7이 4/6으로 떨어졌다)."""
    prov = Scripted([C("finish", summary="확인")])
    AgentLoop(prov, FakeBrowser(CART), agent_registry(), POLICY, SITES, lambda r: True).run(
        "회의록 초안", context="고객 공지는 담당자 미정")
    assert "미정 사항" in prov.seen[0][1].content
    prov = Scripted([C("finish", summary="확인")])
    AgentLoop(prov, FakeBrowser(CART), agent_registry(), POLICY, SITES, lambda r: True).run("매출 자료 왔는지 확인")
    assert "미정 사항" not in prov.seen[0][1].content and "미정 사항" not in prov.seen[0][0].content
    prov = Scripted([C("finish", summary="확인")])
    AgentLoop(prov, FakeBrowser(CART), agent_registry(), POLICY, SITES, lambda r: True).run(
        "자료 요청 초안 저장해 줘. 받는 사람은 아직 안 정했어")
    assert "미정 사항" not in prov.seen[0][1].content


def test_fields_without_a_name_get_one_and_two_fields_never_share_a_name_on_the_approval_card():
    """실기기: 이름표 없는 날짜 칸이 승인 카드에 "2026-11-16 2026-11-16"(이름 자리에 값)으로, 이름표 없는 빈 칸이 이름 없이 "(비어 있음)"으로 나왔다.
    이름이 같은 칸이 둘이면 뒤의 값이 앞의 값을 덮어 승인 내용(과 해시)에서 사라진다."""
    from agent.core.approval import FormValues, form_fields
    from agent.tools.observation import Observation
    obs = Observation.parse("URL: https://shop.example/new\n"
                            '[e1] textbox "테스트항목" = "테스트항목"\n'      # 이름표 없는 칸: 관찰 결과는 값을 이름 자리에 쓴다
                            '[e2] textbox "2026-11-16" = "2026-11-16"\n'
                            '[e3] textbox ""\n[e4] textbox ""\n'
                            '[e5] textbox "담당자" = "담당자"\n'              # 이름표가 있는 칸에 이름과 같은 글을 넣었다
                            '[e6] textbox "메모"\n[e7] textbox "메모" = "둘째"\n[e8] button "저장"')
    raw = FormValues({"e1": "테스트항목", "e2": "2026-11-16", "e3": "", "e4": "", "e5": "담당자", "e6": "", "e7": "둘째"})
    raw.names = {"e1": "제목", "e2": "날짜", "e5": "담당자"}
    assert form_fields(obs, raw) == {"제목": "테스트항목", "날짜": "2026-11-16", "입력 칸": "", "입력 칸 (2)": "", "담당자": "담당자", "메모": "", "메모 (2)": "둘째"}
    assert form_fields(obs, dict(raw)) == {"입력 칸": "테스트항목", "입력 칸 (2)": "2026-11-16", "입력 칸 (3)": "", "입력 칸 (4)": "", "입력 칸 (5)": "담당자",
                                           "메모": "", "메모 (2)": "둘째"}       # 브라우저가 이름을 주지 않아도 값은 하나도 사라지지 않는다


# ---- 복잡한 화면용 도구와 브라우저 확인 창 (D72, 2026-11-15 합성 플래너 측정에서 나온 것) ----
PLANNER = ('URL: http://shop.test/board\n[e1] textbox "태그 입력 후 Enter"\n[e2] button "완료된 할 일 보관"\n[e3] searchbox "검색"\n'
           '[e4] button "저장"\n[e5] heading "완료 7"')


class DialogBrowser(FakeBrowser):
    """'완료된 할 일 보관'(e2)을 누르면 브라우저 확인 창이 뜬다: 승인된 누르기(accept_dialog)가 아니면 취소로 닫힌다."""

    def request(self, op, args=None):
        out = super().request(op, args)
        if op == "click" and (args or {}).get("ref") == "e2":
            return {**out, "dialog": {"type": "confirm", "message": "완료된 할 일 7개를 보관할까요?", "accepted": (args or {}).get("accept_dialog") is True}}
        return out


def test_a_click_that_raised_a_browser_confirm_needs_approval_the_next_time_and_is_then_confirmed():
    """이름으로는 알 수 없는 바꾸는 동작("보관")을, 사이트가 스스로 띄우는 확인 창으로 잡는다.
    처음: 창이 취소로 닫히고 모델에 알린다. 다시 누르면: 사용자에게 승인을 묻고(창의 글을 보여 준다), 승인하면 그 창을 '확인'으로 닫는다."""
    seen = []
    prov = Scripted([C("observe"), C("click", ref="e2"), C("click", ref="e2"), C("finish", summary="보관했어요")])
    b = DialogBrowser(PLANNER)
    a = AgentLoop(prov, b, agent_registry(rich=True), POLICY, SITES, lambda r: seen.append(r) or True)
    r = a.run("완료된 할 일을 보관해줘")
    assert r.status == "completed"
    first = [m for m in prov.seen[2] if m.role == "tool"][-1].content
    assert "[브라우저 확인 창]" in first and "'취소'로 닫았다" in first and "아무것도 바뀌지 않았다" in first
    assert len(seen) == 1 and seen[0].element_label == "완료된 할 일 보관" and seen[0].form_values["브라우저 확인 창"] == "완료된 할 일 7개를 보관할까요?"
    e2 = [args for op, args in b.ops if op == "click" and args.get("ref") == "e2"]
    assert e2 == [{"ref": "e2"}, {"ref": "e2", "accept_dialog": True}]            # 승인 전에는 취소, 승인 뒤에만 '확인'
    # 거절하면 다시 누르지 않는다
    prov = Scripted([C("observe"), C("click", ref="e2"), C("click", ref="e2"), C("finish", summary="x")])
    b = DialogBrowser(PLANNER)
    r = AgentLoop(prov, b, agent_registry(rich=True), POLICY, SITES, lambda r: False).run("보관해줘")
    assert r.status == "approval_denied" and clicks(b, "e2") == 1


def test_enter_in_a_field_asks_for_approval_only_where_approvals_exist_and_other_keys_are_free():
    from dataclasses import replace as _replace
    seen = []
    with_enter = _replace(POLICY, enter_approval=True)
    prov = Scripted([C("observe"), C("type", ref="e1", text="긴급"), C("press_key", ref="e1", key="Enter"), C("press_key", ref="e1", key="Escape"),
                     C("press_key", ref="e3", key="Enter"), C("finish", summary="ok")])
    b = FakeBrowser(PLANNER, raw={"e1": "긴급"})
    r = AgentLoop(prov, b, agent_registry(rich=True), with_enter, SITES, lambda r: seen.append(r) or True).run("태그 달아줘")
    assert r.status == "completed"
    assert [x.element_label for x in seen] == ["Enter 누르기 (태그 입력 후 Enter)"] and seen[0].form_values["태그 입력 후 Enter"] == "긴급"   # 넣은 값이 승인 화면에 보인다
    keys = [args for op, args in b.ops if op == "press_key"]
    assert keys == [{"ref": "e1", "key": "Enter", "accept_dialog": True}, {"ref": "e1", "key": "Escape"}, {"ref": "e3", "key": "Enter"}]   # Escape·검색창의 Enter는 묻지 않는다
    # 승인을 물을 수 없는 길(기본 정책): 검색창이 아닌 칸의 Enter는 하지 않는다
    prov = Scripted([C("observe"), C("press_key", ref="e1", key="Enter"), C("finish", summary="못 했어요")])
    b = FakeBrowser(PLANNER)
    AgentLoop(prov, b, agent_registry(rich=True), POLICY, SITES, lambda r: True).run("태그 달아줘")
    assert [op for op, _ in b.ops if op == "press_key"] == []
    assert "press_key" not in agent_registry().names() and "drag" in agent_registry(rich=True).names()   # 로컬 모델의 길에는 주지 않는다


def test_hover_and_drag_reach_the_browser_and_inner_scroll_is_reported():
    class B(FakeBrowser):
        def request(self, op, args=None):
            out = super().request(op, args)
            return {**out, "scrolled": "inner", "at_bottom": False, "scroll_y": 0, "page_height": 600} if op == "scroll" else out
    prov = Scripted([C("observe"), C("hover", ref="e2"), C("drag", text="회의실 예약 확인", to_ref="e5"), C("scroll", direction="down"), C("finish", summary="ok")])
    b = B(PLANNER)
    r = AgentLoop(prov, b, agent_registry(rich=True), POLICY, SITES, lambda r: True).run("옮겨줘")
    assert r.status == "completed"
    assert ("hover", {"ref": "e2"}) in b.ops and ("drag", {"text": "회의실 예약 확인", "to_ref": "e5"}) in b.ops
    assert "화면 안쪽 목록을 스크롤했다" in [m for m in prov.seen[-1] if m.role == "tool"][-1].content


def test_a_word_inside_content_text_does_not_ask_for_approval_but_a_short_action_name_does():
    """합성 측정: 할 일 제목 "명함 주문"의 칸을 누르는데 '주문' 때문에 승인을 물었다. 표의 칸·머리글·문단은 내용이다 —
    다만 글이 짧아 동작 이름 그 자체로 보이는 것("삭제")과 단추·링크·메뉴는 그대로 묻는다."""
    page = ('URL: http://shop.test/board\n[e1] cell "명함 주문"\n[e2] cell "삭제"\n[e3] button "명함 주문"\n[e4] heading "결제 모듈 삭제 일정 정리"\n'
            '[e5] menuitem "삭제"\n[e6] link "주문 내역"')
    asked = []
    prov = Scripted([C("observe"), C("click", ref="e1"), C("click", ref="e4"), C("click", ref="e2"), C("click", ref="e3"), C("click", ref="e5"), C("click", ref="e6"),
                     C("finish", summary="ok")])
    b = FakeBrowser(page)
    AgentLoop(prov, b, agent_registry(), POLICY, SITES, lambda r: asked.append(r.element_label) or True).run("눌러 봐")
    assert asked == ["삭제", "명함 주문", "삭제", "주문 내역"]          # 칸·머리글의 내용 글(e1·e4)은 묻지 않는다. 칸의 글이 "삭제" 그 자체면(e2) 묻는다
    assert clicks(b, "e1") == 1 and clicks(b, "e4") == 1


REVIEW = ('URL: http://shop.test/popup/reviews/1/update?x=1\n제목: 리뷰 수정\n[e1] textbox "리뷰 내용" = "좋아요"\n'
          '[e2] button "수정"\n[e3] button "닫기"\n[e4] searchbox "검색"\n[e5] button "사진 고르기"\n[e6] button ""')


def test_a_confirming_button_pressed_after_typing_on_that_page_needs_approval():
    """실기기 2026-11-16: 리뷰 수정 창의 저장 단추("수정")가 정해 둔 낱말에 없어 승인 카드 없이 눌렸다.
    칸에 무엇을 넣은 화면에서 누르는 "수정"·"확인"·"완료" 같은 단추와 이름 없는 단추는 승인을 묻는다(닫기·취소, 검색창에 넣은 것은 빼고)."""
    from dataclasses import replace as _replace
    pol = _replace(POLICY, input_approval=True)
    seen = []
    calls = lambda: [C("observe"), C("click", ref="e3"), C("type", ref="e4", text="스티커"), C("click", ref="e2"),   # noqa: E731
                     C("type", ref="e1", text="좋아요ㅎ"), C("click", ref="e3"), C("click", ref="e5"), C("click", ref="e2"), C("finish", summary="고쳤어요")]
    b = FakeBrowser(REVIEW, raw={"e1": "좋아요ㅎ"})
    r = AgentLoop(Scripted(calls()), b, agent_registry(rich=True), pol, SITES, lambda q: seen.append(q) or True).run("리뷰 끝에 ㅎ 붙여줘")
    assert r.status == "completed"
    assert [q.element_label for q in seen] == ["수정"] and seen[0].form_values["리뷰 내용"] == "좋아요ㅎ"   # 입력 전·검색창 입력 뒤·닫기·다른 이름의 단추는 묻지 않는다
    assert clicks(b, "e2") == 2 and clicks(b, "e3") == 2
    b = FakeBrowser(REVIEW, raw={"e1": "좋아요ㅎ"})                                    # 거절하면 누르지 않는다
    r = AgentLoop(Scripted(calls()), b, agent_registry(rich=True), pol, SITES, lambda q: False).run("리뷰 끝에 ㅎ 붙여줘")
    assert r.status == "approval_denied" and clicks(b, "e2") == 1
    b = FakeBrowser(REVIEW, raw={"e1": "좋아요ㅎ"})                                    # 이 규칙이 없는 길(기본 정책)은 그대로
    AgentLoop(Scripted(calls()), b, agent_registry(rich=True), POLICY, SITES, lambda q: seen.append("x") or True).run("리뷰")
    assert "x" not in seen


class WanderingBrowser(FakeBrowser):
    """e1을 누르면 가지 않기로 한 호스트의 화면으로 넘어간다."""
    HOME = 'URL: http://shop.test/my/reviews\n제목: 내 리뷰\n[e1] link "상품 보기"\n[e2] link "내 쇼핑"'
    AWAY = 'URL: http://store.shop.test/products/1\n제목: 상품\n[e1] button "구매"'

    def request(self, op, args=None):
        if op == "click" and (args or {}).get("ref") == "e1" and self.page == self.HOME:
            self.page = self.AWAY
        if op == "navigate":
            self.page = self.HOME
        return super().request(op, args)


def test_a_click_that_lands_on_a_host_we_do_not_open_is_undone():
    """실기기 2026-11-16: 주소로는 못 가게 막아 둔 호스트에 링크를 눌러 들어갔다. 넘어가면 되돌아오고 모델에 알린다."""
    from dataclasses import replace as _replace
    pol = _replace(POLICY, denied_hosts=("store.shop.test",))
    prov = Scripted([C("observe"), C("click", ref="e1"), C("finish", summary="상품 화면은 볼 수 없었어요")])
    b = WanderingBrowser(WanderingBrowser.HOME)
    r = AgentLoop(prov, b, agent_registry(rich=True), pol, SITES, lambda q: True).run("리뷰 보여줘")
    assert r.status == "completed" and b.page == WanderingBrowser.HOME
    assert ("navigate", {"url": "http://shop.test/my/reviews"}) in b.ops
    told = [m for m in prov.seen[2] if m.role == "tool"][-1].content
    assert "열지 않는 곳" in told and "돌아왔다" in told


def test_urls_inside_a_closed_host_can_be_opened_only_by_listed_prefix():
    pol = Policy(allowed_hosts=frozenset({"naver.test"}), denied_hosts=("shopping.naver.test",),
                 denied_except=("https://shopping.naver.test/my/",))
    assert pol.url_allowed("https://pay.naver.test/x") and pol.url_allowed("https://shopping.naver.test/my/written-reviews")
    assert not pol.url_allowed("https://shopping.naver.test/search?q=1") and not pol.url_allowed("https://shopping.naver.test/my")
    assert not pol.url_allowed("http://shopping.naver.test/my/x") and not pol.url_allowed("https://shopping.naver.test.evil.test/my/")
    assert not pol.url_allowed("https://other.test/my/") and not pol.host_allowed("shopping.naver.test")


def test_a_nameless_button_after_typing_also_needs_approval():
    from dataclasses import replace as _replace
    seen = []
    b = FakeBrowser(REVIEW, raw={"e1": "좋아요ㅎ"})
    prov = Scripted([C("observe"), C("type", ref="e1", text="좋아요ㅎ"), C("click", ref="e6"), C("finish", summary="보냈어요")])
    AgentLoop(prov, b, agent_registry(rich=True), _replace(POLICY, input_approval=True), SITES, lambda q: seen.append(q) or True).run("보내줘")
    assert len(seen) == 1 and clicks(b, "e6") == 1


def test_putting_something_in_the_cart_asks_for_approval_inside_a_site():
    from dataclasses import replace as _replace
    from agent.sites.inside import SITE_SENSITIVE
    page = 'URL: http://shop.test/p/1\n제목: 상품\n[e1] button "장바구니 담기"\n[e2] link "장바구니"\n[e3] button "리뷰 더보기"'
    seen = []
    b = FakeBrowser(page)
    prov = Scripted([C("observe"), C("click", ref="e3"), C("click", ref="e2"), C("click", ref="e1"), C("finish", summary="담았어요")])
    AgentLoop(prov, b, agent_registry(rich=True), _replace(POLICY, sensitive_keywords=SITE_SENSITIVE), SITES, lambda q: seen.append(q) or True).run("담아줘")
    assert [q.element_label for q in seen] == ["장바구니 담기"]          # 장바구니 화면으로 가는 링크·더보기는 묻지 않는다


def test_words_that_also_name_menus_ask_for_approval_only_on_buttons():
    """실기기 2026-11-16: 유튜브의 "구독" 메뉴(링크)로 가려는데 승인을 물었다. 구독하기 **단추**만 묻는다."""
    from dataclasses import replace as _replace
    from agent.sites.inside import SITE_BUTTON_ONLY, SITE_SENSITIVE
    page = 'URL: http://shop.test/channel\n제목: 채널\n[e1] link "구독"\n[e2] button "구독"\n[e3] link "차단 목록"'
    seen = []
    b = FakeBrowser(page)
    prov = Scripted([C("observe"), C("click", ref="e1"), C("click", ref="e3"), C("click", ref="e2"), C("finish", summary="구독했어요")])
    pol = _replace(POLICY, sensitive_keywords=SITE_SENSITIVE, button_keywords=SITE_BUTTON_ONLY)
    AgentLoop(prov, b, agent_registry(rich=True), pol, SITES, lambda q: seen.append(q) or True).run("구독해줘")
    assert [q.element_label for q in seen] == ["구독"] and clicks(b, "e1") == 1 and clicks(b, "e2") == 1


def test_enter_in_a_text_box_named_search_needs_no_approval():
    """실기기 2026-11-16: 쿠팡의 상품 검색 칸이 textbox로 읽혀 Enter에 승인 카드가 떴다."""
    from dataclasses import replace as _replace
    seen = []
    page = 'URL: http://shop.test/\n제목: 상점\n[e1] textbox "쿠팡 상품 검색"\n[e2] textbox "받는 분 이름"'
    prov = Scripted([C("observe"), C("type", ref="e1", text="단백질"), C("press_key", ref="e1", key="Enter"), C("press_key", ref="e2", key="Enter"),
                     C("finish", summary="찾았어요")])
    AgentLoop(prov, FakeBrowser(page, raw={"e2": ""}), agent_registry(rich=True), _replace(POLICY, enter_approval=True), SITES,
              lambda q: seen.append(q) or True).run("찾아줘")
    assert [q.element_label for q in seen] == ["Enter 누르기 (받는 분 이름)"]


def test_human_check_pages_are_recognised_not_pressed():
    from agent.sites.inside import CHALLENGE, challenge_say
    for t in ("claude.ai\n보안 확인 수행 중\n이 웹사이트는 보안 서비스를 사용하여…\n사람인지 확인하십시오",
              "Just a moment...\nVerify you are human by completing the action below."):
        assert CHALLENGE.search(t)
    assert not CHALLENGE.search("보안 설정을 확인하세요 — 2단계 인증 켜기")          # 일반 화면의 '보안'·'확인'에는 걸리지 않는다
    assert "대신 누르지 않아요" in challenge_say("claude.ai")


def test_confirm_click_asks_approval_even_if_the_address_query_changed():
    # 실기기 2026-11-18(교육 사이트 '재응시'): 처음 누름 → 확인 창 취소, 화면이 같은데 주소의 물음표 뒤가 바뀌어
    # 다시 눌러도 승인을 묻지 않고 또 취소됐다. 같은 화면은 물음표·# 앞까지로 본다
    p1 = PLANNER.replace("http://shop.test/board", "http://shop.test/board?t=1")
    p2 = PLANNER.replace("http://shop.test/board", "http://shop.test/board?t=2")
    seen = []
    prov = Scripted([C("observe"), C("click", ref="e2"), C("observe"), C("click", ref="e2"), C("finish", summary="보관했어요")])
    b = DialogBrowser(p1, after_observes={2: p2})
    a = AgentLoop(prov, b, agent_registry(rich=True), POLICY, SITES, lambda r: seen.append(r) or True)
    r = a.run("완료된 할 일을 보관해줘")
    assert r.status == "completed" and len(seen) == 1
    e2 = [args for op, args in b.ops if op == "click" and args.get("ref") == "e2"]
    assert e2[-1] == {"ref": "e2", "accept_dialog": True}


def test_lead_in_answers_get_their_list(tmp_path=None):
    # 합성 c9(2026-11-18): "마감일은 다음과 같아요."로 끝나고 목록이 없는 답이 그대로 갔다
    from agent.core.loop import LEADS_IN
    assert LEADS_IN.search("완료되지 않은 할 일은 4개이고, 마감일은 다음과 같아요.")
    assert LEADS_IN.search("목록:")
    assert not LEADS_IN.search("할 일은 4개예요.\n- 보고서: 11월 19일")
