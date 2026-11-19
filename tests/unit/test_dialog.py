import json
import threading
import time
from types import SimpleNamespace as NS

import httpx
import pytest

from agent.api.dialog import DialogManager, decide
from agent.api.jobs import JobManager
from agent.api.server import make_server
from agent.providers.base import ModelProvider, ModelResponse, ProviderError

TOKEN = "t" * 32
H = {"X-App-Token": TOKEN}


def D(type, reply="", choices=(), title="", purpose="", targets=(), instructions=""):
    return json.dumps({"type": type, "reply": reply, "choices": list(choices), "title": title, "purpose": purpose,
                       "targets": list(targets), "instructions": instructions}, ensure_ascii=False)


class Scripted(ModelProvider):
    name = "scripted"

    def __init__(self, outs):
        self.outs, self.seen = list(outs), []

    def generate(self, messages, *, tools=None, json_schema=None, temperature=0.0, max_tokens=None):
        self.seen.append(messages)
        o = self.outs.pop(0)
        if isinstance(o, Exception):
            raise o
        return ModelResponse(text=o, latency_ms=1, model="s", provider="s")


def idle_runner(job, hooks):
    return NS(status="completed", summary="ok")


@pytest.fixture
def jobs():
    m = JobManager(idle_runner)
    yield m
    m.shutdown()


def test_none_answers_without_creating_task(jobs):
    dm = DialogManager(Scripted([D("none", reply="안녕하세요!")]), jobs)
    r = dm.handle("안녕")
    assert r["action"] == {"type": "none"} and r["replies"] == [{"text": "안녕하세요!", "by": "local"}]
    assert r["conversation_id"].startswith("c_") and jobs.list() == []


def test_task_creates_job_with_brief_and_confirmation_first(jobs):
    out = D("task", title="매출 자료 요청 메일 초안", purpose="자료 요청", targets=["test-b@mail.test"],
            instructions="test-b@mail.test에게 매출 자료를 요청하는 초안을 임시 저장")
    dm = DialogManager(Scripted([out]), jobs)
    r = dm.handle("김팀장에게 매출 자료 요청 초안 써줘", contacts={"김팀장": "test-b@mail.test"}, context="회의 발췌(합성)")
    assert r["replies"][0]["text"] == "네, 매출 자료 요청 메일 초안 준비할게요."
    t = r["action"]["task"]
    assert r["action"]["type"] == "task" and t["title"] == "매출 자료 요청 메일 초안"
    assert t["brief"] == {"purpose": "자료 요청", "targets": ["test-b@mail.test"],
                          "instructions": "test-b@mail.test에게 매출 자료를 요청하는 초안을 임시 저장"}
    job = jobs.get(t["id"])
    assert "[작업 브리프]" in job.task and "김팀장에게 매출 자료" in job.task
    assert "회의 발췌(합성)" not in str(t)          # 발췌는 응답에 되돌려 주지 않는다


def test_guessed_address_becomes_ask_and_no_job(jobs):
    out = D("task", title="메일 초안", purpose="p", targets=["choi@company.example"], instructions="choi@company.example에게 초안")
    dm = DialogManager(Scripted([out]), jobs)
    r = dm.handle("최대리에게 메일 초안 써줘", contacts={"김팀장": "test-b@mail.test", "이대리": "test-c@mail.test"})
    assert r["action"]["type"] == "ask" and r["action"]["choices"] == ["김팀장", "이대리"]
    assert jobs.list() == []


def test_address_typed_by_user_is_allowed():
    out = D("task", title="초안", purpose="p", targets=["test-a@mail.test"], instructions="test-a@mail.test에게")
    d = decide(Scripted([out]), "test-a@mail.test에게 초안 써줘")
    assert d.type == "task" and d.guard is None


def test_format_error_becomes_ask():
    d = decide(Scripted(["{not json"]), "아무거나")
    assert d.type == "ask" and d.guard == "format_error"


