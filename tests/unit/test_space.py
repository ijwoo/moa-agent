"""내 공간(D87): 구성은 코드가 검사해 받아들인다, 바뀐 것은 코드가 견줘 말한다, 채운 블록에 답에 없는 숫자가 있으면 버린다. 합성 데이터만."""

import json
import stat
import threading
import time

import httpx
import pytest

from agent.api.dialog import DialogManager
from agent.api.jobs import JobManager
from agent.api.routines import RoutineStore, parse_when
from agent.api.server import make_server
from agent.api.space import MAX_CARDS, Space, SpaceError, clean_blocks, describe_changes, normalize
from agent.providers.base import ModelProvider, ModelResponse, ProviderError

TOKEN = "t" * 32
H = {"X-App-Token": TOKEN}


class Cloud(ModelProvider):
    name = "cloud"

    def __init__(self, *outs):
        self.outs, self.seen = list(outs), []

    def generate(self, messages, *, tools=None, json_schema=None, temperature=0.0, max_tokens=None):
        self.seen.append((messages, json_schema))
        out = self.outs.pop(0)
        if isinstance(out, Exception):
            raise out
        return ModelResponse(text=out if isinstance(out, str) else json.dumps(out, ensure_ascii=False), latency_ms=1, model="c", provider="c")


def card(kind, **kw):
    return {"id": "", "type": kind, "size": "medium", "title": "", "ask": "", "routine": "", **kw}


def test_layout_from_the_model_is_checked_before_it_is_taken(tmp_path):
    store = RoutineStore(None)
    r = store.add("아침 브리핑", "오늘 온 메일 요약해줘", parse_when("daily 07:00"))
    cards, dropped = normalize([card("calendar_today", size="huge"), card("stock_ticker"), card("custom"), card("custom", ask="  내 구독 서비스 결제일 알려줘 ", title="구독"),
                                card("routine_result", routine="없는 예약"), card("routine_result", routine="아침 브리핑"), "x", None], [], store)
    assert [(c["type"], c["size"], c["title"]) for c in cards] == [("calendar_today", "medium", "오늘 일정"), ("custom", "medium", "구독"),
                                                                    ("routine_result", "medium", "아침 브리핑")]
    assert cards[1]["ask"] == "내 구독 서비스 결제일 알려줘" and cards[2]["routine_id"] == r.id and len(dropped) == 3
    again, _ = normalize([card("calendar_today", id=cards[0]["id"], size="large"), card("weather", id=cards[1]["id"])], cards, store)
    assert again[0]["id"] == cards[0]["id"] and again[0]["size"] == "large" and again[1]["id"] != cards[1]["id"]   # 종류가 다르면 같은 카드가 아니다
    many, dropped = normalize([card("weather") for _ in range(MAX_CARDS + 3)], [], None)
    assert len(many) == MAX_CARDS and "12장" in dropped[0]


def test_changes_are_told_from_the_real_difference():
    a = [{"id": "c_1", "type": "weather", "size": "small", "title": "날씨"}, {"id": "c_2", "type": "mail_today", "size": "medium", "title": "오늘 온 메일"},
         {"id": "c_3", "type": "calendar_today", "size": "medium", "title": "오늘 일정"}]
    b = [{**a[1], "size": "large"}, a[2], {"id": "c_9", "type": "custom", "size": "medium", "title": "구독", "ask": "x"}]
    got = describe_changes(a, b)
    assert got == ["'구독' 카드를 더했어요", "'날씨' 카드를 뺐어요", "'오늘 온 메일'을 크게 바꿨어요"]
    assert describe_changes(a, [a[2], a[0], a[1]]) == ["'오늘 일정'을 맨 위로 올렸어요"]
    assert describe_changes(a, [a[0], a[2], a[1]]) == ["카드의 차례를 바꿨어요"] and describe_changes(a, a) == []


def test_blocks_with_numbers_that_are_not_in_the_answer_are_dropped():
    answer = "구독 중인 서비스는 2개예요. 합성뮤직 10,900원(12월 3일), 가상클라우드 2,900원(12월 15일)."
    blocks = clean_blocks([
        {"kind": "stat", "label": "이번 달 합계", "value": "13,800원"},                       # 답에 없는 합계: 버린다
        {"kind": "stat", "label": "구독", "value": "2개"},
        {"kind": "list", "items": [{"title": "합성뮤직", "trailing": "10,900원", "subtitle": "12월 3일"}, {"title": "지어낸 서비스", "trailing": "55,000원"}]},
        {"kind": "progress", "label": "예산", "ratio": 7}, {"kind": "chart", "points": [1, 2]},
        {"kind": "text", "text": "가상클라우드는 12월 15일에 2,900원이 나가요."}], answer)
    assert blocks == [{"kind": "stat", "label": "구독", "value": "2개", "note": ""},
                      {"kind": "list", "items": [{"title": "합성뮤직", "subtitle": "12월 3일", "trailing": "10,900원"}]},
                      {"kind": "text", "text": "가상클라우드는 12월 15일에 2,900원이 나가요."}]


def test_edit_keeps_what_was_not_mentioned_and_can_be_undone(tmp_path):
    first = {"cards": [card("meeting_todos", size="small"), card("weather", size="small"), card("mail_today")], "say": ""}
    cloud = Cloud(first, None, {"cards": [], "say": "화면 색은 바꿀 수 없어요."}, "글자 깨짐", ProviderError("down"))
    sp = Space(tmp_path / "space.json", cloud)
    out = sp.edit("오늘 일정이랑 날씨, 메일을 한 화면에 보여줘")
    ids = [c["id"] for c in out["space"]["cards"]]
    assert [c["type"] for c in out["space"]["cards"]] == ["meeting_todos", "weather", "mail_today"] and len(out["changes"]) == 3
    assert [c["source"] for c in out["space"]["cards"]] == ["app", "app", "mac"] and out["space"]["cards"][2]["state"] == "empty"
    assert stat.S_IMODE((tmp_path / "space.json").stat().st_mode) == 0o600
    cloud.outs[0] = {"cards": [card("mail_today", id=ids[2], size="large"), card("meeting_todos", id=ids[0], size="small")], "say": ""}
    out = sp.edit("메일을 맨 위에 크게, 날씨는 빼줘")
    assert [c["id"] for c in out["space"]["cards"]] == [ids[2], ids[0]] and out["space"]["can_undo"]
    assert out["changes"] == ["'날씨' 카드를 뺐어요", "'오늘 온 메일'을 크게 바꿨어요", "'오늘 온 메일'을 맨 위로 올렸어요"]
    listing = cloud.seen[1][0][1].content                                             # 모델에는 지금 구성과 사용자의 말이 간다(카드의 내용은 가지 않는다)
    assert f"id={ids[1]} · weather · small" in listing and "메일을 맨 위에 크게" in listing
    assert [c["id"] for c in Space(tmp_path / "space.json", cloud).public()["cards"]] == [ids[2], ids[0]]   # 파일에 남는다
    assert [c["id"] for c in sp.undo()["space"]["cards"]] == ids
    with pytest.raises(SpaceError):
        sp.undo()
    out = sp.edit("화면을 분홍색으로 바꿔줘")                                           # 구성이 비어 왔다 → 카드가 다 빠진다(되돌릴 수 있다)
    assert out["say"].startswith("화면 색은")
    for _ in range(2):                                                               # 꼴이 깨진 답·모델을 못 부름: 구성은 그대로
        before = sp.public()["cards"]
        with pytest.raises(SpaceError) as e:
            sp.edit("아무 말")
        assert e.value.code == "model_error" and sp.public()["cards"] == before


