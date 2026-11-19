"""예약해 둔 일(루틴, D80): 때 읽기, 때가 됐는지, 놓친 때, 대화로 만들기·끄기, 도는 중에는 예약을 못 만든다. 합성 데이터만."""

import json
import stat
import threading
import time
from datetime import datetime

import httpx
import pytest

from agent.api.dialog import DialogManager
from agent.api.jobs import JobManager
from agent.api.routines import RoutineError, RoutineStore, Scheduler, describe, parse_when
from agent.api.server import make_server
from agent.core.loop import RunResult
from agent.providers.base import ModelResponse

NOW = datetime(2026, 11, 16, 6, 0)          # 월요일 06:00
TOKEN = "t" * 32
H = {"X-App-Token": TOKEN}


def ts(*a):
    return datetime(*a).timestamp()


def test_when_is_read_only_in_fixed_shapes():
    assert parse_when("daily 7:30") == {"kind": "daily", "times": ["07:30"], "days": [], "at": ""}
    assert parse_when("Daily 17:30,11:00,11:00")["times"] == ["11:00", "17:30"]
    assert parse_when("weekly mon,wed,fri 09:00")["days"] == [0, 2, 4]
    assert parse_when("once 2026-12-05 10:00", NOW)["at"] == "2026-12-05T10:00"
    for bad in ("", "매일 아침", "daily", "daily 25:00", "daily 07:30,08:00,09:00,10:00,11:00", "weekly funday 09:00", "weekly 09:00",
                "once 2026-13-40 10:00", "once tomorrow 10:00", "daily 07:30; rm -rf", "hourly 00:10"):
        with pytest.raises(RoutineError) as e:
            parse_when(bad, NOW)
        assert e.value.code == "invalid_when", bad
    with pytest.raises(RoutineError) as e:
        parse_when("once 2026-11-16 05:00", NOW)
    assert e.value.code == "past"
    assert describe(parse_when("daily 07:31")) == "매일 오전 7:31"
    assert describe(parse_when("weekly mon,wed 17:30")) == "매주 월·수 오후 5:30"
    assert describe(parse_when("once 2026-12-05 10:00", NOW)) == "2026년 12월 5일 오전 10:00에 한 번"


def make(tmp_path, now):
    clock = {"t": now.timestamp()}
    store = RoutineStore(tmp_path / "routines.json", clock=lambda: clock["t"])
    ran, notes = [], []

    def run(r):
        ran.append(r.id)
        return {"status": run.status, "conversation_id": "c1", "replies": [{"text": "합성 답"}]}
    run.status = "done"
    return store, Scheduler(store, run, notes.append, clock=lambda: clock["t"], busy_retry_s=120), clock, ran, notes


def test_due_routines_run_once_and_missed_ones_are_skipped(tmp_path):
    store, sch, clock, ran, notes = make(tmp_path, NOW)
    daily = store.add("아침 브리핑", "오늘 온 메일 요약해줘", parse_when("daily 07:30,17:30"))
    week = store.add("주간", "주간 보고 알려줘", parse_when("weekly tue 09:00"))
    once = store.add("확인", "구독 확인해줘", parse_when("once 2026-11-16 08:00", NOW))
    assert sch.tick() == [] and ran == []                                    # 만든 때보다 앞선 때는 돌리지 않는다
    clock["t"] = ts(2026, 11, 16, 7, 30, 5)
    assert [r["status"] for r in sch.tick()] == ["done"] and ran == [daily.id]
    assert sch.tick() == [] and ran == [daily.id]                            # 같은 때를 두 번 돌리지 않는다
    assert notes[0]["routine_id"] == daily.id and notes[0]["replies"] == [{"text": "합성 답"}]
    clock["t"] = ts(2026, 11, 16, 8, 0, 30)
    sch.tick()
    assert ran == [daily.id, once.id] and store.get(once.id).public(clock["t"])["next_run_at"] is None   # 한 번짜리는 다시 돌지 않는다
    clock["t"] = ts(2026, 11, 17, 9, 0, 10)                                  # 화요일 09:00 — 맥이 그동안 꺼져 있었다(월 17:30, 화 07:30을 놓침)
    got = {r["routine_id"]: r["status"] for r in sch.tick()}
    assert got == {daily.id: "missed", week.id: "done"} and ran[-1] == week.id   # 한 시간 넘게 지난 때는 돌리지 않고 놓쳤다고 적는다
    assert store.get(daily.id).last_status == "missed"
    clock["t"] = ts(2026, 11, 17, 17, 50)                                    # 20분 늦음: 늦게라도 한 번 돌린다
    assert [r["status"] for r in sch.tick()] == ["done"]
    # 파일에는 예약만(0600), 돌린 결과는 없다
    f = tmp_path / "routines.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and "합성 답" not in f.read_text()
    assert [r.title for r in RoutineStore(f).list()] == ["아침 브리핑", "주간", "확인"]


