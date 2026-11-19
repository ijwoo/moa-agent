"""브라우저 컨테이너 보안 설정 점검. RUN_INTEGRATION=1 + colima 실행 + 이미지 빌드 필요."""

import os
import subprocess

import pytest

from agent.browser.container import IMAGE, REPO_ROOT, run_args

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="RUN_INTEGRATION=1 필요"),
]


def docker_run(cmd: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "run", "-i", *run_args(), IMAGE, *cmd],
        input=stdin, capture_output=True, text=True, timeout=120,
    )


def test_chromium_is_sandboxed():
    script = (REPO_ROOT / "docker" / "browser" / "sandbox_check.py").read_text()
    r = docker_run(["python3", "-"], stdin=script)
    assert "SANDBOX_STATUS ok" in r.stdout, r.stdout + r.stderr
    assert "RENDERER_USERNS separate" in r.stdout, r.stdout + r.stderr


def test_container_process_has_no_effective_caps_and_is_not_root():
    r = docker_run(["sh", "-c", "id -u; grep -E '^CapEff' /proc/self/status"])
    uid, capeff = r.stdout.split("\n")[:2]
    assert uid.strip() != "0"
    assert capeff.split()[1] == "0000000000000000"


@pytest.mark.parametrize("cmd", [
    "chroot / true",                                  # 컨테이너 프로세스 자체는 chroot 불가
    "mount -t tmpfs none /mnt",                       # mount 거부
    "echo 1 > /proc/sysrq-trigger",                   # /proc 쓰기 거부
    "python3 -c 'import socket; socket.socket(38, 5, 0)'",  # AF_ALG 거부
    "python3 -c 'import socket; socket.create_connection((\"1.1.1.1\", 80), timeout=3)'",  # 네트워크 없음
])
def test_forbidden_operations_fail(cmd):
    r = docker_run(["sh", "-c", cmd])
    assert r.returncode != 0, f"허용되면 안 됨: {cmd}\n{r.stdout}{r.stderr}"


def test_docker_socket_not_mounted():
    r = docker_run(["sh", "-c", "test -e /var/run/docker.sock && echo present || echo absent"])
    assert r.stdout.strip() == "absent"
