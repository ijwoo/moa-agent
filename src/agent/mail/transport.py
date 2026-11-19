"""메일 서버와의 통신: 로그인 확인, SMTP 발송, 도착 확인(IMAP). 표준 라이브러리만 쓴다.

오류는 종류만 올린다(MailError.reason). 메일 서버의 응답 원문·비밀번호는 예외 메시지에 넣지 않는다.
"""

from __future__ import annotations

import imaplib
import re
import smtplib
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Protocol


class MailError(Exception):
    """reason: auth_failed | unreachable | not_set | rate_limited | not_allowed | rejected"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Backend(Protocol):
    def check_login(self, address: str, password: str) -> None: ...
    def send(self, address: str, password: str, to: str, eml: bytes) -> None: ...
    def find(self, address: str, password: str, message_id: str, wait_s: float) -> tuple[str | None, bytes | None]:
        """보낸 메일을 Message-ID로 찾는다. (찾은 곳: "inbox"|"other"|"sent"|None, 원문)"""


@dataclass(frozen=True)
class Hosts:
    smtp: str
    imap: str


PROVIDERS = {
    "gmail": Hosts("smtp.gmail.com", "imap.gmail.com"),
    "naver": Hosts("smtp.naver.com", "imap.naver.com"),
}
DOMAINS = {"gmail.com": "gmail", "googlemail.com": "gmail", "naver.com": "naver"}
TIMEOUT_S = 15.0
_UNREACHABLE = (OSError, socket.timeout, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, ssl.SSLError,
                imaplib.IMAP4.abort)
_LIST = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')


def parse_folders(lines: list) -> list[tuple[str, set[str]]]:
    """IMAP LIST 응답 → [(폴더 이름(서버 표기 그대로), 속성 집합)]. 이름은 고정하지 않고 SPECIAL-USE 속성으로 찾는다."""
    out = []
    for raw in lines:
        if isinstance(raw, tuple):   # 이름이 literal로 온 경우: (b'(..) "/" {n}', b'name')
            head, name = raw[0], raw[1]
            m = re.match(rb"^\((?P<flags>[^)]*)\)", head)
            flags = m.group("flags") if m else b""
        elif isinstance(raw, bytes):
            m = _LIST.match(raw.strip())
            if not m:
                continue
            flags, name = m.group("flags"), m.group("name")
        else:
            continue
        out.append((name.decode("ascii", "replace").strip(), {f.lower() for f in flags.decode("ascii", "replace").split()}))
    return out


class SmtpImapBackend:
    """실제 계정용: SMTP 465(SSL) 먼저, 연결이 안 되면 587(STARTTLS). 확인은 IMAP 993."""

    def __init__(self, hosts: Hosts, timeout_s: float = TIMEOUT_S):
        self.hosts, self.timeout_s = hosts, timeout_s

    def _smtp(self, address: str, password: str):
        ctx = ssl.create_default_context()
        last: Exception | None = None
        for port in (465, 587):
            try:
                if port == 465:
                    s = smtplib.SMTP_SSL(self.hosts.smtp, port, timeout=self.timeout_s, context=ctx)
                else:
                    s = smtplib.SMTP(self.hosts.smtp, port, timeout=self.timeout_s)
                    s.starttls(context=ctx)
            except _UNREACHABLE as e:
                last = e
                continue
            try:
                s.login(address, password)
            except smtplib.SMTPAuthenticationError:
                _quiet(s.quit)
                raise MailError("auth_failed") from None
            except smtplib.SMTPException:
                _quiet(s.quit)
                raise MailError("auth_failed") from None
            except _UNREACHABLE:
                raise MailError("unreachable") from None
            return s
        raise MailError("unreachable") from last

    def _imap(self, address: str, password: str):
        try:
            m = imaplib.IMAP4_SSL(self.hosts.imap, 993, timeout=self.timeout_s)
        except _UNREACHABLE:
            raise MailError("unreachable") from None
        try:
            m.login(address, password)
        except imaplib.IMAP4.abort:
            raise MailError("unreachable") from None
        except imaplib.IMAP4.error:
            _quiet(m.logout)
            raise MailError("auth_failed") from None
        return m

    def check_login(self, address: str, password: str) -> None:
        _quiet(self._smtp(address, password).quit)
        _quiet(self._imap(address, password).logout)

    def send(self, address: str, password: str, to: str, eml: bytes) -> None:
        s = self._smtp(address, password)
        try:
            refused = s.sendmail(address, [to], eml)
            if refused:
                raise MailError("rejected")
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError,
                smtplib.SMTPResponseException):
            raise MailError("rejected") from None
        except _UNREACHABLE:
            raise MailError("unreachable") from None
        finally:
            _quiet(s.quit)

    # ---- 읽기 (D54): 받은편지함을 읽기 전용으로 연다. PEEK로 받아 '읽음' 표시를 바꾸지 않는다 ----
    def list_since(self, address: str, password: str, since: tuple[int, int, int], limit: int = 50) -> list[dict]:
        """since(년, 월, 일) 이후에 받은 메일의 머리글 목록(최신 먼저). 본문은 받지 않는다."""
        m = self._imap(address, password)
        try:
            if m.select("INBOX", readonly=True)[0] != "OK":
                raise MailError("unreachable")
            y, mo, d = since
            typ, data = m.uid("SEARCH", None, "SINCE", f"{d:02d}-{MONTHS[mo - 1]}-{y}")
            uids = (data[0].split() if typ == "OK" and data and data[0] else [])[-limit:]
            if not uids:
                return []
            typ, items = m.uid("FETCH", b",".join(uids).decode(),
                               "(UID INTERNALDATE FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])")
            return parse_listing(items) if typ == "OK" else []
        except imaplib.IMAP4.abort:
            raise MailError("unreachable") from None
        except imaplib.IMAP4.error:
            raise MailError("rejected") from None
        finally:
            _quiet(m.logout)

    def fetch_text(self, address: str, password: str, uid: str, max_chars: int = 4000) -> str:
        """메일 한 통의 본문 글(읽음 표시를 바꾸지 않는다)."""
        if not str(uid).isdigit():
            return ""
        m = self._imap(address, password)
        try:
            if m.select("INBOX", readonly=True)[0] != "OK":
                return ""
            typ, items = m.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in items or [] if isinstance(p, tuple) and len(p) > 1), None)
            return body_text(raw, max_chars) if typ == "OK" and raw else ""
        except (imaplib.IMAP4.error, imaplib.IMAP4.abort):
            return ""
        finally:
            _quiet(m.logout)

    def find(self, address: str, password: str, message_id: str, wait_s: float = 20.0):
        deadline = time.monotonic() + wait_s
        best: tuple[str | None, bytes | None] = (None, None)
        while True:
            try:
                got = self._find_once(address, password, message_id)
            except MailError:
                got = (None, None)
            if got[0] in ("inbox", "other"):
                return got
            if got[0] is not None:
                best = got
            if time.monotonic() >= deadline:
                return best
            time.sleep(2.0)

    def _find_once(self, address: str, password: str, message_id: str):
        m = self._imap(address, password)
        try:
            typ, lines = m.list()
            folders = parse_folders(lines or []) if typ == "OK" else []
            skip = {"\\noselect", "\\drafts", "\\trash", "\\junk", "\\all"}
            order = sorted((f for f in folders if not (f[1] & skip)),
                           key=lambda f: (f[0].strip('"').upper() != "INBOX", "\\sent" in f[1]))
            if not order:
                order = [("INBOX", set())]
            sent_hit: tuple[str | None, bytes | None] = (None, None)
            for name, flags in order:
                try:
                    if m.select(name, readonly=True)[0] != "OK":
                        continue
                    typ, data = m.search(None, "HEADER", "Message-ID", f'"{message_id}"')
                    ids = data[0].split() if typ == "OK" and data and data[0] else []
                    if not ids:
                        continue
                    typ, msg = m.fetch(ids[-1], "(BODY.PEEK[])")
                    raw = next((p[1] for p in msg or [] if isinstance(p, tuple) and len(p) > 1), None)
                except imaplib.IMAP4.error:
                    continue
                where = "sent" if "\\sent" in flags else ("inbox" if name.strip('"').upper() == "INBOX" else "other")
                if where != "sent":
                    return where, raw
                sent_hit = (where, raw)
            return sent_hit
        finally:
            _quiet(m.logout)


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_UID = re.compile(rb"UID (\d+)")
_FLAGS = re.compile(rb"FLAGS \(([^)]*)\)")
_TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)


def header_text(value) -> str:
    """메일 머리글(MIME 인코딩) → 읽을 수 있는 글."""
    from email.header import decode_header, make_header
    try:
        return " ".join(str(make_header(decode_header(str(value or "")))).split())
    except Exception:
        return " ".join(str(value or "").split())


def parse_listing(items: list) -> list[dict]:
    """IMAP FETCH (UID INTERNALDATE FLAGS BODY[HEADER.FIELDS …]) 응답 → [{uid, from, subject, received_at, seen, message_id}]."""
    from email import message_from_bytes
    out = []
    items = list(items or [])
    for i, it in enumerate(items):
        if not (isinstance(it, tuple) and len(it) >= 2):
            continue
        meta, raw = it[0], it[1]
        # UID는 본문 앞(meta)에 올 수도, 본문 뒤의 닫는 조각에 올 수도 있다(실제 네이버: 요청하지 않으면 아예 없다 → 항상 UID를 요청한다)
        tail = items[i + 1] if i + 1 < len(items) and isinstance(items[i + 1], bytes) else b""
        uid = _UID.search(meta) or _UID.search(tail)
        t = imaplib.Internaldate2tuple(meta)
        if not uid or t is None:
            continue
        h = message_from_bytes(raw)
        f = _FLAGS.search(meta)
        out.append({"uid": uid.group(1).decode(), "from": header_text(h.get("From")), "subject": header_text(h.get("Subject")),
                    "received_at": time.mktime(t), "seen": bool(f and b"\\Seen" in f.group(1)),
                    "message_id": (h.get("Message-ID") or "").strip()})
    return sorted(out, key=lambda m: m["received_at"], reverse=True)


def body_text(raw: bytes, max_chars: int = 4000) -> str:
    """메일 원문 → 본문 글(일반 글 우선, 없으면 HTML에서 태그를 걷어 냄). 길면 자른다."""
    import html
    from email import message_from_bytes, policy
    msg = message_from_bytes(raw, policy=policy.default)
    part = msg.get_body(preferencelist=("plain", "html")) or msg
    try:
        text = str(part.get_content())
    except Exception:
        return ""
    if part.get_content_type() == "text/html":
        text = html.unescape(_TAGS.sub(" ", text))
    text = "\n".join(l.strip() for l in text.replace("\r\n", "\n").split("\n") if l.strip())
    return text[:max_chars]


def _quiet(fn) -> None:
    try:
        fn()
    except Exception:
        pass
