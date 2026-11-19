import threading
import time
from types import SimpleNamespace as NS

import httpx
import pytest

from agent.api.jobs import JobManager
from agent.api.server import make_server
from agent.core.approval import draft_sha256

TOKEN = "t" * 32
H = {"X-App-Token": TOKEN}
H2 = {**H, "X-Api-Version": "2"}   # 3단계 질문 방식을 켠 앱


def draft_runner(job, hooks):
    """가짜 에이전트: 한 단계 진행 → 초안 승인 요청 → (거부 사유면 고쳐서 다시 요청) → 완료."""
    if job.use_plan:
        hooks.set_plan(["받은편지함 확인", "초안 작성"])
    hooks.on_step(NS(tool="navigate", args={"url": "http://mail.test/inbox"}, result_status="ok", check_status="ok",
                     url="http://mail.test/inbox"))
    body = "첫 초안"
    for _ in range(3):
        if hooks.cancel_check():
            return NS(status="cancelled", summary="취소")
        d = hooks.approver(NS(element_label="임시 저장", url="http://mail.test/compose", kind="draft",
                              form_values={"받는 사람": "test-b@mail.test", "제목": "확인 요청", "본문": body}))
        if d is True:
            return NS(status="completed", summary=f"초안 저장: {body}")
        if d is False:
            return NS(status="approval_denied", summary="거부됨")
        body = f"고친 초안({d})"
    return NS(status="approval_denied", summary="거부 반복")


