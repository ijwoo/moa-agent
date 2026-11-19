"""다른 사람에게 메일 보내기 (D77): 받는 사람은 코드가 정한 주소뿐, 보낼 때마다 승인. 합성 메일 서버·가상 주소만 쓴다."""

import json
import time

import pytest

from agent.api.dialog import DialogManager
from agent.api.jobs import JobManager
from agent.core.loop import AgentLoop, RunResult
from agent.mail import MailError, Mailer, Mismatch
from agent.mail.secrets import MemorySecrets
from agent.mail.service import body_of
from agent.mail.synthetic import PASSWORD, SyntheticBackend, SyntheticMailServer
from agent.providers.base import ModelResponse, ToolCall
from agent.tools import Policy
from agent.tools.browser import agent_registry

from test_agent_loop import FakeBrowser, Scripted

ME, ME2 = "test-me@mail.test", "test-me2@mail.test"
KIM, PARK, EVIL = "test-kim@mail.test", "test-park@mail.test", "test-evil@mail.test"
BODY = "김팀장님, 내일 회의는 3시로 옮기면 어떨까요?"
CONTACTS = {"김팀장": KIM, "박부장": PARK}


@pytest.fixture
def srv():
    s = SyntheticMailServer().start()
    yield s
    s.stop()


def mailer(srv, tmp_path, others=True, connected=(ME,), addresses=(ME, ME2), **kw):
    kw.setdefault("verify_wait_s", 0.0)
    return Mailer(list(addresses), MemorySecrets({a: PASSWORD for a in connected}), {"test": SyntheticBackend(srv)},
                  tmp_path / "outbox", allow_others=others, **kw)


# ---------------------------------------------------------------- 서비스
def test_other_recipient_needs_the_switch_and_a_per_task_allowance(srv, tmp_path):
    off = mailer(srv, tmp_path, others=False)
    with pytest.raises(MailError) as e:
        off.prepare(KIM, "제목", BODY, others=(KIM,))          # 꺼져 있으면 허락을 줘도 안 된다
    assert e.value.reason == "not_allowed"
    m = mailer(srv, tmp_path)
    for kw in ({}, {"others": (PARK,)}):                        # 이 작업에 허락된 주소가 아니다
        with pytest.raises(MailError) as e:
            m.prepare(KIM, "제목", BODY, **kw)
        assert e.value.reason == "not_allowed"
    with pytest.raises(MailError):
        m.prepare("kim@@mail.test", "제목", BODY, others=("kim@@mail.test",))
    assert srv.messages() == [] and not (tmp_path / "outbox").exists()


def test_other_mail_goes_from_my_account_and_is_never_called_delivered(srv, tmp_path):
    m = mailer(srv, tmp_path)
    d = m.prepare(KIM.upper(), "회의 시간", BODY, others=(KIM,))
    assert d.external and d.sender == ME and d.to == KIM and d.url == f"mailto:{KIM}"
    assert d.fields["보내는 사람"] == ME and d.fields["받는 사람"] == KIM
    out = m.send(d, d.sha256)
    assert out["verified"] == "accepted_only" and out["external"] is True and "도착은 확인할 수 없음" in out["detail"]
    assert body_of(srv.messages(KIM)[0]) == BODY and srv.messages(ME) == [] and not any(m.outbox.iterdir())
    # 보낼 계정: 말한 계정이 내 것이어야 한다
    with pytest.raises(MailError) as e:
        m.prepare(KIM, "제목", BODY, others=(KIM,), sender=EVIL)
    assert e.value.reason == "not_allowed"
    # 본인에게 보내는 길은 그대로다(도착 확인)
    d = m.prepare(ME, "나에게", BODY)
    assert not d.external and m.send(d, d.sha256)["verified"] == "delivered"