def test_route_claude_has_no_reply_and_no_job(jobs):
    dm = DialogManager(Scripted([D("route_claude", reply="결정은 …")]), jobs)
    r = dm.handle("지난 회의에서 뭐 결정됐지?")
    assert r == {"conversation_id": r["conversation_id"], "replies": [], "action": {"type": "route_claude"}}
    assert jobs.list() == []


def test_history_is_kept_per_conversation_and_unknown_id_resets(jobs):
    p = Scripted([D("ask", reply="누구에게 보낼까요?", choices=["김팀장", "이대리"]),
                  D("task", title="초안", purpose="p", targets=["test-b@mail.test"], instructions="i"),
                  D("none", reply="네")])
    dm = DialogManager(p, jobs)
    r1 = dm.handle("메일 초안 써줘", contacts={"김팀장": "test-b@mail.test", "이대리": "test-c@mail.test"})
    assert r1["action"] == {"type": "ask", "choices": ["김팀장", "이대리"]}
    r2 = dm.handle("김팀장", r1["conversation_id"])           # 연락처는 대화에 남아 있다
    assert r2["action"]["type"] == "task" and r2["conversation_id"] == r1["conversation_id"]
    texts = [m.content for m in p.seen[1]]
    assert "메일 초안 써줘" in texts and "누구에게 보낼까요?" in texts and "김팀장" in texts[-1]
    r3 = dm.handle("고마워", "c_unknownconversation")
    assert r3["conversation_reset"] is True and r3["conversation_id"] != "c_unknownconversation"


@pytest.fixture
def api(jobs):
    def serve(provider):
        srv = make_server(jobs, TOKEN, port=0, dialog=DialogManager(provider, jobs))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"
    servers = []
    yield serve
    for s in servers:
        s.shutdown()


def test_messages_endpoint_validation_and_model_error(api):
    base = api(Scripted([D("none", reply="네"), ProviderError("down")]))
    url = f"{base}/v1/messages"
    assert httpx.post(url, json={"text": "hi"}).status_code == 401
    assert httpx.post(url, headers=H, json={"text": ""}).json()["error"]["code"] == "invalid_request"
    bad = httpx.post(url, headers=H, json={"text": "hi", "contacts": {"김팀장": "not-an-email"}})
    assert bad.status_code == 400
    assert httpx.post(url, headers=H, json={"text": "hi", "conversation_id": "../x"}).status_code == 400
    ok = httpx.post(url, headers=H, json={"text": "안녕", "contacts": {"김팀장": "test-b@mail.test"}})
    assert ok.status_code == 200 and ok.json()["replies"] == [{"text": "네", "by": "local"}]
    err = httpx.post(url, headers=H, json={"text": "안녕"})
    assert err.status_code == 502 and err.json()["error"]["code"] == "model_error"


def test_compose_task_without_recipient_address_becomes_ask():
    out = D("task", title="팀장님께 보고 메일 초안", purpose="보고", targets=["팀장"], instructions="팀장님께 보고 메일 초안 작성")
    d = decide(Scripted([out]), "팀장님께 보고 메일 초안 써줘", contacts={"김팀장": "test-b@mail.test", "이대리": "test-c@mail.test"})
    assert d.type == "ask" and d.guard == "no_recipient" and d.choices == ["김팀장", "이대리"]


def test_read_only_mail_task_needs_no_address():
    out = D("task", title="새 메일 확인", purpose="받은편지함 확인", targets=["받은편지함"], instructions="새 메일이 있는지 본다")
    assert decide(Scripted([out]), "새 메일 왔어?").type == "task"


def test_hybrid_escalates_only_task_and_none_with_context():
    fast = Scripted([D("none", reply="안녕"), D("none", reply="출시는 12월"), D("task", title="t", purpose="p", targets=["x"], instructions="새 메일 확인")])
    slow = Scripted([D("route_claude"), D("ask", reply="무엇을요?")])
    assert decide(fast, "안녕", escalate=slow).escalated_from is None            # 발췌 없는 일상 대화: 빠른 판단 그대로
    d = decide(fast, "출시 언제야?", context="발췌", escalate=slow)
    assert d.type == "route_claude" and d.escalated_from == "none"
    d = decide(fast, "새 메일 봐줘", escalate=slow)
    assert d.type == "ask" and d.escalated_from == "task" and slow.outs == []