def test_cards_filled_by_the_mac_say_why_when_they_cannot_be_filled(tmp_path):
    asked = []

    def ask(text):
        asked.append(text)
        return {"state": "ok", "text": "구독 중인 서비스는 2개예요. 합성뮤직 10,900원, 가상클라우드 2,900원."} if "구독" in text else \
            {"state": "unavailable", "why": "로그인이 필요해요. 로그인한 뒤에 다시 채워 주세요.", "needs": "login"}
    cloud = Cloud({"cards": [card("custom", ask="내 구독 서비스와 요금 알려줘", title="구독"), card("custom", ask="쇼핑몰 포인트 알려줘", title="포인트"),
                             card("mail_today"), card("weather")], "say": ""},
                  {"blocks": [{"kind": "stat", "label": "구독", "value": "2개"}, {"kind": "stat", "label": "합계", "value": "13,800원"}]})
    sp = Space(None, cloud, ask=ask)
    sp.edit("구독이랑 포인트, 메일, 날씨 보여줘")
    cards = sp.refresh()["space"]["cards"]
    assert cards[0]["state"] == "ok" and cards[0]["data"]["blocks"] == [{"kind": "stat", "label": "구독", "value": "2개", "note": ""}]
    assert cards[1]["state"] == "unavailable" and cards[1]["why"] == "로그인이 필요해요" and cards[1]["data"] is None
    assert cards[2]["state"] == "unavailable" and cards[2]["why"] == "메일 계정 연결 필요"           # 메일 계정이 없다
    assert "state" not in cards[3] and asked == ["내 구독 서비스와 요금 알려줘", "쇼핑몰 포인트 알려줘"]   # 폰이 채우는 카드는 건드리지 않는다
    with pytest.raises(SpaceError):
        sp.refresh("c_nothere")


def test_api_serves_edits_undoes_and_refreshes(tmp_path):
    mgr = JobManager(lambda job, hooks: None)
    cloud = Cloud({"cards": [card("meeting_todos"), card("weather")], "say": ""})
    dlg = DialogManager(Cloud(), mgr, cloud=cloud)
    dlg.space = Space(tmp_path / "space.json", cloud)
    http = make_server(mgr, TOKEN, port=0, dialog=dlg)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http.server_address[1]}"
    try:
        assert {"space", "space_arrange"} <= set(httpx.get(f"{base}/v1/health", headers=H).json()["features"])
        assert httpx.post(f"{base}/v1/space/arrange", headers=H, json={"cards": [{"id": "c_nothere"}]}).status_code == 400
        assert httpx.get(f"{base}/v1/space", headers=H).json()["space"]["cards"] == [] and httpx.get(f"{base}/v1/space").status_code == 401
        assert httpx.post(f"{base}/v1/space/edit", headers=H, json={"text": ""}).status_code == 400
        out = httpx.post(f"{base}/v1/space/edit", headers=H, json={"text": "일정이랑 날씨 보여줘"}).json()
        assert [c["type"] for c in out["space"]["cards"]] == ["meeting_todos", "weather"] and len(out["changes"]) == 2
        assert httpx.post(f"{base}/v1/space/refresh", headers=H, json={}).status_code == 200
        assert httpx.post(f"{base}/v1/space/refresh", headers=H, json={"card": "../x"}).status_code == 400
        assert httpx.post(f"{base}/v1/space/undo", headers=H).json()["space"]["cards"] == []
        assert httpx.post(f"{base}/v1/space/undo", headers=H).status_code == 404
        assert httpx.delete(f"{base}/v1/space", headers=H).json()["space"]["cards"] == []
    finally:
        mgr.shutdown()
        http.shutdown()


def test_a_phone_card_the_app_cannot_fill_is_not_newly_made(tmp_path):
    """앱 D94: 캘린더 권한이 없어 '오늘 일정'은 빈 카드가 된다 → 앱이 채울 수 있다고 알릴 때만 새로 만든다. 이미 있던 카드는 그대로 둔다."""
    cloud = Cloud({"cards": [card("calendar_today"), card("weather")], "say": ""}, {"cards": [card("calendar_today"), card("weather")], "say": ""},
                  {"cards": [], "say": ""})
    sp = Space(None, cloud)
    out = sp.edit("일정이랑 날씨 보여줘")
    assert [c["type"] for c in out["space"]["cards"]] == ["weather"] and "아직 채울 수 없어서" in out["say"]
    system = cloud.seen[0][0][0].content
    assert "- calendar_today:" not in system and "앱이 캘린더를 읽지 못한다" in system
    sp.cards = []
    out = sp.edit("일정이랑 날씨 보여줘", ["calendar_today", "weather", "meeting_todos", "mail_today", 3])   # 앱이 채울 수 있다고 알렸다(맥 카드 이름은 무시)
    assert [c["type"] for c in out["space"]["cards"]] == ["calendar_today", "weather"] and "- calendar_today:" in cloud.seen[1][0][0].content
    kept, dropped = normalize([card("calendar_today", id=sp.cards[0]["id"])], sp.cards, None, frozenset({"weather"}))
    assert [c["id"] for c in kept] == [sp.cards[0]["id"]] and dropped == []


