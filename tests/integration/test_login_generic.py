"""공통 로그인(D70) 끝에서 끝까지: 실제 컨테이너 브라우저 + 합성 로그인 사이트. 계정은 합성이다.

RUN_INTEGRATION=1 + colima 실행 + 이미지 빌드 필요.
"""

import json
import os
import time

import pytest

from agent.browser import testenv
from agent.sandbox.manager import SandboxManager
from agent.sites.login import BY_PASSWORD, DONE, EASY_AGREE, GO_ON, NOT_YET, RETRY, STOP, SiteLoginManager, generic_site
from agent.sites.store import MemoryBlobs

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="RUN_INTEGRATION=1 필요"),
]
ID, PW = "tester@example.com", "synthetic-pass-1"
TEST_HOSTS = ("login.test", "auth.test")


@pytest.fixture(scope="module", autouse=True)
def site():
    testenv.start_site()
    yield


def manager(path: str, **kw):
    sb = SandboxManager(network=testenv.NETWORK, max_concurrent=1, persist_profile=False)
    store = MemoryBlobs()
    s = generic_site("synth", "합성 상점", f"http://login.test{path}", "http://login.test/home")
    kw.setdefault("insecure_hosts", TEST_HOSTS)
    return SiteLoginManager(sb, store, {"synth": s}, poll_s=0.2, **kw), store


