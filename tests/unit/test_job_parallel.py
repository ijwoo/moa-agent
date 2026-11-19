"""서로 다른 일은 함께, 같은 자원(브라우저·작업실 폴더)끼리만 차례(사용자 결정 2026-11-18)."""
import threading
import time

from agent.api.jobs import JobManager
from agent.core.loop import RunResult


def test_a_shorts_job_and_a_site_job_run_together_and_two_browser_jobs_take_turns():
    gate, started = threading.Event(), []

    def runner(job, hooks):
        started.append(job.task)
        gate.wait(3)
        return RunResult("completed", "끝")
    m = JobManager(runner)
    try:
        w = m.submit("보고서", skill="workroom", extra={"_project": "p1"})
        a = m.submit("사이트 조회 1", skill="site_do")
        b = m.submit("사이트 조회 2", skill="web_browse")
        w2 = m.submit("같은 폴더에서 또", skill="workroom", extra={"_project": "p1"})
        end = time.time() + 2
        while len(started) < 2 and time.time() < end:
            time.sleep(0.01)
        time.sleep(0.05)
        assert sorted(started) == ["보고서", "사이트 조회 1"]                          # 작업실과 브라우저 일은 함께
        assert m.get(b.id).public()["wait_reason"] == "브라우저를 쓰는 앞 작업이 끝나면 시작해요"
        assert m.get(w2.id).public()["wait_reason"] == "같은 작업실에서 만드는 앞 작업이 끝나면 시작해요"
        gate.set()
        end = time.time() + 3
        while any(m.get(j.id).status != "completed" for j in (w, a, b, w2)) and time.time() < end:
            time.sleep(0.01)
        assert all(m.get(j.id).status == "completed" for j in (w, a, b, w2)) and "wait_reason" not in m.get(b.id).public()
    finally:
        m.shutdown()


def test_cancelling_a_waiting_job_frees_its_turn():
    gate = threading.Event()
    m = JobManager(lambda job, hooks: (gate.wait(3), RunResult("completed", "끝"))[1])
    try:
        a = m.submit("하나", skill="site_do")
        b = m.submit("둘", skill="site_do")
        m.cancel(b.id)
        gate.set()
        end = time.time() + 3
        while m.get(b.id).status not in ("cancelled",) and time.time() < end:
            time.sleep(0.01)
        assert m.get(b.id).status == "cancelled"
    finally:
        m.shutdown()


def test_a_reply_for_a_queued_task_says_why_it_waits():
    from agent.api.dialog import _stamp
    out = _stamp({"replies": [{"text": "해 볼게요", "by": "claude"}],
                  "action": {"type": "task", "task": {"status": "queued", "wait_reason": "브라우저를 쓰는 앞 작업이 끝나면 시작해요"}}})
    assert out["replies"][-1] == {"text": "브라우저를 쓰는 앞 작업이 끝나면 시작해요.", "by": "mac"}
    out = _stamp({"replies": [{"text": "해 볼게요"}], "action": {"type": "task", "task": {"status": "running"}}})
    assert len(out["replies"]) == 1