def test_turned_off_routines_do_not_run_and_turning_on_does_not_replay_the_past(tmp_path):
    store, sch, clock, ran, _ = make(tmp_path, NOW)
    r = store.add("아침", "날씨 알려줘", parse_when("daily 07:00"))
    store.update(r.id, enabled=False)
    clock["t"] = ts(2026, 11, 16, 7, 0, 10)
    assert sch.tick() == []
    clock["t"] = ts(2026, 11, 16, 7, 20)
    store.update(r.id, enabled=True)
    assert sch.tick() == [] and ran == []                                    # 꺼져 있던 동안 지난 때를 켜자마자 돌리지 않는다
    assert store.get(r.id).public(clock["t"])["next_run_at"].startswith("2026-11-17T07:00")


def test_busy_is_retried_later_and_limits_hold(tmp_path):
    store, sch, clock, ran, _ = make(tmp_path, NOW)
    r = store.add("아침", "날씨 알려줘", parse_when("daily 07:00"))
    clock["t"] = ts(2026, 11, 16, 7, 0, 5)
    sch.run.status = "busy"
    assert sch.tick() == [] and ran == [r.id]
    clock["t"] += 60
    assert sch.tick() == [] and ran == [r.id]                                # 아직 다시 해 볼 때가 아니다
    clock["t"] += 70
    sch.run.status = "done"
    assert [x["status"] for x in sch.tick()] == ["done"] and ran == [r.id, r.id]
    for i in range(19):
        store.add(f"예약 {i}", "날씨 알려줘", parse_when("daily 07:00"))
    with pytest.raises(RoutineError) as e:
        store.add("넘침", "날씨 알려줘", parse_when("daily 07:00"))
    assert e.value.code == "too_many"
    with pytest.raises(RoutineError):
        store.add("빈 것", "   ", parse_when("daily 07:00"))


# ---------------------------------------------------------------- 대화로 만들기 · 돌리기
class CloudSeq:
    def __init__(self, *outs):
        self.outs, self.seen, self.model = list(outs), [], "claude-test"

    def generate(self, messages, json_schema=None, **kw):
        self.seen.append((messages, json_schema))
        base = {"action": "reply", "replies": ["네!"], "choices": [], "title": "", "to": [], "purpose": "", "targets": [],
                "instructions": "", "sources": [], "target": "", "question": ""}
        return ModelResponse(text=json.dumps({**base, **self.outs.pop(0)}, ensure_ascii=False), latency_ms=1, model="c", provider="claude")


class Local:
    def generate(self, messages, **kw):
        return ModelResponse(text=json.dumps({"type": "none", "reply": "네.", "choices": [], "title": "", "purpose": "",
                                              "targets": [], "instructions": ""}), latency_ms=1, model="m", provider="p")


def dialog(tmp_path, *outs):
    mgr = JobManager(lambda job, hooks: RunResult("completed", "했다", report={"done": [], "blocked": []}))
    dlg = DialogManager(Local(), mgr)
    dlg.cloud, dlg.real_only = CloudSeq(*outs), True
    dlg.routines = RoutineStore(tmp_path / "routines.json")
    return dlg, mgr