def wait(m, lid, want, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        l = m.get("synth", lid)
        if l["state"] in want:
            if l["state"] in ("logged_in", "failed", "cancelled", "expired") and m._thread is not None:
                m._thread.join(timeout=30)   # 브라우저 컨테이너가 닫힌 뒤에 다음 로그인을 연다(관리자를 새로 만드는 테스트라 직접 기다린다)
            return l
        time.sleep(0.05)
    raise AssertionError(f"{want} 안 됨: {l}")


def wait_step(m, prev, want, timeout=30.0):
    """앞 카드(prev)와 다른 단계의 카드가 뜰 때까지 기다린다."""
    end = time.time() + timeout
    while time.time() < end:
        l = m.get("synth", prev["login_id"])
        if l["state"] in want and (l["step"] != prev["step"] or l["state"] != prev["state"]):
            return l
        if l["state"] in ("failed", "cancelled", "expired") and l["state"] not in want:
            raise AssertionError(f"끝남: {l}")
        time.sleep(0.05)
    raise AssertionError(f"{want} 안 됨: {l}")


def test_login_keeps_only_the_login_state_and_check_reads_it_back():
    m, store = manager("/a/login")
    l = m.start("synth", "password")
    l = wait(m, l["login_id"], ("need_input",))
    assert [(f["name"], f["label"], f["secret"]) for f in l["fields"]] == [("id", "이메일", False), ("password", "비밀번호", True)]
    assert "login.test" in l["message"]                                   # 어느 화면에 넣는지 보여 준다
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    l = wait(m, l["login_id"], ("need_choice",))
    assert l["choices"] == [DONE, NOT_YET, STOP] and "'로그아웃'이 보여요" in l["text"]    # 제출 뒤의 화면 글을 보여 주고 사용자가 고른다
    m.input("synth", l["login_id"], l["step"], {"choice": DONE})
    l = wait(m, l["login_id"], ("logged_in",))
    kept = store.get(m._key(m.sites["synth"]))
    assert kept and "lsid" in [c["name"] for c in json.loads(kept)["cookies"]]
    assert PW not in json.dumps(m.get("synth", l["login_id"]), ensure_ascii=False) and PW not in kept
    assert [s for s in m.check("synth") if s["site"] == "synth"][0]["status"] == "logged_in"


def test_wrong_password_is_counted_by_the_same_fields_coming_back_and_stops_before_a_lockout():
    m, store = manager("/a/login")
    l = m.start("synth", "password")
    for n in range(3):
        l = wait(m, l["login_id"], ("need_input",))
        assert (l.get("error") == "wrong_input") == (n > 0)
        m.input("synth", l["login_id"], l["step"], {"id": ID, "password": f"wrong-{n}"})
        time.sleep(0.3)
    l = wait(m, l["login_id"], ("failed",))
    assert l["reason"] == "wrong_input" and store.get(m._key(m.sites["synth"])) is None
    assert testenv.site_state()["login_attempts"] >= 3


def test_two_pages_then_a_code():
    m, _ = manager("/b/login")
    l = m.start("synth", "password")
    for names, values in ((["id"], {"id": ID}), (["password"], {"password": PW}), (["code"], {"code": "246810"})):
        l = wait(m, l["login_id"], ("need_input",))
        assert [f["name"] for f in l["fields"]] == names
        m.input("synth", l["login_id"], l["step"], values)
        time.sleep(0.4)
    l = wait(m, l["login_id"], ("need_choice",))
    m.input("synth", l["login_id"], l["step"], {"choice": DONE})
    assert wait(m, l["login_id"], ("logged_in",))["state"] == "logged_in"


def test_another_host_is_asked_about_before_any_value_is_typed():
    m, _ = manager("/e/login")
    l = m.start("synth", "password")
    l = wait(m, l["login_id"], ("need_choice",))
    assert l["choices"] == [GO_ON, STOP] and "auth.test" in l["text"]
    m.input("synth", l["login_id"], l["step"], {"choice": GO_ON})
    l = wait(m, l["login_id"], ("need_input",))
    assert "auth.test" in l["message"]
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    l = wait(m, l["login_id"], ("need_choice",))
    m.input("synth", l["login_id"], l["step"], {"choice": DONE})
    assert wait(m, l["login_id"], ("logged_in",))["state"] == "logged_in"
    m2, _ = manager("/e/login")                                            # 거절하면 아무것도 넣지 않고 끝난다
    l = m2.start("synth", "password")
    l = wait(m2, l["login_id"], ("need_choice",))
    m2.input("synth", l["login_id"], l["step"], {"choice": STOP})
    assert wait(m2, l["login_id"], ("failed",))["reason"] == "host_refused"


def test_captcha_picture_is_shown_and_cancel_ends_it():
    m, _ = manager("/d/login")
    l = m.start("synth", "password")
    l = wait(m, l["login_id"], ("need_input",))
    assert [f["name"] for f in l["fields"]] == ["id", "password", "captcha"] and l["image"]["mime"] == "image/png"
    m.cancel("synth", l["login_id"])
    assert wait(m, l["login_id"], ("cancelled",))["state"] == "cancelled"


def test_plain_http_and_signup_screens_take_no_input():
    m, _ = manager("/a/login", insecure_hosts=())
    l = m.start("synth", "password")
    assert wait(m, l["login_id"], ("failed",))["reason"] == "not_https"
    m, _ = manager("/signup")
    l = m.start("synth", "password")
    assert wait(m, l["login_id"], ("failed",))["reason"] == "unsupported_login"
    m, _ = manager("/nologin")                                             # 로그인 칸도 입구도 없는 화면
    l = m.start("synth", "password")
    assert wait(m, l["login_id"], ("failed",), timeout=40)["reason"] == "unsupported_login"


def test_waiting_for_a_phone_approval_and_saying_done_too_early():
    """제출 뒤 칸이 없는 화면(폰 승인 대기): 코드는 끝났는지 모른다. 화면 글을 보여 주고 사용자가 고른다."""
    testenv.reset_site()
    m, store = manager("/f/login")
    l = m.start("synth", "password")
    l = wait(m, l["login_id"], ("need_input",))
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    l = wait(m, l["login_id"], ("need_choice",))
    assert "2단계 인증 알림" in l["text"] or "승인" in l["text"]   # 화면 글 대신 요지(2026-11-17)
    m.input("synth", l["login_id"], l["step"], {"choice": NOT_YET})        # 아직: 그대로 기다리는 화면을 다시 보여 준다
    time.sleep(0.5)
    l = wait(m, l["login_id"], ("need_choice",))
    assert "2단계 인증 알림" in l["text"] or "승인" in l["text"]   # 화면 글 대신 요지(2026-11-17)
    testenv._site_request("POST", "/_test/approve")                          # 폰에서 승인했다
    time.sleep(1.5)
    m.input("synth", l["login_id"], l["step"], {"choice": NOT_YET})
    time.sleep(0.5)
    l = wait(m, l["login_id"], ("need_choice",))
    assert "'로그아웃'이 보여요" in l["text"]
    m.input("synth", l["login_id"], l["step"], {"choice": DONE})
    assert wait(m, l["login_id"], ("logged_in",))["state"] == "logged_in" and store.get(m._key(m.sites["synth"]))

    testenv.reset_site()                                                     # 승인하지 않고 "로그인됐어요"를 누르면
    m, store = manager("/f/login")
    l = m.start("synth", "password")
    l = wait(m, l["login_id"], ("need_input",))
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    l = wait(m, l["login_id"], ("need_choice",))
    m.input("synth", l["login_id"], l["step"], {"choice": DONE})
    time.sleep(0.5)
    l = wait(m, l["login_id"], ("need_input",))                              # 확인 화면에 로그인 칸이 나왔다 → 보관하지 않고 다시 로그인 화면
    assert store.get(m._key(m.sites["synth"])) is None
    m.cancel("synth", l["login_id"])
    wait(m, l["login_id"], ("cancelled",))


def started_by_address(path="/"):
    """사용자가 주소만 말한 사이트처럼: 확인 주소를 모르는 채로 첫 화면에서 시작한다."""
    sb = SandboxManager(network=testenv.NETWORK, max_concurrent=1, persist_profile=False)
    store = MemoryBlobs()
    s = generic_site("synth", "login.test", f"http://login.test{path}", "")
    return SiteLoginManager(sb, store, {"synth": s}, poll_s=0.2, insecure_hosts=TEST_HOSTS), store


def test_from_the_front_page_through_the_entry_link_a_method_choice_and_a_social_login():
    testenv.reset_site()
    m, store = started_by_address("/")
    l = wait(m, m.start("synth", "password")["login_id"], ("need_choice",))       # 첫 화면 → "로그인" 입구 → 방법 고르기
    assert l["choices"] == [BY_PASSWORD, "카카오로 로그인", STOP]                    # 구글은 이 브라우저를 받지 않아 뺀다
    m.input("synth", l["login_id"], l["step"], {"choice": "카카오로 로그인"})
    l = wait(m, l["login_id"], ("need_choice",))                                  # 합성 로그인 호스트는 아는 소셜 호스트가 아니다 → 입력 전에 묻는다
    assert l["choices"] == [GO_ON, STOP] and "auth.test" in l["text"]
    m.input("synth", l["login_id"], l["step"], {"choice": GO_ON})
    l = wait(m, l["login_id"], ("need_input",))
    assert l["host"] == "auth.test"
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    l = wait(m, l["login_id"], ("logged_in",))                                    # 사이트로 돌아왔고 로그인 입구가 없다 → 묻지 않고 끝
    assert store.get(m._key(m.sites["synth"])) and m.sites["synth"].check_url == "http://login.test/home"
    assert [s for s in m.check("synth") if s["site"] == "synth"][0]["status"] == "logged_in"
    testenv.reset_site()                                                          # 사이트 쪽에서 로그인이 풀리면 확인이 알아챈다
    assert [s for s in m.check("synth") if s["site"] == "synth"][0]["status"] == "logged_out"


def test_the_plain_password_way_when_other_methods_are_also_offered():
    testenv.reset_site()
    m, store = started_by_address("/h/login")
    l = wait(m, m.start("synth", "password")["login_id"], ("need_choice",))
    m.input("synth", l["login_id"], l["step"], {"choice": BY_PASSWORD})
    l = wait(m, l["login_id"], ("need_input",))
    assert l["host"] == "login.test" and [f["name"] for f in l["fields"]] == ["id", "password"]
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    l = wait(m, l["login_id"], ("need_choice",))                                  # 같은 호스트의 로그인은 끝났는지 묻는다
    m.input("synth", l["login_id"], l["step"], {"choice": DONE})
    assert wait(m, l["login_id"], ("logged_in",))["state"] == "logged_in"


def test_continue_with_this_account_screen_is_offered_as_buttons_and_pressed_only_when_chosen():
    """실기기: 소셜 로그인 뒤 "이 계정으로 계속하기" 화면(칸 없음)에서 폰 승인만 기다렸다. 단추를 보여 주고 고른 것만 누른다."""
    from agent.sites.login import PROVIDER_HOSTS, WAIT_MORE
    testenv.reset_site()
    sb = SandboxManager(network=testenv.NETWORK, max_concurrent=1, persist_profile=False)
    store = MemoryBlobs()
    s = generic_site("synth", "login.test", "http://login.test/i/login", "", login_hosts=("auth.test",))
    m = SiteLoginManager(sb, store, {"synth": s}, poll_s=0.2, insecure_hosts=TEST_HOSTS)
    l = wait(m, m.start("synth", "password")["login_id"], ("need_input",))        # 아는 로그인 호스트라 묻지 않는다
    assert l["host"] == "auth.test"
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    l = wait(m, l["login_id"], ("need_choice",))                                  # 칸 없는 화면 — 여기서는 끝났는지 묻는다(소셜 단추를 고른 흐름이 아니다)
    assert l["choices"][0] == DONE
    m.cancel("synth", l["login_id"])
    wait(m, l["login_id"], ("cancelled",))

    PROVIDER_HOSTS["kakao"] = (*PROVIDER_HOSTS["kakao"], "auth.test")             # 합성 호스트를 카카오의 로그인 호스트처럼 다룬다(이 테스트에서만)
    try:
        testenv.reset_site()
        s = generic_site("synth", "login.test", "http://login.test/h2/login", "")
        m = SiteLoginManager(sb, store, {"synth": s}, poll_s=0.2, insecure_hosts=TEST_HOSTS)
        l = wait(m, m.start("synth", "password")["login_id"], ("need_choice",))
        m.input("synth", l["login_id"], l["step"], {"choice": "카카오로 로그인"})
        l = wait(m, l["login_id"], ("need_input",))                               # 고른 방법의 호스트는 묻지 않는다
        assert l["host"] == "auth.test" and l["message"].startswith("카카오 로그인 화면(auth.test)")
        m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
        l = wait(m, l["login_id"], ("need_choice",))
        assert l["choices"] == ["계속하기", "다른 계정으로 로그인", WAIT_MORE, STOP]   # 약관·개인정보 링크는 단추가 아니다
        assert "합성 상점에 로그인합니다" in l["text"]
        m.input("synth", l["login_id"], l["step"], {"choice": "계속하기"})
        l = wait(m, l["login_id"], ("logged_in",))
        assert store.get(m._key(m.sites["synth"])) and m.sites["synth"].check_url == "http://login.test/home"
    finally:
        PROVIDER_HOSTS["kakao"] = tuple(h for h in PROVIDER_HOSTS["kakao"] if h != "auth.test")


def test_a_login_already_kept_for_the_provider_is_carried_and_its_cookies_are_not_kept_under_the_site():
    """"네이버로 로그인": 네이버에 로그인해 둔 상태가 있으면 싣는다 → 계정을 다시 넣지 않고 "동의하고 계속하기"만 고른다. 보관은 그 사이트의 것만."""
    from agent.sites import login as L
    from agent.sites.login import PROVIDER_HOSTS, Site, WAIT_MORE
    testenv.reset_site()
    sb = SandboxManager(network=testenv.NETWORK, max_concurrent=1, persist_profile=False)
    store = MemoryBlobs()
    naver = Site("naver", "네이버(합성)", {"password": "http://auth.test/login"}, "http://auth.test/login", ("auth.test",), (), "http://auth.test/")
    s = generic_site("synth", "login.test", "http://login.test/h3/login", "")
    m = SiteLoginManager(sb, store, {"naver": naver, "synth": s}, poll_s=0.2, insecure_hosts=TEST_HOSTS)
    store.set(m._key(naver), json.dumps([{"name": "asid", "value": "synthetic-session", "domain": "auth.test", "path": "/"}]))   # 네이버에 로그인해 둔 상태(합성)
    PROVIDER_HOSTS["naver"] = (*PROVIDER_HOSTS["naver"], "auth.test")
    L.PROVIDER_DOMAINS = (*L.PROVIDER_DOMAINS, "auth.test")
    try:
        l = wait(m, m.start("synth", "password")["login_id"], ("need_choice",))
        assert "네이버로 로그인" in l["choices"]
        m.input("synth", l["login_id"], l["step"], {"choice": "네이버로 로그인"})
        l = wait(m, l["login_id"], ("need_choice",))                              # 계정 입력 없이 바로 중간 화면의 단추
        assert l["choices"] == ["동의하고 계속하기", "다른 계정으로 로그인", WAIT_MORE, STOP] and l["host"] == "auth.test"
        m.input("synth", l["login_id"], l["step"], {"choice": "동의하고 계속하기"})
        assert wait(m, l["login_id"], ("logged_in",))["state"] == "logged_in"
        kept = json.loads(store.get(m._key(m.sites["synth"])))["cookies"]
        assert "lsid" in [c["name"] for c in kept] and not [c for c in kept if c["domain"].endswith("auth.test")]   # 그 회사의 쿠키는 이 사이트 이름으로 보관하지 않는다
        assert testenv.site_state()["login_attempts"] == 0                        # 계정을 한 번도 제출하지 않았다
    finally:
        PROVIDER_HOSTS["naver"] = tuple(h for h in PROVIDER_HOSTS["naver"] if h != "auth.test")
        L.PROVIDER_DOMAINS = tuple(d for d in L.PROVIDER_DOMAINS if d != "auth.test")


def test_a_provider_that_refuses_this_browser_ends_the_login_instead_of_waiting():
    """실기기: 로그인 회사가 "이 브라우저에서는 로그인할 수 없음"이라고 한 화면을 폰 승인 대기로 보여 줬다. 받지 않겠다는 화면이면 멈춘다."""
    from agent.sites.login import PROVIDER_HOSTS
    testenv.reset_site()
    sb = SandboxManager(network=testenv.NETWORK, max_concurrent=1, persist_profile=False)
    PROVIDER_HOSTS["kakao"] = (*PROVIDER_HOSTS["kakao"], "auth.test")
    try:
        m = SiteLoginManager(sb, MemoryBlobs(), {"synth": generic_site("synth", "login.test", "http://login.test/m/login", "")}, poll_s=0.2, insecure_hosts=TEST_HOSTS)
        l = wait(m, m.start("synth", "password")["login_id"], ("need_choice",))
        m.input("synth", l["login_id"], l["step"], {"choice": "카카오로 로그인"})
        l = wait(m, l["login_id"], ("failed",))
        assert l["reason"] == "blocked" and "auth.test가 이 브라우저에서의 로그인을 받지 않았어요" in l["message"]
    finally:
        PROVIDER_HOSTS["kakao"] = tuple(h for h in PROVIDER_HOSTS["kakao"] if h != "auth.test")


def test_the_sites_own_rejection_text_is_shown_and_google_is_not_offered():
    testenv.reset_site()
    sb = SandboxManager(network=testenv.NETWORK, max_concurrent=1, persist_profile=False)
    m = SiteLoginManager(sb, MemoryBlobs(), {"synth": generic_site("synth", "login.test", "http://login.test/l/login", "http://login.test/home")}, poll_s=0.2, insecure_hosts=TEST_HOSTS)
    l = wait(m, m.start("synth", "password")["login_id"], ("need_input",))
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": "wrong-pass"})
    time.sleep(0.5)
    l = wait(m, l["login_id"], ("need_input",))
    assert l["error"] == "wrong_input" and "“계정 정보가 일치하지 않습니다. (합성 안내)”" in l["message"]   # 사이트가 뭐라고 했는지 그대로 보여 준다
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    l = wait(m, l["login_id"], ("need_choice",))
    m.input("synth", l["login_id"], l["step"], {"choice": DONE})
    assert wait(m, l["login_id"], ("logged_in",))["state"] == "logged_in"
    m, _ = started_by_address("/h/login")                                    # 구글 단추가 있는 화면: 구글은 고를 방법에서 빠진다
    l = wait(m, m.start("synth", "password")["login_id"], ("need_choice",))
    assert l["choices"] == [BY_PASSWORD, "카카오로 로그인", STOP] and "구글 로그인은 그 회사가 이 브라우저를 받지 않아서 뺐어요" in l["text"]
    m.cancel("synth", l["login_id"])
    wait(m, l["login_id"], ("cancelled",))


def test_a_hint_that_was_already_on_screen_is_not_a_rejection_and_a_slow_site_is_waited_for():
    """실기기: 입력 칸 아래의 도움말("TIP …")을 오류 안내로 읽어, 맞는 계정을 거절됐다고 했다(사이트는 확인하는 중이었다)."""
    testenv.reset_site()
    sb = SandboxManager(network=testenv.NETWORK, max_concurrent=1, persist_profile=False)
    m = SiteLoginManager(sb, MemoryBlobs(), {"synth": generic_site("synth", "login.test", "http://login.test/n/login", "http://login.test/home")}, poll_s=0.2, insecure_hosts=TEST_HOSTS)
    l = wait(m, m.start("synth", "password")["login_id"], ("need_input",))
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})          # 맞는 계정. 사이트는 2초 뒤에 넘어간다
    l = wait(m, l["login_id"], ("need_choice", "need_input"), timeout=20)
    assert l["state"] == "need_choice" and l["choices"][0] == DONE and "'로그아웃'이 보여요" in l["text"]   # 틀렸다고 하지 않고 기다렸다가 넘어갔다
    assert testenv.site_state()["login_attempts"] == 1                               # 한 번만 제출했다
    m.cancel("synth", l["login_id"])
    wait(m, l["login_id"], ("cancelled",))

    testenv.reset_site()                                                             # 틀린 계정: 제출 뒤 새로 뜬 안내가 거절의 까닭이다(도움말이 아니다)
    m = SiteLoginManager(sb, MemoryBlobs(), {"synth": generic_site("synth", "login.test", "http://login.test/n/login", "http://login.test/home")}, poll_s=0.2, insecure_hosts=TEST_HOSTS)
    l = wait(m, m.start("synth", "password")["login_id"], ("need_input",))
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": "wrong-pass"})
    time.sleep(1.0)
    l = wait(m, l["login_id"], ("need_input",), timeout=20)
    assert l["error"] == "wrong_input" and "계정 정보가 일치하지 않습니다" in l["message"] and "TIP" not in l["message"]
    m.cancel("synth", l["login_id"])
    wait(m, l["login_id"], ("cancelled",))