def test_reading_saved_drafts_is_not_a_compose_task():
    out = D("task", title="임시 보관함 확인", purpose="저장된 초안 개수 확인", targets=["임시 보관함"],
            instructions="임시 보관함에 저장된 초안이 몇 개인지 본다")
    assert decide(Scripted([out]), "임시 보관함에 저장된 초안 몇 개야?").type == "task"


def test_chinese_characters_in_reply_are_retried_then_replaced():
    fast = Scripted([D("none", reply="客气하세요!")])
    slow = Scripted([D("none", reply="천만에요!")])
    d = decide(fast, "고마워", escalate=slow)
    assert d.reply == "천만에요!" and d.escalated_from == "none"
    d = decide(Scripted([D("none", reply="不客气")]), "고마워")
    assert d.reply == "네, 알겠어요." and d.guard == "han_in_reply"


def test_promise_without_task_is_not_sent():
    """r-008: none인데 일을 약속하면 재판단, 그래도면 되묻기로."""
    fast = Scripted([D("none", reply="네, 회의록 초안을 작성해 드릴게요.")])
    slow = Scripted([D("none", reply="네, 바로 확인해 드리겠습니다.")])
    d = decide(fast, "해줄래?", escalate=slow)
    assert d.type == "ask" and d.guard == "promise_without_task"
    d = decide(Scripted([D("none", reply="푹 쉬세요, 응원할게요")]), "피곤해")
    assert d.type == "none" and d.guard is None


def test_capabilities_say_test_sites_only():
    from agent.api.dialog import DEFAULT_CAPABILITIES
    assert "테스트용 사이트 4개뿐" in DEFAULT_CAPABILITIES and "네이버" in DEFAULT_CAPABILITIES


def test_overclaim_about_external_service_is_replaced():
    """r-007: 지원하지 않는 서비스를 할 수 있다고 답하지 않는다."""
    d = decide(Scripted([D("none", reply="네이버 메일은 확인 가능합니다.")]), "네이버도 돼?")
    assert d.guard == "overclaim" and "지원하지 않아요" in d.reply
    d = decide(Scripted([D("none", reply="네이버 같은 실제 서비스는 아직 못 해요.")]), "네이버도 돼?")
    assert d.guard is None


def test_task_on_unsupported_site_is_not_created(jobs):
    out = D("task", title="네이버 메일 확인", purpose="네이버 메일 확인", targets=["네이버 메일"], instructions="네이버 받은편지함 확인")
    r = DialogManager(Scripted([out]), jobs).handle("네이버 메일 확인해줘")
    assert r["action"] == {"type": "none"} and "지원하지 않아요" in r["replies"][0]["text"] and jobs.list() == []


def test_no_promise_ever_leaves_without_task(jobs):
    """사용자 지시: 하겠다고 답할 때는 반드시 작업이 있어야 한다."""
    from agent.api.dialog import PROMISE
    outs = [D("none", reply="네, 정리해서 보내 드릴게요."), D("none", reply="작성해 드리겠습니다.")]
    r = DialogManager(Scripted(outs[:1]), jobs).handle("해줘")
    assert r["action"]["type"] != "none" or not PROMISE.search(r["replies"][0]["text"])
    assert not (r["action"]["type"] in ("none", "ask") and PROMISE.search(r["replies"][0]["text"]))


TASK_OUT = D("task", title="회의록 초안", purpose="p", targets=["test-b@mail.test"], instructions="test-b@mail.test에게 초안")