def test_card_text_is_plain_and_the_mail_card_shows_only_what_to_act_on(tmp_path):
    """실기기 2026-11-16: 메일 카드에 대화용 브리핑(굵게 표시·메일 주소·긴 문단)이 그대로 나왔고, 같은 배포 알림 3통이 세 줄을 다 썼다."""
    from agent.api.space import collapse_subjects, plain
    assert plain("- **보안 경고 (오전 9:24)**: tester@example.com 계정, example.com/notifications 에서 확인", 200) == "보안 경고 (오전 9:24): 계정, 에서 확인"
    got = collapse_subjects([{"service": "네이버", "subject": "합성 앱 0.1.0 (59) 배포"}, {"service": "네이버", "subject": "합성 앱 0.1.0 (58) 배포"},
                             {"service": "지메일", "subject": "보안 알림"}], 3)
    assert [x["subject"] for x in got] == ["합성 앱 0.1.0 (59) 배포 외 1통", "보안 알림"]

    class Mailer:
        addresses, secrets = ["test-me@naver.com"], {"test-me@naver.com": "x"}

        def provider_of(self, a):
            return "naver"

        def reading_account(self, service=None):
            return self.addresses[0]

        def inbox_today(self, address, limit=50):
            rows = [("1", "합성보안", "보안 알림: 새 기기 로그인"), ("2", "합성배포", "합성 앱 0.1.0 (59) 배포"), ("3", "합성배포", "합성 앱 0.1.0 (58) 배포")]
            return address, [{"uid": u, "from": f, "subject": s, "received_at": 0.0, "seen": False} for u, f, s in rows]

        def message_text(self, address, uid, max_chars=4000):
            return "본문"
    reader = Cloud({"headline": "**챙길 메일 1통**", "rest": "배포 안내 2통",
                    "picks": [{"title": "합성보안 알림", "note": "새 기기 로그인 — tester@example.com 확인", "service": ""}]},
                   "깨진 답")
    sp = Space(None, Cloud({"cards": [card("mail_today")], "say": ""}), mailer=Mailer(), local=reader)
    sp.edit("메일 보여줘")
    d = sp.refresh()["space"]["cards"][0]["data"]
    assert d["headline"] == "챙길 메일 1통" and d["rest"] == "배포 안내 2통" and d["count"] == 3
    assert d["picks"] == [{"title": "합성보안 알림", "note": "새 기기 로그인 — 확인", "service": "", "say": "오늘 온 메일 가운데 '합성보안 알림' 메일을 자세히 알려줘"}]
    assert d["items"] == [{"service": "", "subject": "합성보안 알림 — 새 기기 로그인 — 확인", "from": "", "time": ""}]   # 지금 앱이 그리는 줄: 챙길 것이 먼저
    assert d["blocks"] == [{"kind": "text", "text": "챙길 메일 1통 · 배포 안내 2통"}] and "본문" in reader.seen[0][0][1].content
    d = sp.refresh()["space"]["cards"][0]["data"]                                    # 고르지 못하면 코드가 만든 목록만(묶어서)
    assert d["picks"] == [] and d["blocks"] == [] and [x["subject"] for x in d["items"]] == ["보안 알림: 새 기기 로그인", "합성 앱 0.1.0 (59) 배포 외 1통"]


def test_a_routine_card_that_has_not_run_says_when_it_first_will(tmp_path):
    from datetime import datetime
    now = datetime(2026, 11, 16, 15, 0).timestamp()
    store = RoutineStore(None, clock=lambda: now)
    r = store.add("아침 브리핑", "오늘 온 메일 요약해줘", parse_when("daily 08:00"))
    sp = Space(None, Cloud({"cards": [card("routine_result", routine="아침 브리핑")], "say": ""}), routines=store, clock=lambda: now)
    sp.edit("아침 브리핑 결과 보여줘")
    assert sp.refresh()["space"]["cards"][0]["why"] == "내일 오전 8:00에 처음 돌아요"
    store.update(r.id, enabled=False)
    assert sp.refresh()["space"]["cards"][0]["why"] == "꺼져 있는 예약이에요"
    store.delete(r.id)
    assert "지워진 예약" in sp.refresh()["space"]["cards"][0]["why"]


def test_a_card_never_carries_the_assistants_excuses_and_particles_fit_the_word(tmp_path):
    """실기기 2026-11-16(빌드 60): 카드 안에 "폰 알림은 제가 직접 볼 수 없어요…" 같은 설명이 두세 줄씩 들어가 챗봇 티가 났고, "'날씨'을(를)"이 기계 같았다."""
    from agent.api.space import WHY_MAX, josa, short_why
    assert [josa(w, "을를") for w in ("날씨", "오늘 온 메일", "환율", "'회의 할 일'", "API")] == ["를", "을", "을", "을", "를"]
    assert [josa(w, "으로") for w in ("날씨", "메일", "오늘 일정")] == ["로", "로", "으로"]   # ㄹ 받침은 "로"
    assert short_why("로그인이 필요해요. 로그인한 뒤에 다시 채워 주세요.") == "로그인이 필요해요"
    assert short_why("폰 알림은 제가 직접 볼 수 없어서 이 카드는 지금 채워 드릴 수가 없는 상태예요") == "연동 필요" and short_why("") == "연동 필요"

    def ask(text):
        if "알림" in text:
            return {"state": "ok", "text": "폰 알림은 제가 직접 볼 수 없어요. 알림은 폰 안에만 있어서요. 대신 메일은 볼 수 있어요."}
        if "결제" in text:
            return {"state": "unavailable", "why": "사이트 확인이 필요해요", "needs": "site"}
        return {"state": "ok", "text": "오늘 환율은 1,336원이에요. 다만 실시간 값은 제가 직접 볼 수 없어요."}
    cloud = Cloud({"cards": [card("custom", ask="폰 알림 목록 보여줘", title="알림 목록"), card("custom", ask="네이버페이 결제 내역 알려줘", title="결제 내역"),
                             card("custom", ask="오늘 환율 알려줘", title="환율")], "say": ""},
                  {"cannot": False, "blocks": [{"kind": "text", "text": "폰 알림은 제가 직접 볼 수 없어요."}]},     # 모델이 사정 설명을 블록으로 옮겼다: 코드가 거른다
                  {"cannot": False, "blocks": [{"kind": "stat", "label": "환율", "value": "1,336원"}, {"kind": "text", "text": "실시간 값은 제가 직접 볼 수 없어요."}]})
    sp = Space(None, cloud, ask=ask, routines=RoutineStore(None))
    sp.edit("알림이랑 결제 내역, 환율 보여줘")
    a, b, c = sp.refresh()["space"]["cards"]
    assert a["state"] == "unavailable" and a["why"] == "연동 필요" and a["data"] is None and "action" not in a
    assert b["state"] == "unavailable" and b["why"] == "사이트 확인이 필요해요" and len(b["why"]) <= WHY_MAX
    assert b["action"] == {"label": "매일 아침에 모아보기", "say": "매일 아침 9시에 네이버페이 결제 내역 알려줘"}
    assert c["state"] == "ok" and c["data"]["blocks"] == [{"kind": "stat", "label": "환율", "value": "1,336원", "note": ""}]   # 값은 남기고 사정 설명만 뺀다


