"""푸시 알림(D44): 알림에는 고정 문장과 id만, 같은 일로는 한 번만, 보내기 실패가 작업을 막지 않는다. 시험용으로 만든 키와 가짜 전송만 쓴다."""

import base64
import json
import threading
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from agent.api.jobs import JobManager
from agent.api.push import TEXT, ApnsClient, DeviceStore, Notifier, payload
from agent.api.server import make_server
from agent.core.loop import RunResult

DEV = "ab" * 32
DEV2 = "cd" * 32


@pytest.fixture
def key():
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return k, pem


def unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Apple:
    """APNs 자리: 받은 요청을 적어 두고 정해 둔 답을 차례로 돌려준다."""

    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []

    def __call__(self, url, headers, body):
        self.seen.append((url, headers, body))
        return self.answers.pop(0) if self.answers else (200, "")


def test_jwt_is_signed_with_the_key_and_reused_for_fifty_minutes(key):
    k, pem = key
    now = [1_000_000.0]
    c = ApnsClient(pem, "KEYID12345", "TEAMID6789", "com.example.app", post=Apple(), clock=lambda: now[0])
    t = c.jwt()
    head, body, sig = t.split(".")
    assert json.loads(unb64(head)) == {"alg": "ES256", "kid": "KEYID12345"} and json.loads(unb64(body)) == {"iss": "TEAMID6789", "iat": 1_000_000}
    raw = unb64(sig)
    k.public_key().verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                          f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))      # 서명이 그 키의 것이다
    now[0] += 49 * 60
    assert c.jwt() == t
    now[0] += 2 * 60
    assert c.jwt() != t


def test_payload_carries_only_a_fixed_sentence_and_ids():
    p = payload("approval_needed", "task12345678", "c_abc")
    assert p == {"aps": {"alert": {"body": "승인이 필요한 일이 있어요"}, "sound": "default", "thread-id": "task12345678", "mutable-content": 1},
                 "type": "approval_needed", "task_id": "task12345678", "conversation_id": "c_abc"}
    assert set(TEXT) == {"approval_needed", "input_needed", "task_done", "task_failed", "needs_login", "routine_result", "login_expired"}


def test_send_headers_and_retry_when_the_provider_token_expired(key):
    apple = Apple((403, "ExpiredProviderToken"), (200, ""))
    c = ApnsClient(key[1], "K", "T", "com.example.app", post=apple)
    assert c.send(DEV, payload("task_done", "t1", None), "t1", 1234.9) == (200, "")
    url, h, body = apple.seen[1]
    assert url == f"https://api.push.apple.com/3/device/{DEV}" and h["apns-topic"] == "com.example.app" and h["apns-push-type"] == "alert"
    assert h["apns-collapse-id"] == "t1" and h["apns-expiration"] == "1234" and h["authorization"].startswith("bearer ")
    assert apple.seen[0][1]["authorization"] != h["authorization"]                    # 새 인증 토큰으로 다시 보냈다


def test_devices_are_kept_in_a_private_file_and_capped(tmp_path):
    path = tmp_path / "d" / "devices.json"
    s = DeviceStore(path)
    for i in range(12):
        s.register(f"{i:02x}" * 32, "production")
        time.sleep(0.002)
    assert len(s.tokens()) == 10 and "00" * 32 not in s.tokens() and (path.stat().st_mode & 0o777) == 0o600
    s.register(DEV.upper(), "production")
    assert DEV in DeviceStore(path).tokens()                                           # 다시 읽어도 남아 있다(소문자로)
    assert s.unregister(DEV) and not s.unregister(DEV)


def make(tmp_path, key, *answers, **kw):
    store = DeviceStore(tmp_path / "devices.json")
    store.register(DEV, "production")
    apple = Apple(*answers)
    lines = []
    n = Notifier(store, ApnsClient(key[1], "K", "T", "com.example.app", post=apple), sleep=lambda s: None, log=lines.append, **kw)
    return n, store, apple, lines


def test_notifier_sends_once_per_event_and_forgets_dead_devices(tmp_path, key):
    n, store, apple, lines = make(tmp_path, key, (200, ""), (410, "Unregistered"))
    store.register(DEV2, "production")
    assert n.notify("task_done", "t1", "c_1") and not n.notify("task_done", "t1", "c_1")       # 같은 일은 한 번만
    assert not n.notify("모르는종류", "t1", None)
    n.drain()
    assert len(apple.seen) == 2 and store.tokens() == [DEV]                                     # 410이 온 기기는 등록을 지운다
    assert all("type=task_done" in l and ("device=abab" in l or "device=cdcd" in l) for l in lines)
    assert not any(DEV in l or DEV2 in l for l in lines)                                        # 기록에는 토큰 앞 6자만
    n.stop()


