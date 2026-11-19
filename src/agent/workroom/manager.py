"""작업실 관리자(D96): 프로젝트 하나 = 작업실 하나. 컨테이너를 띄우고, 명령을 넣고, 상한을 건다.

격리(docs/design/workroom.md "지키는 선"):
- 작업실 컨테이너는 바깥 길이 없는 내부망(`--internal`)에만 붙는다. 바깥으로 나가는 유일한 길은 나가는 문(프록시) 컨테이너 —
  공인 IP로만 잇는다(egress_proxy.py).
- 비루트 사용자, 모든 권한 제거, no-new-privileges, 루트 파일시스템 읽기 전용, Docker 소켓 없음.
- 마운트는 그 프로젝트 폴더 하나(`/work`). 환경 변수는 프록시 주소뿐 — `.env`·키·키체인·로그인 프로필은 넣지 않는다.
- 메모리·CPU·프로세스 상한, 명령마다 시간 상한(넘으면 KILL), 출력은 잘라서 돌려준다.

명령 문자열은 모델이 쓴다(이것이 원칙 3번의 예외다). 그 명령은 **컨테이너 안의 bash에만** 건넨다 —
맥의 셸로 해석되지 않게 docker CLI에 인자 배열로 넘긴다.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

IMAGE = os.environ.get("WORKROOM_IMAGE", "local-ai-agent/workroom:0.2")
ROOT = Path(os.environ.get("WORKROOM_DIR", "~/.local/share/local-ai-agent/workroom")).expanduser()
UID = 1100
MEMORY, CPUS, PIDS = "2g", "2", 512
CMD_TIMEOUT_S, MAX_CMD_TIMEOUT_S = 120, 600
MAX_OUTPUT = 12_000           # 모델에게 돌려줄 출력(앞·뒤를 남기고 가운데를 자른다)
PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


class WorkroomError(Exception):
    """code: no_docker | no_image | bad_project | start_failed | closed"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


@dataclass
class RunResult:
    exit_code: int
    output: str            # stdout+stderr를 합쳐 잘라 둔 것
    seconds: float
    timed_out: bool = False
    truncated: bool = False

    def for_model(self) -> str:
        tail = " (시간 초과로 끊었다)" if self.timed_out else ""
        return f"[exit {self.exit_code}{tail}, {self.seconds:.1f}s]\n{self.output}"


def _docker(*args: str, timeout: float = 60, check: bool = True) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise WorkroomError("no_docker") from None
    if check and p.returncode != 0:
        raise WorkroomError("start_failed", (p.stderr or p.stdout).strip()[:300])
    return p


def _clip(text: str, limit: int = MAX_OUTPUT) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    half = limit // 2
    return text[:half] + f"\n… ({len(text) - limit}자 잘림) …\n" + text[-half:], True


