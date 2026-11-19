"""사용자가 대화에 붙인 사진·PDF (D73).

앱이 `POST /v1/uploads`로 올리면 맥의 **메모리에만** 둔다(디스크·기록·로그에 쓰지 않는다). 시간이 지나면 지운다.
붙인 것은 그 대화의 답을 위해 Claude로 간다(사용자 결정 2026-11-15 — 로컬 모델은 글만 읽는다).

- 종류는 앱이 말한 Content-Type이 아니라 **파일 머리의 글자**로 정한다(앱이 보낸 값은 믿지 않는다).
- 받는 것: JPEG·PNG·GIF·WebP 사진, PDF. 영상은 받지 않는다(Claude가 입력으로 받지 않는다 — 2026-11-15 공식 문서 확인).
- 한도(2026-11-15 공식 문서: 사진 한 장 base64 10MB, PDF 100쪽·요청 128MB): 사진 7MB, PDF 10MB로 좁게 잡는다.
"""

from __future__ import annotations

import base64
import secrets
import threading
import time
from dataclasses import dataclass

IMAGE_MAX = 7 * 1024 * 1024     # base64로 약 9.3MB — Claude의 한 장 한도(10MB) 아래
PDF_MAX = 10 * 1024 * 1024
UPLOAD_MAX = max(IMAGE_MAX, PDF_MAX)
TOTAL_MAX = 80 * 1024 * 1024    # 메모리에 두는 전체
TTL_S = 30 * 60
PER_MESSAGE = 4
NAME_MAX = 80


class UploadError(Exception):
    """code: unsupported_media_type | payload_too_large | empty"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def sniff(data: bytes) -> tuple[str, str] | None:
    """파일 머리로 (종류, 미디어 타입)을 정한다. 모르는 것은 None."""
    if data[:3] == b"\xff\xd8\xff":
        return "image", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "image/webp"
    if data[:5] == b"%PDF-":
        return "pdf", "application/pdf"
    return None


def clean_name(name: str | None, kind: str) -> str:
    """보여 줄 이름: 경로·제어 글자를 떼고 짧게. 없으면 종류로."""
    name = "".join(ch for ch in (name or "") if ch.isprintable() and ch not in "/\\\"<>[]").strip()
    name = name[-NAME_MAX:] if len(name) > NAME_MAX else name
    return name or ("사진" if kind == "image" else "문서.pdf")


@dataclass
class Upload:
    id: str
    kind: str      # image | pdf
    mime: str
    name: str
    data: bytes
    at: float

    def public(self, now: float) -> dict:
        return {"id": self.id, "kind": self.kind, "mime": self.mime, "bytes": len(self.data), "name": self.name,
                "expires_in_s": max(0, int(TTL_S - (now - self.at)))}

    def block(self) -> dict:
        """Claude Messages API의 내용 블록(2026-11-15 공식 문서의 꼴)."""
        source = {"type": "base64", "media_type": self.mime, "data": base64.standard_b64encode(self.data).decode("ascii")}
        return {"type": "image" if self.kind == "image" else "document", "source": source}


class UploadStore:
    """붙인 것을 메모리에 둔다. 오래된 것·넘치는 것은 버린다."""

    def __init__(self, clock=time.monotonic, ttl_s: float = TTL_S, total_max: int = TOTAL_MAX):
        self.clock, self.ttl_s, self.total_max = clock, ttl_s, total_max
        self._items: dict[str, Upload] = {}
        self._lock = threading.Lock()

    def _purge(self) -> None:
        now = self.clock()
        for k in [k for k, u in self._items.items() if now - u.at > self.ttl_s]:
            del self._items[k]
        while sum(len(u.data) for u in self._items.values()) > self.total_max and self._items:
            del self._items[min(self._items, key=lambda k: self._items[k].at)]   # 넘치면 오래된 것부터

    def put(self, data: bytes, name: str | None = None) -> dict:
        if not data:
            raise UploadError("empty")
        got = sniff(data)
        if got is None:
            raise UploadError("unsupported_media_type")
        kind, mime = got
        if len(data) > (IMAGE_MAX if kind == "image" else PDF_MAX):
            raise UploadError("payload_too_large")
        u = Upload("u_" + secrets.token_urlsafe(12), kind, mime, clean_name(name, kind), bytes(data), self.clock())
        with self._lock:
            self._items[u.id] = u
            self._purge()
        return u.public(self.clock())

    def get(self, ids: list[str]) -> list[Upload]:
        """아직 있는 것만(차례 그대로). 쓰는 동안은 시간을 새로 센다."""
        with self._lock:
            self._purge()
            out = [self._items[i] for i in ids if i in self._items]
            for u in out:
                u.at = self.clock()
            return out

    def drop(self, ids: list[str]) -> None:
        with self._lock:
            for i in ids:
                self._items.pop(i, None)

    def __len__(self) -> int:
        with self._lock:
            self._purge()
            return len(self._items)


def describe(items: list[Upload]) -> str:
    """대화 기록·지시문에 쓰는 한 줄: 종류와 개수, 이름(내용은 없다)."""
    photos = [u for u in items if u.kind == "image"]
    docs = [u for u in items if u.kind == "pdf"]
    parts = []
    if photos:
        parts.append(f"사진 {len(photos)}장")
    if docs:
        parts.append("PDF " + ", ".join(f"'{u.name}'" for u in docs))
    return ", ".join(parts)
