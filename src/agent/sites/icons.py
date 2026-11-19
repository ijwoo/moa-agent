"""사이트 아이콘(D110, 앱 요청 — 연동 센터의 사이트 줄): 로그인한 사이트를 에이전트 브라우저가 열었을 때 그 사이트가 적어 둔 아이콘을 받아 둔다.

- 받는 때: 로그인 상태를 보관할 때(로그인 성공·로그인 확인·다시 로그인) — 이미 그 사이트를 열어 둔 브라우저에서. 따로 접속하지 않는다.
- 받는 곳: 그 페이지의 apple-touch-icon·icon, 없으면 /favicon.ico(브라우저 서비스 `site_icon`). 다른 아이콘 서비스는 거치지 않는다.
- 두는 곳: 맥 파일 `site_icons/{사이트}.png`(64px PNG, macOS `sips`로 바꾼다, 0600). 못 받았으면 하루 뒤에 다시 해 본다.
  회사 안에서만 열리는 사이트처럼 폰이 직접 못 받는 아이콘을 앱이 GET /v1/sites/{site}/icon으로 가져간다.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DIR = Path(os.path.expanduser("~/.local/share/local-ai-agent/site_icons"))
RETRY_S = 24 * 3600
SIZE = 64
KEY = re.compile(r"[a-z0-9_-]{1,32}")
EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
       "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico", "image/ico": ".ico", "application/octet-stream": ".ico"}


class IconStore:
    def __init__(self, path: Path | None = None, clock=time.time):
        self.dir = Path(path or DIR)
        self.clock = clock

    def _file(self, key: str) -> Path | None:
        return self.dir / f"{key}.png" if KEY.fullmatch(key or "") else None

    def get(self, key: str) -> bytes | None:
        f = self._file(key)
        try:
            return f.read_bytes() if f is not None and f.is_file() else None
        except OSError:
            return None

    def _tried(self) -> dict:
        try:
            d = json.loads((self.dir / "tried.json").read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def wants(self, key: str) -> bool:
        """아직 없고, 하루 안에 받아 보다 못 받은 적이 없으면."""
        f = self._file(key)
        if f is None or f.is_file():
            return False
        at = self._tried().get(key)
        return not isinstance(at, (int, float)) or self.clock() - at >= RETRY_S

    def _note_tried(self, key: str) -> None:
        d = self._tried()
        d[key] = self.clock()
        self._write(self.dir / "tried.json", json.dumps(d).encode())

    def _write(self, path: Path, data: bytes) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def save(self, key: str, raw: bytes, ctype: str) -> bool:
        """받은 그림을 64px PNG로 바꿔 둔다. 그림으로 읽히지 않으면 버린다."""
        f = self._file(key)
        if f is None or not raw:
            return False
        ext = EXT.get((ctype or "").lower())
        if ext is None:
            return False
        with tempfile.TemporaryDirectory() as td:
            src, out = Path(td) / f"in{ext}", Path(td) / "out.png"
            src.write_bytes(raw)
            try:
                r = subprocess.run(["sips", "-s", "format", "png", "-Z", str(SIZE), str(src), "--out", str(out)],
                                   capture_output=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                return False
            if r.returncode != 0 or not out.is_file() or out.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
                return False
            self._write(f, out.read_bytes())
        return True

    def grab(self, b, key: str) -> bool:
        """열려 있는 브라우저 b(그 사이트의 화면)에서 아이콘을 받아 둔다. 실패해도 일을 막지 않는다."""
        if not self.wants(key):
            return False
        ok = False
        try:
            got = b.request("site_icon") or {}
            if got.get("type") and isinstance(got.get("data"), str):
                ok = self.save(key, base64.b64decode(got["data"]), got["type"])
        except Exception as e:
            sys.stderr.write(f"[site-icon] {time.strftime('%Y-%m-%d %H:%M:%S')} site={key} error={type(e).__name__}\n")
        if not ok:
            self._note_tried(key)
        sys.stderr.write(f"[site-icon] {time.strftime('%Y-%m-%d %H:%M:%S')} site={key} saved={ok}\n")
        return ok

    def delete(self, key: str) -> None:
        f = self._file(key)
        if f is not None:
            f.unlink(missing_ok=True)