def test_a_challenge_after_submitting_is_shown_to_the_person_and_their_answer_is_entered():
    """실기기: 계정을 제출하자 "안전한 서비스 이용을 위해 아래 문제에 답해 주세요"가 떴다. 그림과 물음을 카드로 보여 주고 사용자의 답을 넣는다(대신 풀지 않는다)."""
    testenv.reset_site()
    sb = SandboxManager(network=testenv.NETWORK, max_concurrent=1, persist_profile=False)
    m = SiteLoginManager(sb, MemoryBlobs(), {"synth": generic_site("synth", "login.test", "http://login.test/o/login", "http://login.test/home")}, poll_s=0.2, insecure_hosts=TEST_HOSTS)
    l = wait(m, m.start("synth", "password")["login_id"], ("need_input",))
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    time.sleep(1.0)
    l = wait(m, l["login_id"], ("need_input",), timeout=25)
    assert [f["name"] for f in l["fields"]] == ["captcha"] and l["image"]["mime"] == "image/png" and len(l["image"]["b64"]) > 500
    assert "확인 문제를 냈어요" in l["message"] and "아래 문제에 답해 주세요" in l["message"] and l.get("error") is None
    m.input("synth", l["login_id"], l["step"], {"captcha": "틀린답"})                 # 틀린 답: 사이트가 다시 문제를 낸다(화면이 새로 그려진다)
    time.sleep(1.0)
    l = wait(m, l["login_id"], ("need_input",), timeout=25)
    if [f["name"] for f in l["fields"]] != ["captcha"]:                                # 틀린 답 뒤 로그인 화면이 새로 떴다: 계정부터 다시
        m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
        time.sleep(1.0)
        l = wait(m, l["login_id"], ("need_input",), timeout=25)
    assert [f["name"] for f in l["fields"]] == ["captcha"]
    m.input("synth", l["login_id"], l["step"], {"captcha": "7K2P"})
    l = wait(m, l["login_id"], ("need_choice",), timeout=25)
    assert l["choices"][0] == DONE and "'로그아웃'이 보여요" in l["text"]
    m.input("synth", l["login_id"], l["step"], {"choice": DONE})
    assert wait(m, l["login_id"], ("logged_in",))["state"] == "logged_in"


