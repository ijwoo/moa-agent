"""활동 탭·검색(D103): 개인 결과는 내용을 남기지 않음, 기다리는 작업은 위로, 시간대가 섞여도 최근 순, 검색은 맥에 있는 것만."""

from types import SimpleNamespace

from agent.api.activity import ActivityLog, activity, search


def job(i, title, at, conv=None, skill=None):
    return SimpleNamespace(id=i, title=title, task=title, skill=skill, created_at=at, updated_at=at, conversation_id=conv)


def test_record_keeps_no_content_for_private_results(tmp_path):
    log = ActivityLog(tmp_path / "a.json")
    log.record(job("j1", "네이버페이 결제내역", "2026-11-17T01:00:00+00:00", skill="naver_pay_recent"), "completed",
               {"outcome": "completed", "report": {"done": ["스타벅스 5,600원"], "private": True, "card": {"kind": "answer", "title": "최근 결제 내역"}}})
    log.record(job("j2", "번개 검색", "2026-11-17T02:00:00+00:00"), "completed",
               {"outcome": "completed", "report": {"done": ["번개는 계단형 선구로 나아갑니다."]}})
    a, b = log.all()
    assert "line" not in a and a["private"] is True and a["card_title"] == "최근 결제 내역"
    assert b["line"].startswith("번개는") and (tmp_path / "a.json").stat().st_mode & 0o777 == 0o600


def test_activity_orders_by_time_across_zones_and_lifts_waiting(tmp_path):
    log = ActivityLog(tmp_path / "a.json")
    log.record(job("j1", "작업 하나", "2026-11-17T00:30:00+00:00"), "completed", {"outcome": "completed", "report": {"done": ["끝"]}})   # 09:30 KST
    runs = [{"id": "rr_1", "routine_id": "rt_1", "title": "아침 메일", "ran_at": "2026-11-17T08:00:21+09:00", "status": "done",
             "replies": [{"text": "메일 3통"}]}]                                                                                            # 08:00 KST
    live = [{"id": "j9", "status": "awaiting_approval", "title": "유튜브에 올리기", "updated_at": "2026-11-17T01:00:00+00:00"},
            {"id": "j1", "status": "completed", "title": "작업 하나"}]
    out = activity(log, live, runs)
    assert [w["id"] for w in out["waiting"]] == ["j9"]
    assert [i["id"] for i in out["items"]] == ["j1", "rr_1"]


def test_search_finds_words_anywhere_on_the_mac_side(tmp_path):
    log = ActivityLog(tmp_path / "a.json")
    log.record(job("j2", "번개 검색", "2026-11-17T02:00:00+00:00"), "completed",
               {"outcome": "completed", "report": {"done": ["번개는 계단형 선구로 나아갑니다."]}})
    routines = [{"id": "rt_1", "title": "아침 메일 브리핑", "prompt": "오늘 받은 메일 정리해줘", "notes": ""}]
    assert [i["id"] for i in search("계단형  번개", log, [], routines)] == ["j2"]
    assert [i["kind"] for i in search("메일", log, [], routines)] == ["routine_def"]
    assert search("  ", log, [], routines) == []
