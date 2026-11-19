import subprocess
import threading
import time

import pytest

from agent.sandbox.manager import SandboxBusy, SandboxManager, user_key


class FakeSession:
    live = 0
    peak = 0
    lock = threading.Lock()

    def __init__(self, network, extra_args):
        self.network, self.extra_args, self.closed = network, extra_args, False
        with FakeSession.lock:
            FakeSession.live += 1
            FakeSession.peak = max(FakeSession.peak, FakeSession.live)

    def close(self):
        self.closed = True
        with FakeSession.lock:
            FakeSession.live -= 1


@pytest.fixture
def mgr(tmp_path):
    FakeSession.live = FakeSession.peak = 0
    calls = []

    def docker(*args):
        calls.append(args)
        out = "laa-browser-aaaa\nlaa-browser-bbbb\nother\n" if args[0] == "ps" else ""
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    m = SandboxManager(network="net", max_concurrent=2, max_pending=3, session_factory=FakeSession,
                       docker=docker, runs_dir=tmp_path)
    m.calls = calls
    yield m
    m.shutdown()


def test_user_key_is_stable_hash_and_validates():
    assert user_key("test-a") == user_key("test-a") != user_key("test-b")
    assert len(user_key("a; rm -rf /")) == 16 and user_key("a; rm -rf /").isalnum()
    for bad in ("", None, "x" * 300):
        with pytest.raises(ValueError):
            user_key(bad)


def test_session_args_are_per_user_and_cleanup_runs(mgr):
    with mgr.session("test-a") as s:
        ka = user_key("test-a")
        assert f"laa-browser-{ka}" in s.extra_args and f"laa-profile-{ka}:/data/profile" in s.extra_args
        assert "test-a" not in " ".join(s.extra_args)  # 원래 id는 docker 인자에 없다
        assert mgr.active_users() == {ka}
    assert s.closed and mgr.active_users() == set()
    assert mgr.calls[0] == ("rm", "-f", f"laa-browser-{ka}") and mgr.calls[-1] == ("rm", "-f", f"laa-browser-{ka}")


def test_session_released_on_error(mgr):
    with pytest.raises(RuntimeError):
        with mgr.session("test-a"):
            raise RuntimeError("boom")
    with mgr.session("test-a") as s:  # 잠금이 풀려 다시 쓸 수 있다
        assert not s.closed


def test_concurrency_limit_and_results(mgr):
    futs = [mgr.submit(f"u{i}", lambda b: (time.sleep(0.05), b.network)[1]) for i in range(5)]
    assert [f.result(timeout=5) for f in futs] == ["net"] * 5
    assert FakeSession.peak <= 2 and FakeSession.live == 0


def test_same_user_tasks_never_overlap(mgr):
    running, overlap = [], []

    def job(b):
        running.append(1)
        overlap.append(len(running))
        time.sleep(0.03)
        running.pop()

    for f in [mgr.submit("same", job) for _ in range(3)]:
        f.result(timeout=5)
    assert max(overlap) == 1


def test_queue_limit_raises_busy(mgr):
    gate = threading.Event()
    futs = [mgr.submit(f"u{i}", lambda b: gate.wait(5)) for i in range(2)]   # 실행 2
    time.sleep(0.05)
    futs += [mgr.submit(f"w{i}", lambda b: None) for i in range(3)]          # 대기 3 (상한)
    time.sleep(0.05)
    with pytest.raises(SandboxBusy):
        with mgr.session("one-more"):
            pass
    gate.set()
    for f in futs:
        f.result(timeout=5)


def test_cleanup_orphans_and_logs_path(mgr):
    assert mgr.cleanup_orphans() == 2
    assert ("rm", "-f", "laa-browser-aaaa") in mgr.calls and ("rm", "-f", "other") not in mgr.calls
    pa, pb = mgr.user_log_path("test-a"), mgr.user_log_path("test-b")
    assert pa.parent != pb.parent and pa.parent.name == user_key("test-a")


def test_delete_user_data_refuses_while_running(mgr):
    with mgr.session("test-a"):
        assert mgr.delete_user_data("test-a") is False
    assert mgr.delete_user_data("test-a") is True
    assert mgr.calls[-1] == ("volume", "rm", "-f", f"laa-profile-{user_key('test-a')}")


def test_screenshot_dir_per_user_and_purge(mgr):
    import os
    a = mgr.screenshot_dir("test-a", "20261111-010101")
    assert user_key("test-a") in str(a) and mgr.screenshot_dir("test-b", "r1").parent != a.parent
    with pytest.raises(ValueError):
        mgr.screenshot_dir("test-a", "../x")
    a.mkdir(parents=True); (a / "s.jpg").write_bytes(b"x")
    assert mgr.purge_screenshots("test-a", older_than_days=1) == 0
    old = time.time() - 10 * 86400
    os.utime(a, (old, old))
    assert mgr.purge_screenshots("test-a", older_than_days=7) == 1 and not a.exists()
    a.mkdir(parents=True)
    mgr.delete_user_data("test-a")
    assert not a.exists()


def test_session_env_is_added_only_for_that_session_and_public_paths_ask_for_a_headed_browser(mgr):
    """공개 길만 창 있는 브라우저로 띄운다(2026-11-15 측정). 다른 세션(로그인하는 사이트)은 그대로다."""
    from agent.sites.public import open_session
    with mgr.session("test-a", env={"BROWSER_HEADED": "1"}) as s:
        assert "BROWSER_HEADED=1" in s.extra_args
    with mgr.session("test-a") as s:
        assert "BROWSER_HEADED=1" not in s.extra_args
    with open_session(mgr, "test-a", headed=True) as s:
        assert "BROWSER_HEADED=1" in s.extra_args
    with open_session(mgr, "test-a") as s:
        assert "BROWSER_HEADED=1" not in s.extra_args and "ALLOW_DOWNLOADS=1" in s.extra_args   # 공개 길은 내려받은 표(D125)
    with mgr.session("test-a") as s:
        assert "ALLOW_DOWNLOADS=1" not in s.extra_args                                          # 다른 세션은 내려받지 않는다

    class Old:   # env를 모르는 세션 관리자(시험용 가짜들)도 꺼 둔 채로는 그대로 쓴다
        def session(self, user):
            return "s"
    assert open_session(Old(), "u") == "s"