def test_same_client_message_id_never_creates_second_task(jobs):
    """실기기: 앱 요청이 끊긴 뒤 다시 보내 같은 작업이 2개 생겨 앞 작업이 승인 대기로 줄을 막았다."""
    prov = Scripted([TASK_OUT])           # 모델 응답은 한 번분만: 두 번째 판단이 일어나면 실패한다
    dm = DialogManager(prov, jobs)
    kw = dict(contacts={"김팀장": "test-b@mail.test"}, client_message_id="m-0001-abcd")
    a = dm.handle("김팀장에게 회의록 초안", **kw)
    b = dm.handle("김팀장에게 회의록 초안", conversation_id=a["conversation_id"], **kw)
    assert b["replayed"] is True and b["action"]["task"]["id"] == a["action"]["task"]["id"]
    assert len(jobs.list()) == 1 and len(prov.seen) == 1


def test_retry_while_first_is_still_deciding_waits_for_it(jobs):
    gate = threading.Event()

    class Slow(Scripted):
        def generate(self, *a, **k):
            gate.wait(5)
            return super().generate(*a, **k)

    dm = DialogManager(Slow([TASK_OUT]), jobs)
    kw = dict(contacts={"김팀장": "test-b@mail.test"}, client_message_id="m-0002-abcd")
    first = {}
    t = threading.Thread(target=lambda: first.update(dm.handle("김팀장에게 회의록 초안", **kw)))
    t.start()
    time.sleep(0.2)                       # 첫 요청이 판단 중일 때 앱이 끊고 다시 보냄
    threading.Timer(0.3, gate.set).start()
    again = dm.handle("김팀장에게 회의록 초안", **kw)
    t.join()
    assert again["action"]["task"]["id"] == first["action"]["task"]["id"] and len(jobs.list()) == 1


def test_failed_first_attempt_is_not_remembered(jobs):
    dm = DialogManager(Scripted([ProviderError("down"), D("none", reply="네")]), jobs)
    with pytest.raises(ProviderError):
        dm.handle("안녕", client_message_id="m-0003-abcd")
    assert dm.handle("안녕", client_message_id="m-0003-abcd")["replies"] == [{"text": "네", "by": "local"}]


def test_client_message_id_format_is_checked(api):
    base = api(Scripted([D("none", reply="네")]))
    assert httpx.post(f"{base}/v1/messages", headers=H, json={"text": "hi", "client_message_id": "../x"}).status_code == 400


def test_health_lists_client_message_id_only_with_dialog(api):
    base = api(Scripted([]))
    f = httpx.get(f"{base}/v1/health", headers=H).json()["features"]
    assert f[:3] == ["messages", "client_message_id", "tone"]


def test_tone_never_touches_the_decision_prompt(jobs):
    """말투는 판단이 끝난 뒤 답만 다시 쓴다. 판단 지시문은 말투와 상관없이 같다(지시문 한 줄이 브리프를 흔들었다, D48)."""
    prov = Scripted([D("none", reply="안녕하세요. 도와드릴까요?"), "안녕하십니까. 도와드릴까요?"])
    d = decide(prov, "안녕", tone="formal")
    plain = Scripted([D("none", reply="안녕하세요. 도와드릴까요?")])
    decide(plain, "안녕")
    assert prov.seen[0][0].content == plain.seen[0][0].content
    assert "합니다체" in prov.seen[1][-1].content and d.reply == "안녕하십니까. 도와드릴까요?"


def test_restyle_that_changes_meaning_is_dropped():
    prov = Scripted([D("none", reply="회의는 3시예요."), "회의는 4시입니다."])
    assert decide(prov, "회의 몇 시야?", tone="formal").reply == "회의는 3시예요."    # 숫자가 바뀌면 원래 문장
    prov = Scripted([D("ask", reply="누구에게 보낼까요?"), "누구에게 보내겠습니다."])
    assert decide(prov, "메일 써 줘", tone="formal").reply == "누구에게 보낼까요?"   # 질문이 사라지면 원래 문장