def test_arranging_by_hand_only_moves_resizes_and_removes_what_is_there(tmp_path):
    """끌어 옮기기 편집: 모델 없이 차례·크기·빼기만. 모르는 id는 버리고 새 카드는 만들지 못한다."""
    cloud = Cloud({"cards": [card("weather"), card("mail_today"), card("meeting_todos")], "say": ""})
    sp = Space(tmp_path / "s.json", cloud)
    ids = [c["id"] for c in sp.edit("날씨, 메일, 회의 할 일")["space"]["cards"]]
    for bad in ([{"id": ids[2]}, {"id": "c_madeup"}], [{"id": ids[0], "size": "tiny"}], [{"id": ids[2]}, {"id": ids[2]}], [{"id": ids[0]}, "x"],
                [{"id": "c_new", "type": "custom", "ask": "몰래 더하기"}]):                      # 하나라도 어긋나면 아무것도 바꾸지 않는다
        with pytest.raises(SpaceError):
            sp.arrange(bad)
    assert [c["id"] for c in sp.public()["cards"]] == ids
    out = sp.arrange([{"id": ids[2], "size": "large"}, {"id": ids[0]}])
    assert [(c["id"], c["size"]) for c in out["space"]["cards"]] == [(ids[2], "large"), (ids[0], "medium")] and len(cloud.seen) == 1
    assert out["changes"] == ["'오늘 온 메일' 카드를 뺐어요", "'회의에서 나온 할 일'을 크게 바꿨어요", "'회의에서 나온 할 일'을 맨 위로 올렸어요"]
    assert [c["id"] for c in sp.undo()["space"]["cards"]] == ids
    assert sp.arrange([{"id": i} for i in ids])["changes"] == []
    with pytest.raises(SpaceError):
        sp.arrange("x")


def test_a_card_tied_to_a_routine_shows_what_that_routine_last_brought(tmp_path):
    """카드의 단추로 만든 예약은 그 카드에 이어진다. 그 뒤로 카드는 예약이 돈 결과로 채워지고, 사이트에서 읽은 결과는 로컬 모델만 다듬는다."""
    from agent.api.routines import Scheduler
    clock = {"t": time.time()}
    store = RoutineStore(None, clock=lambda: clock["t"])
    sched = Scheduler(store, lambda r: {"status": "done"}, clock=lambda: clock["t"])
    local = Cloud({"cannot": False, "blocks": [{"kind": "list", "items": [{"title": "합성 스티커", "subtitle": "배송 중", "trailing": "3,300원"},
                                                                         {"title": "지어낸 물건", "trailing": "99,000원"}]}]}, "깨진 답")
    cloud = Cloud({"cards": [card("custom", ask="네이버페이 결제 내역 알려줘", title="결제 내역")], "say": ""})
    sp = Space(None, cloud, routines=store, scheduler=sched, local=local, ask=lambda t: {"state": "unavailable", "why": "사이트 확인이 필요해요", "needs": "site"},
               clock=lambda: clock["t"])
    sp.edit("결제 내역 보여줘")
    assert sp.refresh()["space"]["cards"][0]["action"]["say"] == "매일 아침 9시에 네이버페이 결제 내역 알려줘"
    r = store.add("결제 내역", "네이버페이 결제 내역 알려줘", parse_when("daily 09:00"))
    assert sp.link_routine("다른 말", r) is None and sp.link_routine("매일 아침 9시에 네이버페이 결제 내역 알려줘", r) == "결제 내역"
    c = sp.refresh()["space"]["cards"][0]
    assert c["routine_id"] == r.id and c["state"] == "unavailable" and "처음 돌아요" in c["why"] and "action" not in c
    rec = sched._record(r, clock["t"], "task", {"task_id": "job12345"})
    assert sp.refresh()["space"]["cards"][0]["why"] == "가져오는 중이에요"
    sched.task_finished("job12345", "completed", ["합성 스티커 3,300원 배송 중", "가상 케이스 12,000원 구매확정"])
    c = sp.refresh()["space"]["cards"][0]
    assert c["state"] == "ok" and c["data_at"] == rec["ran_at"] and len(cloud.seen) == 1          # 사이트에서 읽은 것은 Claude로 가지 않았다
    assert c["data"]["blocks"] == [{"kind": "list", "items": [{"title": "합성 스티커", "subtitle": "배송 중", "trailing": "3,300원"}]}]
    sp.refresh()
    assert len(local.seen) == 1                                                                  # 같은 실행은 다시 다듬지 않는다
    rec2 = sched._record(r, clock["t"], "task", {"task_id": "job67890"})
    sched.task_finished("job67890", "completed", ["가상 케이스 12,000원 구매확정", "합성 펜 1,500원 배송 준비"])
    c = sp.refresh()["space"]["cards"][0]                                                        # 로컬 모델이 못 다듬었다: 줄을 그대로 목록으로
    assert [i["title"] for i in c["data"]["blocks"][0]["items"]] == ["가상 케이스 12,000원 구매확정", "합성 펜 1,500원 배송 준비"]
    sched._record(r, clock["t"], "needs_login", {})
    assert sp.refresh()["space"]["cards"][0]["why"] == "로그인이 필요해요"


def test_what_changed_since_last_time_is_marked_and_survives_a_restart(tmp_path):
    """지난 결과와 견준다(D87 확장): 처음 채운 것은 바뀐 것이 아니다, 목록의 새 줄·큰 값의 차이·추세, 그리고 서버가 다시 떠도 화면이 비지 않는다."""
    answers = ["환율은 1,336원이에요.", "환율은 1,336원이에요.", "환율은 1,340.5원이에요.", "환율은 1,338원이에요."]
    fills = [{"cannot": False, "blocks": [{"kind": "stat", "label": "환율", "value": v}]} for v in ("1,336원", "1,336원", "1,340.5원", "1,338원")]
    cloud = Cloud({"cards": [card("custom", ask="오늘 환율 알려줘", title="환율")], "say": ""}, *fills)
    clock = {"t": 1_800_000_000.0}
    sp = Space(tmp_path / "s.json", cloud, ask=lambda t: {"state": "ok", "text": answers.pop(0)}, data_path=tmp_path / "d.json", clock=lambda: clock["t"])
    cid = sp.edit("환율 보여줘")["space"]["cards"][0]["id"]
    sp.cards[0]["type"] = "mail_today"; sp._fill = lambda c: sp._blocks_from({**c, "ask": "오늘 환율 알려줘"}, answers.pop(0), private=False)   # 같은 곳에서 오는 내용으로 친다
    out = sp.refresh()
    c = out["space"]["cards"][0]
    assert out["changed"] == [] and "changed_at" not in c and "delta" not in c["data"]                 # 처음
    assert sp.refresh()["changed"] == []                                                              # 같은 값
    clock["t"] += 3600
    out = sp.refresh()
    c = out["space"]["cards"][0]
    assert out["changed"] == [cid] and c["changed_at"] and c["data"]["delta"] == {"diff": 4.5, "text": "+4.5"}
    assert stat.S_IMODE((tmp_path / "d.json").stat().st_mode) == 0o600
    again = Space(tmp_path / "s.json", cloud, data_path=tmp_path / "d.json", clock=lambda: clock["t"]).public()["cards"][0]   # 서버가 다시 떴다
    assert again["state"] == "ok" and again["data"]["blocks"][0]["value"] == "1,340.5원" and again["changed_at"] == c["changed_at"]
    c = sp.refresh()["space"]["cards"][0]
    assert c["data"]["delta"]["text"] == "−2.5" and c["data"]["blocks"][-1] == {"kind": "trend", "points": [1336.0, 1340.5, 1338.0]}
    sp.arrange([])                                                                                    # 카드를 빼면 그 내용·요약도 지운다
    assert json.loads((tmp_path / "d.json").read_text())["data"] == {} and sp._hist == {}