def test_a_challenge_in_a_frame_is_answered_and_a_tap_only_challenge_is_shown_then_stopped():
    """실기기: 문제 상자의 글이 "…답해 주세요. 닫기"뿐이라 "답할 수 없다"고만 하고 그림도 못 보여 줬다. 틀 안의 답 칸을 찾고, 답할 수 없는 종류도 그림은 보여 준다."""
    testenv.reset_site()
    sb = SandboxManager(network=testenv.NETWORK, max_concurrent=1, persist_profile=False)
    mk = lambda path: SiteLoginManager(sb, MemoryBlobs(), {"synth": generic_site("synth", "login.test", f"http://login.test{path}", "http://login.test/home")}, poll_s=0.2, insecure_hosts=TEST_HOSTS)
    m = mk("/p/login")
    l = wait(m, m.start("synth", "password")["login_id"], ("need_input",))
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    time.sleep(1.0)
    l = wait(m, l["login_id"], ("need_input",), timeout=25)
    assert [f["name"] for f in l["fields"]] == ["captcha"] and l["fields"][0]["label"] == "정답 입력" and len(l["image"]["b64"]) > 500
    assert "그림에 보이는 글자를 입력해 주세요" in l["message"]
    m.input("synth", l["login_id"], l["step"], {"captcha": "7K2P"})
    l = wait(m, l["login_id"], ("need_choice",), timeout=25)
    assert l["choices"][0] == DONE
    m.cancel("synth", l["login_id"])
    wait(m, l["login_id"], ("cancelled",))

    m = mk("/p2/login")                                                              # (D106) 눌러서 고르는 문제: 손 넘기기 — 폰에서 직접 푼다
    l = wait(m, m.start("synth", "password")["login_id"], ("need_input",))
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    time.sleep(1.0)
    l = wait(m, l["login_id"], ("need_choice",), timeout=25)
    from agent.sites.handover import STORE
    from agent.sites.login import HAND_DONE
    assert l["choices"] == [HAND_DONE, STOP] and l["handover"]["frame"].startswith("/v1/handover/") and "직접 풀어" in l["text"]
    hid = l["handover"]["id"]
    f = STORE.frame(hid)
    assert f and len(f.get("jpeg_b64") or f.get("b64") or "") > 500                     # 그 브라우저의 화면이 폰으로 간다
    m.cancel("synth", l["login_id"])
    wait(m, l["login_id"], ("cancelled",))

    m = mk("/p2/login")                                                              # 손 넘기기를 끈 관리자: 예전처럼 그림만 보여 주고 멈춘다
    m.handover_on = False
    l = wait(m, m.start("synth", "password")["login_id"], ("need_input",))
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    time.sleep(1.0)
    l = wait(m, l["login_id"], ("need_input",), timeout=25)
    assert len(l["image"]["b64"]) > 500 and "모두 고르세요" in l["message"] and "답할 칸을 찾지 못했어요" in l["message"]
    m.input("synth", l["login_id"], l["step"], {"captcha": "봤어요"})
    l = wait(m, l["login_id"], ("failed",))
    assert l["reason"] == "blocked" and "답할 칸을 찾지 못해" in l["message"]