def test_notifier_retries_once_on_a_busy_answer_and_survives_errors(tmp_path, key):
    n, store, apple, lines = make(tmp_path, key, (503, ""), (200, ""))
    n.notify("task_failed", "t2", None)
    n.drain()
    assert len(apple.seen) == 2 and "status=200" in lines[-1]

    def boom(url, headers, body):
        raise RuntimeError("연결 실패")
    n.client._post = boom
    n.notify("task_done", "t3", None)
    n.drain()
    assert "error=RuntimeError" in lines[-1]
    n.client._post = Apple()
    n.notify("task_done", "t4", None)
    n.drain()
    assert "status=200" in lines[-1]                                                            # 실패 뒤에도 계속 보낸다
    n.stop()


def test_job_status_changes_become_the_right_kinds_and_never_carry_content(tmp_path, key):
    n, store, apple, lines = make(tmp_path, key)
    gate = {"ask": True}

    def runner(job, hooks):
        if "결재" in job.task:
            class Req:
                form_values, element_label, url, kind = {"본문": "비밀 초안 본문 SECRET-BODY"}, "보내기", "mailto:someone@mail.test", "send"
            hooks.approver(Req())
            return RunResult("completed", "했다", report={"done": ["비밀 결과 SECRET-RESULT"], "blocked": []})
        if "로그인" in job.task:
            return RunResult("needs_login", "네이버 로그인이 필요해요.", reason="login_expired")
        if "실패" in job.task:
            return RunResult("provider_error", "못 했다")
        return RunResult("completed", "했다", report={"done": ["비밀 결과 SECRET-RESULT"], "blocked": []})

    mgr = JobManager(runner, approval_timeout_s=30)
    mgr.on_status = n.on_status

    def wait(job, statuses):
        end = time.time() + 3
        while time.time() < end and mgr.get(job.id).status not in statuses:
            time.sleep(0.01)
        return mgr.get(job.id)
    try:
        a = mgr.submit("결재 받을 일 TASK-TEXT", title="김비밀 부장에게 메일", conversation_id="c_conv0001")
        j = wait(a, ("awaiting_approval",))
        mgr.decide(a.id, j.approval["approval_id"], "approve", sha256=j.approval["sha256"])
        wait(a, ("completed", "failed"))
        b = mgr.submit("로그인이 풀린 일")
        wait(b, ("completed", "failed"))
        c = mgr.submit("실패하는 일")
        wait(c, ("completed", "failed"))
        d = mgr.submit("취소할 일")
        wait(d, ("completed", "failed"))
        n.drain()
        kinds = [(body["type"], body["task_id"]) for _, _, body in apple.seen]
        assert (("approval_needed", a.id) in kinds and ("task_done", a.id) in kinds and ("needs_login", b.id) in kinds
                and ("task_failed", c.id) in kinds and ("task_done", d.id) in kinds)
        sent = json.dumps([body for _, _, body in apple.seen], ensure_ascii=False)
        for secret in ("SECRET", "김비밀", "someone@mail.test", "네이버", "TASK-TEXT", "결재"):          # 제목·초안·받는 사람·사이트 이름·작업 글은 없다
            assert secret not in sent, secret
        assert next(body for _, _, body in apple.seen if body["task_id"] == a.id)["conversation_id"] == "c_conv0001"
        assert all(set(body) <= {"aps", "type", "task_id", "conversation_id"} for _, _, body in apple.seen)
        # 알림 담당이 터져도 작업은 끝난다
        mgr.on_status = lambda job, status: (_ for _ in ()).throw(RuntimeError("x"))
        e = mgr.submit("그냥 일")
        assert wait(e, ("completed", "failed")).status == "completed"
    finally:
        mgr.shutdown()
        n.stop()


def test_device_endpoints_and_health_feature(tmp_path, key):
    H = {"X-App-Token": "t" * 32, "X-Api-Version": "4"}
    n, store, apple, lines = make(tmp_path, key)
    mgr = JobManager(lambda job, hooks: RunResult("completed", "했다"))
    for push in (n, None):
        http = make_server(mgr, "t" * 32, port=0, push=push)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{http.server_address[1]}"
        try:
            feats = httpx.get(f"{base}/v1/health", headers=H).json()["features"]
            assert ("push" in feats) == (push is not None)
            r = httpx.post(f"{base}/v1/push/devices", headers=H, json={"token": DEV2, "environment": "production", "bundle_id": "com.example.app"})
            if push is None:
                assert r.status_code == 404                                                      # 설정이 없으면 없는 경로다
                continue
            assert r.status_code == 200 and r.json() == {"registered": True} and DEV2 in store.tokens()
            bad = [{"token": "xyz", "environment": "production"}, {"token": DEV2, "environment": "dev"},
                   {"token": DEV2, "environment": "sandbox"}, {"token": DEV2, "environment": "production", "bundle_id": "com.other.app"}]
            assert [httpx.post(f"{base}/v1/push/devices", headers=H, json=b).status_code for b in bad] == [400, 400, 400, 400]
            assert httpx.post(f"{base}/v1/push/devices", headers={"X-Api-Version": "4"}, json=bad[0]).status_code == 401
            r = httpx.delete(f"{base}/v1/push/devices/{DEV2}", headers=H)
            assert r.status_code == 200 and r.json() == {"registered": False} and DEV2 not in store.tokens()
            assert httpx.delete(f"{base}/v1/push/devices/{DEV2}", headers=H).status_code == 200  # 없어도 200
        finally:
            http.shutdown()
    mgr.shutdown()
    n.stop()