def test_new_rows_are_flagged_and_a_tied_card_can_be_run_now(tmp_path):
    from agent.api.routines import Scheduler
    store = RoutineStore(None)
    ran = []
    sched = Scheduler(store, lambda r: ran.append(r.id) or {"status": "done", "replies": [{"text": "합성 답"}]})
    r = store.add("결제 내역", "결제 내역 알려줘", parse_when("daily 09:00"))
    local = Cloud("x", "x")   # 로컬 모델이 못 다듬는다: 줄을 그대로 목록으로
    sp = Space(None, Cloud({"cards": [card("custom", ask="결제 내역 알려줘", title="결제 내역")], "say": ""}), routines=store, scheduler=sched, local=local)
    sp.edit("결제 내역")
    assert sp.link_routine("매일 아침 9시에 결제 내역 알려줘", r) and sp.linked(r.id) and not sp.linked("rt_other")
    for n, lines in enumerate((["가상 케이스 구매확정", "합성 펜 배송 준비"], ["합성 스티커 배송 중", "가상 케이스 구매확정", "합성 펜 배송 준비"])):
        sched._record(r, time.time(), "task", {"task_id": f"job{n}"})
        sched.task_finished(f"job{n}", "completed", lines)
        names = sp.on_routine_done(r.id)
    c = sp.public()["cards"][0]
    assert names == ["결제 내역"] and c["data"]["new_count"] == 1
    assert [(i["title"], i.get("new", False)) for i in c["data"]["blocks"][0]["items"]] == [("합성 스티커 배송 중", True), ("가상 케이스 구매확정", False), ("합성 펜 배송 준비", False)]
    assert c["actions"] == [{"label": "지금 가져오기", "run": r.id}]
    sched.run_now(r.id)
    for _ in range(300):                                                             # 뒤에서 돈다: 기록이 적힐 때까지 기다린다
        if ran and sched.recent(1)[0]["status"] == "done":
            break
        time.sleep(0.01)
    assert ran == [r.id] and sched.recent(1)[0]["status"] == "done"
    with pytest.raises(Exception):
        sched.run_now("rt_nothere")


def test_cards_that_cannot_be_shown_are_not_made_and_account_cards_get_their_button_at_once(tmp_path):
    """실기기: 만든 뒤에야 "연동 필요"를 봤다 → 만드는 순간에 가른다. 볼 길이 없는 것은 만들지 않고, 계정에 들어가 봐야 하는 것은 바로 단추를 단다."""
    asked = []
    cloud = Cloud({"cards": [card("custom", ask="폰 알림 목록 보여줘", title="알림 목록", needs="cannot"),
                             card("custom", ask="네이버페이 결제 내역 알려줘", title="결제 내역", needs="daily"),
                             card("custom", ask="오늘 환율 알려줘", title="환율", needs="now")], "say": ""})
    sp = Space(None, cloud, routines=RoutineStore(None), ask=lambda t: asked.append(t) or {"state": "unavailable", "why": "x"})
    out = sp.edit("알림이랑 결제 내역, 환율 보여줘")
    a, b = out["space"]["cards"]
    assert [a["title"], b["title"]] == ["결제 내역", "환율"] and "'알림 목록'은 제가 볼 수 없어서 만들지 않았어요" in out["say"]
    assert a["state"] == "unavailable" and a["why"] == "매일 모아 와야 해요" and a["action"]["say"] == "매일 아침 9시에 네이버페이 결제 내역 알려줘"
    assert b["state"] == "empty" and "action" not in b
    sp.refresh()
    assert asked == ["오늘 환율 알려줘"]                                             # 계정 카드는 채워 보려고 묻지 않는다(작업을 만들었다 거두지 않는다)


def test_an_unclear_request_is_asked_back_and_the_answer_is_read_with_it(tmp_path):
    cloud = Cloud({"cards": [], "say": "", "question": "어느 종목을 볼까요?"},
                  {"cards": [card("custom", ask="토트넘 최근 경기 결과 알려줘", title="토트넘", needs="now")], "say": ""},
                  {"cards": [], "say": ""})
    sp = Space(None, cloud)
    out = sp.edit("경기 결과 보여줘")
    assert out["asking"] is True and out["say"] == "어느 종목을 볼까요?" and out["changes"] == [] and out["space"]["cards"] == []
    out = sp.edit("토트넘")
    assert "[방금 물은 것] 사용자가 \"경기 결과 보여줘\"라고 해서 \"어느 종목을 볼까요?\"라고 물었다" in cloud.seen[1][0][1].content
    assert [c["title"] for c in out["space"]["cards"]] == ["토트넘"] and "asking" not in out
    sp.edit("다 지워")
    assert "[방금 물은 것]" not in cloud.seen[2][0][1].content                        # 한 번 답하면 끝


def test_the_chat_can_edit_the_space_and_says_what_the_code_saw_change(tmp_path):
    base = {"choices": [], "title": "", "to": [], "purpose": "", "targets": [], "instructions": "", "sources": [], "target": ""}
    head = Cloud({**base, "action": "space", "question": "환율 카드 넣어줘", "replies": []})
    mgr = JobManager(lambda job, hooks: None)
    dlg = DialogManager(Cloud(), mgr, cloud=head)
    dlg.real_only = True
    dlg.space = Space(None, Cloud({"cards": [card("custom", ask="오늘 환율 알려줘", title="환율", needs="now")], "say": ""}))
    try:
        out = dlg.handle("내 공간에 환율 카드 넣어줘", None)
        assert out["replies"] == [{"text": "'환율' 카드를 더했어요", "by": "mac", "kind": "status"}]   # 한 줄로 끝나는 일: 상태 줄 and [c["title"] for c in out["space"]["cards"]] == ["환율"]
        system, schema = head.seen[0][0][0].content, head.seen[0][1]
        assert "space —" in system and "space" in schema["properties"]["action"]["enum"]
        dlg.space = None                                                              # 꺼져 있으면 고를 수 없다
        head.outs.append({**base, "action": "reply", "question": "", "replies": ["합성 답"]})
        dlg.handle("내 공간에 날씨 넣어줘", None)
        assert "space —" not in head.seen[1][0][0].content and "space" not in head.seen[1][1]["properties"]["action"]["enum"]
    finally:
        mgr.shutdown()