def test_a_routine_is_made_listed_turned_off_and_deleted_by_talking(tmp_path):
    dlg, mgr = dialog(tmp_path,
                      {"action": "routine", "target": "add", "title": "아침 브리핑", "question": "오늘 온 메일 요약해줘", "when": "daily 07:30", "replies": ["지어낸 말"]},
                      {"action": "routine", "target": "add", "title": "모호", "question": "날씨 알려줘", "when": "아침마다"},
                      {"action": "routine", "target": "off", "title": "아침 브리핑"},
                      {"action": "routine", "target": "list"},
                      {"action": "routine", "target": "delete", "title": "없는 것"},
                      {"action": "routine", "target": "delete", "title": "브리핑"})
    try:
        r = dlg.handle("매일 아침 7시 반에 메일 브리핑해줘")
        assert r["replies"][0]["by"] == "mac" and "매일 오전 7:30" in r["replies"][0]["text"] and "지어낸 말" not in r["replies"][0]["text"]
        assert r["replies"][0]["text"] == "예약했어요. 매일 오전 7:30" and [x["title"] for x in r["routines"]] == ["아침 브리핑"]   # 주의 문구는 붙이지 않는다
        cid = r["conversation_id"]
        system, schema = dlg.cloud.seen[0][0][0].content, dlg.cloud.seen[0][1]
        assert "routine —" in system and "routine" in schema["properties"]["action"]["enum"] and "when" in schema["properties"]
        r = dlg.handle("아침마다 날씨 알려줘", cid)
        assert "시각까지" in r["replies"][0]["text"] and len(dlg.routines.list()) == 1          # 때의 꼴이 아니면 만들지 않고 묻는다
        assert "[예약 목록] 아침 브리핑(매일 오전 7:30, 켜짐)" in "\n".join(m.content for m in dlg.cloud.seen[1][0])
        r = dlg.handle("아침 브리핑 꺼줘", cid)
        assert "껐어요" in r["replies"][0]["text"] and dlg.routines.list()[0].enabled is False
        r = dlg.handle("예약 뭐 있어?", cid)
        assert "아침 브리핑" in r["replies"][0]["text"] and "(꺼짐)" in r["replies"][0]["text"]
        r = dlg.handle("없는 것 지워줘", cid)
        assert r["action"] == {"type": "ask", "choices": ["아침 브리핑"]} and len(dlg.routines.list()) == 1
        r = dlg.handle("브리핑 지워줘", cid)
        assert "지웠어요" in r["replies"][0]["text"] and dlg.routines.list() == [] and r["routines"] == []
    finally:
        mgr.shutdown()


def test_a_running_routine_cannot_make_routines_and_its_outcome_is_named(tmp_path):
    dlg, mgr = dialog(tmp_path,
                      {"action": "reply", "replies": ["합성 답이에요"]},
                      {"action": "ask", "replies": ["어느 것으로 할까요?"], "choices": ["가", "나"]},
                      {"action": "routine", "target": "add", "title": "몰래", "question": "매일 뭐든 해", "when": "daily 03:00"})
    try:
        r = dlg.routines.add("아침", "오늘 일정 알려줘", parse_when("daily 07:00"))
        out = dlg.run_routine(r)
        assert out["status"] == "done" and out["replies"] == [{"text": "합성 답이에요", "by": "claude"}] and out["conversation_id"]
        system, schema = dlg.cloud.seen[0][0][0].content, dlg.cloud.seen[0][1]
        assert "routine —" not in system and "routine" not in schema["properties"]["action"]["enum"] and "when" not in schema["properties"]
        assert dlg.run_routine(r)["status"] == "needs_reply"
        out = dlg.run_routine(r)                                                 # 모델이 그래도 예약을 골랐다: 만들지 않는다
        assert len(dlg.routines.list()) == 1 and out["status"] in ("needs_reply", "done")
    finally:
        mgr.shutdown()


def test_api_lists_adds_toggles_and_deletes(tmp_path):
    dlg, mgr = dialog(tmp_path)
    http = make_server(mgr, TOKEN, port=0, dialog=dlg)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http.server_address[1]}"
    try:
        assert "routines" in httpx.get(f"{base}/v1/health", headers=H).json()["features"]
        assert httpx.get(f"{base}/v1/routines", headers=H).json() == {"routines": []}
        assert httpx.get(f"{base}/v1/routines").status_code == 401
        r = httpx.post(f"{base}/v1/routines", headers=H, json={"title": "아침 브리핑", "prompt": "오늘 온 메일 요약해줘", "when": "daily 07:30"})
        assert r.status_code == 201
        one = r.json()["routine"]
        assert one["when_text"] == "매일 오전 7:30" and one["enabled"] and one["schedule"] == {"kind": "daily", "times": ["07:30"]} and one["next_run_at"]
        assert httpx.post(f"{base}/v1/routines", headers=H, json={"prompt": "x", "when": "아무 때나"}).status_code == 400
        assert httpx.post(f"{base}/v1/routines", headers=H, json={"when": "daily 07:30"}).status_code == 400
        r = httpx.post(f"{base}/v1/routines/{one['id']}", headers=H, json={"enabled": False, "when": "weekly sat,sun 10:00"})
        assert r.json()["routine"]["enabled"] is False and r.json()["routine"]["when_text"] == "매주 토·일 오전 10:00" and r.json()["routine"]["next_run_at"] is None
        assert httpx.post(f"{base}/v1/routines/rt_nothere1", headers=H, json={"enabled": True}).status_code == 404
        assert httpx.get(f"{base}/v1/routines/runs", headers=H).json() == {"runs": []}
        assert httpx.delete(f"{base}/v1/routines/{one['id']}", headers=H).json() == {"routines": []}
    finally:
        mgr.shutdown()
        http.shutdown()