def test_a_service_that_keeps_its_login_mark_in_browser_storage_stays_logged_in_next_time():
    """실기기: 로그인은 끝까지 됐는데, 곧바로 그 사이트를 다시 열자 "로그인이 풀렸다"였다 — 로그인 표식이 쿠키가 아니라 브라우저 저장소에 있었고, 쿠키만 보관했다."""
    testenv.reset_site()
    sb = SandboxManager(network=testenv.NETWORK, max_concurrent=1, persist_profile=False)
    store = MemoryBlobs()
    m = SiteLoginManager(sb, store, {"synth": generic_site("synth", "login.test", "http://login.test/q/login", "http://login.test/q/app")}, poll_s=0.2, insecure_hosts=TEST_HOSTS)
    l = wait(m, m.start("synth", "password")["login_id"], ("need_input",))
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    l = wait(m, l["login_id"], ("need_choice",), timeout=25)
    assert "합성 사용자님의 작업 공간" in l["text"]
    m.input("synth", l["login_id"], l["step"], {"choice": DONE})
    assert wait(m, l["login_id"], ("logged_in",))["state"] == "logged_in"
    kept = json.loads(store.get(m._key(m.sites["synth"])))
    assert kept["origins"] == [{"origin": "http://login.test", "localStorage": [{"name": "synth_token", "value": "합성-표식-1"}]}]
    assert kept["session"] == {"origin": "http://login.test", "items": [["synth_tab", "1"]]}       # 탭 저장소의 것도(우리 표식은 넣지 않는다)
    assert [s for s in m.check("synth") if s["site"] == "synth"][0]["status"] == "logged_in"         # 새 브라우저로 다시 열어도 로그인돼 있다
    with sb.session("fresh") as b:                                                                   # 보관한 것을 싣지 않으면 로그인 화면이다
        b.request("navigate", {"url": "http://login.test/q/app"})
        time.sleep(0.5)
        assert [x["name"] for x in b.request("form_fields")["fields"]] == ["id", "password"]
    with sb.session("fresh") as b:                                                                   # 싣고 연 뒤 사이트에서 나가면(표식을 지우면) 다음 화면에서 되살리지 않는다
        m.restore(b, m.sites["synth"])
        b.request("navigate", {"url": "http://login.test/q/app"})
        time.sleep(0.5)
        o = b.request("observe")
        assert "내 작업" in o["text"]
        ref = next(e for e in o["text"].splitlines() if "나가기" in e).split("]")[0].strip("[ ")
        b.request("click", {"ref": ref})
        time.sleep(0.5)
        b.request("navigate", {"url": "http://login.test/q/app"})
        time.sleep(0.5)
        assert [x["name"] for x in b.request("form_fields")["fields"]] == ["id", "password"]


def test_the_state_is_kept_after_the_returning_screen_has_settled_and_checked_in_a_fresh_browser_before_saying_logged_in():
    """소셜 로그인에서 돌아온 화면은 로그인 칸도 입구도 없지만, 1.5초 뒤에야 로그인 표식을 적고 앱 화면으로 간다.
    돌아온 순간에 보관하면 표식이 빠져 다음에 열 때 풀려 있다 → 화면이 자리 잡은 뒤 보관하고, 새 브라우저로 다시 열어 본 뒤에 로그인됐다고 한다."""
    from agent.sites.login import PROVIDER_HOSTS
    testenv.reset_site()
    PROVIDER_HOSTS["kakao"] = (*PROVIDER_HOSTS["kakao"], "auth.test")
    try:
        m, store = started_by_address("/r/login")
        l = wait(m, m.start("synth", "password")["login_id"], ("need_choice",))
        m.input("synth", l["login_id"], l["step"], {"choice": "카카오로 로그인"})
        l = wait(m, l["login_id"], ("need_input",))
        m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
        l = wait(m, l["login_id"], ("logged_in",), timeout=40)
        assert m.sites["synth"].check_url == "http://login.test/r/app"                 # 돌아오는 주소(/r/back)가 아니라 자리 잡은 화면을 적는다
        kept = json.loads(store.get(m._key(m.sites["synth"])))
        assert kept["origins"] == [{"origin": "http://login.test", "localStorage": [{"name": "synth_r", "value": "합성-표식-2"}]}]
        assert [s for s in m.check("synth") if s["site"] == "synth"][0]["status"] == "logged_in"
    finally:
        PROVIDER_HOSTS["kakao"] = tuple(h for h in PROVIDER_HOSTS["kakao"] if h != "auth.test")


def test_a_site_that_shows_only_an_access_blocked_notice_is_reported_as_blocked_not_as_no_login_form():
    """실측 2026-11-15: 공공 포털이 로그인 화면 대신 "서비스 접속이 차단되었습니다"만 보였다. "로그인 칸을 찾지 못했어요"가 아니라 막혔다고 알리고, 공개 길에서도 다시 열지 않게 적는다."""
    from agent.sites import public
    testenv.reset_site()
    old_path, old_set = public._refused_path, set(public._refused)
    try:
        public._refused_path, public._refused = None, set()
        m, store = started_by_address("/u/login")
        l = wait(m, m.start("synth", "password")["login_id"], ("failed",), timeout=30)
        assert l["reason"] == "blocked" and "접속을 막았어요" in l["message"] and "login.test" in l["message"]
        assert "login.test" in public._refused and store.get(m._key(m.sites["synth"])) is None
    finally:
        public._refused_path, public._refused = old_path, old_set


def test_a_site_that_sends_login_to_a_provider_that_refuses_this_browser_stops_before_asking_for_the_account():
    """실기기 2026-11-15: 구글 계정으로만 로그인하는 사이트에서, 계정을 입력받고 나서야 "막혔다"고 알렸다. 그 회사의 로그인 화면으로 넘어간 것을 보면 입력받기 전에 멈춘다."""
    from agent.sites.login import PROVIDER_HOSTS
    testenv.reset_site()
    PROVIDER_HOSTS["google"] = (*PROVIDER_HOSTS["google"], "auth.test")              # 합성 호스트를 그 회사의 로그인 호스트처럼 다룬다(이 테스트에서만)
    try:
        m, store = started_by_address("/e/login")                                    # 로그인 주소가 곧바로 다른 호스트(auth.test)로 넘어간다
        l = wait(m, m.start("synth", "password")["login_id"], ("failed", "need_choice", "need_input"), timeout=30)
        assert l["state"] == "failed" and l["reason"] == "blocked"                   # 호스트를 묻지도, 계정을 받지도 않는다
        assert "구글 계정으로 로그인하는데" in l["message"] and "받지 않아요" in l["message"] and l.get("fields", []) == []
    finally:
        PROVIDER_HOSTS["google"] = tuple(h for h in PROVIDER_HOSTS["google"] if h != "auth.test")