def test_cards_the_user_did_not_ask_to_remove_are_put_back(tmp_path):
    """측정 2026-11-16: "메일만 크게"에 모델이 나머지 카드를 뺀 구성을 냈다. 빼라는 말이 없으면 코드가 되살린다."""
    from agent.api.space import keep_unmentioned
    old = [{"id": f"c_{k}", "type": t, "size": "medium", "title": t} for k, t in enumerate(("calendar_today", "weather", "mail_today", "meeting_todos"))]
    big = [{**old[2], "size": "large"}]
    got, n = keep_unmentioned(old, big, "메일만 크게")
    assert n == 3 and [c["id"] for c in got] == ["c_0", "c_1", "c_2", "c_3"] and got[2]["size"] == "large"
    got, n = keep_unmentioned(old, [old[3], {**old[2], "size": "large"}], "회의 할 일을 맨 위로, 메일은 크게")
    assert n == 2 and [c["id"] for c in got] == ["c_0", "c_1", "c_3", "c_2"]            # 옮긴 카드의 새 자리는 지키고, 빠진 것만 끼운다
    for said in ("날씨 빼줘", "메일만 남기고 다 지워", "일정 말고 다른 건 필요 없어", "다 지워"):
        assert keep_unmentioned(old, big, said) == (big, 0)
    cloud = Cloud({"cards": [card("weather"), card("mail_today")], "say": ""}, None)
    sp = Space(None, cloud)
    ids = [c["id"] for c in sp.edit("날씨, 메일")["space"]["cards"]]
    cloud.outs[0] = {"cards": [card("mail_today", id=ids[1], size="large")], "say": ""}
    out = sp.edit("메일만 크게")
    assert [c["id"] for c in out["space"]["cards"]] == ids and out["changes"] == ["'오늘 온 메일'을 크게 바꿨어요"]


def test_the_same_number_written_with_korean_units_is_not_taken_for_an_invented_one():
    """측정 2026-11-16: 답 "20만5,500원" → 블록 "205,500원"을 지어낸 숫자로 보고 버려, 카드에 안내 문구만 남았다."""
    answer = "삼성전자는 20만5,500원에 거래되고 있고 시가총액은 1,226조 원, 금 한 돈은 73만4000원이에요."
    got = clean_blocks([{"kind": "stat", "label": "현재가", "value": "205,500원"}, {"kind": "stat", "label": "금", "value": "734,000원"},
                        {"kind": "stat", "label": "지어낸 값", "value": "215,500원"}], answer)
    assert [b["value"] for b in got] == ["205,500원", "734,000원"]


def test_a_value_written_with_korean_units_is_read_whole_and_wild_jumps_are_not_called_a_change_in_value(tmp_path):
    """실기기 2026-11-16(빌드 65): '삼성전자 주가'에 차이 "−205,480"과 뚝 떨어진 추세선 — "20만 5,500원"을 20으로 읽었다."""
    from agent.api.space import comparable, tidy_value, to_number
    assert [to_number(v) for v in ("20만 5,500원", "205,500원", "73만4000원", "약 8,900만 원", "1.2억 원", "1,336.0원", "보통", "−3")] == \
        [205500.0, 205500.0, 734000.0, 89000000.0, 120000000.0, 1336.0, None, 3.0]
    assert [tidy_value(v) for v in ("20만 5,500원", "73만4000원", "약 8,900만 원", "205,500원", "1억 2,000만 원")] == \
        ["205,500원", "734,000원", "약 8,900만 원", "205,500원", "1억 2,000만 원"]
    assert comparable(205500, 196300) and not comparable(20, 205500) and not comparable(0, 5) and comparable(0, 0)
    answers = ["주가는 205,500원이에요.", "주가는 20만 5,500원이에요.", "주가는 20만 6,000원이에요."]
    fills = [{"cannot": False, "blocks": [{"kind": "stat", "label": "현재가", "value": v}]} for v in ("205,500원", "20만 5,500원", "20만 6,000원")]
    sp = Space(None, Cloud({"cards": [card("custom", ask="금 한 돈 시세", title="금 시세")], "say": ""}, *fills), ask=lambda t: {"state": "ok", "text": answers.pop(0)})
    cid = sp.edit("금 시세 카드")["space"]["cards"][0]["id"]
    sp.cards[0]["type"] = "mail_today"; sp._fill = lambda c: sp._blocks_from({**c, "ask": "금 한 돈 시세"}, answers.pop(0), private=False)   # 같은 곳에서 오는 내용으로 친다
    sp.refresh()
    out = sp.refresh()                                                                 # 같은 값을 다른 꼴로 받았다: 바뀐 것이 아니다
    c = out["space"]["cards"][0]
    assert c["data"]["blocks"][0]["value"] == "205,500원" and out["changed"] == [] and "delta" not in c["data"]
    c = sp.refresh()["space"]["cards"][0]
    assert c["data"]["blocks"][0]["value"] == "206,000원" and c["data"]["delta"] == {"diff": 500.0, "text": "+500"}
    sp._hist[cid][0]["num"] = 20.0                                                     # 예전에 잘못 읽어 둔 값이 남아 있어도 추세에 잇지 않는다
    sp._hist[cid].insert(0, {"at": 0, "digest": "x", "num": 19.0, "titles": []})
    data = {"blocks": [{"kind": "stat", "label": "현재가", "value": "206,500원", "note": ""}]}
    sp._note(cid, data)
    assert all(b["kind"] != "trend" for b in data["blocks"]) and data["delta"]["text"] == "+500" and [h["num"] for h in sp._hist[cid]][:2] == [None, None]


def test_rewording_is_not_a_change_only_values_are():
    """실기기 2026-11-16: 값은 그대로인데 덧붙인 글이 달라질 때마다 "바뀜"으로 적혔다(15분에 11번)."""
    a = {"blocks": [{"kind": "stat", "label": "삼성전자 (장 초반)", "value": "205,500원", "note": "전일 대비 500원 하락"}, {"kind": "text", "text": "장중이라 조금씩 달라요."}]}
    b = {"blocks": [{"kind": "stat", "label": "삼성전자 현재가", "value": "20만 5,500원", "note": "0.24% 내림"}, {"kind": "text", "text": "시세는 계속 움직여요."}]}
    c = {"blocks": [{"kind": "stat", "label": "삼성전자 현재가", "value": "206,000원", "note": ""}]}
    d1, d2, d3 = (Space._summary(x)["digest"] for x in (a, b, c))
    assert d1 == d2 and d1 != d3
    rows = lambda *t: {"blocks": [{"kind": "list", "items": [{"title": x, "subtitle": "말씨가 다른 설명", "trailing": ""} for x in t]}]}   # noqa: E731
    assert Space._summary(rows("가", "나"))["digest"] == Space._summary({"blocks": [{"kind": "list", "items": [{"title": "가", "subtitle": "x", "trailing": ""},
                                                                                                           {"title": "나", "subtitle": "y", "trailing": ""}]}]})["digest"]
    assert Space._summary(rows("가", "나"))["digest"] != Space._summary(rows("가", "다"))["digest"]


