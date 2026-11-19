"""공통 로그인(D70)의 흐름: 가짜 브라우저로. 실제 브라우저와 합성 사이트로 도는 것은 tests/integration/test_login_generic.py."""

import contextlib
import json
import time

import pytest

from agent.sites.login import DONE, GO_ON, NOT_YET, STOP, LoginError, SiteLoginManager, generic_site
from agent.sites.store import MemoryBlobs

SECRET = "synthetic-pass-1"


class FakeBrowser:
    """화면 몇 장을 흉내 낸다: login(아이디·비밀번호) → (맞으면) wait(칸 없음, 승인 대기) → home. other=True면 로그인은 auth.example에서."""

    def __init__(self, w):
        self.w, self.page, self.host, self.filled = w, "blank", "shop.example", {}

    def request(self, op, args=None):
        args = args or {}
        self.w["ops"].append(op if op != "form_fill" else "form_fill:" + ",".join(sorted(args["values"])))
        if op == "navigate":
            url = args["url"]
            if getattr(self, "restored", False):   # 보관한 로그인 상태를 실은 브라우저: 어느 주소를 열어도 로그인된 화면
                self.page, self.host = "home", "shop.example"
            elif url.endswith("/my"):
                self.page, self.host = ("home", "shop.example") if self.w.get("in") else ("login", self.w.get("login_host", "shop.example"))
            else:
                self.page, self.host = self.w.get("first", "login"), self.w.get("login_host", "shop.example")
            return {"url": url}
        if op == "form_fields":
            if self.page == "wait" and getattr(self, "social", False) and self.w.get("approved"):
                self.page, self.host, self.w["in"] = "home", "shop.example", True   # 폰에서 승인하자 사이트로 돌아왔다
            fields = {"login": [{"name": "id", "kind": "text", "label": "이메일"}, {"name": "password", "kind": "password", "label": ""}],
                      "signup": [{"name": "id", "kind": "text", "label": "이메일"}, {"name": "password", "kind": "password", "label": "비밀번호"}]}
            return {"host": self.host, "https": self.w.get("https", True), "fields": fields.get(self.page, []), "url": f"https://{self.host}/{self.page}",
                    "alert": (getattr(self, "rejected", False) or bool(self.w.get("hint"))) and self.page == "login", "pw_empty": None,
                    "alert_text": (self.w.get("reject_text", "계정 정보가 맞지 않습니다") if getattr(self, "rejected", False) else self.w.get("hint", "")) if self.page == "login" else "",
                    "submit": True, "captcha_image": False, "signup": self.page == "signup"}
        if op == "login_methods":
            return {"host": self.host, "providers": list(self.w.get("providers", [])) if self.page == "login" else [],
                    "entry": self.page == "front"}
        if op == "login_cards":
            return {"host": self.host, "cards": list(self.w.get("cards", [])) if self.page == "login" else []}
        if op == "login_buttons":
            return {"host": self.host, "buttons": list(self.w.get("buttons", [])) if self.page == "wait" else []}
        if op == "login_press":
            self.w.setdefault("pressed", []).append(args["text"])
            if args["text"] == "계속하기":
                self.page, self.host, self.w["in"] = "home", "shop.example", True
            return {}
        if op == "login_open":
            self.w.setdefault("opened", []).append(args)
            if args.get("entry"):
                self.page = "login"
            else:
                self.page, self.host = "login", {"kakao": "accounts.kakao.com"}[args["provider"]]
                self.social = True
            return {"how": "same_tab"}
        if op == "form_fill":
            self.rejected = False
            if self.w.get("fill_fails"):
                self.w["fill_fails"] -= 1
                raise RuntimeError("잘못된 인자: 화면의 입력 칸이 바뀌었다 (form_fields를 다시 한다)")
            self.filled = dict(args["values"])
            return {"filled": len(self.filled)}
        if op == "form_submit":
            if self.filled.get("password") == SECRET and getattr(self, "social", False):
                self.page = "wait"            # 소셜 로그인: 그 회사 화면에서 폰 승인을 기다린다(호스트 그대로)
            elif self.filled.get("password") == SECRET:
                self.page, self.host = ("wait" if self.w.get("approval") else "home"), "shop.example"
                self.w["in"] = not self.w.get("approval")
            else:
                self.rejected = not self.w.get("silent")   # 틀린 값: 사이트가 오류 안내를 띄운다(silent면 아무 반응이 없다)
            self.filled = {}
            return {"how": "button"}
        if op == "extract_text":
            if self.page == "wait" and self.w.get("approved"):
                self.page, self.w["in"] = "home", True
            return {"text": {"home": "내 정보  합성 사용자님", "wait": "휴대폰에서 승인해 주세요"}.get(self.page, "")}
        if op == "cookies_export":
            return {"cookies": [{"name": "sid", "value": "v", "domain": "shop.example", "path": "/"}]}
        if op == "cookies_import":
            if not self.w.get("state_lost"):   # state_lost: 보관한 것만으로는 로그인이 이어지지 않는 서비스
                self.w["in"] = self.restored = True
            return {"count": 1}
        raise AssertionError(op)


