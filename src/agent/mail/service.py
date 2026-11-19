"""메일 발송 서비스: 허용 목록 · 계정 상태 · 초안 고정(.eml + 해시) · 횟수 제한 · 발송 · 도착 확인.

- 보낼 수 있는 주소 = 실행하는 쪽 설정(SEND_SELF_ADDRESSES)에 적은 주소 + 앱에서 연결(connect)하며 **로그인을 확인한** 주소.
  로그인할 수 없는 주소는 등록되지 않는다(그 계정의 앱 비밀번호를 아는 사람만 그 주소를 '본인 주소'로 만들 수 있다, D51).
- 본인에게: 보내는 계정 = 받는 주소. 받는 사람이 허용 목록 밖이면 not_allowed.
- 다른 사람에게(D77, allow_others를 켰을 때만): 보내는 계정 = 사용자의 연결된 계정, 받는 사람 = **이 작업에 허락된 주소**
  (prepare의 others — 부르는 쪽이 연락처·사용자가 직접 친 주소에서 코드로 정한 것). 모델이 쓴 주소는 그 안에 있을 때만 통과한다.
  받는 분의 받은편지함은 볼 수 없으므로 '도착 확인'이라고 하지 않는다(accepted_only).
- 비밀번호는 SecretStore에서 쓸 때만 읽는다. 이 모듈의 예외·반환값·기록에는 비밀번호와 메일 서버 응답 원문이 없다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets as pysecrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from ..core.approval import draft_sha256
from .secrets import PASSWORD, SecretStore
from .transport import DOMAINS, Backend, MailError

SEND_ACTION = "보내기"
FIELD_FROM, FIELD_TO, FIELD_SUBJECT, FIELD_BODY = "보내는 사람", "받는 사람", "제목", "본문"
PROVIDER_WORDS = {"naver": ("네이버", "naver"), "gmail": ("지메일", "gmail", "구글 메일", "g메일"), "test": ("테스트",)}
PROVIDER_LABEL = {"naver": "네이버", "gmail": "지메일", "test": "테스트"}
MAX_SUBJECT, MAX_BODY = 200, 20000
ADDRESS = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
EXTERNAL_VERIFY_S = 6.0                        # 다른 사람에게 보낸 뒤 보낸편지함에서 찾아보는 시간(받은편지함에는 오지 않는다)
CONNECT_LIMIT, CONNECT_WINDOW_S = 5, 600.0      # 주소당
CONNECT_LIMIT_ALL = 15                         # 주소를 바꿔 가며 시도하는 것도 막는다(같은 시간 창)
MAX_ACCOUNTS = 5


class Mismatch(Exception):
    """승인한 초안과 보내려는 초안이 다르다(해시 불일치). 보내지 않는다."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Draft:
    draft_id: str
    sender: str
    to: str
    fields: dict[str, str]
    sha256: str            # 승인 카드의 해시 (draft_sha256, kind "send")
    message_id: str
    eml_path: Path
    eml_sha256: str        # 디스크의 .eml 바이트 해시
    external: bool = False  # 받는 사람이 사용자 본인이 아니다(D77)

    @property
    def url(self) -> str:
        return f"mailto:{self.to}"


@dataclass
class _Status:
    status: str
    checked_at: str | None = None


def build_eml(sender: str, to: str, subject: str, body: str, message_id: str, date: datetime) -> bytes:
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg["Date"], msg["Message-ID"] = format_datetime(date), message_id
    msg.set_content(body, subtype="plain", charset="utf-8", cte="quoted-printable")
    return msg.as_bytes()


def body_of(raw: bytes) -> str:
    """받은 원문에서 본문 텍스트를 꺼낸다(대조용). 줄바꿈 표기와 끝 공백 차이는 무시한다."""
    msg = message_from_bytes(raw, policy=policy.default)
    part = msg.get_body(preferencelist=("plain",)) or msg
    try:
        text = part.get_content()
    except Exception:
        return ""
    return "\n".join(l.rstrip() for l in str(text).replace("\r\n", "\n").split("\n")).strip()


