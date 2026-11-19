"""전용 크롬 길: 맥에 깔린 진짜 크롬을 에이전트 전용 프로필로 띄워, 컨테이너 안과 같은 브라우저 서비스로 다룬다.

왜: 구글은 자동화된 브라우저의 **로그인**을 받지 않는다(2026-11-15 실기기). 꾸미거나 우회하지 않고 되는 길은 —
로그인은 사람이 진짜 크롬의 전용 프로필에서 손으로 해 두고, 에이전트는 그 프로필을 자동화 표시를 단 채로 여는 것이다.

달라지는 것(알고 쓰는 것):
- 격리가 없다. 브라우저가 컨테이너가 아니라 맥에서 돈다. 그래서 정해 둔 사이트에만 쓰고, 허용 호스트를 좁게 둔다.
- 로그인 상태가 키체인이 아니라 그 프로필 폴더(디스크)에 남는다. 평소 쓰는 크롬 프로필과는 다른 폴더다.
- 창이 맥 화면에 뜬다.

브라우저 서비스는 따로 띄우는 프로세스다. 이 프로세스의 환경(API 키 등)을 물려주지 않는다 — 필요한 것만 준다.
필요한 것: Playwright가 깔린 파이썬(`HOST_BROWSER_PYTHON`). 엔진의 .venv에는 넣지 않았다(큰 설치 — 이 길을 켜기로 하면 그때).
"""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path
from typing import Iterator

from .client import SERVICE_DIR, BrowserSession

PROFILE = Path.home() / ".local" / "share" / "local-ai-agent" / "chrome-agent-profile"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
_lock = threading.Lock()   # 프로필 하나를 크롬 둘이 동시에 열 수 없다


def host_python() -> str | None:
    """브라우저 서비스를 맥에서 돌릴 파이썬(Playwright가 깔린 것). 없으면 이 길을 쓸 수 없다."""
    p = os.environ.get("HOST_BROWSER_PYTHON", "").strip()
    return p if p and Path(p).exists() else None


def available() -> bool:
    return host_python() is not None and Path(CHROME).exists()


def service_env(profile: Path, extra_args: tuple[str, ...] = ()) -> dict[str, str]:
    """브라우저 서비스에 줄 환경: 필요한 것만(이 프로세스의 API 키·토큰을 넘기지 않는다)."""
    keep = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER") if k in os.environ}
    return {**keep, "BROWSER_REAL_PROFILE": str(profile), "BROWSER_CHANNEL": "chrome",
            "BROWSER_EXTRA_ARGS": "\n".join(a for a in extra_args if a.startswith("--"))}


@contextlib.contextmanager
def session(profile: Path | None = None, *, python: str | None = None, extra_args: tuple[str, ...] = (),
            start_timeout: float = 40.0) -> Iterator[BrowserSession]:
    """전용 프로필로 진짜 크롬을 띄운 브라우저 세션. 블록을 나가면 크롬을 닫는다(프로필은 남는다)."""
    py = python or host_python()
    if py is None:
        raise RuntimeError("전용 크롬 길을 쓸 수 없다: HOST_BROWSER_PYTHON(Playwright가 깔린 파이썬)이 없다")
    profile = Path(profile or PROFILE)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _lock:
        s = BrowserSession(command=[py, "-u", str(SERVICE_DIR / "service.py")], env=service_env(profile, extra_args), start_timeout=start_timeout)
        try:
            yield s
        finally:
            s.close()


def open_for_login(profile: Path | None = None, url: str = "https://accounts.google.com/") -> list[str]:
    """사람이 손으로 로그인할 보통의 크롬 창을 띄우는 명령(자동화 없음). 화면 앞에서, 키체인을 쓸 수 있는 세션에서 돌려야 한다."""
    profile = Path(profile or PROFILE)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    return [CHROME, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", url]
