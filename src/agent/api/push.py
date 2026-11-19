"""푸시 알림(APNs) — 앱이 뒤에 있을 때 작업의 상태 변화를 알린다 (D44, 형식은 docs/design/push.md 0절).

- 알림에는 고정 문장과 종류·task_id·conversation_id만 싣는다. 작업 제목·초안·받는 사람·사이트 이름·읽은 결과는 넣지 않는다(Apple을 거친다).
- 인증 키(.p8)의 내용은 맥 키체인에만 있다(서비스 local-ai-agent.apns, 계정 apns-p8). 읽어서 메모리에만 두고 출력·기록하지 않는다.
  Key ID·Team ID·번들 ID는 .env(APNS_KEY_ID, APNS_TEAM_ID, APNS_TOPIC). 넷이 다 있을 때만 푸시가 켜진다.
- 접속하는 곳은 api.push.apple.com(production) 하나다. sandbox 호스트는 승인되지 않아 그 환경의 등록은 받지 않는다.
- 보내기는 작업을 막지 않는다: 대기열에 넣고 별도 스레드가 보낸다. 기록에는 종류·상태 코드·토큰 앞 6자만 남긴다.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT = "local-ai-agent.apns", "apns-p8"
HOST = "https://api.push.apple.com"
TOKEN = re.compile(r"^[0-9a-fA-F]{64,200}$")
MAX_DEVICES = 10
JWT_TTL_S = 50 * 60          # APNs: 1시간 안에 새로, 20분보다 자주 바꾸지 말 것
DEVICES_PATH = Path.home() / ".local" / "share" / "local-ai-agent" / "devices.json"

# 종류 → 고정 문장 (앱 세션과 맞춘 것. 바꿀 때는 앱에 먼저 알린다)
TEXT = {
    "approval_needed": "승인이 필요한 일이 있어요",
    "input_needed": "확인이 필요한 질문이 있어요",
    "task_done": "맡긴 일이 끝났어요",
    "task_failed": "맡긴 일을 끝내지 못했어요",
    "needs_login": "로그인이 필요해서 멈췄어요",
    "routine_result": "루틴 결과가 도착했어요",
    "login_expired": "로그인이 풀린 사이트가 있어요",   # D105: 살려 두기 점검에서 풀린 것을 알았다. task_id는 "site:<사이트 키>"
}


class PushError(Exception):
    pass


def keychain_key() -> str | None:
    """키체인에서 .p8의 내용을 읽는다. 없거나 읽지 못하면 None. 값은 어디에도 출력하지 않는다."""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        return None
    if not out.startswith("-----BEGIN"):   # 줄바꿈이 든 값은 security가 16진수로 돌려준다
        try:
            out = bytes.fromhex(out).decode("utf-8").strip()
        except ValueError:
            return None
    return out if out.startswith("-----BEGIN") else None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class DeviceStore:
    """등록한 기기 토큰. 파일(권한 600)에 두어 서버를 다시 띄워도 남는다. 최대 10개 — 넘으면 가장 오래 쓰지 않은 것부터 지운다."""

    def __init__(self, path: Path | None = None):
        self.path = path or DEVICES_PATH
        self._lock = threading.Lock()
        self._d: dict[str, dict] = {}
        try:
            for x in json.loads(self.path.read_text()):
                if isinstance(x, dict) and TOKEN.match(str(x.get("token", ""))):
                    self._d[x["token"].lower()] = {"environment": x.get("environment", "production"), "used_at": float(x.get("used_at", 0))}
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([{"token": t, **v} for t, v in self._d.items()]))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def register(self, token: str, environment: str) -> None:
        with self._lock:
            self._d[token.lower()] = {"environment": environment, "used_at": time.time()}
            while len(self._d) > MAX_DEVICES:
                del self._d[min(self._d, key=lambda k: self._d[k]["used_at"])]
            self._save()

    def unregister(self, token: str) -> bool:
        with self._lock:
            had = self._d.pop(token.lower(), None) is not None
            if had:
                self._save()
            return had

    def tokens(self) -> list[str]:
        with self._lock:
            return list(self._d)


class ApnsClient:
    """APNs에 알림 하나를 보낸다(토큰 방식 인증, HTTP/2). post를 주면 그것으로 보낸다(시험용)."""

    def __init__(self, key_pem: str, key_id: str, team_id: str, topic: str, post=None, clock=time.time):
        from cryptography.hazmat.primitives import serialization
        self._key = serialization.load_pem_private_key(key_pem.encode(), password=None)
        self.key_id, self.team_id, self.topic = key_id, team_id, topic
        self._post, self._clock = post, clock
        self._jwt: tuple[float, str] | None = None
        self._http = None

    def jwt(self, fresh: bool = False) -> str:
        now = self._clock()
        if not fresh and self._jwt is not None and now - self._jwt[0] < JWT_TTL_S:
            return self._jwt[1]
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        head = _b64(json.dumps({"alg": "ES256", "kid": self.key_id}, separators=(",", ":")).encode())
        body = _b64(json.dumps({"iss": self.team_id, "iat": int(now)}, separators=(",", ":")).encode())
        r, s = decode_dss_signature(self._key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
        token = f"{head}.{body}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        self._jwt = (now, token)
        return token

    def _send(self, url: str, headers: dict, payload: dict) -> tuple[int, str]:
        if self._post is not None:
            return self._post(url, headers, payload)
        import httpx
        if self._http is None:
            self._http = httpx.Client(http2=True, timeout=10.0)
        r = self._http.post(url, headers=headers, content=json.dumps(payload, ensure_ascii=False).encode())
        reason = ""
        if r.status_code != 200:
            try:
                reason = str(r.json().get("reason", ""))
            except ValueError:
                pass
        return r.status_code, reason

    def send(self, device: str, payload: dict, collapse_id: str, expires_at: float) -> tuple[int, str]:
        """(상태 코드, 이유). 인증 토큰이 낡았다고 하면(403 ExpiredProviderToken) 새로 만들어 한 번 더 보낸다."""
        for attempt in (0, 1):
            headers = {"authorization": f"bearer {self.jwt(fresh=attempt == 1)}", "apns-topic": self.topic, "apns-push-type": "alert",
                       "apns-priority": "10", "apns-collapse-id": collapse_id[:64], "apns-expiration": str(int(expires_at))}
            code, reason = self._send(f"{HOST}/3/device/{device}", headers, payload)
            if not (code == 403 and reason == "ExpiredProviderToken" and attempt == 0):
                return code, reason
        return code, reason


def payload(kind: str, task_id: str, conversation_id: str | None) -> dict:
    """알림의 내용 전부. 고정 문장과 종류·id뿐이다."""
    # mutable-content: 앱의 알림 확장이 띄우기 전에 글을 바꿀 수 있게 한다 — 확장이 맥에 직접 물어(GET …/notification) 실제 글을 받아 온다.
    # Apple을 거치는 것은 여전히 이 고정 문장과 id뿐이다(확장이 맥에 닿지 못하면 이 문장이 그대로 뜬다)
    out = {"aps": {"alert": {"body": TEXT[kind]}, "sound": "default", "thread-id": task_id, "mutable-content": 1},
           "type": kind, "task_id": task_id}
    if conversation_id:
        out["conversation_id"] = conversation_id
    if task_id.startswith("site:"):
        out["site"] = task_id[5:]
    return out


NOTICE_CHARS = 300


def notice(job, kind: str) -> dict | None:
    """알림에 띄울 실제 글 — 앱의 알림 확장이 맥에서 직접 받아 간다(Apple을 거치지 않는다). 그 종류에 띄울 것이 없으면 None.

    {"type", "title", "body", "private"}. private: 로그인한 화면에서 읽은 내용처럼 사용자의 실제 개인 데이터가 든 글이다(앱이 가릴지 정한다).
    """
    r = job.result or {}
    rep = r.get("report") or {}
    body, private = "", False
    if kind == "approval_needed" and job.status == "awaiting_approval" and job.approval:
        subject = str((job.approval.get("fields") or {}).get("제목") or "").strip()
        what = "메일을 보내도 될까요?" if job.approval.get("kind") == "send" else "초안을 저장해도 될까요?"
        body = f"{what} 제목: {subject}" if subject else what
    elif kind == "input_needed" and job.question:
        body = str(job.question.get("text") or "")
    elif kind == "input_needed" and job.status == "needs_input":
        body = str(r.get("summary") or "")
    elif kind == "task_done" and job.status == "completed":
        body = "\n".join(x for x in rep.get("done") or [] if isinstance(x, str)) or str(r.get("summary") or "")
        private = bool(rep.get("private")) or bool(getattr(job, "_private", False))
    elif kind in ("task_failed", "needs_login") and job.status == "failed":
        body = next((x for x in rep.get("blocked") or [] if isinstance(x, str) and x.strip()), "") or str(r.get("summary") or "")
    body = "\n".join(l.strip() for l in body.splitlines() if l.strip())
    if not body:
        return None
    if len(body) > NOTICE_CHARS:
        body = body[:NOTICE_CHARS - 1].rstrip() + "…"
    return {"type": kind, "title": job.title or "작업", "body": body, "private": private}


class Notifier:
    """작업의 상태 변화 → 알림. 같은 일로는 한 번만 보내고, 보내기는 별도 스레드가 한다(작업을 막지 않는다)."""

    def __init__(self, store: DeviceStore, client: ApnsClient, approval_timeout_s: float = 1800.0, sleep=time.sleep, log=None):
        self.store, self.client, self.approval_timeout_s = store, client, approval_timeout_s
        self._sleep = sleep
        self._log = log or (lambda line: sys.stderr.write(line + "\n"))
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._thread = threading.Thread(target=self._work, name="push", daemon=True)
        self._thread.start()

    # ---- 작업 상태 → 종류 ----
    def on_status(self, job, status: str) -> None:
        kind, key = None, ""
        if status == "awaiting_approval" and job.approval:
            kind, key = "approval_needed", job.approval.get("approval_id", "")
        elif status == "awaiting_input" and job.question:
            kind, key = "input_needed", job.question.get("question_id", "")
        elif status == "needs_input":
            kind = "input_needed"
        elif status == "completed":
            kind = "task_done"
        elif status == "failed":
            kind = "needs_login" if (job.result or {}).get("outcome") == "needs_login" else "task_failed"
        if kind is None:   # running·send_pending·cancelled: 보내지 않는다
            return
        expires = time.time() + (self.approval_timeout_s if kind in ("approval_needed", "input_needed") and key else 24 * 3600)
        self.notify(kind, job.id, job.conversation_id, key, expires)

    def notify(self, kind: str, task_id: str, conversation_id: str | None, key: str = "", expires_at: float | None = None) -> bool:
        if kind not in TEXT:
            return False
        dedupe = f"{task_id}:{kind}:{key}"
        with self._lock:
            if dedupe in self._seen:
                return False
            self._seen[dedupe] = time.time()
            if len(self._seen) > 2000:
                for k in sorted(self._seen, key=self._seen.get)[:1000]:
                    del self._seen[k]
        try:
            self._q.put_nowait((kind, task_id, conversation_id, expires_at or time.time() + 24 * 3600))
        except queue.Full:
            return False
        return True

    def _work(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            kind, task_id, conv, expires = item
            body = payload(kind, task_id, conv)
            for device in self.store.tokens():
                try:
                    code, reason = self.client.send(device, body, task_id, expires)
                    if code in (429, 500, 502, 503):
                        self._sleep(2.0)
                        code, reason = self.client.send(device, body, task_id, expires)
                    if code == 410 or (code == 400 and reason == "BadDeviceToken"):
                        self.store.unregister(device)   # 없는 기기: 등록을 지운다(앱이 다음 실행 때 다시 등록한다)
                    self._log(f"[push] {time.strftime('%Y-%m-%d %H:%M:%S')} type={kind} status={code} reason={reason or '-'} device={device[:6]}")
                except Exception as e:   # 보내기 실패가 다른 알림·작업을 막지 않는다. 내용은 기록하지 않는다
                    self._log(f"[push] {time.strftime('%Y-%m-%d %H:%M:%S')} type={kind} error={type(e).__name__} device={device[:6]}")
            self._q.task_done()

    def drain(self, timeout: float = 5.0) -> None:
        """대기열이 빌 때까지 기다린다(시험용)."""
        end = time.time() + timeout
        while self._q.unfinished_tasks and time.time() < end:
            time.sleep(0.01)

    def stop(self) -> None:
        self._q.put(None)


def make_notifier(approval_timeout_s: float = 1800.0) -> Notifier | None:
    """키체인의 키와 .env의 세 값이 다 있을 때만 알림 담당을 만든다. 하나라도 없으면 None(푸시 꺼짐)."""
    kid, team, topic = (os.environ.get(k, "").strip() for k in ("APNS_KEY_ID", "APNS_TEAM_ID", "APNS_TOPIC"))
    if not (kid and team and topic):
        return None
    pem = keychain_key()
    if pem is None:
        return None
    try:
        client = ApnsClient(pem, kid, team, topic)
    except Exception:   # 키 꼴이 다르다: 켜지 않는다(값은 기록하지 않는다)
        return None
    return Notifier(DeviceStore(), client, approval_timeout_s)