class Mailer:
    """addresses: 실행하는 쪽 설정(.env)에 적은 본인 주소. registry_path: 앱에서 연결해 등록한 본인 주소(로그인 확인된 것만)."""

    def __init__(self, addresses: list[str], secrets: SecretStore, backends: dict[str, Backend], outbox: Path,
                 *, per_hour: int = 5, per_day: int = 20, state_path: Path | None = None, verify_wait_s: float = 20.0,
                 clock=time.time, registry_path: Path | None = None, max_accounts: int = MAX_ACCOUNTS,
                 allow_others: bool = False):
        self.allow_others = allow_others   # 다른 사람에게 보내기(D77). 꺼져 있으면 본인 주소 밖은 모두 not_allowed
        self.secrets, self.backends = secrets, backends
        self.outbox, self.state_path = Path(outbox), state_path
        self.per_hour, self.per_day, self.verify_wait_s, self.clock = per_hour, per_day, verify_wait_s, clock
        self.registry_path, self.max_accounts = registry_path, max_accounts
        self._status: dict[str, _Status] = {}
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        # 지원하는 서비스의 주소만 남긴다(그 밖의 도메인은 첫 구현에서 목록에 넣지 않는다)
        self._fixed = [a for a in dict.fromkeys(x.strip().lower() for x in addresses if x.strip())
                       if self.provider_of(a) in self.backends]
        self._registered: list[str] = []
        self._default: str | None = None
        if registry_path is not None and Path(registry_path).exists():
            try:
                data = json.loads(Path(registry_path).read_text())
                self._registered = [a for a in data.get("addresses", []) if isinstance(a, str)
                                    and self.provider_of(a) in self.backends]
                self._default = data.get("default") if isinstance(data.get("default"), str) else None
            except (ValueError, OSError):
                pass

    @property
    def addresses(self) -> list[str]:
        """보낼 수 있는 본인 주소. 기본 계정이 맨 앞. 설정에 적은 주소 + 앱에서 로그인을 확인하고 등록한 주소."""
        out = list(dict.fromkeys([*self._fixed, *self._registered]))
        if self._default in out:
            out.remove(self._default)
            out.insert(0, self._default)
        return out

    def _save_registry(self) -> None:
        if self.registry_path is None:
            return
        path = Path(self.registry_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:   # 주소만. 비밀번호는 보안 저장소에만 있다
            json.dump({"addresses": self._registered, "default": self._default}, f)

    # ---- 계정 ----
    def provider_of(self, address: str) -> str | None:
        dom = address.rsplit("@", 1)[-1].lower()
        return DOMAINS.get(dom) or ("test" if dom == "mail.test" else None)

    @property
    def default(self) -> str | None:
        return self.addresses[0] if self.addresses else None

    def self_contacts(self) -> dict[str, str]:
        """대화 담당에게 줄 '나'의 주소. 로컬 모델에만 들어간다."""
        if not self.addresses:
            return {}
        out = {"나(사용자 본인)": self.addresses[0]}
        for a in self.addresses:
            out.setdefault(f"나({PROVIDER_LABEL.get(self.provider_of(a) or '', '메일')})", a)
        return out

    def target_in(self, text: str) -> str | None:
        """글에 들어 있는 본인 주소(있으면 첫 번째). 발송 작업인지 가리는 데 쓴다."""
        low = text.lower()
        hits = [(low.find(a), a) for a in self.addresses if a in low]
        return min(hits)[1] if hits else None

    def _backend(self, address: str) -> Backend:
        return self.backends[self.provider_of(address) or ""]

    def accounts(self) -> list[dict]:
        out = []
        for i, a in enumerate(self.addresses):
            st = self._status.get(a)
            has = self.secrets.get(a) is not None
            status = "not_set" if not has else (st.status if st else "ready")
            out.append({"provider": self.provider_of(a), "address": a, "status": status, "default": i == 0,
                        "checked_at": st.checked_at if st and has else None})
        return out

    def _check_one(self, address: str, password: str) -> str:
        try:
            self._backend(address).check_login(address, password)
            return "ready"
        except MailError as e:
            return e.reason if e.reason in ("auth_failed", "unreachable") else "unreachable"

    def check(self) -> list[dict]:
        """보관한 비밀번호로 계정마다 로그인해 본다(보내지 않음)."""
        for a in self.addresses:
            pw = self.secrets.get(a)
            if pw is not None:
                self._status[a] = _Status(self._check_one(a, pw), _now_iso())
        return self.accounts()

    def connect(self, address: str, password: str, make_default: bool = False) -> list[dict]:
        """앱에서 받은 주소와 앱 비밀번호. **로그인이 확인된 뒤에만** 비밀번호를 보관하고 주소를 본인 주소로 등록한다.

        실패하면 아무것도 보관·등록하지 않는다. 지원하지 않는 메일 서비스는 not_allowed, 계정 수 한도는 too_many_accounts.
        """
        address = address.strip().lower()
        if self.provider_of(address) not in self.backends:
            raise MailError("not_allowed")
        known = address in self.addresses
        if not known and len(self.addresses) >= self.max_accounts:
            raise MailError("too_many_accounts")
        now = self.clock()
        with self._lock:
            for k in list(self._attempts):
                self._attempts[k] = [t for t in self._attempts[k] if now - t < CONNECT_WINDOW_S]
            if len(self._attempts.get(address, [])) >= CONNECT_LIMIT or \
                    sum(len(v) for v in self._attempts.values()) >= CONNECT_LIMIT_ALL:
                raise MailError("rate_limited")
            self._attempts.setdefault(address, []).append(now)
        result = self._check_one(address, password) if PASSWORD.match(password) else "auth_failed"
        if result == "ready":
            self.secrets.set(address, password)
            self._status[address] = _Status("ready", _now_iso())
            if not known:
                self._registered.append(address)
            if make_default or (self._default is None and not self._fixed and self._registered[:1] == [address]):
                self._default = address
            self._save_registry()
            return self.accounts()
        out = self.accounts()
        if not known:   # 등록되지 않은 주소의 실패: 목록에는 없지만 이번 시도의 결과는 보여 준다
            out.append({"provider": self.provider_of(address), "address": address, "status": result, "default": False,
                        "checked_at": _now_iso()})
        for acc in out:   # 이번 시도의 결과를 보여 준다. 이미 보관한 값이 있으면 그것은 그대로 둔다
            if acc["address"] == address:
                acc["status"], acc["checked_at"] = result, _now_iso()
        return out

    def disconnect(self, address: str) -> list[dict]:
        address = address.strip().lower()
        if address not in self.addresses:
            raise MailError("not_allowed")
        self.secrets.delete(address)
        self._status.pop(address, None)
        if address in self._registered:   # 앱에서 등록한 주소는 목록에서도 뺀다(설정에 적은 주소는 not_set으로 남는다)
            self._registered.remove(address)
        if self._default == address:
            self._default = None
        self._save_registry()
        return self.accounts()

    def precheck(self, address: str) -> None:
        """승인을 요청하기 전에: 비밀번호가 있는가, 로그인이 되는가, 횟수 제한 안인가. 아니면 MailError."""
        pw = self.secrets.get(address)
        if pw is None:
            raise MailError("not_set")
        self._rate_check()
        status = self._check_one(address, pw)
        self._status[address] = _Status(status, _now_iso())
        if status != "ready":
            raise MailError(status)

    # ---- 읽기 (D54) ----
    def reading_account(self, service: str | None = None) -> str | None:
        """읽을 계정: 말한 서비스(naver·gmail)가 있으면 그 계정, 없으면 기본 계정. 비밀번호가 있는 것만."""
        for a in self.addresses:
            if (service is None or self.provider_of(a) == service) and self.secrets.get(a) is not None:
                return a
        return None

    def inbox_today(self, address: str | None = None, limit: int = 50) -> tuple[str, list[dict]]:
        """오늘(이 컴퓨터의 날짜) 받은 메일의 머리글 목록(최신 먼저). 본문은 받지 않고 읽음 표시도 바꾸지 않는다."""
        address = address or self.reading_account()
        if address is None or address not in self.addresses:
            raise MailError("not_set")
        pw = self.secrets.get(address)
        if pw is None:
            raise MailError("not_set")
        now = time.localtime(self.clock())
        y, mo, d = now.tm_year, now.tm_mon, now.tm_mday
        start = time.mktime((y, mo, d, 0, 0, 0, 0, 0, -1))
        # 서버의 날짜 기준이 다를 수 있어 하루 앞에서부터 받아 이 컴퓨터의 '오늘'로 다시 거른다
        prev = time.localtime(start - 86400)
        rows = self._backend(address).list_since(address, pw, (prev.tm_year, prev.tm_mon, prev.tm_mday), limit=limit * 2)
        return address, [r for r in rows if r["received_at"] >= start][:limit]

    def message_text(self, address: str, uid: str, max_chars: int = 4000) -> str:
        pw = self.secrets.get(address)
        if address not in self.addresses or pw is None:
            raise MailError("not_set")
        return self._backend(address).fetch_text(address, pw, uid, max_chars)

    # ---- 횟수 제한 ----
    def _sent_times(self) -> list[float]:
        if self.state_path is None or not self.state_path.exists():
            return list(getattr(self, "_mem_sent", []))
        try:
            return [float(t) for t in json.loads(self.state_path.read_text()).get("sent", [])]
        except (ValueError, OSError):
            return []

    def _rate_check(self) -> None:
        now = self.clock()
        times = self._sent_times()
        if sum(now - t < 3600 for t in times) >= self.per_hour or sum(now - t < 86400 for t in times) >= self.per_day:
            raise MailError("rate_limited")

    def _rate_record(self) -> None:
        now = self.clock()
        times = [t for t in self._sent_times() if now - t < 86400] + [now]
        if self.state_path is None:
            self._mem_sent = times
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.state_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"sent": times}, f)   # 시각만. 주소·내용은 적지 않는다

    # ---- 초안 고정 → 발송 ----
    def sender_for(self, service: str | None = None) -> str | None:
        """다른 사람에게 보낼 때 쓸 계정: 말한 서비스의 계정, 없으면 기본 계정."""
        return next((a for a in self.addresses if service and self.provider_of(a) == service), None) or self.default

    def prepare(self, to: str, subject: str, body: str, *, others=(), sender: str | None = None) -> Draft:
        """정책 검사 → 계정 확인 → 초안 고정(.eml). 통과하지 못하면 MailError(승인 요청도 하지 않는다).

        others: 이 작업에서 받는 사람으로 허락된 남의 주소(부르는 쪽이 코드로 정한 것). sender: 그때 보낼 내 계정(없으면 기본 계정).
        """
        to = to.strip().lower()
        external = to not in self.addresses
        if external:
            if not self.addresses:
                raise MailError("not_set")
            if not (self.allow_others and ADDRESS.match(to) and to in {o.strip().lower() for o in others}):
                raise MailError("not_allowed")
        if not subject.strip() or not body.strip() or len(subject) > MAX_SUBJECT or len(body) > MAX_BODY \
                or "\n" in subject or "\r" in subject:
            raise MailError("rejected")
        if external:
            sender = sender.strip().lower() if sender else self.default
            if sender not in self.addresses:
                raise MailError("not_allowed")
        else:
            sender = to   # 본인에게: 보내는 계정 = 받는 주소
        self.precheck(sender)
        fields = {FIELD_FROM: sender, FIELD_TO: to, FIELD_SUBJECT: subject, FIELD_BODY: body}
        message_id = f"<{pysecrets.token_hex(16)}@{sender.rsplit('@', 1)[-1]}>"
        eml = build_eml(sender, to, subject, body, message_id, datetime.now(timezone.utc))
        self.outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.outbox, 0o700)
        draft_id = "d_" + pysecrets.token_urlsafe(9)
        path = self.outbox / f"{draft_id}.eml"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(eml)
        return Draft(draft_id, sender, to, fields, draft_sha256("send", SEND_ACTION, f"mailto:{to}", fields),
                     message_id, path, hashlib.sha256(eml).hexdigest(), external)

    def discard(self, draft: Draft) -> None:
        try:
            draft.eml_path.unlink()
        except OSError:
            pass

    def send(self, draft: Draft, approved_sha256: str) -> dict:
        """승인한 해시와 고정 초안이 같고, 디스크의 .eml이 고정할 때와 같을 때만 보낸다. 끝나면 .eml을 지운다."""
        try:
            if approved_sha256 != draft.sha256 or \
                    draft_sha256("send", SEND_ACTION, draft.url, draft.fields) != draft.sha256:
                raise Mismatch("승인한 해시가 고정한 초안과 다르다")
            try:
                eml = draft.eml_path.read_bytes()
            except OSError:
                raise Mismatch("고정한 초안 파일이 없다") from None
            if hashlib.sha256(eml).hexdigest() != draft.eml_sha256:
                raise Mismatch("고정한 초안 파일이 바뀌었다")
            if draft.sender not in self.addresses:
                raise MailError("not_allowed")
            if draft.to not in self.addresses and not (draft.external and self.allow_others):
                raise MailError("not_allowed")
            if draft.fields.get(FIELD_TO) != draft.to or draft.fields.get(FIELD_FROM) != draft.sender:
                raise Mismatch("승인 카드의 주소와 보낼 주소가 다르다")
            pw = self.secrets.get(draft.sender)
            if pw is None:
                raise MailError("not_set")
            self._rate_check()
            backend = self._backend(draft.sender)
            backend.send(draft.sender, pw, draft.to, eml)
            sent_at = _now_iso()
            self._rate_record()
            if draft.external:
                # 받는 분의 받은편지함은 볼 수 없다: 내 보낸편지함에 남았는지만 잠깐 본다. '도착 확인'이라고 하지 않는다
                where, _ = backend.find(draft.sender, pw, draft.message_id, min(self.verify_wait_s, EXTERNAL_VERIFY_S))
                detail = "보낸편지함에서 확인됨(받는 분의 도착은 확인할 수 없음)" if where else "접수됨(받는 분의 도착은 확인할 수 없음)"
                return {"draft_id": draft.draft_id, "sha256": draft.sha256, "to": draft.to, "message_id": draft.message_id,
                        "sent_at": sent_at, "verified": "accepted_only", "detail": detail, "external": True}
            where, raw = backend.find(draft.sender, pw, draft.message_id, self.verify_wait_s)
            if where in ("inbox", "other") and raw is not None and body_of(raw) == body_of(eml):
                verified, detail = "delivered", "받은 메일의 본문이 보낸 내용과 같음"
            elif where in ("inbox", "other"):
                verified, detail = "accepted_only", "도착했지만 본문을 대조하지 못함"
            elif where == "sent":
                verified, detail = "accepted_only", "보낸편지함에서만 확인됨"
            else:
                verified, detail = "accepted_only", "접수됐지만 도착을 확인하지 못함"
            return {"draft_id": draft.draft_id, "sha256": draft.sha256, "to": draft.to, "message_id": draft.message_id,
                    "sent_at": sent_at, "verified": verified, "detail": detail}
        finally:
            self.discard(draft)