def test_login_methods_shown_as_unnamed_cards_are_offered_by_their_headings_and_a_popup_method_keeps_its_window():
    """실측 2026-11-16(정부 통합로그인): 로그인 방법이 이름 없는 그림 카드였다 — 방법을 고르게 하지 못했다. 그리고 간편인증은 새 창으로 뜨는데,
    그 주소를 같은 탭에서 다시 열면 "비정상적인 접근입니다"가 뜬다(앞 화면에서 넘어온 창이어야 한다)."""
    testenv.reset_site()
    m, store = started_by_address("/x/login")
    l = wait(m, m.start("synth", "password")["login_id"], ("need_choice",))
    assert l["choices"] == ["간편 인증", "공동인증서", "아이디 로그인", STOP]                 # 카드 안의 머리글이 방법 이름이다("사용자 등록"은 방법이 아니다)
    m.input("synth", l["login_id"], l["step"], {"choice": "아이디 로그인"})
    l = wait(m, l["login_id"], ("need_input",))                                        # 아이디·비밀번호 화면으로 이어진다
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    l = wait(m, l["login_id"], ("need_choice",), timeout=25)
    m.input("synth", l["login_id"], l["step"], {"choice": DONE})
    assert wait(m, l["login_id"], ("logged_in",), timeout=40)["state"] == "logged_in"

    testenv.reset_site()
    m, store = started_by_address("/x/login")
    l = wait(m, m.start("synth", "password")["login_id"], ("need_choice",))
    m.input("synth", l["login_id"], l["step"], {"choice": "간편 인증"})
    # 간편인증(D79): 새 창이 유지돼 화면이 열리고("비정상적인 접근"이 아니다), 인증 앱 → 이름·생년월일·번호 → 동의 → 요청 → 폰 승인 → 인증 완료
    l = wait(m, l["login_id"], ("need_choice",), timeout=30)
    assert sorted(l["choices"]) == sorted(["가온인증", "새벽패스", "합성톡", STOP]) and "인증 앱" in l["text"]   # "사용방법 매뉴얼 안내"는 인증 기관이 아니다
    m.input("synth", l["login_id"], l["step"], {"choice": "새벽패스"})
    l = wait(m, l["login_id"], ("need_input",))
    assert [(x["name"], x["secret"]) for x in l["fields"]] == [("name", False), ("birth", False), ("phone", False)]
    m.input("synth", l["login_id"], l["step"], {"name": "가상인", "birth": "1990-01-01", "phone": "010-1234"})
    l = wait_step(m, l, ("need_input",))                                               # 번호가 짧다: 화면에 넣기 전에 다시 받는다
    assert "휴대폰 번호" in l["message"] and testenv.site_state()["login_attempts"] == 0
    m.input("synth", l["login_id"], l["step"], {"name": "가상인", "birth": "1990-01-01", "phone": "010-1234-9999"})
    l = wait(m, l["login_id"], ("need_choice",))
    assert l["choices"] == [EASY_AGREE, STOP]
    for c in ("개인정보 수집ㆍ이용 동의", "개인정보 제3자 제공 동의", "서비스이용약관 동의"):
        assert c in l["text"]                                                          # 동의 항목은 화면에서 읽은 이름 그대로 보여 준다
    assert "전체동의" not in l["text"]
    m.input("synth", l["login_id"], l["step"], {"choice": EASY_AGREE})
    l = wait_step(m, l, ("need_input",), timeout=30)                                   # 번호가 틀렸다: 화면의 안내를 옮기고 처음부터 다시 받는다
    assert "다시 확인해 주세요" in l["message"] and testenv.site_state()["login_attempts"] == 1
    m.input("synth", l["login_id"], l["step"], {"name": "가상인", "birth": "19900101", "phone": "01012341234"})   # 인증 앱·동의는 다시 묻지 않는다
    l = wait_step(m, l, ("need_choice",), timeout=30)
    assert l["choices"] == ["인증 완료", STOP] and "인증 앱" in l["text"] and testenv.site_state()["easy_requested"] == "새벽패스"
    m.input("synth", l["login_id"], l["step"], {"choice": "인증 완료"})                    # 아직 폰에서 인증하지 않았다: 화면의 안내와 함께 다시 묻는다
    l = wait_step(m, l, ("need_choice",), timeout=30)
    assert l["choices"] == ["인증 완료", STOP] and "확인되지 않았어요" in l["text"]       # 화면 글을 잘라 옮기지 않고 할 일 한 문장(실기기 2026-11-17)
    m.input("synth", l["login_id"], l["step"], {"choice": "인증 완료"})                    # 누르자 단추 없는 "확인 중" 화면: 묻지 않고 지켜본다
    l = wait_step(m, l, ("need_approval", "need_choice"), timeout=30)
    assert l["state"] == "need_approval" and "확인하고 있어요" in l["message"]             # 실기기 2026-11-17: 여기서 "로그인이 끝났나요?"를 물었다
    testenv._site_request("POST", "/_test/approve")                                    # 그 사이 폰에서 인증했다
    l = wait_step(m, l, ("need_choice", "logged_in"), timeout=40)
    assert l["state"] == "logged_in"                                                   # "로그인이 끝났나요?"를 다시 묻지 않는다(실기기 2026-11-16)
    kept = json.dumps(store.get(m._key(m.sites["synth"])) if isinstance(store.get(m._key(m.sites["synth"])), dict) else str(store.get(m._key(m.sites["synth"]))), ensure_ascii=False)
    for secret in ("가상인", "19900101", "01012341234", "12341234"):
        assert secret not in kept, secret                                              # 넣은 값은 보관하지 않는다(로그인 상태만)


