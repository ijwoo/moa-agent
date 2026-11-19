"""로그인 상태(쿠키) 보관소 (D52·D27). 로그인된 상태는 계정에 들어갈 수 있는 열쇠라서 암호화된 저장소에만 둔다.

기본은 맥 키체인. `security -i`는 한 줄이 4천 자쯤에서 잘리므로(실측) 압축한 뒤 여러 항목으로 나눠 넣는다.
값은 명령 인자로 넘기지 않고(프로세스 목록에 보임) 표준 입력으로 넘긴다. 값은 기록·예외 메시지에 넣지 않는다.
"""

from __future__ import annotations

import base64
import hashlib
import re
import subprocess
import zlib
from typing import Protocol

SERVICE = "local-ai-agent.site"
CHUNK = 3000
MAX_CHUNKS = 64     # 압축 후 약 190KB까지
_KEY = re.compile(r"^[a-z0-9_.:-]{1,64}$")
_HEAD = re.compile(r"^v1:(\d{1,3}):([0-9a-f]{16})$")


class BlobStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, text: str) -> None: ...
    def delete(self, key: str) -> None: ...


class MemoryBlobs:
    def __init__(self):
        self._d: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._d.get(key)

    def set(self, key: str, text: str) -> None:
        self._d[key] = text

    def delete(self, key: str) -> None:
        self._d.pop(key, None)


class KeychainBlobs:
    def __init__(self, service: str = SERVICE):
        self.service = service

    def _read(self, account: str) -> str | None:
        r = subprocess.run(["security", "find-generic-password", "-s", self.service, "-a", account, "-w"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.rstrip("\n") if r.returncode == 0 and r.stdout.strip() else None

    def _write(self, account: str, value: str) -> None:
        cmd = f'add-generic-password -U -s {self.service} -a "{account}" -w "{value}"\n'
        r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            raise RuntimeError("보안 저장소에 저장하지 못했다")

    def _remove(self, account: str) -> bool:
        return subprocess.run(["security", "delete-generic-password", "-s", self.service, "-a", account],
                              capture_output=True, text=True, timeout=10).returncode == 0

    def get(self, key: str) -> str | None:
        if not _KEY.match(key):
            return None
        m = _HEAD.match(self._read(key) or "")
        if not m:
            return None
        parts = [self._read(f"{key}#{i}") for i in range(int(m.group(1)))]
        if any(p is None for p in parts):
            return None
        data = "".join(parts)   # type: ignore[arg-type]
        if hashlib.sha256(data.encode()).hexdigest()[:16] != m.group(2):
            return None
        try:
            return zlib.decompress(base64.b64decode(data)).decode()
        except Exception:
            return None

    def set(self, key: str, text: str) -> None:
        if not _KEY.match(key):
            raise ValueError("키 형식 오류")
        data = base64.b64encode(zlib.compress(text.encode(), 9)).decode()
        chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [""]
        if len(chunks) > MAX_CHUNKS:
            raise ValueError("보관할 내용이 너무 크다")
        self.delete(key)
        for i, c in enumerate(chunks):
            self._write(f"{key}#{i}", c)
        self._write(key, f"v1:{len(chunks)}:{hashlib.sha256(data.encode()).hexdigest()[:16]}")
        if self.get(key) != text:
            self.delete(key)
            raise RuntimeError("보안 저장소에 저장하지 못했다")

    def delete(self, key: str) -> None:
        if not _KEY.match(key):
            return
        self._remove(key)
        for i in range(MAX_CHUNKS):
            if not self._remove(f"{key}#{i}"):
                break