def test_cards_filled_by_web_search_are_never_compared_with_last_time():
    """실기기 2026-11-16: 몇 초 사이 두 번 채운 주가 카드에 "−9,200"·"+9,200" — 검색이 다른 기사의 값을 집어 왔을 뿐이다. 검색으로 채운 카드는 견주지 않는다."""
    answers = ["주가는 205,500원이에요.", "주가는 196,300원이에요.", "주가는 205,500원이에요."]
    fills = [{"cannot": False, "blocks": [{"kind": "stat", "label": "현재가", "value": v}]} for v in ("205,500원", "196,300원", "205,500원")]
    sp = Space(None, Cloud({"cards": [card("custom", ask="금 한 돈 시세", title="금 시세")], "say": ""}, *fills), ask=lambda t: {"state": "ok", "text": answers.pop(0)})
    sp.edit("금 시세 카드")
    for _ in range(3):
        out = sp.refresh()
        c = out["space"]["cards"][0]
        assert out["changed"] == [] and "delta" not in c["data"] and "changed_at" not in c and all(b["kind"] != "trend" for b in c["data"]["blocks"])
    assert sp._hist == {} and c["actions"] == [{"label": "자세히 묻기", "say": "금 한 돈 시세"}]


def test_cards_for_prices_that_move_by_the_minute_are_not_made(tmp_path):
    """사용자 결정 2026-11-16: 주가·코인·지수는 검색으로 채우면 채울 때마다 값이 달라 믿을 수 없다 → 공식 시세를 붙이기 전까지 만들지 않는다(모델이 표시하지 않아도 코드가 막는다)."""
    cards, dropped = normalize([card("custom", ask="삼성전자 주가 알려줘", title="삼성전자", needs="now"), card("custom", ask="비트코인 얼마야", title="코인", needs="live"),
                                card("custom", ask="오늘 코스피 지수", title="코스피"), card("custom", ask="오늘 원달러 환율 알려줘", title="환율", needs="now"),
                                card("custom", ask="금 한 돈 시세 알려줘", title="금 시세", needs="now")], [], None)
    assert [c["title"] for c in cards] == ["환율", "금 시세"] and dropped == ["정확한 시세는 아직 볼 수 없어서 그 카드는 만들지 않았어요"]
    old = [{"id": "c_1", "type": "custom", "size": "medium", "title": "삼성전자 주가", "ask": "삼성전자 주가"}]
    kept, _ = normalize([card("custom", id="c_1", ask="삼성전자 주가", title="삼성전자 주가", size="small")], old, None)
    assert [c["id"] for c in kept] == ["c_1"]                                           # 이미 있던 카드는 건드리지 않는다


def test_pairs_and_date_blocks_are_checked_against_the_answer():
    answer = "서울 내일은 최고 15도, 최저 6도예요. 다음 공휴일은 12월 25일(금) 성탄절이에요."
    got = clean_blocks([
        {"kind": "pairs", "items": [{"title": "최고", "trailing": "15도"}, {"title": "최저", "trailing": "6도"}, {"title": "지어낸 값", "trailing": "99도"}]},
        {"kind": "pairs", "items": [{"title": "하나뿐", "trailing": "15도"}]},
        {"kind": "date", "label": "성탄절", "date": "2026-12-25", "note": "금요일"},
        {"kind": "date", "label": "지어낸 날", "date": "2026-12-31"}, {"kind": "date", "label": "꼴이 틀림", "date": "12월 25일"},
        {"kind": "date", "label": "없는 날", "date": "2026-02-30"}], answer, today=__import__("datetime").date(2026, 11, 16))
    assert got == [{"kind": "pairs", "items": [{"label": "최고", "value": "15도"}, {"label": "최저", "value": "6도"}]},
                   {"kind": "date", "label": "성탄절", "date": "2026-12-25", "note": "금요일"}]
    assert Space._summary({"blocks": got})["digest"] != Space._summary({"blocks": [got[0], {**got[1], "date": "2026-12-26"}]})["digest"]


def test_suggestions_offer_only_cards_that_can_be_made_and_are_not_there_yet(tmp_path):
    store = RoutineStore(None)
    store.add("아침 메일 브리핑", "오늘 온 메일 요약해줘", parse_when("daily 08:00"))
    store.add("한 번짜리", "x", parse_when("daily 09:00"))
    store.update(store.list()[1].id, enabled=False)
    cloud = Cloud({"cards": [card("weather"), card("custom", ask="오늘 원달러 환율 알려줘", title="환율")], "say": ""})
    sp = Space(None, cloud, routines=store)
    sp.site_cards = [("네이버페이 결제 내역", "네이버페이 최근 결제 내역 카드 넣어줘")]
    labels = [s["label"] for s in sp.public()["suggestions"]]
    assert labels == ["회의에서 나온 할 일", "오늘 일정", "날씨", "아침 메일 브리핑 결과", "네이버페이 결제 내역", "환율"]   # 메일 계정이 없으면 메일은 권하지 않는다
    sp.edit("날씨랑 환율")
    got = sp.public()["suggestions"]
    assert [s["label"] for s in got] == ["회의에서 나온 할 일", "오늘 일정", "아침 메일 브리핑 결과", "네이버페이 결제 내역", "미세먼지", "오늘 뉴스"]
    assert all(set(s) == {"label", "say"} and len(s["label"]) <= 16 for s in got)


def test_a_date_whose_year_the_answer_does_not_give_becomes_the_coming_one():
    """측정 2026-11-16: 답은 "12월 25일(금)"뿐인데 블록의 날짜가 2025-12-25였다(모델이 지어낸 해)."""
    from datetime import date
    day = lambda d, ans, today: clean_blocks([{"kind": "date", "label": "그날", "date": d}], ans, today=today)[0]["date"]   # noqa: E731
    assert day("2025-12-25", "다음 공휴일은 12월 25일(금) 성탄절이에요.", date(2026, 11, 16)) == "2026-12-25"
    assert day("2026-01-01", "다음 공휴일은 1월 1일 신정이에요.", date(2026, 12, 28)) == "2027-01-01"
    assert day("2026-11-16", "오늘 11월 16일이 마감이에요.", date(2026, 11, 16)) == "2026-11-16"
    assert day("2027-03-01", "행사는 2027년 3월 1일이에요.", date(2026, 11, 16)) == "2027-03-01"      # 답에 해가 적혀 있으면 그대로
    assert clean_blocks([{"kind": "date", "label": "지난 시험", "date": "2025-11-13"}], "시험은 2025년 11월 13일이었어요.", today=date(2026, 11, 16)) == []   # 지난 날은 싣지 않는다