def test_run_history_survives_a_restart_and_goes_away_with_its_routine(tmp_path):
    """실행 기록은 파일(0600)에 남는다 — 그때의 답도(사용자 결정 2026-11-16). 예약을 지우면 그 기록도 지운다."""
    store, sched, clock, ran, notes = make(tmp_path, NOW)
    path = tmp_path / "runs.json"
    sched.runs_path = path
    a = store.add("아침", "오늘 온 메일 요약해줘", parse_when("daily 07:00"))
    b = store.add("저녁", "내일 일정 알려줘", parse_when("daily 07:00"))
    clock["t"] = ts(2026, 11, 16, 7, 0, 30)
    assert len(sched.tick()) == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = Scheduler(store, lambda r: {"status": "done"}, clock=lambda: clock["t"], runs_path=path)   # 서버가 다시 떴다
    mine = again.recent(routine_id=a.id)
    assert len(again.recent()) == 2 and len(mine) == 1 and mine[0]["replies"] == [{"text": "합성 답"}]
    assert again.find_run(mine[0]["id"])["title"] == "아침" and again.find_run("rr_nothere") is None
    again.runs[0]["task_id"] = "job12345"                                       # 예약이 만든 작업이 끝나면 그 결과가 기록에 붙는다
    again.task_finished("job12345", "completed", ["메일을 보냈어요", ""])
    assert Scheduler(store, lambda r: {}, runs_path=path).runs[0]["task_result"] == ["메일을 보냈어요"]
    again.forget(a.id)
    left = Scheduler(store, lambda r: {}, runs_path=path).recent()
    assert [x["routine_id"] for x in left] == [b.id]


def test_notes_ride_along_when_the_routine_runs_and_can_be_added_by_talking(tmp_path):
    dlg, mgr = dialog(tmp_path,
                      {"action": "routine", "target": "note", "title": "아침", "question": "보안 메일은 맨 위에 말해줘"},
                      {"action": "reply", "replies": ["합성 답이에요"]})
    try:
        r = dlg.routines.add("아침", "오늘 온 메일 요약해줘", parse_when("daily 07:00"), notes="- 광고는 빼기\n\x00- 두 계정 모두")
        assert r.notes == "- 광고는 빼기\n- 두 계정 모두" and r.public()["notes"] == r.notes
        out = dlg.handle("아침 브리핑은 보안 메일을 맨 위에 말해줘", None)
        assert "적어 뒀어요" in out["replies"][0]["text"] and out["routines"][0]["notes"].endswith("- 보안 메일은 맨 위에 말해줘")
        dlg.run_routine(dlg.routines.get(r.id))
        said = dlg.cloud.seen[-1][0][-1].content
        assert "오늘 온 메일 요약해줘" in said and "[이 예약에 내가 적어 둔 지침]" in said and "보안 메일은 맨 위에" in said and "광고는 빼기" in said
        assert RoutineStore(tmp_path / "routines.json").get(r.id).notes == dlg.routines.get(r.id).notes   # 파일에 남는다
        assert len(dlg.routines.update(r.id, notes="가" * 9000).notes) == 4000
    finally:
        mgr.shutdown()