class FakeSandbox:
    def __init__(self, w):
        self.w = w

    @contextlib.contextmanager
    def session(self, user_id, env=None):
        self.w["env"] = env
        self.w["in"] = False
        yield FakeBrowser(self.w)


@pytest.fixture
def w():
    return {"ops": []}


def make(w, **kw):
    w["store"] = MemoryBlobs()
    site = generic_site("shop", "합성 상점", "https://shop.example/login", "https://shop.example/my")
    kw.setdefault("settle_s", 0.15)
    return SiteLoginManager(FakeSandbox(w), w["store"], {"shop": site}, poll_s=0.01, **kw)


def wait_k(m, key, lid, want, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        l = m.get(key, lid)
        if l["state"] in want:
            return l
        time.sleep(0.005)
    raise AssertionError(f"{want} 안 됨: {l}")


def wait(m, lid, want, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        l = m.get("shop", lid)
        if l["state"] in want:
            return l
        time.sleep(0.005)
    raise AssertionError(f"{want} 안 됨: {l}")


def test_fields_are_shown_with_the_host_and_only_the_login_state_is_kept(w):
    m = make(w)
    l = wait(m, m.start("shop", "password")["login_id"], ("need_input",))
    assert l["fields"] == [{"name": "id", "label": "이메일", "secret": False}, {"name": "password", "label": "비밀번호", "secret": True}]
    assert "shop.example" in l["message"]
    with pytest.raises(LoginError):
        m.input("shop", l["login_id"], l["step"], {"id": "a"})                      # 칸을 다 채워야 받는다
    m.input("shop", l["login_id"], l["step"], {"id": "tester@example.com", "password": SECRET})
    l = wait(m, l["login_id"], ("need_choice",))
    assert l["choices"] == [DONE, NOT_YET, STOP] and "내 정보" in l["text"]
    m.input("shop", l["login_id"], l["step"], {"choice": DONE})
    l = wait(m, l["login_id"], ("logged_in",))
    kept = w["store"].get(m._key(m.sites["shop"]))
    assert json.loads(kept)["cookies"][0]["name"] == "sid" and SECRET not in kept
    assert SECRET not in json.dumps(l, ensure_ascii=False) and SECRET not in " ".join(w["ops"])
    assert [s["status"] for s in m.status() if s["site"] == "shop"] == ["logged_in"]
    assert [s["methods"] for s in m.status() if s["site"] == "shop"] == [["password"]]


def test_same_fields_coming_back_count_as_wrong_and_stop_at_three(w):
    m = make(w)
    l = m.start("shop", "password")
    for n in range(3):
        l = wait(m, l["login_id"], ("need_input",))
        assert (l.get("error") == "wrong_input") == (n > 0)
        m.input("shop", l["login_id"], l["step"], {"id": "x", "password": f"bad{n}"})
        time.sleep(0.05)
    assert wait(m, l["login_id"], ("failed",))["reason"] == "wrong_input"
    assert w["store"].get(m._key(m.sites["shop"])) is None


def test_other_host_plain_http_and_signup_take_no_values_without_a_yes(w):
    w["login_host"] = "auth.example"
    m = make(w)
    l = wait(m, m.start("shop", "password")["login_id"], ("need_choice",))
    assert l["choices"] == [GO_ON, STOP] and "auth.example" in l["text"] and not any(o.startswith("form_fill") for o in w["ops"])
    m.input("shop", l["login_id"], l["step"], {"choice": STOP})
    assert wait(m, l["login_id"], ("failed",))["reason"] == "host_refused"
    m._thread.join(2)
    w2 = {"ops": [], "https": False}
    m = make(w2)
    assert wait(m, m.start("shop", "password")["login_id"], ("failed",))["reason"] == "not_https"
    m._thread.join(2)
    w3 = {"ops": [], "first": "signup"}
    m = make(w3)
    assert wait(m, m.start("shop", "password")["login_id"], ("failed",))["reason"] == "unsupported_login"
    m._thread.join(2)
    w4 = {"ops": [], "first": "blank"}
    m = make(w4)
    assert wait(m, m.start("shop", "password")["login_id"], ("failed",))["reason"] == "unsupported_login"   # 로그인 칸을 못 찾음


def test_approval_wait_screen_is_left_to_the_user_and_done_too_early_keeps_nothing(w):
    w["approval"] = True
    m = make(w)
    l = wait(m, m.start("shop", "password")["login_id"], ("need_input",))
    m.input("shop", l["login_id"], l["step"], {"id": "x", "password": SECRET})
    l = wait(m, l["login_id"], ("need_choice",))
    assert "2단계 인증 알림" in l["text"] or "승인" in l["text"]   # 화면 글 대신 요지(2026-11-17)
    m.input("shop", l["login_id"], l["step"], {"choice": DONE})                     # 아직 승인하지 않았는데 끝났다고 했다
    l = wait(m, l["login_id"], ("need_input",))                                     # 확인 화면에 로그인 칸 → 보관하지 않는다
    assert w["store"].get(m._key(m.sites["shop"])) is None
    m.input("shop", l["login_id"], l["step"], {"id": "x", "password": SECRET})
    l = wait(m, l["login_id"], ("need_choice",))
    w["approved"] = True
    m.input("shop", l["login_id"], l["step"], {"choice": NOT_YET})
    time.sleep(0.05)
    l = wait(m, l["login_id"], ("need_choice",))
    assert "내 정보" in l["text"]
    m.input("shop", l["login_id"], l["step"], {"choice": DONE})
    assert wait(m, l["login_id"], ("logged_in",))["state"] == "logged_in"


def test_check_reads_the_kept_state_and_headed_mode_is_asked_for_only_when_on(w):
    m = make(w, headed=True)
    l = wait(m, m.start("shop", "password")["login_id"], ("need_input",))
    assert w["env"] == {"BROWSER_HEADED": "1"}
    m.input("shop", l["login_id"], l["step"], {"id": "x", "password": SECRET})
    l = wait(m, l["login_id"], ("need_choice",))
    m.input("shop", l["login_id"], l["step"], {"choice": DONE})
    wait(m, l["login_id"], ("logged_in",))
    m._thread.join(2)
    assert [s["status"] for s in m.check("shop") if s["site"] == "shop"] == ["logged_in"]
    w["store"].delete(m._key(m.sites["shop"]))
    assert [s["status"] for s in m.check("shop") if s["site"] == "shop"] == ["logged_out"]
    with pytest.raises(ValueError):
        generic_site("Shop.Example", "x", "https://a.example/login", "https://a.example/my")


def test_site_list_file_takes_only_https_sites_with_a_plain_key(tmp_path):
    from agent.sites.login import load_generic_sites
    f = tmp_path / "sites_generic.json"
    f.write_text(json.dumps([
        {"key": "shop", "name": "합성 상점", "login_url": "https://shop.example/login", "check_url": "https://shop.example/my", "login_hosts": ["auth.example"]},
        {"key": "plain", "name": "평문", "login_url": "http://plain.example/login", "check_url": "https://plain.example/my"},
        {"key": "Bad Key", "name": "x", "login_url": "https://a.example/login", "check_url": "https://a.example/my"},
        {"key": "nourl", "name": "x"}, "글자"]))
    sites = load_generic_sites(f)
    assert list(sites) == ["shop"] and sites["shop"].generic and sites["shop"].login_hosts == ("auth.example",)
    assert load_generic_sites(tmp_path / "none.json") == {}
    f.write_text("{깨진")
    assert load_generic_sites(f) == {}


def test_a_site_named_by_address_goes_through_the_entry_link_and_a_social_login_without_extra_questions(w, tmp_path):
    """주소로 시작한 사이트: 첫 화면의 "로그인" 입구 → 방법 고르기 → 카카오 → 그 회사 화면에 입력 → 폰 승인을 지켜봄 → 돌아오면 끝."""
    from agent.sites.login import BY_PASSWORD, LoginError as LE, load_generic_sites
    w.update(first="front", providers=["kakao"])
    w["store"] = MemoryBlobs()
    m = SiteLoginManager(FakeSandbox(w), w["store"], {}, poll_s=0.01, settle_s=0.15, sites_path=tmp_path / "sites_generic.json")
    key = m.add_site("Shop.Example")
    assert key == "shop-example" and m.add_site("shop.example") == key
    assert [(s["site"], s["name"], s["status"]) for s in m.status() if s["site"] == key] == [("shop-example", "shop.example", "logged_out")]
    assert m.add_site("nid.naver.com") == "naver"                                    # 이미 아는 사이트의 주소면 그 사이트
    assert m.add_alias(key, "예시몰") and not (tmp_path / "sites_generic.json").exists()   # 로그인에 성공하기 전에는 파일에 적지 않는다
    l = wait_k(m, key, m.start(key, "password")["login_id"], ("need_choice",))
    assert w["opened"] == [{"entry": True}]                                          # 로그인 화면이 아니어서 입구를 눌렀다
    assert l["choices"] == [BY_PASSWORD, "카카오로 로그인", STOP] and "어떤 방법" in l["text"]
    m.input(key, l["login_id"], l["step"], {"choice": "카카오로 로그인"})
    l = wait_k(m, key, l["login_id"], ("need_input",))                               # 고른 방법의 호스트는 다시 묻지 않는다
    assert l["host"] == "accounts.kakao.com" and "accounts.kakao.com" in l["message"] and w["opened"][-1] == {"provider": "kakao"}
    assert l["message"] == "카카오 로그인 화면(accounts.kakao.com)이에요. 카카오 계정 정보를 입력해 주세요"   # 앱은 호스트를 따로 보여 주지 않는다
    m.input(key, l["login_id"], l["step"], {"id": "x", "password": SECRET})
    l = wait_k(m, key, l["login_id"], ("need_approval",))
    assert "카카오가 2단계 인증 알림을 보냈어요" in l["message"] and l["host"] == "accounts.kakao.com"
    w["approved"] = True                                                             # 폰에서 승인했다 → 묻지 않고 이어 간다
    l = wait_k(m, key, l["login_id"], ("logged_in",))
    m._thread.join(2)
    assert m.sites[key].check_url == "https://shop.example/home"                     # 다음에 확인할 화면을 적어 둔다
    assert m.sites[key].aliases == ("예시몰",)                                        # 로그인하는 사이에 적힌 '부르는 이름'이 남아 있다
    assert load_generic_sites(tmp_path / "sites_generic.json")[key].aliases == ("예시몰",)
    rows = json.loads((tmp_path / "sites_generic.json").read_text())
    assert rows == [{"key": key, "name": "shop.example", "login_url": "https://shop.example/", "check_url": "https://shop.example/home", "login_hosts": [], "aliases": ["예시몰"], "via": "kakao"}]   # 어느 방법으로 들어왔는지도 적는다(D90)
    assert SECRET not in (tmp_path / "sites_generic.json").read_text()
    from agent.sites.login import load_generic_sites
    assert load_generic_sites(tmp_path / "sites_generic.json")[key].check_url == "https://shop.example/home"
    for bad in ("localhost", "192.168.0.1", "shop", "a b.example", "-x.example", ""):
        with pytest.raises(LE):
            m.add_site(bad)


def test_choosing_the_password_way_or_stopping_at_the_method_question(w):
    from agent.sites.login import BY_PASSWORD
    w["providers"] = ["kakao", "google"]
    m = make(w)
    l = wait(m, m.start("shop", "password")["login_id"], ("need_choice",))
    assert l["choices"] == [BY_PASSWORD, "카카오로 로그인", STOP] and "구글 로그인은" in l["text"]   # 구글은 이 브라우저의 로그인을 받지 않아 고를 방법에서 뺀다
    m.input("shop", l["login_id"], l["step"], {"choice": BY_PASSWORD})
    l = wait(m, l["login_id"], ("need_input",))
    assert l["host"] == "shop.example" and "opened" not in w
    m.cancel("shop", l["login_id"])
    wait(m, l["login_id"], ("cancelled",))
    m._thread.join(2)
    w2 = {"ops": [], "providers": ["kakao"]}
    m = make(w2)
    l = wait(m, m.start("shop", "password")["login_id"], ("need_choice",))
    m.input("shop", l["login_id"], l["step"], {"choice": STOP})
    assert wait(m, l["login_id"], ("cancelled",))["state"] == "cancelled" and "opened" not in w2


def test_chat_starts_a_login_for_a_site_the_user_named_by_address_and_only_then():
    """"<주소> 로그인하자": 주소는 사용자의 말에서 코드가 읽는다(모델이 쓰지 않는다). 주소가 없으면 묻는다."""
    from agent.api.dialog import said_host
    from test_dialog_context import make_cloud, sent
    added = []

    def add_site(host):
        added.append(host)
        if host == "bad.example":
            raise LoginError("invalid_input")
        return "shop-example-com"
    dlg, mgr, sites, model, ran = make_cloud({"action": "site_login", "target": "new", "replies": ["로그인 화면 열어 볼게요!"]},
                                             {"action": "site_login", "target": "new"},
                                             {"action": "site_login", "target": "new"})
    sites.add_site = add_site
    try:
        r = dlg.handle("shop.example.com 로그인하자", site_login=True)
        assert r["action"] == {"type": "site_login", "site": "shop-example-com"} and added == ["shop.example.com"]
        assert r["replies"][0]["text"] == "로그인 화면 열어 볼게요!"
        assert "목록에 없는 사이트인데 사용자가 **이번 말에 주소" in sent(dlg.cloud)          # 주소가 있을 때만 그 길을 알려 준다
        r = dlg.handle("거기 로그인해줘", r["conversation_id"], site_login=True)             # 주소 없이 new → 묻는다(짐작하지 않는다)
        assert r["action"]["type"] == "ask" and "주소" in r["replies"][0]["text"] and added == ["shop.example.com"]
        r = dlg.handle("bad.example 로그인", r["conversation_id"], site_login=True)          # 받을 수 없는 주소
        assert r["action"]["type"] == "none" and "열 수 없어요" in r["replies"][0]["text"]
    finally:
        mgr.shutdown()
    assert said_host("a@b.example 로 보내줘") is None and said_host("버전 3.14") is None and said_host("https://Plan.Example.com/x 로그인") == "plan.example.com"


def test_an_in_between_screen_with_buttons_is_shown_and_only_the_chosen_button_is_pressed(w):
    """실기기: 카카오 계정을 넣은 뒤 "이 계정으로 계속하기" 화면(칸 없음, 단추 있음)을 폰 승인 대기로 잘못 보고 기다리기만 했다."""
    from agent.sites.login import WAIT_MORE
    w.update(providers=["kakao"], buttons=["계속하기", "다른 계정으로 로그인"])
    m = make(w)
    l = wait(m, m.start("shop", "password")["login_id"], ("need_choice",))
    m.input("shop", l["login_id"], l["step"], {"choice": "카카오로 로그인"})
    l = wait(m, l["login_id"], ("need_input",))
    m.input("shop", l["login_id"], l["step"], {"id": "x", "password": SECRET})
    l = wait(m, l["login_id"], ("need_choice",))
    assert l["choices"] == ["계속하기", "다른 계정으로 로그인", WAIT_MORE, STOP] and "카카오" in l["text"] and "본문 바로가기" not in l["text"]
    assert "pressed" not in w                                                        # 대신 누르지 않는다
    m.input("shop", l["login_id"], l["step"], {"choice": WAIT_MORE})                 # 기다리기: 누르지 않고 다시 본다
    time.sleep(0.05)
    l = wait(m, l["login_id"], ("need_choice",))
    assert "pressed" not in w
    m.input("shop", l["login_id"], l["step"], {"choice": "계속하기"})
    assert wait(m, l["login_id"], ("logged_in",))["state"] == "logged_in" and w["pressed"] == ["계속하기"]


def test_a_question_on_screen_gives_way_when_the_page_moves_on_by_itself(w):
    w.update(providers=["kakao"], buttons=["다시 요청"])
    m = make(w)
    l = wait(m, m.start("shop", "password")["login_id"], ("need_choice",))
    m.input("shop", l["login_id"], l["step"], {"choice": "카카오로 로그인"})
    l = wait(m, l["login_id"], ("need_input",))
    m.input("shop", l["login_id"], l["step"], {"id": "x", "password": SECRET})
    l = wait(m, l["login_id"], ("need_choice",))
    assert l["choices"][0] == "다시 요청"
    w["approved"] = True                                                             # 폰에서 승인하자 화면이 스스로 넘어갔다 → 고르지 않아도 이어 간다
    assert wait(m, l["login_id"], ("logged_in",))["state"] == "logged_in" and "pressed" not in w


def test_a_fill_that_fails_asks_again_instead_of_ending(w):
    """실기기: 값을 넣는 사이 화면의 칸이 바뀌어 넣기가 실패하자 로그인이 통째로 끝났다."""
    m = make(w)
    w["fill_fails"] = 1
    l = wait(m, m.start("shop", "password")["login_id"], ("need_input",))
    m.input("shop", l["login_id"], l["step"], {"id": "x", "password": SECRET})
    time.sleep(0.1)
    l = wait(m, l["login_id"], ("need_input",))                                      # 끝나지 않고 다시 묻는다(틀린 입력으로 세지 않는다)
    assert l.get("error") is None
    m.input("shop", l["login_id"], l["step"], {"id": "x", "password": SECRET})
    assert wait(m, l["login_id"], ("need_choice",))["choices"][0] == DONE


def test_an_unread_request_body_does_not_break_the_next_request_on_the_same_connection():
    """실기기: 본문을 읽지 않는 경로(취소)에 앱이 {}를 보내자, 같은 연결의 다음 요청이 "{}POST …"로 읽혀 501이 됐다."""
    import http.client
    import threading
    from agent.api.jobs import JobManager
    from agent.api.server import make_server
    mgr = JobManager(lambda job, hooks: None)
    srv = make_server(mgr, "t" * 32, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        h = {"X-App-Token": "t" * 32, "Content-Type": "application/json"}
        c.request("POST", "/v1/tasks/nope/cancel", body="{}", headers=h)      # 본문을 읽지 않고 답하는 경로
        r = c.getresponse(); r.read()
        first = r.status
        c.request("GET", "/v1/health", headers=h)                              # 같은 연결로 다음 요청
        r = c.getresponse(); r.read()
        assert first in (404, 409, 200) and r.status == 200
    finally:
        srv.shutdown()
        mgr.shutdown()


def test_same_fields_after_a_submit_are_not_called_wrong_unless_the_site_rejects():
    """실기기: 맞는 비밀번호인데 "틀렸다"고 떴다 — 제출 1초 뒤 칸이 그대로라는 것만 보고 틀렸다고 했다. 사이트가 확인하는 동안에도 칸은 그대로다."""
    from agent.sites.login import RETYPE, WAIT_MORE
    w = {"ops": [], "silent": True}                                                  # 제출해도 화면에 아무 반응이 없는 사이트
    m = make(w)
    l = wait(m, m.start("shop", "password")["login_id"], ("need_input",))
    m.input("shop", l["login_id"], l["step"], {"id": "x", "password": "아직 확인 중"})
    l = wait(m, l["login_id"], ("need_choice",))                                     # 틀렸다고 하지 않고, 화면의 글과 함께 묻는다
    assert l["choices"] == [RETYPE, WAIT_MORE, STOP] and "넘어가지 않았어요" in l["text"]
    m.input("shop", l["login_id"], l["step"], {"choice": WAIT_MORE})
    time.sleep(0.05)
    l = wait(m, l["login_id"], ("need_choice",))                                     # 더 지켜본 뒤에도 그대로면 다시 묻는다
    m.input("shop", l["login_id"], l["step"], {"choice": RETYPE})
    l = wait(m, l["login_id"], ("need_input",))
    assert l.get("error") is None                                                    # 틀린 횟수로 세지 않는다
    m.input("shop", l["login_id"], l["step"], {"id": "x", "password": SECRET})
    assert wait(m, l["login_id"], ("need_choice",))["choices"][0] == DONE


def test_a_hint_already_on_screen_before_submitting_is_not_read_as_a_rejection():
    """실기기: 칸 아래 도움말("TIP …")이 오류 안내처럼 잡혀, 맞는 계정을 넣었는데 거절됐다고 했다. 제출 뒤에 달라진 안내만 거절이다."""
    from agent.sites.login import RETYPE, WAIT_MORE
    w = {"ops": [], "silent": True, "hint": "TIP 메일 아이디만 입력해 보세요"}      # 도움말이 떠 있고, 제출해도 화면은 그대로다(확인 중)
    m = make(w)
    l = wait(m, m.start("shop", "password")["login_id"], ("need_input",))
    m.input("shop", l["login_id"], l["step"], {"id": "x", "password": "확인 중인 값"})
    l = wait(m, l["login_id"], ("need_choice", "need_input"))
    assert l["state"] == "need_choice" and l["choices"] == [RETYPE, WAIT_MORE, STOP]  # 틀렸다고 하지 않는다
    m.cancel("shop", l["login_id"])
    wait(m, l["login_id"], ("cancelled",))
    m._thread.join(2)
    w2 = {"ops": [], "hint": "TIP 메일 아이디만 입력해 보세요"}                        # 같은 도움말이 있어도, 사이트가 새 안내를 띄우면 거절이다
    m = make(w2)
    l = wait(m, m.start("shop", "password")["login_id"], ("need_input",))
    m.input("shop", l["login_id"], l["step"], {"id": "x", "password": "bad"})
    time.sleep(0.1)
    l = wait(m, l["login_id"], ("need_input",))
    assert l["error"] == "wrong_input" and "계정 정보가 맞지 않습니다" in l["message"] and "TIP" not in l["message"]


def test_login_that_does_not_carry_over_is_not_announced_as_logged_in(w, tmp_path):
    """로그인은 끝났는데 보관한 것만으로는 새 브라우저에서 로그인이 이어지지 않는다 → "로그인했어요"라고 하지 않고, 보관한 것도 사이트 등록도 남기지 않는다.

    실기기: 로그인 표식을 쿠키가 아닌 곳에 두는 서비스에서 로그인 직후 "풀렸어요"가 나왔고, 사용자는 까닭을 모른 채 다시 로그인했다.
    """
    w.update(first="front", providers=["kakao"], state_lost=True)
    w["store"] = MemoryBlobs()
    m = SiteLoginManager(FakeSandbox(w), w["store"], {}, poll_s=0.01, settle_s=0.15, sites_path=tmp_path / "sites_generic.json")
    key = m.add_site("shop.example")
    l = wait_k(m, key, m.start(key, "password")["login_id"], ("need_choice",))
    m.input(key, l["login_id"], l["step"], {"choice": "카카오로 로그인"})
    l = wait_k(m, key, l["login_id"], ("need_input",))
    m.input(key, l["login_id"], l["step"], {"id": "x", "password": SECRET})
    l = wait_k(m, key, l["login_id"], ("need_approval",))
    w["approved"] = True
    l = wait_k(m, key, l["login_id"], ("failed",))
    m._thread.join(2)
    assert l["reason"] == "unsupported_login" and "이어지지 않았어요" in l["message"]
    assert w["store"].get(m._key(m.sites[key])) is None                     # 쓸 수 없는 로그인 상태는 두지 않는다
    assert not (tmp_path / "sites_generic.json").exists()                   # 이어 쓸 수 없는 사이트는 등록하지 않는다
    assert [s["status"] for s in m.status() if s["site"] == key] == ["logged_out"]


def test_a_restored_page_that_looks_logged_out_is_read_again_before_the_state_is_thrown_away(w):
    """보관한 상태를 싣고 연 화면이 처음에는 로그인 입구를 보이다가(스크립트가 표식을 읽기 전) 곧 로그인된 화면이 된다 → 풀렸다고 하지 않는다."""
    m = make(w)

    class Slow:
        def __init__(self, late):
            self.reads, self.late = 0, late

        def request(self, op, args=None):
            if op == "form_fields":
                self.reads += 1
                return {"host": "shop.example", "fields": [], "url": "https://shop.example/my"}
            if op == "login_methods":
                return {"host": "shop.example", "providers": [], "entry": self.reads <= self.late, "entry_kind": "login"}
            raise AssertionError(op)

    site = m.sites["shop"]
    quick, slow, never = Slow(0), Slow(1), Slow(99)
    assert m._in_restored(quick, site) and quick.reads == 1
    assert m._in_restored(slow, site) and slow.reads == 2                   # 한 번 더 읽고 로그인된 것을 알았다
    assert not m._in_restored(never, site) and never.reads == 3             # 끝까지 로그아웃 화면이면 풀린 것


def test_a_site_that_blocked_this_browser_before_is_not_opened_again_and_says_why(w, tmp_path):
    """전에 접속을 막았던 사이트(적어 둔 곳)는 로그인하자고 해도 다시 열지 않는다 — "주소가 잘못됐다"가 아니라 그 까닭으로 구분한다."""
    from agent.sites import public
    old_path, old_set = public._refused_path, set(public._refused)
    try:
        public.use_refused(tmp_path / "sites_refused.json")
        public.note_refused("portal.example.go.kr")
        w["store"] = MemoryBlobs()
        m = SiteLoginManager(FakeSandbox(w), w["store"], {}, poll_s=0.01, sites_path=tmp_path / "sites_generic.json")
        with pytest.raises(LoginError) as e:
            m.add_site("portal.example.go.kr")
        assert e.value.code == "site_refused" and w["ops"] == []           # 브라우저를 열지도 않는다
        assert m.add_site("other.example.go.kr") == "other-example-go-kr"  # 공공 사이트라고 미리 막지 않는다
    finally:
        public._refused_path, public._refused = old_path, old_set


def test_a_site_on_the_dedicated_chrome_lane_keeps_nothing_in_the_store_and_tells_the_person_how_to_log_in(w):
    """전용 크롬 길(맥의 진짜 크롬 + 에이전트 전용 프로필)의 사이트: 로그인 상태를 꺼내 보관하지 않고(그 프로필에 있다), 앱의 카드로는 로그인하지 않는다."""
    from agent.sites.login import YOUTUBE
    w["store"] = MemoryBlobs()
    m = SiteLoginManager(FakeSandbox(w), w["store"], {"youtube": YOUTUBE}, poll_s=0.01)
    m.chrome_card_login = False                     # 앱의 카드로 받는 로그인을 끈 경우
    assert YOUTUBE.backend == "chrome" and YOUTUBE.hosts == ("youtube.com",)

    class B:
        def request(self, op, args=None):
            raise AssertionError(f"전용 크롬 길에서는 로그인 상태를 옮기지 않는다: {op}")
    assert m.restore(B(), YOUTUBE) is True          # 옮길 것이 없다(프로필에 있다)
    m._keep(B(), YOUTUBE)                           # 꺼내지 않는다
    assert w["store"].get(m._key(YOUTUBE)) is None
    l = m.start("youtube", "password")
    assert l["state"] == "failed" and l["reason"] == "unsupported_login" and "맥 화면 앞에서" in l["message"] and w["ops"] == []
    assert "accounts.google" not in l["message"]    # 고정 문장(화면의 글이 아니다)


def test_confirm_card_does_not_dump_the_whole_page():
    from agent.sites.login import confirm_text
    long = "전체메뉴 바로가기 본문 바로가기 " * 30
    out = confirm_text("www.example.test", long)
    assert len(out) < 140 and out.endswith("로그인이 끝났나요?") and "…" in out
    assert confirm_text("www.example.test", long + " 마이포털 로그아웃") == "www.example.test 화면에 '로그아웃'이 보여요.\n로그인이 끝났나요?"
    assert confirm_text("www.example.test", "") == "www.example.test 화면으로 넘어갔어요.\n로그인이 끝났나요?"


def test_spoken_site_names_are_cleaned_unique_and_saved(w, tmp_path):
    """사용자가 사이트를 부른 이름(D78): 낱말만 받는다, 한 이름이 두 사이트를 가리키지 않는다, 정해 둔 사이트에는 붙지 않는다."""
    import json
    from agent.sites.login import clean_alias, load_generic_sites
    assert clean_alias(" 브이월드 ") == "브이월드" and clean_alias("V World 3") == "V World 3"
    for bad in ("vworld.kr", "https://x", "사이트", "거기", "가", "", None, "이름이 너무너무너무너무너무 길어서 안 되는 것", "a/b", "<b>"):
        assert clean_alias(bad) is None, bad
    f = tmp_path / "sites_generic.json"
    f.write_text(json.dumps([{"key": "shop-example", "name": "shop.example", "login_url": "https://shop.example/", "check_url": "https://shop.example/home",
                              "aliases": ["예시몰", "bad.alias", 3]},
                             {"key": "map-example", "name": "map.example", "login_url": "https://map.example/", "check_url": ""}]))
    w["store"] = MemoryBlobs()
    m = SiteLoginManager(FakeSandbox(w), w["store"], load_generic_sites(f), poll_s=0.01, sites_path=f)
    assert m.sites["shop-example"].aliases == ("예시몰",)                              # 파일의 이상한 값은 버린다
    assert not m.add_alias("map-example", "예시몰")                                    # 다른 사이트가 쓰는 이름
    assert not m.add_alias("map-example", "map.example") and not m.add_alias("nope", "지도")
    assert m.add_alias("map-example", "예시지도") and not m.add_alias("map-example", "예시지도")
    assert load_generic_sites(f)["map-example"].aliases == ("예시지도",)               # 이미 파일에 있는 사이트는 바로 적힌다
    assert [s.get("aliases") for s in m.status() if s["site"] == "map-example"] == [["예시지도"]]


def test_a_notice_that_the_request_failed_is_not_a_wrong_password_and_ends_without_a_second_try(w):
    """실기기 2026-11-16: 사이트가 "데이터 요청에 실패"라고 한 것을 틀린 입력으로 보고 다시 받았고, 두 번째 제출 뒤 접속이 막혔다."""
    w["reject_text"] = "데이터 요청에 실패하였습니다. 잠시 후 다시 시도해 주세요."
    m = make(w)
    l = wait(m, m.start("shop", "password")["login_id"], ("need_input",))
    m.input("shop", l["login_id"], l["step"], {"id": "x", "password": "bad"})
    l = wait(m, l["login_id"], ("failed",))
    assert l["reason"] == "blocked" and "요청을 받지 않았어요" in l["message"] and "데이터 요청에 실패" in l["message"]
    assert sum(1 for op in w["ops"] if op.startswith("form_fill")) == 1          # 다시 넣지 않았다


def test_relogin_only_presses_the_social_button_the_person_chose_and_gives_up_if_asked_for_anything(tmp_path):
    """D90(사용자 결정 2026-11-16): 풀린 로그인을, 처음 고른 소셜 로그인으로 스스로 다시. 단추만 누른다 — 그 회사가 무엇을 더 물으면 그만둔다."""
    import dataclasses
    from agent.sites.login import NAVER, generic_site

    class B:
        def __init__(self, script):
            self.script, self.ops, self.page = script, [], "front"

        def request(self, op, args=None):
            self.ops.append((op, args or {}))
            if op == "navigate":
                self.page = "front"
            if op == "login_open":
                self.page = "login" if (args or {}).get("entry") else self.script["after_provider"]
            if op == "form_fields":
                if self.page == "passing":          # 그 회사 화면을 스쳐 지나 사이트로 돌아온다
                    self.page = "home"
                    return {"host": "nid.naver.com", "fields": []}
                host = "nid.naver.com" if self.page in ("naver_form", "naver_consent") else "shop.example"
                return {"host": host, "fields": [{"name": "id"}, {"name": "password"}] if self.page == "naver_form" else [], "url": f"https://{host}/{self.page}"}
            if op == "login_buttons":
                return {"buttons": ["동의하고 계속하기"] if self.page == "naver_consent" else []}
            if op == "login_methods":
                return {"providers": self.script.get("providers", ["naver"]) if self.page == "login" else [], "entry": self.page == "front"}
            return {}

    def manager(**kw):
        site = dataclasses.replace(generic_site("shop", "합성 상점", "https://shop.example/", "https://shop.example/my"), via=kw.pop("via", "naver"))
        m = SiteLoginManager(FakeSandbox({"ops": []}), MemoryBlobs(), {"naver": NAVER, "shop": site}, poll_s=0.01, sites_path=tmp_path / "s.json")
        m.auto_relogin = kw.pop("on", True)
        kept = []
        m.restore = lambda b, s: kw.get("provider_state", True)
        m._landed = lambda b, s: "https://shop.example/my" if b.page == "home" else None
        m._keep = lambda b, s: kept.append(s.key)
        return m, site, kept

    m, site, kept = manager()
    b = B({"after_provider": "passing"})
    assert m.relogin(b, site) is True and kept == ["shop"]
    assert [x["status"] for x in m.status() if x["site"] == "shop"] == ["logged_in"]
    opened = [a for op, a in b.ops if op == "login_open"]
    assert opened == [{"entry": True}, {"provider": "naver"}]                          # 입구 → 네이버 단추. 그 밖의 것은 누르지 않았다
    assert not any(op in ("form_fill", "form_submit", "login_press") for op, _ in b.ops)
    for script, why in (({"after_provider": "naver_form"}, "계정을 묻는다"), ({"after_provider": "naver_consent"}, "동의 단추가 떴다"),
                        ({"after_provider": "passing", "providers": ["kakao"]}, "그 방법의 단추가 없다")):
        m, site, kept = manager()
        b = B(script)
        assert m.relogin(b, site) is False and kept == [], why
        assert not any(op in ("form_fill", "form_submit", "login_press") for op, _ in b.ops)
    for kw in ({"on": False}, {"via": ""}, {"via": "kakao"}, {"provider_state": False}):   # 꺼져 있다 / 고른 소셜 로그인이 없다 / 그 회사의 로그인 상태가 맥에 없다
        m, site, kept = manager(**kw)
        assert m.relogin(B({"after_provider": "passing"}), site) is False and kept == []


def test_the_way_the_person_logged_in_is_written_down_with_the_site(tmp_path):
    from agent.sites.login import generic_site, load_generic_sites
    import dataclasses
    m = SiteLoginManager(FakeSandbox({"ops": []}), MemoryBlobs(), {}, poll_s=0.01, sites_path=tmp_path / "s.json")
    m._remember(dataclasses.replace(generic_site("shop", "합성 상점", "https://shop.example/", "https://shop.example/my"), via="naver"))
    m._remember(generic_site("other", "다른 곳", "https://other.example/", ""))
    got = load_generic_sites(tmp_path / "s.json")
    assert got["shop"].via == "naver" and got["other"].via == "" and "via" not in json.loads((tmp_path / "s.json").read_text())[1]
    assert generic_site("x", "x", "https://x.example/", "", via="지어낸 것").via == ""