def test_widget_gives_one_short_line_per_card_without_calling_a_model(tmp_path):
    cloud = Cloud({"cards": [card("weather"), card("custom", ask="오늘 환율", title="환율"), card("custom", ask="다음 공휴일", title="공휴일"),
                             card("custom", ask="뉴스", title="뉴스"), card("custom", ask="결제 내역 알려줘", title="결제", needs="daily"), card("mail_today")], "say": ""})
    sp = Space(None, cloud, routines=RoutineStore(None))
    ids = [c["id"] for c in sp.edit("이것저것")["space"]["cards"]]
    now = sp.clock()
    sp._data[ids[1]] = {"state": "ok", "at": now, "data": {"blocks": [{"kind": "stat", "label": "원달러", "value": "1,336.0원", "note": ""}, {"kind": "trend", "points": [1, 2, 3]}]}}
    sp._data[ids[2]] = {"state": "ok", "at": now, "data": {"blocks": [{"kind": "date", "label": "성탄절", "date": "2026-12-25", "note": ""}]}}
    sp._data[ids[3]] = {"state": "ok", "at": now, "data": {"new_count": 1, "blocks": [{"kind": "list", "items": [{"title": "합성 뉴스 하나"}, {"title": "둘"}, {"title": "셋"}]}]}}
    sp._data[ids[5]] = {"state": "ok", "at": now, "data": {"count": 8, "headline": "챙길 메일 1통", "blocks": []}}
    w = sp.widget()["items"]
    assert len(cloud.seen) == 1 and [x["title"] for x in w] == ["날씨", "환율", "공휴일", "뉴스", "결제", "오늘 온 메일"]
    assert w[0] == {"id": ids[0], "type": "weather", "title": "날씨", "source": "app"}                 # 폰이 채우는 카드는 이름만
    assert (w[1]["value"], w[1]["line"]) == ("1,336.0원", "원달러") and (w[2]["value"], w[2]["date"], w[2]["line"]) == ("2026-12-25", "2026-12-25", "성탄절")
    assert (w[3]["line"], w[3]["new_count"]) == ("합성 뉴스 하나 외 2건", 1) and (w[4]["state"], w[4]["line"]) == ("unavailable", "매일 모아 와야 해요")
    assert (w[5]["value"], w[5]["line"]) == ("8통", "챙길 메일 1통")


def test_a_finished_task_becomes_one_card_of_a_fixed_shape():
    """사용자 지시 2026-11-16: 답이 참고 앱처럼 일정한 카드로. 줄글 보고 → answer / result / failed 카드, 단추는 코드가 정한다."""
    from types import SimpleNamespace as NS
    from agent.api.cards import make_card
    job = NS(task="쿠팡 장바구니에 가상 프로틴 담아줘")
    m = Cloud({"title": "장바구니에 담았어요", "subtitle": "가상 프로틴 초코 2.27kg · 1개",
               "blocks": [{"kind": "pairs", "items": [{"title": "도착", "trailing": "내일 새벽"}, {"title": "금액", "trailing": "88,890원"}]},
                          {"kind": "stat", "label": "할인", "value": "9,999원"}]})
    res = {"outcome": "completed", "approved_actions": ["장바구니 담기"],
           "report": {"done": ["가상 프로틴 초코 2.27kg을 담았어요.\n- 도착: 내일 새벽\n- 금액: 88,890원\n길: 쿠팡 > 상품 > 담기"], "sources": ["https://www.coupang.com/"]}}
    c = make_card(job, res, m, None)
    assert c["kind"] == "result" and c["title"] == "장바구니에 담았어요" and c["subtitle"].startswith("가상 프로틴")
    assert c["blocks"] == [{"kind": "pairs", "items": [{"label": "도착", "value": "내일 새벽"}, {"label": "금액", "value": "88,890원"}]}]   # 답에 없는 할인은 버린다
    assert c["actions"] == [{"label": "쿠팡에서 보기", "url": "https://www.coupang.com/"}] and "길:" not in m.seen[0][0][1].content
    c = make_card(job, {"outcome": "completed", "report": {"done": ["결제 2건\n- 합성 스티커 3,300원\n- 가상 케이스 12,000원"]}}, None, Cloud("깨짐"), private=True)
    assert c["kind"] == "answer" and [b["kind"] for b in c["blocks"]] == ["text", "list"]           # 다듬지 못하면 줄을 그대로
    c = make_card(job, {"outcome": "needs_login", "action": {"type": "site_login", "site": "coupang"},
                        "report": {"blocked": ["쿠팡 로그인이 풀렸어요. 다시 로그인해 주세요."]}}, None, None)
    assert c == {"kind": "failed", "title": "쿠팡 로그인이 풀렸어요", "actions": [{"label": "다시 로그인", "site": "coupang"}]}
    assert make_card(job, {"outcome": "cancelled", "report": {}}, None, None)["title"] == "그만뒀어요"


def test_failed_card_gets_a_reason_subtitle():
    from agent.api.cards import make_card
    why = "claude.ai가 사람 확인(보안 확인) 화면을 띄웠어요. 그 확인은 제가 대신 누르지 않아요 — 폰이나 맥에서 직접 열어 확인해 주세요."
    c = make_card(None, {"outcome": "site_blocked", "report": {"blocked": [why], "sources": ["https://claude.ai/"]}}, None, None)
    assert c["kind"] == "failed" and c["subtitle"].startswith("claude.ai가 사람 확인") and c["actions"]
    c = make_card(None, {"outcome": "loop_detected", "summary": "", "report": {"blocked": []}}, None, None)
    assert c["title"] == "끝내지 못했어요" and c["subtitle"] == "같은 화면을 맴돌아 멈췄어요"


def test_a_routine_card_shows_the_task_result_not_the_ack_bubble():
    # 실기기 2026-11-17: '채용 사이트 지원 현황' 카드에 결과 대신 "확인해 볼게요" 한 줄이 들어갔다(작업을 만든 실행에도 말풍선이 있다)
    from agent.api.routines import Scheduler
    clock = {"t": time.time()}
    store = RoutineStore(None, clock=lambda: clock["t"])
    sched = Scheduler(store, lambda r: {"status": "done"}, clock=lambda: clock["t"])
    r = store.add("채용 사이트 지원 현황", "채용 사이트 지원 현황 알려줘", parse_when("daily 08:00"))
    sp = Space(None, Cloud({"cards": [card("routine_result", routine="채용 사이트 지원 현황")], "say": ""}), routines=store, scheduler=sched,
               clock=lambda: clock["t"])
    sp.edit("채용 사이트 지원 현황 보여줘")
    sched._record(r, clock["t"], "task", {"task_id": "job11111", "replies": [{"text": "네! 채용 사이트 열어서 확인해 볼게요"}]})
    assert sp.refresh(force=True)["space"]["cards"][0]["why"] == "가져오는 중이에요"
    sched.task_finished("job11111", "completed", ["지원 3건 · 서류 검토 중 1건"])
    c = sp.refresh(force=True)["space"]["cards"][0]
    assert c["state"] == "ok" and "확인해 볼게요" not in json.dumps(c, ensure_ascii=False) and "지원 3건" in json.dumps(c, ensure_ascii=False)
    sched._record(r, clock["t"], "needs_login", {"replies": [{"text": "채용 사이트 로그인이 필요해요"}]})
    assert "로그인이 필요해" in sp.refresh(force=True)["space"]["cards"][0]["why"]