def test_realtime_rule_only_for_realtime_questions():
    prov = Scripted([D("none", reply="확인할 수 없어요."), D("none", reply="안녕하세요")])
    decide(prov, "오늘 서울 날씨 어때?")
    decide(prov, "안녕")
    assert "실시간 정보" in prov.seen[0][0].content and "실시간 정보" not in prov.seen[1][0].content


def test_formal_tone_applies_to_fixed_sentences(jobs):
    out = D("task", title="회의록 초안", purpose="p", targets=["test-b@mail.test"], instructions="test-b@mail.test에게 초안")
    dm = DialogManager(Scripted([out]), jobs)
    r = dm.handle("김팀장에게 회의록 초안", contacts={"김팀장": "test-b@mail.test"}, tone="formal")
    assert r["replies"][0]["text"] == "네, 회의록 초안 준비하겠습니다."
    d = decide(Scripted([D("none", reply="네이버 메일도 확인 가능합니다.")]), "네이버도 돼?", tone="formal")
    assert d.guard == "overclaim" and d.reply.endswith("지원하지 않습니다.")


def test_tone_validation_and_feature(api):
    base = api(Scripted([D("none", reply="네")]))
    assert "tone" in httpx.get(f"{base}/v1/health", headers=H).json()["features"]
    assert httpx.post(f"{base}/v1/messages", headers=H, json={"text": "hi", "tone": "cute"}).status_code == 400
    assert httpx.post(f"{base}/v1/messages", headers=H, json={"text": "hi", "tone": "formal"}).status_code == 200


def test_restyle_output_in_example_format_is_cleaned():
    prov = Scripted([D("none", reply="확인할 수 없어요."), "확인할 수 없어요. → 확인할 수 없습니다."])
    assert decide(prov, "날씨 어때?", tone="formal").reply == "확인할 수 없습니다."


def test_restyle_that_flips_negation_is_dropped():
    """측정에서 본 일: '구글 캘린더는 확인할 수 없어요'를 '확인할 수 있습니다'로 뒤집음."""
    orig = "구글 캘린더는 확인할 수 없어요. 테스트용 사이트만 확인할 수 있어요."
    prov = Scripted([D("none", reply=orig), "구글 캘린더는 확인할 수 있습니다. 테스트용 사이트만 확인할 수 있습니다."])
    assert decide(prov, "구글 캘린더 일정 확인돼?", tone="formal").reply == orig


MAIL_TEXT = "오늘 회의 내용 요약좀해서 나한테 메일로 보내줘"


def test_mail_request_routed_to_claude_asks_for_address_instead():
    # 실기기 r-009: 메일 부탁을 회의 질문으로 넘김 → 다시 판단하지 않고 받을 주소를 묻는다
    prov = Scripted([D("route_claude")])
    d = decide(prov, MAIL_TEXT, context="## 제품 회의 (가상)")
    assert d.type == "ask" and d.guard == "mail_request_routed" and "주소" in d.reply and len(prov.seen) == 1
    assert decide(Scripted([D("route_claude")]), MAIL_TEXT, tone="formal").reply.endswith("십시오.")
    # 작업 없이 약속만 한 경우도 같은 되물음 (일반 되물음 대신)
    d = decide(Scripted([D("none", reply="주소를 알려 주시면 보내 드릴게요.")]), MAIL_TEXT)
    assert d.type == "ask" and d.guard == "mail_request_routed"
    # 지원하지 않는 서비스가 대상이면 건드리지 않는다
    assert decide(Scripted([D("route_claude")]), "회의 요약해서 네이버 메일로 보내줘").type == "route_claude"


