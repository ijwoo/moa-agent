"""전용 크롬 길: 맥의 진짜 크롬을 에이전트 전용 프로필로 띄워 같은 브라우저 서비스로 다룬다 (browser/hostchrome.py).

켜야 돈다: RUN_REAL_CHROME=1 + HOST_BROWSER_PYTHON(Playwright가 깔린 파이썬). 맥 화면에 크롬 창이 떴다 닫힌다.
합성 사이트는 이 프로세스 안에서 띄우고, 크롬에 그 이름(login.test)을 127.0.0.1로 풀게 한다. 프로필은 임시 폴더(진짜 전용 프로필을 건드리지 않는다).
"""

import importlib.util
import os
import threading
from pathlib import Path

import pytest

from agent.browser import hostchrome
from agent.tools.observation import Observation

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_REAL_CHROME") != "1" or not hostchrome.available(),
                       reason="RUN_REAL_CHROME=1 + HOST_BROWSER_PYTHON 필요(맥 화면에 크롬 창이 뜬다)"),
]


@pytest.fixture(scope="module")
def site():
    path = Path(__file__).resolve().parents[2] / "testsite" / "server.py"
    spec = importlib.util.spec_from_file_location("testsite_server_hc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    srv = mod.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://login.test:{srv.server_address[1]}"
    srv.shutdown()


def test_real_chrome_with_a_dedicated_profile_runs_the_same_operations_and_keeps_the_login_between_sessions(site, tmp_path):
    """같은 동작(열기·읽기·입력·누르기)이 되고, 프로필에 남은 로그인이 다음에 열 때 이어진다 — 이 길이 기대는 성질이다."""
    rules = ("--host-resolver-rules=MAP login.test 127.0.0.1",)
    profile = tmp_path / "profile"
    with hostchrome.session(profile, extra_args=rules) as b:
        b.request("navigate", {"url": site + "/a/login"})
        assert [f["name"] for f in b.request("form_fields")["fields"]] == ["id", "password"]
        b.request("form_fill", {"values": {"id": "tester@example.com", "password": "synthetic-pass-1"}})
        b.request("form_submit", {})
        o = Observation.parse(b.request("observe")["text"])
        assert o.url.endswith("/home") and any("워크스페이스" in e.label for e in o.elements.values())
        b.request("click", {"ref": next(e.ref for e in o.elements.values() if e.role == "link" and e.label == "서연")})
        assert "주간 보고" in b.request("extract_text", {"ref": None})["text"]
    with hostchrome.session(profile, extra_args=rules) as b:                      # 새로 띄워도 로그인돼 있다(프로필에 남았다)
        b.request("navigate", {"url": site + "/home"})
        assert "합성 사용자님" in b.request("extract_text", {"ref": None})["text"]
    with hostchrome.session(tmp_path / "other", extra_args=rules) as b:           # 다른 프로필은 로그인돼 있지 않다
        b.request("navigate", {"url": site + "/home"})
        assert [f["name"] for f in b.request("form_fields")["fields"]] == ["id", "password"]


def test_the_browser_service_does_not_inherit_secrets_from_this_process(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-secret")
    monkeypatch.setenv("AGENT_APP_TOKEN", "synthetic-token")
    env = hostchrome.service_env(tmp_path, ("--host-resolver-rules=MAP a b", "rm -rf"))
    assert "ANTHROPIC_API_KEY" not in env and "AGENT_APP_TOKEN" not in env and env["BROWSER_REAL_PROFILE"] == str(tmp_path)
    assert env["BROWSER_EXTRA_ARGS"] == "--host-resolver-rules=MAP a b"            # 옵션 꼴이 아닌 것은 넘기지 않는다


def test_a_site_opened_with_the_dedicated_chrome_is_checked_operated_and_logged_out_through_the_site_manager(site, tmp_path):
    """사이트 관리 쪽에서: 앱의 카드로는 로그인하지 않는다(방법을 알린다) → 사람이 그 프로필에 로그인해 두면 확인이 '로그인됨' →
    그 안에서 일한다(로그인 상태를 옮기지 않는다) → 로그아웃은 전용 프로필 폴더를 지운다."""
    from agent.providers.base import ModelResponse, ToolCall
    from agent.sites.inside import inside_site
    from agent.sites.login import Site, SiteLoginManager
    from agent.sites.store import MemoryBlobs
    rules = ("--host-resolver-rules=MAP login.test 127.0.0.1",)
    s = Site("synthc", "합성 서비스", {"password": site + "/a/login"}, site + "/home", (), (), site + "/home", generic=True, cloud_ok=True, backend="chrome")
    store = MemoryBlobs()
    m = SiteLoginManager(None, store, {"synthc": s}, poll_s=0.2, insecure_hosts=("login.test",))
    m.chrome_profile, m.chrome_args = tmp_path / "agent-profile", rules
    m.chrome_card_login = False
    assert [x["status"] for x in m.check("synthc")] == ["logged_out"]
    l = m.start("synthc", "password")
    assert l["state"] == "failed" and l["reason"] == "unsupported_login" and "맥 화면 앞에서" in l["message"] and "site login synthc" in l["message"]
    with hostchrome.session(m.chrome_profile, extra_args=rules) as b:            # 사람이 그 프로필에서 손으로 로그인한 것을 흉내 낸다
        b.request("navigate", {"url": site + "/a/login"})
        b.request("form_fill", {"values": {"id": "tester@example.com", "password": "synthetic-pass-1"}})
        b.request("form_submit", {})
    assert [x["status"] for x in m.check("synthc")] == ["logged_in"] and store.get(m._key(s)) is None   # 로그인 상태를 꺼내 보관하지 않는다

    class Driver:
        name = "scripted"

        def __init__(self):
            self.n = 0

        def generate(self, messages, *, tools=None, json_schema=None, temperature=0.0, max_tokens=None):
            self.n += 1
            if self.n == 1:
                call = ToolCall(name="observe", arguments={})
            elif self.n == 2:
                obs = next(m_.content for m_ in reversed(messages) if m_.role == "tool" and "URL:" in m_.content)
                ref = next(line.split("]")[0].strip("[ ") for line in obs.splitlines() if 'link "서연"' in line)
                call = ToolCall(name="click", arguments={"ref": ref})
            else:
                call = ToolCall(name="finish", arguments={"summary": "워크스페이스를 서연으로 바꿨어요."})
            return ModelResponse(text="", tool_calls=[call], latency_ms=1, model="s", provider="s")
    run, _ = inside_site(m, s, Driver(), "워크스페이스를 서연으로 바꿔줘", approver=lambda r: True, max_steps=6, max_seconds=60)
    assert run.status == "completed"
    with hostchrome.session(m.chrome_profile, extra_args=rules) as b:
        b.request("navigate", {"url": site + "/home"})
        assert "워크스페이스: 서연" in b.request("extract_text", {"ref": None})["text"]
    assert [x["status"] for x in m.logout("synthc")] == ["logged_out"] and not (tmp_path / "agent-profile").exists()
    assert [x["status"] for x in m.check("synthc")] == ["logged_out"]