@pytest.fixture
def api():
    mgr = JobManager(draft_runner, max_queue=1, approval_timeout_s=5)
    srv = make_server(mgr, TOKEN, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", mgr
    mgr.shutdown()
    srv.shutdown()


def wait_status(base, jid, want, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        j = httpx.get(f"{base}/v1/tasks/{jid}", headers=H).json()
        if j["status"] in want:
            return j
        time.sleep(0.05)
    raise AssertionError(f"{want} 안 됨: {j}")


def test_binds_loopback_only_and_requires_token(api):
    base, _ = api
    assert base.startswith("http://127.0.0.1:")
    assert httpx.get(f"{base}/v1/health").status_code == 401
    r = httpx.get(f"{base}/v1/health", headers={"X-App-Token": "wrong" * 8})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    ok = httpx.get(f"{base}/v1/health", headers=H)
    h = ok.json()
    assert ok.status_code == 200 and h["ok"] is True and h["scope"] == "read_draft" and h["api_version"] == 2
    assert {"hash_approval", "task_questions", "task_report", "narration"} <= set(h["features"])
    assert "messages" not in h["features"] and "client_message_id" not in h["features"] and "tone" not in h["features"] and "send_self" not in h["features"] and "push" not in h["features"]
    assert ok.headers["cache-control"] == "no-store"
    with pytest.raises(ValueError):
        make_server(JobManager(draft_runner), "short")


def test_full_flow_reject_with_reason_then_approve(api):
    base, _ = api
    r = httpx.post(f"{base}/v1/tasks", headers=H, json={"task": "자료 왔는지 확인하고 없으면 초안", "plan": True,
                                                        "context": "비밀 회의 발췌"})
    assert r.status_code == 202
    jid = r.json()["id"]
    j = wait_status(base, jid, {"awaiting_approval"})
    assert j["plan"] == ["받은편지함 확인", "초안 작성"] and j["steps"][0]["tool"] == "navigate"
    ap = j["approval"]
    assert ap["kind"] == "draft" and ap["action"] == "임시 저장" and ap["fields"]["본문"] == "첫 초안"
    assert "비밀 회의 발췌" not in str(j)                      # 앱이 보낸 발췌는 응답에 되돌려 주지 않는다
    bad = httpx.post(f"{base}/v1/tasks/{jid}/approval", headers=H, json={"approval_id": "nope", "decision": "approve"})
    assert bad.status_code == 409
    rej = httpx.post(f"{base}/v1/tasks/{jid}/approval", headers=H,
                     json={"approval_id": ap["approval_id"], "decision": "reject", "reason": "더 정중하게"})
    assert rej.status_code == 200
    for _ in range(100):                                       # 새 승인 요청(다른 approval_id)이 올 때까지
        j2 = httpx.get(f"{base}/v1/tasks/{jid}", headers=H).json()
        if j2["status"] == "awaiting_approval" and j2["approval"]["approval_id"] != ap["approval_id"]:
            break
        time.sleep(0.05)
    assert j2["approval"]["fields"]["본문"] == "고친 초안(더 정중하게)"
    ap2 = j2["approval"]
    assert ap2["sha256"] != ap["sha256"] and ap2["draft_id"] != ap["draft_id"]
    httpx.post(f"{base}/v1/tasks/{jid}/approval", headers=H,
               json={"approval_id": ap2["approval_id"], "decision": "approve", "sha256": ap2["sha256"]})
    done = wait_status(base, jid, {"completed"})
    assert done["result"]["outcome"] == "completed" and "approval" not in done
    fields = {"받는 사람": "test-b@mail.test", "제목": "확인 요청", "본문": "고친 초안(더 정중하게)"}
    assert done["result"]["approved_drafts"] == [{"draft_id": ap2["draft_id"], "sha256": ap2["sha256"],
        "kind": "draft", "action": "임시 저장", "url": "http://mail.test/compose", "fields": fields}]
    assert ap2["sha256"] == draft_sha256("draft", "임시 저장", "http://mail.test/compose", fields)
    assert httpx.post(f"{base}/v1/tasks/{jid}/approval", headers=H, json={"approval_id": "x", "decision": "approve"}).status_code == 409
    assert httpx.get(f"{base}/v1/tasks", headers=H).json()["tasks"][0]["id"] == jid


def test_reject_without_reason_fails_and_expiry(api):
    base, mgr = api
    jid = httpx.post(f"{base}/v1/tasks", headers=H, json={"task": "초안"}).json()["id"]
    ap = wait_status(base, jid, {"awaiting_approval"})["approval"]
    httpx.post(f"{base}/v1/tasks/{jid}/approval", headers=H, json={"approval_id": ap["approval_id"], "decision": "reject"})
    j = wait_status(base, jid, {"failed"})
    assert j["result"]["outcome"] == "approval_denied"
    mgr.approval_timeout_s = 0.3
    jid2 = httpx.post(f"{base}/v1/tasks", headers=H, json={"task": "초안"}).json()["id"]
    assert wait_status(base, jid2, {"failed"})["result"]["outcome"] == "approval_expired"


def test_cancel_queue_limit_and_errors(api):
    base, _ = api
    a = httpx.post(f"{base}/v1/tasks", headers=H, json={"task": "1"}).json()["id"]
    wait_status(base, a, {"awaiting_approval"})
    b = httpx.post(f"{base}/v1/tasks", headers=H, json={"task": "2"})
    assert b.status_code == 202 and b.json()["status"] == "queued"
    busy = httpx.post(f"{base}/v1/tasks", headers=H, json={"task": "3"})
    assert busy.status_code == 429 and busy.json()["error"]["code"] == "busy"
    assert httpx.post(f"{base}/v1/tasks/{a}/cancel", headers=H).status_code == 202
    assert wait_status(base, a, {"cancelled"})["result"]["outcome"] == "cancelled"
    assert httpx.post(f"{base}/v1/tasks/{a}/cancel", headers=H).status_code == 409
    httpx.post(f"{base}/v1/tasks/{b.json()['id']}/cancel", headers=H)
    for body, code in [({"task": ""}, "invalid_request"), ({"task": "x" * 3000}, "invalid_request"),
                       ({"task": "x", "context": 5}, "invalid_request"), ({"task": "x", "plan": "yes"}, "invalid_request")]:
        r = httpx.post(f"{base}/v1/tasks", headers=H, json=body)
        assert r.status_code == 400 and r.json()["error"]["code"] == code
    assert httpx.post(f"{base}/v1/tasks", headers=H, content=b"{nope").json()["error"]["code"] == "invalid_json"
    assert httpx.post(f"{base}/v1/tasks", headers=H, content=b"x" * 70000).status_code == 413
    assert httpx.get(f"{base}/v1/tasks/doesnotexist12", headers=H).json()["error"]["code"] == "task_not_found"
    assert httpx.get(f"{base}/v1/nope", headers=H).status_code == 404
    assert httpx.post(f"{base}/v1/health", headers=H).status_code == 404


def test_needs_input_and_internal_error_and_auth_lockout():
    def asker(job, hooks):
        if job.task == "boom":
            raise RuntimeError("secret detail")
        return NS(status="needs_user", summary="누구에게?", question="누구에게 보낼까요?")
    mgr = JobManager(asker)
    srv = make_server(mgr, TOKEN, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        j = wait_status(base, httpx.post(f"{base}/v1/tasks", headers=H, json={"task": "메일 써줘"}).json()["id"], {"needs_input"})
        assert j["result"]["question"] == "누구에게 보낼까요?"
        e = wait_status(base, httpx.post(f"{base}/v1/tasks", headers=H, json={"task": "boom"}).json()["id"], {"failed"})
        assert e["result"] == {"outcome": "internal_error", "summary": "내부 오류: RuntimeError"} and "secret" not in str(e)
        for _ in range(10):
            assert httpx.get(f"{base}/v1/health").status_code == 401
        locked = httpx.get(f"{base}/v1/health", headers=H)       # 올바른 토큰이어도 잠금 중에는 429
        assert locked.status_code == 429 and locked.json()["error"]["code"] == "too_many_auth_failures"
    finally:
        mgr.shutdown()
        srv.shutdown()


def test_hash_approval_rejects_wrong_sha_and_double_answer(api):
    """D42: 승인에 실린 sha256이 고정 초안과 다르면 409 hash_mismatch, 실행되지 않는다. 같은 승인에 두 번 답하면 409."""
    base, _ = api
    jid = httpx.post(f"{base}/v1/tasks", headers=H, json={"task": "초안"}).json()["id"]
    ap = wait_status(base, jid, {"awaiting_approval"})["approval"]
    assert len(ap["sha256"]) == 64 and ap["draft_id"].startswith("d_")
    url = f"{base}/v1/tasks/{jid}/approval"
    r = httpx.post(url, headers=H, json={"approval_id": ap["approval_id"], "decision": "approve", "sha256": "0" * 64})
    assert r.status_code == 409 and r.json()["error"]["code"] == "hash_mismatch"
    assert httpx.post(url, headers=H, json={"approval_id": ap["approval_id"], "decision": "approve",
                                            "sha256": "XYZ"}).status_code == 400
    assert httpx.get(f"{base}/v1/tasks/{jid}", headers=H).json()["status"] == "awaiting_approval"  # 아직 대기
    ok = httpx.post(url, headers=H, json={"approval_id": ap["approval_id"], "decision": "approve", "sha256": ap["sha256"]})
    assert ok.status_code == 200
    # 승인 직후 응답은 아직 같은 승인 대기 상태일 수 있다 — 받은 응답은 received로 표시된다
    body = ok.json()
    if body["status"] == "awaiting_approval":
        assert body["approval"]["approval_id"] == ap["approval_id"] and body["approval"]["received"] == "approve"
        again = httpx.post(url, headers=H, json={"approval_id": ap["approval_id"], "decision": "approve"})
        assert again.status_code == 409
    done = wait_status(base, jid, {"completed"})
    assert done["result"]["approved_drafts"][0]["sha256"] == ap["sha256"]


def test_approver_refuses_when_approved_hash_differs():
    """decide를 거치지 않고 해시가 어긋난 경우에도(방어 2중) 승인으로 넘기지 않는다."""
    import threading as th
    seen = {}

    def runner(job, hooks):
        seen["d"] = hooks.approver(NS(element_label="임시 저장", url="u", kind="draft", form_values={"본문": "a"}))
        return NS(status="completed" if seen["d"] is True else "approval_denied", summary="")

    mgr = JobManager(runner, approval_timeout_s=5)
    job = mgr.submit("x")
    for _ in range(100):
        if job.status == "awaiting_approval":
            break
        time.sleep(0.02)
    job._approved_sha, job._decision = "f" * 64, True   # 다른 해시로 승인된 상황을 흉내
    job._event.set()
    for _ in range(100):
        if job.status in ("completed", "failed"):
            break
        time.sleep(0.02)
    assert seen["d"] is False and job.result["approved_drafts"] == []
    mgr.shutdown()


def question_runner(job, hooks):
    """가짜 작업 담당: 한 단계 → 막혀서 질문 → 답(또는 시간 초과 안내)을 받아 끝냄."""
    hooks.on_step(NS(tool="navigate", args={"url": "http://mail.test/inbox"}, result_status="ok", check_status="ok",
                     url="http://mail.test/inbox", label=None))
    hooks.on_step(NS(tool="type", args={"ref": "e3", "text": "비밀 본문"}, result_status="ok", check_status="ok",
                     url="http://mail.test/compose", label="본문"))
    a = hooks.ask("어느 분기 자료인가요?", ["3분기", "4분기"])
    if a is None:   # v1 앱의 작업: 질문하지 않고 needs_input으로 끝남
        return NS(status="needs_user", summary="어느 분기 자료인가요?", question="어느 분기 자료인가요?",
                  question_choices=["3분기", "4분기"])
    if a.startswith("사용자가"):
        return NS(status="completed", summary="일부만 함", report={"done": ["받은편지함 확인"], "blocked": []})
    return NS(status="completed", summary=f"{a} 자료 확인", report={"done": [f"{a} 자료 확인"], "blocked": []})


@pytest.fixture
def qapi():
    mgr = JobManager(question_runner, approval_timeout_s=5)
    srv = make_server(mgr, TOKEN, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", mgr
    mgr.shutdown()
    srv.shutdown()


def test_task_question_answer_flow_with_narration_and_report(qapi):
    base, _ = qapi
    jid = httpx.post(f"{base}/v1/tasks", headers=H2, json={"task": "자료 왔는지 확인해 줘. 아주 긴 요청 문장이 들어가면 제목은 잘린다"}).json()["id"]
    j = wait_status(base, jid, {"awaiting_input"})
    q = j["question"]
    assert q["text"] == "어느 분기 자료인가요?" and q["choices"] == ["3분기", "4분기"] and q["expires_in_s"] == 5
    assert j["title"].endswith("…") and len(j["title"]) == 30                       # v1 작업도 짧은 제목
    texts = [n["text"] for n in j["narration"]]
    assert texts[:3] == ["작업을 시작했어요", "테스트 메일을 열었어요", "'본문' 칸을 채웠어요"]
    assert "'본문' 칸을 채웠어요" in texts and "비밀 본문" not in str(j["narration"])  # 입력 내용은 진행 설명에 없음
    url = f"{base}/v1/tasks/{jid}/answer"
    assert httpx.post(url, headers=H, json={"question_id": "x", "answer": "3분기"}).status_code == 409
    assert httpx.post(url, headers=H, json={"question_id": q["question_id"], "answer": ""}).status_code == 400
    r = httpx.post(url, headers=H, json={"question_id": q["question_id"], "answer": "3분기"})
    assert r.status_code == 200
    done = wait_status(base, jid, {"completed"})
    rep = done["result"]["report"]
    assert rep["done"] == ["3분기 자료 확인"] and rep["blocked"] == []
    assert rep["sources"] == ["http://mail.test/inbox", "http://mail.test/compose"]
    assert done["narration"][-1]["text"] == "작업을 마쳤어요" and "question" not in done


def test_unanswered_question_continues_after_timeout(qapi):
    base, mgr = qapi
    mgr.approval_timeout_s = 0.3
    jid = httpx.post(f"{base}/v1/tasks", headers=H2, json={"task": "자료 확인"}).json()["id"]
    done = wait_status(base, jid, {"completed"})
    assert done["result"]["summary"] == "일부만 함"
    assert done["result"]["report"]["blocked"] == ["답을 받지 못한 질문: 어느 분기 자료인가요?"]
    assert any("할 수 있는 부분부터" in n["text"] for n in done["narration"])


def test_v1_app_without_version_header_never_sees_awaiting_input(qapi):
    """배포된 v1 앱(빌드 21까지)은 모르는 상태를 실패로 처리한다 → 헤더가 없으면 예전처럼 needs_input으로 끝낸다."""
    base, _ = qapi
    jid = httpx.post(f"{base}/v1/tasks", headers=H, json={"task": "자료 확인"}).json()["id"]
    seen = set()
    for _ in range(100):
        j = httpx.get(f"{base}/v1/tasks/{jid}", headers=H).json()
        seen.add(j["status"])
        if j["status"] in ("needs_input", "failed", "completed", "cancelled"):
            break
        time.sleep(0.05)
    assert "awaiting_input" not in seen and j["status"] == "needs_input"
    assert j["result"]["question"] == "어느 분기 자료인가요?" and j["result"]["choices"] == ["3분기", "4분기"]
    assert httpx.post(f"{base}/v1/tasks/{jid}/answer", headers=H,
                      json={"question_id": "x", "answer": "3분기"}).status_code == 409


def test_later_api_version_includes_v2_questions(qapi):
    """나중 버전(3 = 발송, 설계 중)은 앞 버전을 포함한다. 숫자가 아니거나 1이면 v1."""
    base, _ = qapi
    for ver, expect in (("3", "awaiting_input"), ("1", "needs_input"), ("abc", "needs_input")):
        jid = httpx.post(f"{base}/v1/tasks", headers={**H, "X-Api-Version": ver}, json={"task": "자료 확인"}).json()["id"]
        j = wait_status(base, jid, {"awaiting_input", "needs_input", "failed", "completed"})
        assert j["status"] == expect, ver
        if expect == "awaiting_input":
            httpx.post(f"{base}/v1/tasks/{jid}/cancel", headers=H)
            wait_status(base, jid, {"cancelled", "completed", "failed", "needs_input"})


def test_tasks_can_be_listed_by_conversation(api):
    base, mgr = api
    a = mgr.submit("작업 A", conversation_id="c_abcdefgh12")
    mgr.submit("작업 B")
    got = httpx.get(f"{base}/v1/tasks", headers=H, params={"conversation_id": "c_abcdefgh12"}).json()["tasks"]
    assert [t["id"] for t in got] == [a.id] and got[0]["conversation_id"] == "c_abcdefgh12"
    assert httpx.get(f"{base}/v1/tasks", headers=H, params={"conversation_id": "../x"}).status_code == 400


def test_narration_particles():
    from agent.api.jobs import narrate
    n = lambda tool, **kw: narrate(NS(tool=tool, result_status="ok", check_status="ok", args=kw.get("args", {}), label=kw.get("label")))
    assert n("navigate", args={"url": "http://shop.test/"}) == "테스트 쇼핑몰을 열었어요"
    assert n("click", label="임시 저장") == "'임시 저장'을 눌렀어요"
    assert n("click", label="보내기") == "'보내기'를 눌렀어요"
    assert n("observe") is None


def test_verify_failed_report_hides_unverified_done():
    def runner(job, hooks):
        return NS(status="verify_failed", summary="임시 저장했습니다 [검증 실패: 저장 버튼을 누른 적이 없다]",
                  report={"done": ["초안 임시 저장"], "blocked": []})
    mgr = JobManager(runner)
    try:
        j = mgr.submit("초안 저장")
        for _ in range(100):
            if j.status == "failed":
                break
            time.sleep(0.02)
        rep = j.result["report"]
        assert j.result["outcome"] == "verify_failed" and rep["done"] == []
        assert rep["blocked"] == ["완료 보고가 검증을 통과하지 못함: 저장 버튼을 누른 적이 없다"]
    finally:
        mgr.shutdown()


def test_approval_card_carries_the_screen_it_is_about_when_the_runner_left_one():
    """실기기 2026-11-16: 어느 리뷰를 고치려는지 화면을 못 본 채 승인을 물었다. 멈춘 그때의 화면이 카드에 붙는다(없으면 안 붙는다)."""
    JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60

    def runner(job, hooks):
        job._pause_shot = {"mime": "image/jpeg", "data": JPEG, "page_host": "shop.test", "caption": "쇼핑 화면", "site": "shop"}
        ok = hooks.approver(NS(element_label="수정", url="http://shop.test/review", kind="action", form_values={"리뷰": "좋아요ㅎ"}))
        ok2 = hooks.approver(NS(element_label="닫기", url="http://shop.test/review", kind="action", form_values={}))
        return NS(status="completed" if ok and ok2 else "approval_denied", summary="끝")
    mgr = JobManager(runner, approval_timeout_s=5)
    srv = make_server(mgr, TOKEN, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        jid = httpx.post(f"{base}/v1/tasks", headers=H, json={"task": "리뷰 고쳐줘"}).json()["id"]
        ap = wait_status(base, jid, {"awaiting_approval"})["approval"]
        at = ap["attachment"]
        assert at["type"] == "screenshot" and at["page_host"] == "shop.test" and at["path"] == f"/v1/tasks/{jid}/attachments/{at['id']}"
        got = httpx.get(base + at["path"], headers=H)
        assert got.status_code == 200 and got.content == JPEG
        httpx.post(f"{base}/v1/tasks/{jid}/approval", headers=H, json={"approval_id": ap["approval_id"], "decision": "approve", "sha256": ap["sha256"]})
        for _ in range(100):
            j = httpx.get(f"{base}/v1/tasks/{jid}", headers=H).json()
            if j["status"] == "awaiting_approval" and j["approval"]["approval_id"] != ap["approval_id"]:
                break
            time.sleep(0.05)
        assert "attachment" not in j["approval"]            # 화면을 남기지 않은 승인에는 붙지 않는다(앞의 것을 다시 쓰지 않는다)
    finally:
        mgr.shutdown()
        srv.shutdown()