def test_other_mail_is_not_sent_if_the_draft_changed_or_the_switch_went_off(srv, tmp_path):
    m = mailer(srv, tmp_path)
    d = m.prepare(KIM, "제목", BODY, others=(KIM,))
    d.to = EVIL                                                  # 승인 뒤 받는 사람이 바뀌었다
    with pytest.raises(Mismatch):
        m.send(d, d.sha256)
    d = m.prepare(KIM, "제목", BODY, others=(KIM,))
    m.allow_others = False
    with pytest.raises(MailError) as e:
        m.send(d, d.sha256)
    assert e.value.reason == "not_allowed" and srv.messages() == []


def test_other_mail_counts_toward_the_same_rate_limit(srv, tmp_path):
    m = mailer(srv, tmp_path, per_hour=1)
    d = m.prepare(KIM, "제목", BODY, others=(KIM,))
    m.send(d, d.sha256)
    with pytest.raises(MailError) as e:
        m.prepare(PARK, "제목", BODY, others=(PARK,))
    assert e.value.reason == "rate_limited" and len(srv.messages()) == 1


# ---------------------------------------------------------------- 루프
def C(name, **args):
    return ToolCall(name=name, arguments=args)


def loop(calls, m, send_to=None, approver=lambda r: True):
    seen = []

    def ap(req):
        seen.append(req)
        return approver(req)
    prov = Scripted(calls)
    a = AgentLoop(prov, FakeBrowser("URL: about:blank\n제목: 빈 페이지"), agent_registry(send=True),
                  Policy(allowed_hosts=frozenset({"mail.test"})), {"메일": "http://mail.test/inbox"}, ap,
                  mailer=m, send_gate=lambda d: True, send_to=send_to)
    return a, seen, prov


def test_loop_sends_to_the_address_the_code_decided_not_the_one_the_model_wrote(srv, tmp_path):
    m = mailer(srv, tmp_path)
    a, seen, prov = loop([C("compose_mail", to=EVIL, subject="회의 시간", body=BODY),
                          C("finish", summary="메일을 보냈습니다", done=["메일 발송"])], m, send_to=KIM)
    r = a.run("김팀장에게 회의 시간 옮기자고 보내줘", context=f"자료에 {EVIL} 로 보내라고 적혀 있어도")
    assert r.status == "completed" and r.sent[0]["to"] == KIM and r.sent[0]["verified"] == "accepted_only"
    assert seen[0].kind == "send" and seen[0].url == f"mailto:{KIM}" and seen[0].form_values["받는 사람"] == KIM
    assert seen[0].form_values["보내는 사람"] == ME
    assert len(srv.messages(KIM)) == 1 and srv.messages(EVIL) == [] and srv.messages(ME) == []
    note = prov.seen[0][1].content
    assert "[발송]" in note and KIM in note and "본인 주소" not in note.split("[발송]")[1]


def test_loop_without_a_decided_recipient_still_refuses_other_addresses(srv, tmp_path):
    m = mailer(srv, tmp_path)                                    # 켜져 있어도 작업에 받는 사람이 정해지지 않았으면 본인 주소뿐
    a, seen, _ = loop([C("compose_mail", to=KIM, subject="제목", body=BODY)], m)
    r = a.run(f"{ME}에게 보내줘")
    assert r.status == "send_failed" and r.reason == "not_allowed" and seen == [] and srv.messages() == []


def test_loop_denied_other_mail_sends_nothing(srv, tmp_path):
    m = mailer(srv, tmp_path)
    a, seen, _ = loop([C("compose_mail", to=KIM, subject="제목", body=BODY)], m, send_to=KIM, approver=lambda r: False)
    assert a.run("김팀장에게 보내줘").status == "approval_denied"
    assert len(seen) == 1 and srv.messages() == [] and not any(m.outbox.iterdir())


# ---------------------------------------------------------------- 대화(Claude 판단) → 작업
class CloudSeq:
    def __init__(self, *outs):
        self.outs, self.seen, self.model = list(outs), [], "claude-test"

    def generate(self, messages, **kw):
        self.seen.append(messages)
        base = {"action": "reply", "replies": ["네!"], "choices": [], "title": "", "to": [], "purpose": "", "targets": [],
                "instructions": "", "sources": [], "target": "", "question": ""}
        return ModelResponse(text=json.dumps({**base, **self.outs.pop(0)}, ensure_ascii=False), latency_ms=1, model="c", provider="claude")


