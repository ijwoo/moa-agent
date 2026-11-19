"""합성 메일 서버 (설계 6절 (a)): 루프백에서만 듣는 최소 SMTP 수신기 + 메모리 우편함. 표준 라이브러리만 쓴다.

@mail.test 주소만 받는다. 발송 코드는 실제 계정과 같은 smtplib 경로로 이 서버에 로그인·발송하고,
도착 확인은 우편함을 직접 조회한다(IMAP 경로는 단위 테스트에서 가짜 IMAP으로 확인).
외부로 나가는 메일은 없다.
"""

from __future__ import annotations

import base64
import smtplib
import socketserver
import threading
from email import message_from_bytes

from .transport import MailError

DOMAIN = "mail.test"
PASSWORD = "synthetic-app-pass-1234"   # 합성 계정 공용 앱 비밀번호(비밀 아님). 다른 값이면 535로 거절한다


class _Handler(socketserver.StreamRequestHandler):
    def _say(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")

    def handle(self) -> None:
        srv: SyntheticMailServer = self.server.owner   # type: ignore[attr-defined]
        authed: str | None = None
        sender, rcpts = None, []
        self._say("220 synthetic mail ready")
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            cmd = line.split(" ", 1)[0].upper()
            arg = line[len(cmd):].strip()
            if cmd in ("EHLO", "HELO"):
                self.wfile.write(b"250-synthetic\r\n250 AUTH PLAIN LOGIN\r\n")
            elif cmd == "AUTH":
                parts = arg.split()
                try:
                    if parts and parts[0].upper() == "PLAIN":
                        blob = parts[1] if len(parts) > 1 else self._prompt("334 ")
                        _, user, pw = base64.b64decode(blob).decode().split("\0")
                    else:   # LOGIN
                        user = base64.b64decode(parts[1] if len(parts) > 1 else self._prompt("334 VXNlcm5hbWU6")).decode()
                        pw = base64.b64decode(self._prompt("334 UGFzc3dvcmQ6")).decode()
                except Exception:
                    self._say("501 bad auth")
                    continue
                if srv.reachable and user.lower().endswith("@" + DOMAIN) and pw == srv.password:
                    authed = user.lower()
                    self._say("235 ok")
                else:
                    self._say("535 authentication failed")
            elif cmd == "MAIL":
                if authed is None:
                    self._say("530 authentication required")
                else:
                    sender, rcpts = arg, []
                    self._say("250 ok")
            elif cmd == "RCPT":
                addr = arg.split(":", 1)[-1].strip().strip("<>").lower()
                if sender is None:
                    self._say("503 need MAIL first")
                elif not addr.endswith("@" + DOMAIN) or addr in srv.reject:
                    self._say("550 no such user")
                else:
                    rcpts.append(addr)
                    self._say("250 ok")
            elif cmd == "DATA":
                if not rcpts:
                    self._say("503 need RCPT first")
                    continue
                self._say("354 end with .")
                lines = []
                while True:
                    l = self.rfile.readline()
                    if not l or l in (b".\r\n", b".\n"):
                        break
                    lines.append(l[1:] if l.startswith(b"..") else l)
                data = b"".join(lines)
                if not srv.drop:
                    for r in rcpts:
                        srv.deliver(r, data)
                sender, rcpts = None, []
                self._say("250 queued")
            elif cmd in ("RSET", "NOOP"):
                self._say("250 ok")
            elif cmd == "QUIT":
                self._say("221 bye")
                return
            else:
                self._say("502 not implemented")

    def _prompt(self, text: str) -> str:
        self._say(text)
        return self.rfile.readline().decode().strip()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class SyntheticMailServer:
    def __init__(self, password: str = PASSWORD):
        self.password = password
        self.reachable = True          # False면 로그인을 거절하지 않고… 백엔드가 연결 실패로 처리(테스트용)
        self.drop = False              # True면 접수는 하지만 우편함에 넣지 않는다(도착 확인 실패 테스트)
        self.reject: set[str] = set()  # 받기를 거절할 주소(테스트)
        self._boxes: dict[str, list[bytes]] = {}
        self._lock = threading.Lock()
        self._srv = _Server(("127.0.0.1", 0), _Handler)
        self._srv.owner = self          # type: ignore[attr-defined]
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.05},
                                        name="synthetic-smtp", daemon=True)

    def start(self) -> "SyntheticMailServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def deliver(self, address: str, raw: bytes) -> None:
        with self._lock:
            self._boxes.setdefault(address, []).append(raw)

    def messages(self, address: str | None = None) -> list[bytes]:
        with self._lock:
            if address is not None:
                return list(self._boxes.get(address.lower(), []))
            return [m for box in self._boxes.values() for m in box]

    def find(self, address: str, message_id: str) -> bytes | None:
        for raw in self.messages(address):
            if (message_from_bytes(raw).get("Message-ID") or "").strip() == message_id:
                return raw
        return None

    def clear(self) -> None:
        with self._lock:
            self._boxes.clear()


class SyntheticBackend:
    """합성 서버용 백엔드. 로그인·발송은 실제와 같은 smtplib(암호화 없는 루프백), 도착 확인은 우편함 직접 조회."""

    def __init__(self, server: SyntheticMailServer, timeout_s: float = 5.0):
        self.server, self.timeout_s = server, timeout_s

    def _smtp(self, address: str, password: str) -> smtplib.SMTP:
        if not self.server.reachable:
            raise MailError("unreachable")
        try:
            s = smtplib.SMTP("127.0.0.1", self.server.port, timeout=self.timeout_s)
        except OSError:
            raise MailError("unreachable") from None
        try:
            s.login(address, password)
        except smtplib.SMTPException:
            try:
                s.quit()
            except Exception:
                pass
            raise MailError("auth_failed") from None
        return s

    def check_login(self, address: str, password: str) -> None:
        self._smtp(address, password).quit()

    def send(self, address: str, password: str, to: str, eml: bytes) -> None:
        s = self._smtp(address, password)
        try:
            if s.sendmail(address, [to], eml):
                raise MailError("rejected")
        except smtplib.SMTPException:
            raise MailError("rejected") from None
        except OSError:
            raise MailError("unreachable") from None
        finally:
            try:
                s.quit()
            except Exception:
                pass

    def list_since(self, address: str, password: str, since: tuple[int, int, int], limit: int = 50) -> list[dict]:
        import time as _t
        from email.utils import parsedate_to_datetime
        from .transport import header_text
        self._smtp(address, password).quit()   # 로그인 확인(틀린 비밀번호면 auth_failed)
        start = _t.mktime((*since, 0, 0, 0, 0, 0, -1))
        out = []
        for i, raw in enumerate(self.server.messages(address), 1):
            h = message_from_bytes(raw)
            try:
                ts = parsedate_to_datetime(h.get("Date")).timestamp()
            except Exception:
                continue
            if ts >= start:
                out.append({"uid": str(i), "from": header_text(h.get("From")), "subject": header_text(h.get("Subject")),
                            "received_at": ts, "seen": False, "message_id": (h.get("Message-ID") or "").strip()})
        return sorted(out, key=lambda m: m["received_at"], reverse=True)[:limit]

    def fetch_text(self, address: str, password: str, uid: str, max_chars: int = 4000) -> str:
        from .transport import body_text
        box = self.server.messages(address)
        return body_text(box[int(uid) - 1], max_chars) if str(uid).isdigit() and 0 < int(uid) <= len(box) else ""

    def find(self, address: str, password: str, message_id: str, wait_s: float = 0.0):
        raw = self.server.find(address, message_id)
        return ("inbox", raw) if raw is not None else (None, None)