def test_answer_to_mail_question_is_not_routed_to_claude(jobs):
    # 되물음에 주소로 답했는데 회의 질문으로 넘기던 것(측정: '그 날 회의 없음' 머리 줄에서 3/3)
    no_meeting = "오늘 날짜: 11월 12일 (목)\n부탁이 가리키는 날: 오늘(11월 12일). 그 날 녹음한 회의는 없습니다. 아래는 다른 날 회의입니다.\n\n## 제품 회의(가상)"
    dm = DialogManager(Scripted([D("route_claude"), D("route_claude"), D("route_claude")]), jobs)
    r1 = dm.handle(MAIL_TEXT, context="## 제품 회의(가상)")
    assert r1["replies"][0]["text"] == "메일을 받을 주소를 알려 주세요."
    r2 = dm.handle("test-a@mail.test", r1["conversation_id"], context=no_meeting)
    assert r2["action"]["type"] == "ask" and "녹음한 회의가 없어요" in r2["replies"][0]["text"]
    r3 = dm.handle("음", r1["conversation_id"], context=no_meeting)      # 같은 안내를 되풀이하지 않는다
    assert r3["action"]["type"] == "ask" and "어떤 내용" in r3["replies"][0]["text"] and jobs.list() == []
    # 첫 메시지부터 '그 날 회의 없음'이면 주소보다 그 사실을 먼저 알린다
    d = decide(Scripted([D("route_claude")]), MAIL_TEXT, context=no_meeting, tone="formal")
    assert d.type == "ask" and d.reply.startswith("그 날 녹음한 회의가 없습니다.")
    # 되물음과 상관없는 대화에서 주소만 온 경우는 건드리지 않는다
    assert decide(Scripted([D("route_claude")]), "test-a@mail.test", history=[("user", "안녕"), ("assistant", "안녕하세요!")]).type == "route_claude"


def test_meeting_question_that_mentions_mail_is_still_routed():
    for q in ("회의에서 메일 보내기로 한 사람이 누구였지?", "메일 보내달라고 한 게 누구야?", "회의 요약해줘"):
        prov = Scripted([D("route_claude")])
        assert decide(prov, q).type == "route_claude" and len(prov.seen) == 1


def test_mail_request_decided_as_task_is_not_decided_again():
    out = D("task", title="회의 요약 메일 초안", purpose="p", targets=["test-a@mail.test"], instructions="test-a@mail.test에게 초안")
    prov = Scripted([out])
    d = decide(prov, "회의 요약해서 test-a@mail.test로 메일 보내줘")
    assert d.type == "task" and d.guard is None and len(prov.seen) == 1


def test_decision_log_has_no_content(jobs, capsys):
    dm = DialogManager(Scripted([D("none", reply="비밀스러운 답")]), jobs)
    dm.handle("비밀스러운 말", context="비밀 회의", tone="friendly")
    err = capsys.readouterr().err
    assert "[dialog]" in err and "action=none" in err and "tone=friendly" in err and "context=y" in err
    assert "비밀" not in err


def test_send_flag_tells_the_model_my_address_and_marks_the_job(jobs):
    # 발송을 켠 요청(v3)에서만: '나'의 주소가 연락처에 들어가고 능력 설명이 바뀐다. 작업은 send_enabled로 만들어진다
    mailer = NS(self_contacts=lambda: {"나(사용자 본인)": "test-me@mail.test"})
    out = D("task", title="회의 요약 메일", purpose="p", targets=["test-me@mail.test"], instructions="test-me@mail.test로 회의 요약을 보낸다")
    prov = Scripted([out, out])
    dm = DialogManager(prov, jobs, mailer=mailer)
    r = dm.handle("회의 요약해서 나한테 메일로 보내줘", send=True)
    assert r["action"]["type"] == "task" and jobs.get(r["action"]["task"]["id"]).send_enabled is True
    assert "본인 주소로만" in prov.seen[0][0].content and "나(사용자 본인): test-me@mail.test" in prov.seen[0][1].content
    assert "test-me@mail.test" not in str({k: v for k, v in r.items() if k != "action"})
    r2 = dm.handle("회의 요약해서 test-me@mail.test로 메일 보내줘")          # 발송을 켜지 않은 앱: 예전 그대로
    assert jobs.get(r2["action"]["task"]["id"]).send_enabled is False
    assert "본인 주소로만" not in prov.seen[1][0].content and "나(사용자 본인)" not in prov.seen[1][1].content