def test_qr_login_shows_the_code_waits_for_the_phone_and_takes_no_password():
    """QR 로그인(D85): 방법 카드에 QR이 있고, 고르면 QR 그림·숫자를 보여 주고, 만료되면 새로 받을지 묻고, 폰에서 승인하면 스스로 이어 간다."""
    from agent.sites.login import BY_PASSWORD, BY_QR, QR_REFRESH
    testenv.reset_site()
    m, store = manager("/qr/login")
    l = wait(m, m.start("synth", "password")["login_id"], ("need_choice",))
    assert l["choices"] == [BY_PASSWORD, BY_QR, STOP]                                  # "QR 로그인 방법 안내" 링크는 방법이 아니다
    m.input("synth", l["login_id"], l["step"], {"choice": BY_QR})
    l = wait(m, l["login_id"], ("need_approval",), timeout=25)
    first = l["qr"]["b64"]
    assert l["qr"]["mime"] == "image/png" and len(first) > 200 and l["number"] == "65" and "65" in l["message"]
    testenv._site_request("POST", "/_test/qr_expire")                                  # 시간이 지났다: 새로 받을지 묻는다
    l = wait_step(m, l, ("need_choice",), timeout=25)
    assert l["choices"] == [QR_REFRESH, STOP]
    m.input("synth", l["login_id"], l["step"], {"choice": QR_REFRESH})
    l = wait_step(m, l, ("need_approval",), timeout=25)
    assert l["qr"]["b64"] != first                                                     # 새 그림
    testenv._site_request("POST", "/_test/approve")                                    # 폰에서 승인했다
    l = wait(m, l["login_id"], ("logged_in",), timeout=40)
    assert l["state"] == "logged_in" and testenv.site_state()["login_attempts"] == 0   # 아이디·비밀번호는 한 번도 내지 않았다
    assert store.get(m._key(m.sites["synth"])) is not None


def test_handover_lets_a_person_tap_and_type_in_the_agent_browser():
    # 손 넘기기(D106): 사람 확인 화면을 사람이 폰에서 직접 푼다 — 화면을 받고, 좌표로 누르고, 글자를 친다(에이전트는 누르지 않는다)
    from agent.browser.client import BrowserSession
    from agent.sites.handover import STORE
    from agent.sites.inside import challenge_on
    b = BrowserSession(network=testenv.NETWORK)
    try:
        b.request("navigate", {"url": "http://login.test/human-check"})
        assert challenge_on(b)
        hid = STORE.open(b, "사람 확인", "login.test")
        f = STORE.frame(hid)
        assert f["width"] > 300 and len(f["jpeg_b64"]) > 1000
        assert STORE.input(hid, {"kind": "tap", "x": 200, "y": 130})
        assert STORE.input(hid, {"kind": "tap", "x": 200, "y": 220}) and STORE.input(hid, {"kind": "text", "text": "토끼"})
        text = b.request("extract_text", {"ref": None})["text"]
        assert "확인 완료" in text and "토끼" in text and not challenge_on(b)
        STORE.close(hid)
        assert STORE.frame(hid) is None and not STORE.input(hid, {"kind": "tap", "x": 1, "y": 1})   # 닫히면 더는 못 쓴다
    finally:
        b.close()


def test_going_elsewhere_from_a_kept_popup_returns_to_the_original_tab_so_the_popup_opens_again():
    # 실기기 2026-11-18 에어코리아: 그대로 쓰던 주소 찾기 창 안에서 다른 곳으로 간 뒤, 원래 화면의 단추가 같은 이름의 창을 다시 열지 못했다
    from agent.browser.client import BrowserSession
    from agent.tools.observation import Observation
    b = BrowserSession(network=testenv.NETWORK)
    try:
        b.request("dialog_policy", {"keep_popups": True})
        for _ in (1, 2):   # 두 번째에도 같은 이름의 창이 다시 떠야 한다
            b.request("navigate", {"url": "http://login.test/pop-list"})
            o = Observation.parse(b.request("observe")["text"])
            b.request("click", {"ref": next(e.ref for e in o.elements.values() if e.role == "button" and e.label == "자세히 보기")})
            assert Observation.parse(b.request("observe")["text"]).url.endswith("/pop-win")
    finally:
        b.close()


def test_failure_notice_but_actually_signed_in_is_checked_without_asking():
    # D120(실기기 2026-11-18 채용 사이트): 제출 뒤 '로그인 실패' 안내가 떠 있는데 "로그인이 끝났나요?"를 물었다 — 실제로는 로그인돼 있었다.
    # 로그아웃이 보이고 로그인 칸·입구가 없으면 코드가 스스로 확인하고 묻지 않는다
    m, store = manager("/fx/login")
    l = m.start("synth", "password")
    l = wait(m, l["login_id"], ("need_input",))
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    l = wait(m, l["login_id"], ("logged_in", "need_choice"))
    assert l["state"] == "logged_in", l


def test_real_failure_notice_offers_retry_instead_of_asking_if_done():
    m, store = manager("/gx/login")
    l = m.start("synth", "password")
    l = wait(m, l["login_id"], ("need_input",))
    m.input("synth", l["login_id"], l["step"], {"id": ID, "password": PW})
    l = wait(m, l["login_id"], ("need_choice",))
    assert l["choices"] == [RETRY, DONE, STOP] and "실패했다고" in l["text"]
    m.input("synth", l["login_id"], l["step"], {"choice": RETRY})
    l = wait(m, l["login_id"], ("need_input",))                     # 로그인 화면을 다시 열어 처음부터
    assert [f["name"] for f in l["fields"]] == ["id", "password"]
    m.cancel("synth", l["login_id"])
    wait(m, l["login_id"], ("cancelled",))


def test_easy_auth_window_left_checking_while_the_site_tab_already_moved_on():
    # D120(실기기 2026-11-18 브이월드): 폰에서 승인한 뒤 "인증 완료" — 원래 탭은 곧바로 사이트로 넘어갔는데 인증 창은 '확인 중'으로 남았다.
    # 기다리기 시작한 때 이미 넘어가 있어 견줄 기준이 그 화면이 됐고, 3분 뒤 실패로 끝냈다(실제로는 로그인돼 있었다)
    testenv.reset_site()
    m, store = started_by_address("/x2/login")
    l = wait(m, m.start("synth", "password")["login_id"], ("need_choice",))
    m.input("synth", l["login_id"], l["step"], {"choice": "간편 인증"})
    l = wait(m, l["login_id"], ("need_choice",), timeout=30)
    m.input("synth", l["login_id"], l["step"], {"choice": "새벽패스"})
    l = wait(m, l["login_id"], ("need_input",))
    m.input("synth", l["login_id"], l["step"], {"name": "가상인", "birth": "19900101", "phone": "01012341234"})
    l = wait(m, l["login_id"], ("need_choice",))
    m.input("synth", l["login_id"], l["step"], {"choice": EASY_AGREE})
    l = wait_step(m, l, ("need_choice",), timeout=30)
    assert l["choices"] == ["인증 완료", STOP]
    testenv._site_request("POST", "/_test/approve")                                    # 폰에서 먼저 승인했다
    t0 = time.time()
    m.input("synth", l["login_id"], l["step"], {"choice": "인증 완료"})
    l = wait(m, l["login_id"], ("logged_in",), timeout=60)
    assert l["state"] == "logged_in" and time.time() - t0 < 60 and store.get(m._key(m.sites["synth"]))