def test_api_takes_notes_and_serves_runs_per_routine(tmp_path):
    dlg, mgr = dialog(tmp_path)
    dlg.scheduler = Scheduler(dlg.routines, lambda r: {"status": "done", "replies": [{"text": "합성 답"}]}, runs_path=tmp_path / "runs.json")
    http = make_server(mgr, TOKEN, port=0, dialog=dlg)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http.server_address[1]}"
    try:
        one = httpx.post(f"{base}/v1/routines", headers=H, json={"title": "아침", "prompt": "메일 요약", "when": "daily 07:30", "notes": "첫 줄\n둘째 줄"}).json()["routine"]
        assert one["notes"] == "첫 줄\n둘째 줄"
        assert httpx.post(f"{base}/v1/routines/{one['id']}", headers=H, json={"notes": ""}).json()["routine"]["notes"] == ""
        assert httpx.post(f"{base}/v1/routines/{one['id']}", headers=H, json={"notes": 3}).status_code == 400
        rec = dlg.scheduler._record(dlg.routines.get(one["id"]), time.time(), "done", {"replies": [{"text": "합성 답"}]})
        runs = httpx.get(f"{base}/v1/routines/runs?routine_id={one['id']}&limit=5", headers=H).json()["runs"]
        assert [x["id"] for x in runs] == [rec["id"]]
        assert httpx.get(f"{base}/v1/routines/runs?routine_id=rt_other123", headers=H).json() == {"runs": []}
        assert httpx.get(f"{base}/v1/routines/runs/{rec['id']}", headers=H).json()["run"]["replies"] == [{"text": "합성 답"}]
        assert httpx.get(f"{base}/v1/routines/runs/rr_nothere1", headers=H).status_code == 404
        httpx.delete(f"{base}/v1/routines/{one['id']}", headers=H)
        assert httpx.get(f"{base}/v1/routines/runs", headers=H).json() == {"runs": []}     # 예약을 지우면 기록도
    finally:
        mgr.shutdown()
        http.shutdown()


def test_a_task_resumed_in_the_same_conversation_fills_the_run_that_stopped(tmp_path):
    """실기기 2026-11-16: 예약이 돌 때 로그인이 풀려 있었다 → 다시 로그인해 이어 읽은 결과가 그 실행 기록에 붙는다(카드가 그것으로 채워진다)."""
    store, sched, clock, ran, notes = make(tmp_path, NOW)
    r = store.add("지원 현황", "지원 현황 알려줘", parse_when("daily 09:00"))
    sched._record(r, clock["t"], "task", {"task_id": "job-first", "conversation_id": "c9"})
    sched.task_finished("job-first", "failed", ["(못 한 것) 로그인이 풀렸어요."])
    assert sched.task_followup("c-other", "job-x", "completed", ["x"]) is None and sched.task_followup("c9", "job-x", "failed", ["x"]) is None
    rec = sched.task_followup("c9", "job-second", "completed", ["합성 공고 3건", ""])
    assert rec["task_id"] == "job-second" and rec["task_status"] == "completed" and rec["task_result"] == ["합성 공고 3건"]
    assert sched.task_followup("c9", "job-third", "completed", ["y"]) is None                  # 이미 채운 기록은 다시 덮지 않는다


def test_several_routines_change_time_together_without_deleting(tmp_path):
    """실기기 2026-11-17: "3개 다 오전 8시로 통일"에 하나만 지우고 끝났다 → 때 바꾸기(when)와 여러 예약 한 번에."""
    dlg, mgr = dialog(tmp_path,
                      {"action": "routine", "target": "add", "title": "아침 브리핑", "question": "메일 요약해줘", "when": "daily 08:00"},
                      {"action": "routine", "target": "add", "title": "결제 내역", "question": "결제 내역 알려줘", "when": "daily 09:00"},
                      {"action": "routine", "target": "when", "title": "*", "when": "daily 08:00"},
                      {"action": "routine", "target": "when", "title": "결제 내역", "when": "daily 07:15"},
                      {"action": "routine", "target": "off", "title": "아침 브리핑 | 결제 내역"})
    try:
        cid = dlg.handle("매일 8시 메일 브리핑")["conversation_id"]
        dlg.handle("매일 9시 결제 내역", cid)
        r = dlg.handle("다 8시로 통일해줘", cid)
        assert r["replies"][0]["text"] == "예약 2개를 매일 오전 8:00로 바꿨어요 · 아침 브리핑, 결제 내역" and r["replies"][0]["kind"] == "status"
        assert [x["title"] for x in r["routines"]] == ["아침 브리핑", "결제 내역"]                     # 지우지 않는다
        r = dlg.handle("결제 내역은 7시 15분으로", cid)
        assert "매일 오전 7:15" in r["replies"][0]["text"]
        r = dlg.handle("둘 다 꺼줘", cid)
        assert "2개를 껐어요" in r["replies"][0]["text"] and not any(x.enabled for x in dlg.routines.list())
    finally:
        mgr.shutdown()