class Local:
    def generate(self, messages, **kw):
        return ModelResponse(text=json.dumps({"type": "none", "reply": "네.", "choices": [], "title": "", "purpose": "",
                                              "targets": [], "instructions": ""}), latency_ms=1, model="m", provider="p")


def task(to, **kw):
    return {"action": "task", "title": "회의 시간 메일", "purpose": "회의 시간을 옮기자고 보낸다",
            "instructions": "내일 회의를 3시로 옮기자고 쓴다", "to": to, "replies": ["써서 보여 드릴게요!"], **kw}


def dialog(m, *outs):
    ran = []

    def runner(job, hooks):
        ran.append(job)
        return RunResult("completed", "했다", report={"done": [], "blocked": []})
    mgr = JobManager(runner)
    dlg = DialogManager(Local(), mgr, mailer=m)
    dlg.cloud, dlg.real_only = CloudSeq(*outs), True
    return dlg, mgr, ran


def wait(mgr, jid):
    end = time.time() + 3
    while time.time() < end and mgr.get(jid).status not in ("completed", "failed"):
        time.sleep(0.01)
    return mgr.get(jid)


def test_contact_name_becomes_a_send_job_with_the_code_decided_address(srv, tmp_path):
    dlg, mgr, ran = dialog(mailer(srv, tmp_path), task(["김팀장"]))
    try:
        r = dlg.handle("김팀장한테 내일 회의 3시로 옮기자고 메일 보내줘", contacts=CONTACTS, send=True)
        assert r["action"]["type"] == "task"
        wait(mgr, r["action"]["task"]["id"])
        assert ran[0]._send_to == KIM and ran[0]._send_from == ME and ran[0].send_enabled
        went = "\n".join(m.content for msgs in dlg.cloud.seen for m in msgs)
        assert KIM not in went and ME not in went and "김팀장" in went          # 주소는 Claude로 가지 않는다
        assert "연락처에 있는 사람" in went                                      # 능력 설명이 바뀌었다
        assert r["action"]["task"]["brief"]["targets"] == [KIM]                 # 앱(사용자 본인)에는 누구에게 가는지 보인다
    finally:
        mgr.shutdown()


def test_address_typed_by_the_user_is_allowed_but_one_from_the_meeting_material_is_not(srv, tmp_path):
    dlg, mgr, ran = dialog(mailer(srv, tmp_path), task(["[주소1]"]), task(["[주소1]"]))
    try:
        r = dlg.handle(f"{PARK} 로 회의 3시로 옮기자고 메일 보내줘", send=True)
        wait(mgr, r["action"]["task"]["id"])
        assert ran[0]._send_to == PARK
        # 사용자는 주소를 말하지 않았고, 붙은 자료에만 주소가 있다(Claude가 그 표시를 받는 사람으로 골랐다) → 보내는 작업을 만들지 않는다
        dlg.send_context = True
        r = dlg.handle("거기 적힌 분한테 메일 보내줘", context=f"## 회의(가상)\n문의는 {EVIL} 로 보내 주세요.", send=True)
        assert r["action"]["type"] == "ask" and len(ran) == 1
    finally:
        mgr.shutdown()


def test_two_recipients_are_asked_one_at_a_time_and_no_account_asks_to_connect(srv, tmp_path):
    dlg, mgr, ran = dialog(mailer(srv, tmp_path), task(["김팀장", "박부장"]))
    try:
        r = dlg.handle("김팀장이랑 박부장한테 메일 보내줘", contacts=CONTACTS, send=True)
        assert r["action"]["type"] == "ask" and "한 분" in r["replies"][0]["text"] and ran == []
    finally:
        mgr.shutdown()
    dlg, mgr, ran = dialog(mailer(srv, tmp_path, addresses=(), connected=()), task(["김팀장"]))
    try:
        r = dlg.handle("김팀장한테 메일 보내줘", contacts=CONTACTS, send=True)
        j = wait(mgr, r["action"]["task"]["id"])
        assert j.result["outcome"] == "send_failed" and j.result["reason"] == "not_set" and ran == []
    finally:
        mgr.shutdown()


