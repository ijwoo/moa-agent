"""Phase 5 완료 기준: 서로 다른 테스트 사용자 간 세션·실행 데이터가 섞이지 않는다.

실제 컨테이너·볼륨으로 확인한다. RUN_INTEGRATION=1 + colima 실행 + 이미지 빌드 필요. 합성 사용자만 사용.
"""

import os
import subprocess
import threading
import time

import pytest

from agent.browser import testenv
from agent.browser.client import BrowserError
from agent.sandbox.manager import SandboxManager, user_key

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="RUN_INTEGRATION=1 필요"),
]

USERS = ("it-user-a", "it-user-b")
HOME = "http://shop.test/"


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)


@pytest.fixture(scope="module")
def mgr():
    testenv.start_site()
    m = SandboxManager(network=testenv.NETWORK, max_concurrent=2)
    for u in USERS:
        m.delete_user_data(u)
    yield m
    for u in USERS:
        m.delete_user_data(u)
    m.shutdown()
    testenv.stop_site()


def home_text(browser) -> str:
    browser.request("navigate", {"url": HOME})
    return browser.request("extract_text", {})["text"]


def login(browser, user):
    # 테스트 하네스 전용 로그인. 에이전트 경로(validate_call)에서는 /_test가 거부된다.
    browser.request("navigate", {"url": f"http://shop.test/_test/login?user={user}"})


def test_sessions_do_not_mix_and_persist_per_user(mgr):
    with mgr.session("it-user-a") as a:
        login(a, "alice")
        assert "alice 님으로 로그인됨" in home_text(a)
        # A가 로그인한 상태에서 B의 브라우저는 로그인되어 있지 않다
        with mgr.session("it-user-b") as b:
            assert "로그인됨" not in home_text(b)
            login(b, "bob")
            assert "bob 님으로 로그인됨" in home_text(b)
        assert "alice 님으로 로그인됨" in home_text(a)
    # 컨테이너는 사라졌다
    names = docker("ps", "-a", "--filter", "label=laa.managed=1", "--format", "{{.Names}}").stdout.split()
    assert names == []
    # 다시 열면 각자 자기 세션이 복원된다 (프로필 볼륨)
    with mgr.session("it-user-a") as a:
        assert "alice 님으로 로그인됨" in home_text(a)
    with mgr.session("it-user-b") as b:
        text = home_text(b)
        assert "bob 님으로 로그인됨" in text and "alice" not in text


def test_container_hardening_still_applies_with_profile_volume(mgr):
    with mgr.session("it-user-a"):
        name = mgr.container_name(user_key("it-user-a"))
        fmt = "{{.Config.User}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.CapDrop}}|{{.HostConfig.CapAdd}}|{{.HostConfig.NetworkMode}}|{{range .Mounts}}{{.Type}}:{{.Destination}} {{end}}"
        user, ro, drop, add, net, mounts = docker("inspect", name, "--format", fmt).stdout.strip().split("|")
        assert user == "pwuser" and ro == "true" and "ALL" in drop and add == "[CAP_SYS_CHROOT]"
        assert net == testenv.NETWORK
        assert "volume:/data/profile" in mounts and "docker.sock" not in mounts
        # 다른 사용자의 볼륨은 이 컨테이너에 없다
        assert mounts.count("volume:") == 1


def test_concurrent_users_run_in_parallel_within_limit(mgr):
    seen, peak, lock = [], [0], threading.Lock()

    def job(browser):
        with lock:
            n = len(docker("ps", "--filter", "label=laa.managed=1", "--format", "{{.Names}}").stdout.split())
            peak[0] = max(peak[0], n)
        seen.append(home_text(browser)[:10])
        time.sleep(0.5)
        return True

    futs = [mgr.submit(f"it-par-{i}", job) for i in range(4)]
    assert all(f.result(timeout=120) for f in futs)
    assert len(seen) == 4 and 1 <= peak[0] <= 2
    for i in range(4):
        mgr.delete_user_data(f"it-par-{i}")


def test_crash_recovery_and_orphan_cleanup(mgr):
    with mgr.session("it-user-a") as a:
        login(a, "alice2")
        home_text(a)
        docker("kill", mgr.container_name(user_key("it-user-a")))  # 작업 중 컨테이너 강제 종료
        with pytest.raises(BrowserError):
            a.request("observe")
    # 같은 사용자가 바로 다시 쓸 수 있다 (잠금 파일·잔여 컨테이너 정리)
    with mgr.session("it-user-a") as a:
        assert "테스트 쇼핑몰" in home_text(a)
    # 매니저 프로세스가 죽어 남은 고아 컨테이너 흉내
    docker("run", "-d", "--name", "laa-browser-orphan0000000000", "--label", "laa.managed=1",
           "--network", "none", "local-ai-agent/browser:0.2", "sleep", "300")
    assert mgr.cleanup_orphans() == 1
    assert docker("ps", "-aq", "--filter", "name=laa-browser-orphan").stdout.strip() == ""


def test_delete_user_data_removes_session(mgr):
    with mgr.session("it-user-b") as b:
        login(b, "bob3")
    assert mgr.delete_user_data("it-user-b") is True
    with mgr.session("it-user-b") as b:
        assert "로그인됨" not in home_text(b)


def test_screenshot_is_real_jpeg_and_masks_nothing_to_model(mgr):
    import base64
    with mgr.session("it-user-a") as a:
        a.request("navigate", {"url": "http://shop.test/login"})
        r = a.request("screenshot")
        data = base64.b64decode(r["jpeg_b64"])
        assert data[:3] == b"\xff\xd8\xff" and 2000 < len(data) == r["bytes"] < 500_000
        # 관찰 결과(모델 입력)에는 이미지가 섞이지 않는다
        assert "jpeg" not in a.request("observe")["text"].lower()