def test_send_flag_without_configured_address_changes_nothing(jobs):
    prov = Scripted([D("none", reply="네")])
    DialogManager(prov, jobs, mailer=NS(self_contacts=lambda: {})).handle("안녕", send=True)
    assert "본인 주소로만" not in prov.seen[0][0].content


def test_mail_to_self_routed_to_claude_becomes_a_task_in_send_mode():
    # 측정: 발송을 켜고 '나'의 주소를 줘도 14b는 "회의 요약 … 나한테 메일로 보내줘"를 회의 질문으로 넘겼다 → 코드가 작업을 만든다
    me = {"나(사용자 본인)": "test-me@mail.test", "나(네이버)": "test-me@mail.test", "나(지메일)": "test-me2@mail.test"}
    d = decide(Scripted([D("route_claude")]), MAIL_TEXT, me=me)                     # 발췌 없이 온 부탁: 모델이 판단하고, 넘기면 코드가 만든다
    assert d.type == "task" and d.guard == "mail_to_self" and d.brief.targets == ["test-me@mail.test"]
    assert MAIL_TEXT in d.brief.instructions and d.title == "회의 요약 메일"
    # 빠른 길(실기기 2026-11-12: 판단에 58.7초 → 앱이 응답을 못 받음): 발췌가 있는 분명한 부탁은 모델을 부르지 않는다
    never = Scripted([])
    d = decide(never, MAIL_TEXT, context="## 제품 회의(가상)", me=me)
    assert d.type == "task" and d.guard == "mail_to_self_fast" and d.brief.targets == ["test-me@mail.test"] and never.seen == []
    assert MAIL_TEXT in d.brief.instructions and d.latency_ms == 0
    # 다른 받는 사람이 같이 있거나, 본인 주소가 없으면(발송을 켜지 않은 요청) 빠른 길로 가지 않는다
    prov = Scripted([D("ask", reply="민수 님의 주소를 알려 주세요.")])
    assert decide(prov, "민수한테랑 나한테 회의 요약 메일 보내줘", context="## 제품 회의(가상)", me=me).type == "ask" and len(prov.seen) == 1
    prov = Scripted([D("ask", reply="메일을 받을 주소를 알려 주세요.")])
    assert decide(prov, MAIL_TEXT, context="## 제품 회의(가상)").guard != "mail_to_self_fast" and len(prov.seen) == 1
    assert decide(Scripted([D("ask", reply="받는 분 주소를 알려 주세요.")]), "박과장한테 일정 안내 메일 초안 남겨줘",
                  context="## 제품 회의(가상)", me=me).type == "ask"                      # "안내 메일"은 "내 메일"이 아니다
    d = decide(Scripted([D("route_claude")]), "회의록 정리해서 지메일로 나한테 보내줘", me=me)
    assert d.type == "task" and d.brief.targets == ["test-me2@mail.test"]          # 서비스 이름을 말하면 그 계정
    # 그 날 회의가 없다는 표시가 있으면 작업을 만들지 않고 먼저 알린다
    d = decide(Scripted([D("route_claude")]), MAIL_TEXT, me=me,
               context="부탁이 가리키는 날: 오늘. 그 날 녹음한 회의는 없습니다. 아래는 다른 날 회의입니다.\n\n## 회의")
    assert d.type == "ask" and "녹음한 회의가 없어요" in d.reply
    # 받는 사람이 '나'가 아니면 만들지 않는다 / 발송을 켜지 않았으면 예전처럼 주소를 묻는다
    assert decide(Scripted([D("route_claude")]), "회의 요약해서 메일로 보내줘", me=me).type == "ask"
    assert decide(Scripted([D("route_claude")]), MAIL_TEXT).reply == "메일을 받을 주소를 알려 주세요."