def test_a_window_that_opens_late_is_picked_up_on_the_next_look():
    # D120(실기기 서울교통공사): 누르고 몇 초 뒤에야 새 창이 떴다 — 0.3초만 기다리고 지나쳐 모델은 첫 화면만 봤다
    from agent.browser.client import BrowserSession
    from agent.tools.observation import Observation
    b = BrowserSession(network=testenv.NETWORK)
    try:
        b.request("dialog_policy", {"keep_popups": True, "late_popups": True})
        b.request("navigate", {"url": "http://login.test/late-list"})
        o = Observation.parse(b.request("observe")["text"])
        b.request("click", {"ref": next(e.ref for e in o.elements.values() if e.label == "열차시각표")})
        for _ in range(4):                                       # 모델처럼: 화면이 그대로면 한 번 더 본다
            time.sleep(1.0)
            o = Observation.parse(b.request("observe")["text"])
            if o.url.endswith("/late-pop"):
                break
        assert o.url.endswith("/late-pop") and any("역" in (e.label or "") for e in o.elements.values())
    finally:
        b.close()


def test_a_downloaded_csv_is_read_as_a_table_and_queried_by_code():
    # D125: 화면에는 일부만, 전체는 CSV — 받아서 표로 읽고, 거르기·정렬은 코드가(전세=월세 0, 84㎡ 이상)
    from agent.browser.client import BrowserSession
    from agent.tools.observation import Observation
    b = BrowserSession(network=testenv.NETWORK, extra_args=["-e", "ALLOW_DOWNLOADS=1"])
    try:
        b.request("navigate", {"url": "http://login.test/rent-list"})
        o = Observation.parse(b.request("observe")["text"])
        got = b.request("download_table", {"ref": next(e.ref for e in o.elements.values() if e.label == "CSV 다운로드")})
        assert got["table_id"] == "t1" and got["columns"][3] == "보증금(만원)" and got["rows_total"] == 4
        q = b.request("table_query", {"table_id": "t1", "where": [{"col": "전월세구분", "op": "=", "value": "전세"},
                                                                   {"col": "전용면적(㎡)", "op": ">=", "value": "84"}],
                                       "sort_by": "보증금(만원)", "desc": True, "limit": 3, "columns": ["단지명", "보증금(만원)"]})
        assert q["matched"] == 2 and q["rows"][0] == ["가람", "110,000"]          # 보증금이 더 큰 반전세(나래)는 빠졌다
    finally:
        b.close()
    b = BrowserSession(network=testenv.NETWORK, extra_args=["-e", "ALLOW_DOWNLOADS=1", "-e", "DOWNLOADS_GATE=1"])   # D126: 로그인한 사이트의 세션
    try:
        b.request("navigate", {"url": "http://login.test/rent-list"})
        ref = next(e.ref for e in Observation.parse(b.request("observe")["text"]).elements.values() if e.label == "CSV 다운로드")
        try:
            b.request("download_table", {"ref": ref})
            raise AssertionError("downloaded before allowed")
        except Exception as e:
            assert "내려받지 않는다" in str(e)
        b.request("dialog_policy", {"downloads_ok": True})                          # 사용자가 허락한 뒤
        assert b.request("download_table", {"ref": ref})["rows_total"] == 4
    finally:
        b.close()
    b = BrowserSession(network=testenv.NETWORK)                                       # 켜지 않은 세션(로그인한 사이트 등)은 받지 않는다
    try:
        b.request("navigate", {"url": "http://login.test/rent-list"})
        o = Observation.parse(b.request("observe")["text"])
        try:
            b.request("download_table", {"ref": next(e.ref for e in o.elements.values() if e.label == "CSV 다운로드")})
            raise AssertionError("downloaded")
        except Exception as e:
            assert "내려받지 않는다" in str(e)
    finally:
        b.close()


def test_a_human_check_before_the_login_form_is_handed_over_then_the_login_goes_on():
    # D106(실기기 2026-11-18 claude.ai): 로그인 칸 대신 사람 확인이 먼저 떠 "로그인 칸을 찾지 못했어요"로 끝났다 — 폰으로 넘긴다
    from agent.sites.handover import STORE
    from agent.sites.login import HAND_DONE
    testenv.reset_site()
    m, store = manager("/hc/login")
    l = wait(m, m.start("synth", "password")["login_id"], ("need_choice",), timeout=30)
    assert l["choices"] == [HAND_DONE, STOP] and "사람 확인" in l["text"] and l["handover"]["id"]
    assert STORE.input(l["handover"]["id"], {"kind": "tap", "x": 200, "y": 130})            # 사람이 폰에서 '확인'을 누른다
    m.input("synth", l["login_id"], l["step"], {"choice": HAND_DONE})
    l = wait(m, l["login_id"], ("need_input",), timeout=30)                                 # 로그인 칸이 나왔다
    assert [f["name"] for f in l["fields"]] == ["id", "password"]
    m.cancel("synth", l["login_id"])
    wait(m, l["login_id"], ("cancelled",))


def test_long_result_pages_are_captured_as_several_screen_tall_shots():
    # 결과 화면이 창 높이에서 잘리던 것(2026-11-17): 창 높이씩 나눠 3장까지
    from agent.browser.client import BrowserSession
    from agent.browser.shots import captioned, page_shots
    b = BrowserSession(network=testenv.NETWORK)
    try:
        b.request("navigate", {"url": "http://login.test/long"})
        shots = page_shots(b, 900_000)
        assert len(shots) == 3 and all(s[:2] == b"\xff\xd8" for s in shots)
        assert [c for _, c in captioned(shots, "합성 화면")] == ["합성 화면 (1/3)", "합성 화면 (2/3)", "합성 화면 (3/3)"]
        b.request("navigate", {"url": "http://login.test/human-check"})
        assert len(page_shots(b, 900_000)) == 1                                     # 창 안에 다 들어오는 화면은 한 장
    finally:
        b.close()


def test_site_icon_is_taken_from_the_page_skipping_svg_and_saved_as_a_small_png(tmp_path):
    # 사이트 아이콘(D110): 연동 센터 줄에 쓸 그림 — 그 페이지가 적어 둔 것을 같은 사이트에서 받는다
    import base64
    from agent.browser.client import BrowserSession
    from agent.sites.icons import IconStore
    b = BrowserSession(network=testenv.NETWORK)
    try:
        b.request("navigate", {"url": "http://login.test/iconpage"})
        got = b.request("site_icon")
        assert got["type"] == "image/png" and base64.b64decode(got["data"])[:4] == b"\x89PNG"   # SVG는 건너뛰고 apple-touch-icon
        store = IconStore(tmp_path)
        assert store.grab(b, "synth") and store.get("synth")[:4] == b"\x89PNG"
        b.request("navigate", {"url": "http://auth.test/"})
        assert b.request("site_icon") == {"type": None}   # 적어 둔 것도 /favicon.ico도 없으면 없다(그림이 아닌 404 화면은 받지 않는다)
    finally:
        b.close()
