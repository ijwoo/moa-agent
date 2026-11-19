"""호스트 쪽 브라우저 세션: 컨테이너 안 browser_service와 표준 입출력(JSON 한 줄씩)으로 통신한다 (D18).

포트를 열지 않는다. 명령은 이 프로세스만 보낼 수 있고, 같은 네트워크의 웹페이지는 제어 채널에 닿을 수 없다.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from typing import Any

from .container import IMAGE, REPO_ROOT, run_args

SERVICE_DIR = REPO_ROOT / "browser_service"


class BrowserError(Exception):
    """서비스가 ok=false로 답했거나(작업 실패), 서비스가 응답하지 않을 때."""


class BrowserSession:
    def __init__(self, network: str = "none", start_timeout: float = 30.0, request_timeout: float = 30.0,
                 extra_args: list[str] | None = None, command: list[str] | None = None, env: dict[str, str] | None = None):
        """extra_args: Sandbox Manager가 붙이는 docker run 옵션(이름·라벨·프로필 볼륨). 코드가 만든 값만 넣는다.
        command·env: 컨테이너가 아닌 곳에서 같은 서비스를 띄울 때(전용 크롬 길 — browser/hostchrome.py). 코드가 만든 값만."""
        self.request_timeout = request_timeout
        self._next_id = 1
        self._req_lock = threading.Lock()   # 손 넘기기(D106): 작업 스레드와 HTTP 스레드가 같은 브라우저에 번갈아 묻는다
        self._lines: queue.Queue[str | None] = queue.Queue()
        cmd = command or [
            "docker", "run", "-i", *run_args(network=network), *(extra_args or []),
            "-v", f"{SERVICE_DIR}:/app:ro",
            IMAGE, "python3", "-u", "/app/service.py",
        ]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env=env,
        )
        self._stderr: list[str] = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        ready = self._wait_line(start_timeout)
        if not json.loads(ready).get("ok"):
            self.close()
            raise BrowserError(f"브라우저 서비스 시작 실패: {ready}")

    def _read_stdout(self):
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _read_stderr(self):
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            self._stderr = (self._stderr + [line.rstrip()])[-20:]

    def _wait_line(self, timeout: float) -> str:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise BrowserError(f"브라우저 서비스 응답 시간 초과({timeout}s)") from None
        if line is None:
            raise BrowserError("브라우저 서비스가 종료됨: " + " | ".join(self._stderr[-5:]))
        return line

    def request(self, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._req_lock:
            return self._request(op, args)

    def _request(self, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._proc.poll() is not None:
            raise BrowserError("브라우저 서비스가 이미 종료됨")
        rid = self._next_id
        self._next_id += 1
        assert self._proc.stdin is not None
        self._proc.stdin.write(json.dumps({"id": rid, "op": op, "args": args or {}}, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        reply = json.loads(self._wait_line(self.request_timeout))
        if reply.get("id") != rid:
            raise BrowserError(f"응답 id 불일치: {reply.get('id')} != {rid}")
        if not reply.get("ok"):
            raise BrowserError(reply.get("error", "알 수 없는 오류"))
        return reply["result"]

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                assert self._proc.stdin is not None
                self._proc.stdin.write(json.dumps({"id": 0, "op": "close"}) + "\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=10)
            except Exception:
                self._proc.kill()
                self._proc.wait(timeout=10)

    def __enter__(self) -> BrowserSession:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