@dataclass
class Workroom:
    """띄운 작업실 하나. with 문으로 쓰거나 close()를 부른다."""
    project: str
    path: Path
    sid: str = field(default_factory=lambda: secrets.token_hex(4))
    started: bool = False

    @property
    def net(self) -> str:
        return f"laa-wr-net-{self.sid}"

    @property
    def box(self) -> str:
        return f"laa-wr-{self.sid}"

    @property
    def gate(self) -> str:
        return f"laa-wr-gate-{self.sid}"

    def start(self) -> "Workroom":
        if _docker("image", "inspect", IMAGE, check=False).returncode != 0:
            raise WorkroomError("no_image", IMAGE)
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            _docker("network", "create", "--internal", "--label", "laa=workroom", self.net)
            # 나가는 문: 바깥(bridge)에 붙여 띄운 뒤 내부망에도 'egress'라는 이름으로 잇는다
            _docker("run", "-d", "--name", self.gate, "--label", "laa=workroom", "--user", str(UID),
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
                    "--memory", "128m", "--cpus", "0.5", "--pids-limit", "64", "--network", "bridge",
                    IMAGE, "python", "-u", "/opt/workroom/egress_proxy.py")
            _docker("network", "connect", "--alias", "egress", self.net, self.gate)
            proxy = "http://egress:3128"
            _docker("run", "-d", "--init", "--name", self.box, "--label", "laa=workroom", "--user", str(UID),
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g", "--tmpfs", f"/home/worker:rw,nosuid,nodev,size=64m,uid={UID},gid={UID}",
                    "--network", self.net, "--memory", MEMORY, "--memory-swap", MEMORY, "--cpus", CPUS, "--pids-limit", str(PIDS),
                    "-v", f"{self.path}:/work:rw",
                    "-e", f"HTTP_PROXY={proxy}", "-e", f"HTTPS_PROXY={proxy}", "-e", f"http_proxy={proxy}", "-e", f"https_proxy={proxy}",
                    "-e", "NO_PROXY=", IMAGE, "sleep", "infinity")
        except WorkroomError:
            self.close()
            raise
        self.started = True
        sys.stderr.write(f"[workroom] {time.strftime('%Y-%m-%d %H:%M:%S')} start project={self.project} sid={self.sid}\n")
        return self

    def run(self, command: str, timeout_s: float = CMD_TIMEOUT_S) -> RunResult:
        """컨테이너 안의 bash로 명령 하나. 시간을 넘기면 안쪽에서 KILL한다(바깥 상한은 조금 더 넉넉히)."""
        if not self.started:
            raise WorkroomError("closed")
        t = max(1, min(int(timeout_s), MAX_CMD_TIMEOUT_S))
        t0 = time.monotonic()
        try:
            # 환경은 처음부터 정한 것만(env -i): 이미지가 물려준 변수도 넘기지 않는다
            proxy = "http://egress:3128"
            clean = ["env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/home/worker", "LANG=C.UTF-8", "MPLCONFIGDIR=/tmp/mpl",
                     "PYTHONDONTWRITEBYTECODE=1", f"HTTP_PROXY={proxy}", f"HTTPS_PROXY={proxy}", f"http_proxy={proxy}", f"https_proxy={proxy}"]
            p = subprocess.run(["docker", "exec", "-i", "-u", str(UID), "-w", "/work", self.box,
                                *clean, "timeout", "-s", "KILL", str(t), "bash", "-c", command],
                               capture_output=True, text=True, timeout=t + 30, errors="replace")
            code, out = p.returncode, (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        except subprocess.TimeoutExpired as e:
            code, out = 137, ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        secs = time.monotonic() - t0
        clipped, cut = _clip(out.strip())
        return RunResult(code, clipped, secs, timed_out=code in (124, 137) and secs >= t - 0.5, truncated=cut)

    def gate_log(self) -> str:
        """나가는 문의 기록(호스트·포트·결과만)."""
        return _docker("logs", self.gate, check=False).stdout

    def close(self) -> None:
        for name in (self.box, self.gate):
            _docker("rm", "-f", name, check=False)
        _docker("network", "rm", self.net, check=False)
        if self.started:
            sys.stderr.write(f"[workroom] {time.strftime('%Y-%m-%d %H:%M:%S')} close sid={self.sid}\n")
        self.started = False

    def __enter__(self) -> "Workroom":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()


def open_project(project: str) -> Workroom:
    """프로젝트 이름(소문자·숫자·하이픈) → 그 폴더를 /work로 둔 작업실(아직 띄우지 않음)."""
    if not PROJECT.match(project):
        raise WorkroomError("bad_project", project)
    return Workroom(project, ROOT / "projects" / project)


def sweep() -> int:
    """남아 있는 작업실(서버가 죽어 못 치운 것)을 치운다."""
    ids = _docker("ps", "-aq", "--filter", "label=laa=workroom", check=False).stdout.split()
    if ids:
        _docker("rm", "-f", *ids, check=False)
    nets = _docker("network", "ls", "-q", "--filter", "label=laa=workroom", check=False).stdout.split()
    for n in nets:
        _docker("network", "rm", n, check=False)
    return len(ids)
