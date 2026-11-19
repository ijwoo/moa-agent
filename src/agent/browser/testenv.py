"""로컬 합성 테스트 환경: 외부와 단절된 Docker 내부 네트워크 + 테스트 사이트 컨테이너.

브라우저 컨테이너를 run_args(network=NETWORK)로 띄우면 shop.test 등에는 닿고 인터넷에는 닿지 않는다
(2026-11-10 확인: 외부 DNS 해석 불가, 외부 IP 경로 없음).
모든 명령은 고정된 인자로 코드가 실행한다. 모델이 만든 문자열은 여기에 들어오지 않는다.
"""

from __future__ import annotations

import json
import subprocess
import time

from .container import IMAGE, REPO_ROOT

NETWORK = "laa-testnet"
SITE_CONTAINER = "laa-testsite"
SITE_HOSTS = ("shop.test", "mail.test", "intranet.test", "booking.test", "login.test", "auth.test")


def _docker(*args: str, check: bool = True, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, timeout=timeout)


def ensure_network() -> None:
    r = _docker("network", "inspect", NETWORK, "--format", "{{.Internal}}", check=False)
    if r.returncode == 0:
        if r.stdout.strip() != "true":
            raise RuntimeError(f"{NETWORK} 네트워크가 internal이 아니다. 삭제 후 다시 만들어야 한다")
        return
    _docker("network", "create", "--internal", NETWORK)


def start_site() -> None:
    """테스트 사이트를 (다시) 띄운다. 상태는 항상 초기값에서 시작."""
    stop_site()
    ensure_network()
    aliases = [a for h in SITE_HOSTS for a in ("--network-alias", h)]
    _docker(
        "run", "-d", "--rm", "--name", SITE_CONTAINER,
        "--network", NETWORK, *aliases,
        "--user", "pwuser", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--memory", "256m", "--pids-limit", "64",
        "-v", f"{REPO_ROOT / 'testsite'}:/site:ro",
        IMAGE, "python3", "/site/server.py", "--port", "80",
    )
    for _ in range(50):
        if "listening" in _docker("logs", SITE_CONTAINER, check=False).stdout:
            return
        time.sleep(0.1)
    raise RuntimeError("테스트 사이트가 시작되지 않았다")


def stop_site() -> None:
    _docker("rm", "-f", SITE_CONTAINER, check=False)


def _site_request(method: str, path: str) -> str:
    code = (
        "import urllib.request,sys;"
        f"r=urllib.request.Request('http://127.0.0.1{path}',method='{method}');"
        "sys.stdout.write(urllib.request.urlopen(r).read().decode())"
    )
    return _docker("exec", SITE_CONTAINER, "python3", "-c", code).stdout


def set_inbox(mails: list[dict]) -> None:
    """테스트 하네스 전용: 받은편지함을 합성 메일 목록으로 바꾼다 (시나리오 준비)."""
    import base64
    payload = base64.b64encode(json.dumps(mails, ensure_ascii=False).encode()).decode()  # 따옴표 문제 없이 전달
    code = (
        "import urllib.request,base64;"
        f"d=base64.b64decode('{payload}');"
        "urllib.request.urlopen(urllib.request.Request('http://127.0.0.1/_test/inbox',data=d,method='POST')).read()"
    )
    _docker("exec", SITE_CONTAINER, "python3", "-c", code)


def site_state() -> dict:
    """테스트 하네스 전용: 사이트 상태(주문·보낸 메일·예약 등). 검증 단계에서 '정말 일어났는지' 확인용."""
    return json.loads(_site_request("GET", "/_test/state"))


def reset_site() -> None:
    _site_request("POST", "/_test/reset")