def test_send_mode_allows_real_service_only_for_my_own_address():
    me = {"나(사용자 본인)": "test-me@mail.test"}
    out = D("task", title="네이버로 회의 요약 보내기", purpose="p", targets=["test-me@mail.test"], instructions="네이버 메일로 test-me@mail.test에게 보낸다")
    assert decide(Scripted([out]), "네이버로 나한테 회의 요약 보내줘", me=me).type == "task"
    assert decide(Scripted([out]), "네이버로 나한테 회의 요약 보내줘").guard == "unknown_address"   # 발송을 켜지 않으면 모르는 주소
    other = D("task", title="네이버 메일 확인", purpose="p", targets=["네이버 받은편지함"], instructions="네이버 메일함을 확인한다")
    d = decide(Scripted([other]), "네이버 메일함 확인해줘", me=me)
    assert d.guard == "unsupported_site" and "보내기만" in d.reply


def test_mail_to_self_without_connected_account_fails_fast_for_connect_card(jobs):
    # D51: 계정이 없으면 모델을 부르지 않고 바로 send_failed/not_set 작업 → 앱이 대화창에 계정 연결 화면을 띄운다
    me = {}
    mailer = NS(self_contacts=lambda: dict(me))
    prov = Scripted([D("none", reply="네"), D("ask", reply="누구에게 보낼까요?")])
    dm = DialogManager(prov, jobs, mailer=mailer)
    r = dm.handle("오늘 회의 내용 요약좀해서 나한테 메일로 보내줘", context="## 회의(가상)", send=True)
    t = r["action"]["task"]
    assert r["action"]["type"] == "task" and t["status"] == "failed" and prov.seen == []
    assert t["result"]["outcome"] == "send_failed" and t["result"]["reason"] == "not_set" and t["result"]["report"]["sent"] == []
    assert r["replies"] == [{"text": "메일 계정을 먼저 연결해 주세요.", "by": "mac"}]
    # 발송을 켜지 않은 앱(이전 빌드)에는 이 실패 작업을 만들지 않는다
    r0 = dm.handle("안녕")
    assert r0["action"]["type"] == "none"
    # "메일 보내줘" → "누구에게?" → "나한테": 앞의 부탁과 이어서 본다
    cid = dm.handle("회의 요약 메일 좀 보내줘", send=True)["conversation_id"]
    r2 = dm.handle("나한테", cid, send=True)
    assert r2["action"]["task"]["result"]["reason"] == "not_set"
    # 네이버만 연결했는데 "지메일로": 그 서비스 계정이 없으니 연결 화면
    me.update({"나(사용자 본인)": "test-me@mail.test", "나(네이버)": "test-me@mail.test"})
    r3 = dm.handle("회의 요약 지메일로 나한테 메일 보내줘", send=True)
    assert r3["action"]["task"]["result"]["reason"] == "not_set"


def test_broken_or_failed_second_opinion_does_not_replace_a_valid_first_decision():
    # 실서버: 재판단이 형식이 깨진 답을 내 "이해하지 못했어요"가 멀쩡한 첫 판단(task)을 덮었다
    task = D("task", title="회의 요약 메일", purpose="p", targets=["test-a@mail.test"], instructions="test-a@mail.test에게 회의 요약 초안")
    d = decide(Scripted([task]), "test-a@mail.test에게 회의 요약 메일 초안 써줘", escalate=Scripted(["{깨진 JSON"]))
    assert d.type == "task" and d.guard is None and d.escalated_from == "failed"
    d = decide(Scripted([task]), "test-a@mail.test에게 회의 요약 메일 초안 써줘", escalate=Scripted([ProviderError("timeout")]))
    assert d.type == "task" and d.escalated_from == "failed"


def test_account_missing_exchange_is_not_kept_in_history(jobs):
    prov = Scripted([D("none", reply="네")])
    dm = DialogManager(prov, jobs, mailer=NS(self_contacts=lambda: {}))
    cid = dm.handle("회의 요약해서 나한테 메일로 보내줘", send=True)["conversation_id"]
    dm.handle("안녕", cid, send=True)
    assert [m.content for m in prov.seen[0][3:]] == ["안녕"]     # 실패한 부탁은 다음 판단의 기록에 없다