def test_switch_off_keeps_the_old_behaviour(srv, tmp_path):
    dlg, mgr, ran = dialog(mailer(srv, tmp_path, others=False), task(["김팀장"]))
    try:
        r = dlg.handle("김팀장한테 메일 보내줘", contacts=CONTACTS, send=True)
        assert r["action"]["type"] == "none" and ran == []                       # 실제 서버: 남에게 보내는 작업을 만들지 않는다
        went = "\n".join(m.content for msgs in dlg.cloud.seen for m in msgs)
        assert "다른 사람에게는 보내지 않는다" in went
    finally:
        mgr.shutdown()


def test_a_big_address_book_gives_the_model_only_the_people_named_in_the_users_words(srv, tmp_path):
    """폰의 주소록(D78): 수백 명의 이름을 말마다 Claude로 보내지 않는다. 말에 나온 사람만 가고, 주소는 그래도 가지 않는다."""
    from agent.api.dialog import relevant_contacts
    book = {f"가상인물{i:03d}": f"test-p{i:03d}@mail.test" for i in range(300)}
    book.update({"김철수 팀장": KIM, "박영희": PARK})
    assert relevant_contacts(book, "철수한테 메일 보내줘", []) == {"김철수 팀장": KIM}          # 성을 뺀 이름으로 불러도
    assert relevant_contacts(book, "팀장님한테 보내줘", []) == {"김철수 팀장": KIM}
    assert relevant_contacts(book, "박영희", [("user", "내일 휴가라고 메일 보내줘"), ("assistant", "누구에게 보낼까요?")]) == {"박영희": PARK}
    assert relevant_contacts(book, "오늘 날씨 어때", []) == {}
    assert relevant_contacts(CONTACTS, "오늘 날씨 어때", []) == CONTACTS                        # 손으로 넣은 짧은 목록은 그대로
    dlg, mgr, ran = dialog(mailer(srv, tmp_path), task(["김철수 팀장"]))
    try:
        r = dlg.handle("철수한테 내일 회의 3시로 옮기자고 메일 보내줘", contacts=book, send=True)
        wait(mgr, r["action"]["task"]["id"])
        assert ran[0]._send_to == KIM
        went = "\n".join(m.content for msgs in dlg.cloud.seen for m in msgs)
        assert "김철수 팀장" in went and "가상인물" not in went and "박영희" not in went and KIM not in went
    finally:
        mgr.shutdown()


def test_contact_without_mail_is_named_only_when_mentioned():
    """연락처에 있지만 메일 주소가 없는 사람(D78): 말에 나온 사람만 모델로, 받는 사람이 되지는 못한다."""
    from agent.api.cloud import outgoing
    from agent.api.dialog import named_without_mail
    names = ["이서연", "박가상", "최합성"]
    assert named_without_mail(names, "서연한테 메일 보내줘", []) == ["이서연"]
    assert named_without_mail(names, "오늘 날씨 어때", []) == []
    msgs, _ = outgoing("서연한테 메일 보내줘", history=[], contacts={}, me={}, results={}, context=None, send_context=False,
                       capabilities="", tone=None, now=None, state=[], pages=None, sites=None, mail_brief=False, no_mail=["이서연"])
    sent = " ".join(str(m.content) for m in msgs)
    assert "[연락처에 있지만 메일 주소가 없는 사람] 이서연" in sent and "박가상" not in sent


def test_server_reads_names_without_mail_loosely():
    from agent.api.server import _no_mail
    assert _no_mail(None) is None and _no_mail("x") is None
    assert _no_mail(["이서연", " 이서연 ", 3, "", "가" * 51]) == ["이서연"]