def test_notification_text_comes_from_the_mac_not_through_apple(tmp_path, key):
    """알림에 띄울 실제 글은 앱이 맥에서 직접 받아 간다. Apple로 가는 내용은 그대로 고정 문장과 id다."""
    from agent.api.push import notice
    H = {"X-App-Token": "t" * 32, "X-Api-Version": "4"}
    n, store, apple, lines = make(tmp_path, key)

    def runner(job, hooks):
        if "보내" in job.task:
            class Req:
                form_values, element_label, url, kind = {"제목": "합성 결제내역 정리", "본문": "SECRET-BODY"}, "보내기", "mailto:x@mail.test", "send"
            hooks.approver(Req())
            return RunResult("completed", "했다", report={"done": ["메일을 보냈어요"], "blocked": []})
        if "로그인" in job.task:
            return RunResult("needs_login", "합성 사이트 로그인이 풀렸어요. 다시 로그인해 주세요.", reason="login_expired")
        return RunResult("completed", "했다", report={"done": ["첫 줄 답이에요.\n\n둘째 줄 " + "가" * 400], "blocked": []}, private="읽기" in job.task)

    mgr = JobManager(runner, approval_timeout_s=30)
    mgr.on_status = n.on_status
    http = make_server(mgr, "t" * 32, port=0, push=n)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http.server_address[1]}"

    def wait(job, statuses):
        end = time.time() + 3
        while time.time() < end and mgr.get(job.id).status not in statuses:
            time.sleep(0.01)
        return mgr.get(job.id)
    try:
        assert "push_text" in httpx.get(f"{base}/v1/health", headers=H).json()["features"]
        a = mgr.submit("메일 보내 줘", title="결제내역 메일")
        j = wait(a, ("awaiting_approval",))
        r = httpx.get(f"{base}/v1/tasks/{a.id}/notification", params={"type": "approval_needed"}, headers=H)
        assert r.status_code == 200 and r.json() == {"type": "approval_needed", "title": "결제내역 메일",
                                                     "body": "메일을 보내도 될까요? 제목: 합성 결제내역 정리", "private": False}   # 본문·받는 사람은 없다
        assert httpx.get(f"{base}/v1/tasks/{a.id}/notification", params={"type": "task_done"}, headers=H).status_code == 404  # 아직 끝나지 않았다
        mgr.decide(a.id, j.approval["approval_id"], "approve", sha256=j.approval["sha256"])
        wait(a, ("completed", "failed"))
        assert httpx.get(f"{base}/v1/tasks/{a.id}/notification", params={"type": "approval_needed"}, headers=H).json()["error"]["code"] == "notification_gone"
        b = mgr.submit("결제내역 읽기", title="결제내역 확인")
        wait(b, ("completed", "failed"))
        got = httpx.get(f"{base}/v1/tasks/{b.id}/notification", params={"type": "task_done"}, headers=H).json()
        assert got["private"] is True and got["body"].startswith("첫 줄 답이에요.\n둘째 줄") and len(got["body"]) == 300 and got["body"].endswith("…")
        c = mgr.submit("로그인이 풀린 일", title="결제내역 확인")
        wait(c, ("completed", "failed"))
        assert httpx.get(f"{base}/v1/tasks/{c.id}/notification", params={"type": "needs_login"}, headers=H).json()["body"].startswith("합성 사이트 로그인이 풀렸어요")
        assert httpx.get(f"{base}/v1/tasks/{c.id}/notification", params={"type": "모르는것"}, headers=H).status_code == 400
        assert httpx.get(f"{base}/v1/tasks/{'x' * 12}/notification", params={"type": "task_done"}, headers=H).status_code == 404
        assert httpx.get(f"{base}/v1/tasks/{c.id}/notification", params={"type": "needs_login"}).status_code == 401
        n.drain()
        sent = json.dumps([body for _, _, body in apple.seen], ensure_ascii=False)
        assert all(body["aps"]["mutable-content"] == 1 for _, _, body in apple.seen)
        for secret in ("합성 결제내역 정리", "첫 줄 답", "합성 사이트", "SECRET"):        # Apple로 간 것에는 실제 글이 없다
            assert secret not in sent, secret
    finally:
        http.shutdown()
        mgr.shutdown()
        n.stop()
