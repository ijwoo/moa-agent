"""Sandbox Manager (Phase 5): 사용자별 브라우저 샌드박스의 생성·실행·종료.

- 사용자마다: 전용 컨테이너(작업할 때만 뜨고 끝나면 사라짐) + 전용 프로필 볼륨(쿠키·로그인 세션이 다음 작업까지 유지).
- 분리 대상: 브라우저 프로필·쿠키, 임시 파일(컨테이너 tmpfs), 실행 로그 경로. 한 사용자의 작업은 한 번에 하나.
- 동시 실행 제한(max_concurrent)과 대기열 상한(max_pending). 넘치면 SandboxBusy.
- 장애 복구: 시작 전 같은 이름의 잔여 컨테이너 제거, cleanup_orphans()로 라벨이 붙은 고아 컨테이너 정리.

사용자 id는 Docker 이름·라벨에 직접 쓰지 않고 해시(키)로만 쓴다. 모든 docker 명령은 고정 인자 목록으로 실행한다.

미구현 (D27): 프로필 볼륨의 저장 암호화, 사용자별 네트워크 분리, 다중 호스트. 실제 계정 정보는 넣지 않는다.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
import threading
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from ..browser.client import BrowserSession
from ..browser.container import REPO_ROOT

LABEL_MANAGED = "laa.managed=1"
PROFILE_MOUNT = "/data/profile"


class SandboxBusy(Exception):
    """대기열이 가득 찼다. 나중에 다시 시도한다."""


def user_key(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id or len(user_id) > 256:
        raise ValueError("user_id는 1~256자 문자열이어야 한다")
    return hashlib.sha256(user_id.encode()).hexdigest()[:16]


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


def host_timezone() -> str | None:
    """이 컴퓨터의 시간대 이름("Asia/Seoul"). 알 수 없으면 None."""
    import os
    import re
    try:
        m = re.search(r"zoneinfo/([A-Za-z_]+(?:/[A-Za-z0-9_+-]+){0,2})$", os.path.realpath("/etc/localtime"))
    except OSError:
        return None
    return m.group(1) if m else None


class SandboxManager:
    def __init__(
        self,
        network: str = "none",
        max_concurrent: int = 2,
        max_pending: int = 8,
        session_factory: Callable[..., Any] = BrowserSession,
        docker: Callable[..., subprocess.CompletedProcess] = _docker,
        runs_dir: Path | None = None,
        env: dict[str, str] | None = None,
        persist_profile: bool = True,
    ):
        self.env = dict(env or {})   # 브라우저 컨테이너에 줄 고정 환경 변수(코드가 정한 값만)
        # 브라우저의 시간대는 이 컴퓨터의 것으로(컨테이너는 UTC라, 사이트가 시각을 9시간 어긋나게 보여 주고 한국어 설정과도 맞지 않았다 — 2026-11-16 진단)
        tz = host_timezone()
        if tz:
            self.env.setdefault("BROWSER_TZ", tz)
        # False면 프로필 볼륨을 붙이지 않는다: 브라우저는 일회용 프로필로 뜨고 닫으면 아무것도 남지 않는다
        # (실제 사이트 로그인용 — 로그인 상태는 볼륨이 아니라 암호화 저장소에 둔다, D52)
        self.persist_profile = persist_profile
        self.network = network
        self.max_pending = max_pending
        self._factory = session_factory
        self._docker = docker
        self._slots = threading.Semaphore(max_concurrent)
        self._user_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._guard = threading.Lock()
        self._pending = 0
        self._active: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent + max_pending, thread_name_prefix="sandbox")
        self.runs_dir = runs_dir or (REPO_ROOT / "runs" / "users")

    # ---- 이름 (모두 해시 키에서 파생) ----
    @staticmethod
    def container_name(key: str) -> str:
        return f"laa-browser-{key}"

    @staticmethod
    def volume_name(key: str) -> str:
        return f"laa-profile-{key}"

    def user_log_path(self, user_id: str, filename: str = "steps.jsonl") -> Path:
        d = self.runs_dir / user_key(user_id)
        d.mkdir(parents=True, exist_ok=True)
        return d / filename

    def screenshot_dir(self, user_id: str, run_id: str) -> Path:
        if not run_id.replace("-", "").isalnum():
            raise ValueError("run_id는 영숫자와 '-'만")
        return self.runs_dir / user_key(user_id) / "screenshots" / run_id

    def purge_screenshots(self, user_id: str, older_than_days: float = 7.0) -> int:
        """보관 기간이 지난 스크린샷 실행 폴더를 지운다. 지운 폴더 수를 돌려준다."""
        base = self.runs_dir / user_key(user_id) / "screenshots"
        cutoff, removed = time.time() - older_than_days * 86400, 0
        for d in base.iterdir() if base.is_dir() else []:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d)
                removed += 1
        return removed

    def _extra_args(self, key: str, env: dict[str, str] | None = None) -> list[str]:
        return [
            "--name", self.container_name(key),
            "--hostname", "laa-browser",
            "--label", LABEL_MANAGED, "--label", f"laa.user={key}",
            *(["-v", f"{self.volume_name(key)}:{PROFILE_MOUNT}", "-e", f"PROFILE_DIR={PROFILE_MOUNT}"]
              if self.persist_profile else []),
            *[x for k, v in {**self.env, **(env or {})}.items() for x in ("-e", f"{k}={v}")],
        ]

    # ---- 생명주기 ----
    @contextmanager
    def session(self, user_id: str, env: dict[str, str] | None = None) -> Iterator[Any]:
        """사용자 전용 브라우저 세션. 블록을 나가면 컨테이너는 사라지고 프로필 볼륨만 남는다.

        env: 이 세션에만 더할 환경 변수(코드가 정한 값만 — 모델·사용자 입력을 넣지 않는다)."""
        key = user_key(user_id)
        with self._guard:
            if self._pending >= self.max_pending:
                raise SandboxBusy(f"대기 중인 작업이 {self._pending}개라 더 받을 수 없다")
            self._pending += 1
            lock = self._user_locks[key]
        try:
            lock.acquire()          # 사용자당 한 번에 하나 (프로필을 두 컨테이너가 동시에 쓰지 않게)
            self._slots.acquire()   # 전체 동시 실행 제한
        finally:
            with self._guard:
                self._pending -= 1
        name = self.container_name(key)
        session = None
        try:
            self._docker("rm", "-f", name)  # 이전 비정상 종료의 잔여 컨테이너
            with self._guard:
                self._active.add(key)
            session = self._factory(network=self.network, extra_args=self._extra_args(key, env))
            yield session
        finally:
            try:
                if session is not None:
                    session.close()
            finally:
                self._docker("rm", "-f", name)  # 정상이면 이미 없음(--rm). 멈춘 경우 강제 제거
                with self._guard:
                    self._active.discard(key)
                self._slots.release()
                lock.release()

    def submit(self, user_id: str, fn: Callable[[Any], Any]) -> Future:
        """작업을 대기열에 넣는다. fn(browser_session)의 결과가 Future로 온다."""
        user_key(user_id)  # 형식 오류는 바로 알린다

        def job():
            with self.session(user_id) as browser:
                return fn(browser)

        return self._pool.submit(job)

    def active_users(self) -> set[str]:
        with self._guard:
            return set(self._active)

    # ---- 장애 복구·정리 ----
    def cleanup_orphans(self) -> int:
        """관리 라벨이 붙었는데 지금 이 매니저가 쓰고 있지 않은 컨테이너를 제거한다 (프로세스 재시작 후 호출)."""
        out = self._docker("ps", "-a", "--filter", f"label={LABEL_MANAGED}", "--format", "{{.Names}}").stdout
        active = {self.container_name(k) for k in self.active_users()}
        removed = 0
        for name in out.split():
            if name.startswith("laa-browser-") and name not in active:
                self._docker("rm", "-f", name)
                removed += 1
        return removed

    def delete_user_data(self, user_id: str) -> bool:
        """사용자의 프로필 볼륨(쿠키·세션)과 실행 기록·스크린샷 폴더를 지운다. 실행 중이면 지우지 않는다."""
        key = user_key(user_id)
        lock = self._user_locks[key]
        if not lock.acquire(blocking=False):
            return False
        try:
            self._docker("rm", "-f", self.container_name(key))
            shutil.rmtree(self.runs_dir / key, ignore_errors=True)
            return self._docker("volume", "rm", "-f", self.volume_name(key)).returncode == 0
        finally:
            lock.release()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)
