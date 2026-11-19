"""사이트 로그인 관리 (D52). 가짜 브라우저(합성 로그인 화면)로 상태 흐름을 검사한다. 실제 사이트·실제 계정은 쓰지 않는다."""

import contextlib
import time

import pytest

from agent.sites.login import LoginError, Site, SiteLoginManager
from agent.sites.store import MemoryBlobs

SITE = Site("naver", "네이버", {"qr": "https://login.test/login?mode=qrcode", "password": "https://login.test/login?mode=form"},
            "https://www.login.test/mail", ("login.test",), ("T_AUT", "T_SES"), "https://login.test/")
USER, PW = "test-user", "test-pass-1234"


class FakeSiteBrowser:
    """합성 로그인 사이트를 흉내 낸다. mode: plain | twofactor | captcha | device | blocked | weird"""

    def __init__(self, world):
        self.w = world
        self.page = "blank"
        self.typed: dict[str, str] = {}
        self.url = "about:blank"

    def _render(self) -> tuple[str, str]:
        w, pg = self.w, self.page
        if pg == "qr":
            if w.get("qr_expired"):
                return '[e1] img "QR Code"', "QR Code\n시간이 초과되었습니다. 다시 시도해 주세요"
            return ('[e1] heading "네이버"\n[e2] img "QR Code"\n[e3] link "아이디·비밀번호 로그인"',
                    f"QR 코드를 촬영하고 로그인 하세요\nQR Code\n{w.get('number', 70)}\n남은 시간\n02:57\n앱에서 위 숫자를 선택하면 로그인 완료")
        if pg in ("form", "wrong"):
            err = "아이디 또는 비밀번호가 잘못 되었습니다.\n" if pg == "wrong" else ""
            return ('[e4] textbox "아이디 또는 전화번호"\n[e5] textbox "비밀번호"\n[e6] checkbox "로그인 상태 유지"\n[e7] button "로그인"\n'
                    '[e8] link "QR 코드 로그인"', err + "아이디 또는 전화번호\n비밀번호\n로그인 상태 유지\n로그인")
        if pg == "captcha":
            return ('[e1] img "자동입력 방지 문자 그림"\n[e2] textbox "비밀번호"\n[e3] textbox "자동입력 방지 문자"\n[e4] button "로그인"',
                    "자동입력 방지를 위해 아래 문자를 입력해 주세요")
        if pg == "extra":   # 실제 네이버의 '추가 확인' 화면 뼈대(2026-11-12 실기기 기록)
            return ('[e1] heading "네이버"\n[e2] heading "보안을 위해 추가 확인을 해주세요"\n[e3] img "영수증, 가상 슈퍼마켓"\n'
                    '[e4] textbox "정답을 입력해 주세요"\n[e5] button "새로고침"\n[e6] button "음성듣기"\n[e7] button "확인"',
                    "본문 바로가기\n네이버\n보안을 위해 추가 확인을 해주세요\n해당 영수증은 가상으로 제작된 것으로, 실제 영수증 사진이 아니에요.\n"
                    "가장 가격이 싼 물건의 이름은 무엇입니까?\n새로고침\n음성듣기\n확인\n스마트봇 상담\n고객센터\n© NAVER Corp.")
        if pg == "twofa":
            return '[e1] heading "2단계 인증"', "2단계 인증\n휴대폰의 네이버 앱 알림을 확인하고 승인해 주세요"
        if pg == "device":
            return ('[e1] button "등록"\n[e2] button "등록안함"', "새로운 기기에서 로그인했습니다. 이 기기를 등록하시겠습니까?")
        if pg == "blocked":
            return "", "비정상적인 접근이 감지되어 보호조치 되었습니다"
        if pg == "weird":
            return '[e1] textbox "주민등록번호"\n[e2] button "확인"', "본인 확인이 필요합니다"
        if pg == "home":
            return '[e1] link "메일"', "test-user 님"
        if pg == "mail":
            return '[e1] link "받은메일함"', "받은메일함"
        return "", ""

    def request(self, op, args=None):
        args, w = args or {}, self.w
        w["ops"].append(op)
        if w.get("crash"):
            raise RuntimeError("브라우저가 죽음 secret-page-text")
        if op == "navigate":
            self.url = args["url"]
            if "mode=qrcode" in self.url:
                self.page = "qr"
            elif "mode=form" in self.url:
                self.page = "form"
            elif self.url.endswith("/mail"):
                self.page = "mail" if w.get("cookies") else "form"
                if not w.get("cookies"):
                    self.url = "https://login.test/login?mode=form"
            return {"url": self.url, "title": "t"}
        if self.page in ("qr", "twofa") and w.get("approved"):
            self._login()
        if op == "observe":
            body, _ = self._render()
            return {"url": self.url, "title": "t", "text": f"URL: {self.url}\n제목: t\n{body}"}
        if op == "extract_text":
            return {"text": self._render()[1], "truncated": False}
        if op == "cookie_names":
            return {"names": sorted(w.get("cookies", set()))}
        if op == "cookies_export":
            return {"cookies": [{"name": n, "value": "secret-cookie-value", "domain": ".login.test", "path": "/"}
                                for n in sorted(w.get("cookies", set()))]}
        if op == "cookies_import":
            w["cookies"] = {c["name"] for c in args["cookies"]} if not w.get("server_expired") else set()
            return {"count": len(args["cookies"])}
        if op == "element_screenshot":
            return {"png_b64": "UE5H"}
        if op == "secret_type":
            names = {"e4": "id", "e5": "password", "e2": "password", "e3": "captcha"} if self.page != "captcha" else \
                {"e2": "password", "e3": "captcha"}
            if self.page == "extra":
                names = {"e4": "captcha"}
            self.typed[names[args["ref"]]] = args["text"]
            w["typed_log"].append(dict(self.typed))
            return {}
        if op == "click":
            ref = args["ref"]
            if self.page in ("form", "wrong") and ref == "e6":
                w["keep"] = w.get("keep", 0) + 1
            elif self.page in ("form", "wrong") and ref == "e7":
                w["submits"] = w.get("submits", 0) + 1
                ok = self.typed.get("id") == USER and self.typed.get("password") == PW
                self.typed = {}
                if not ok:
                    self.page = "wrong"
                elif w["mode"] == "plain":
                    self._login()
                else:
                    self.page = {"twofactor": "twofa", "captcha": "captcha", "device": "device", "blocked": "blocked",
                                 "weird": "weird", "extra": "extra"}[w["mode"]]
            elif self.page == "captcha" and ref == "e4":
                ok = self.typed.get("password") == PW and self.typed.get("captcha") == "AB12"
                self.typed = {}
                self.page = "captcha" if not ok else "home"
                if ok:
                    self._login()
            elif self.page == "extra" and ref == "e7":
                ok = self.typed.get("captcha") == "1234"
                self.typed = {}
                self.page = "twofa" if ok else "extra"
            elif self.page == "device":
                w["choice"] = "등록" if ref == "e1" else "등록안함"
                self._login()
            return {}
        if op == "site_icon":
            return w.get("icon") or {"type": None}
        raise AssertionError(op)

    def _login(self):
        self.w["cookies"] = {"T_AUT", "T_SES", "other"}
        self.page, self.url = "home", "https://login.test/"


class FakeSandbox:
    def __init__(self, world):
        self.w = world

    @contextlib.contextmanager
    def session(self, user_id):
        self.w["users"].append(user_id)
        self.w["open"] = self.w.get("open", 0) + 1
        self.w["cookies"] = set()          # 일회용 프로필: 열 때마다 빈 브라우저
        try:
            yield FakeSiteBrowser(self.w)
        finally:
            self.w["open"] -= 1
            self.w["closed"] = self.w.get("closed", 0) + 1

    def delete_user_data(self, user_id):
        self.w["deleted"] = user_id
        return True


@pytest.fixture
def world():
    return {"mode": "plain", "ops": [], "typed_log": [], "users": []}


def make(world, tmp_path=None, **kw):
    world.setdefault("store", MemoryBlobs())
    return SiteLoginManager(FakeSandbox(world), world["store"], {"naver": SITE}, poll_s=0.02,
                            state_path=(tmp_path / "sites.json") if tmp_path else None, **kw)


def wait(m, lid, want, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        l = m.get("naver", lid)
        if l["state"] in want:
            return l
        time.sleep(0.01)
    raise AssertionError(f"{want} 안 됨: {l}")


def test_qr_login_shows_code_and_number_then_logs_in_when_approved_on_phone(world, tmp_path):
    m = make(world, tmp_path)
    assert m.status() == [{"site": "naver", "name": "네이버", "methods": ["qr", "password"], "host": "www.login.test", "status": "logged_out", "checked_at": None}]
    lid = m.start("naver", "qr")["login_id"]
    l = wait(m, lid, {"need_approval"})
    assert l["qr"] == {"mime": "image/png", "b64": "UE5H"} and l["number"] == "70" and l["step"] == 1
    assert 0 < l["expires_in_s"] <= 177 and "70" in l["message"]
    time.sleep(0.1)
    assert m.get("naver", lid)["step"] == 1                      # 같은 화면을 지켜보는 동안 step은 그대로
    world["approved"] = True                                     # 사용자가 폰에서 승인
    l = wait(m, lid, {"logged_in"})
    assert "qr" not in l and world["closed"] == 1
    kept = world["store"].get("app.naver")                       # 로그인 상태는 암호화 저장소에만 남는다
    assert kept and "T_AUT" in kept and "secret-cookie-value" not in str(l)
    assert m.status()[0]["status"] == "logged_in" and world["users"] == ["app:site:naver"]
    assert make(world, tmp_path).status()[0]["status"] == "logged_in"   # 서버를 다시 띄워도 상태를 안다
    assert world["typed_log"] == []                               # 아이디·비밀번호는 지나가지 않았다


def test_password_login_with_two_factor_approval_keeps_nothing(world):
    world["mode"] = "twofactor"
    m = make(world)
    lid = m.start("naver", "password")["login_id"]
    l = wait(m, lid, {"need_input"})
    assert l["fields"] == [{"name": "id", "label": "아이디 또는 전화번호", "secret": False},
                           {"name": "password", "label": "비밀번호", "secret": True}]
    out = m.input("naver", lid, l["step"], {"id": USER, "password": PW})
    assert out["state"] == "working" and PW not in str(out) and USER not in str(out)
    l2 = wait(m, lid, {"need_approval"})
    assert l2["step"] == l["step"] + 1 and "qr" not in l2 and "승인" in l2["message"]
    world["approved"] = True
    done = wait(m, lid, {"logged_in"})
    assert world["keep"] == 1 and world["submits"] == 1          # '로그인 상태 유지'를 켜고 한 번만 제출
    assert PW not in str(done) and PW not in str(vars(m._logins[lid]))   # 값은 남지 않는다


def test_wrong_password_returns_to_input_then_fails_after_two(world):
    m = make(world)
    lid = m.start("naver", "password")["login_id"]
    l = wait(m, lid, {"need_input"})
    m.input("naver", lid, l["step"], {"id": USER, "password": "wrong-pass"})
    l2 = wait(m, lid, {"need_input"})
    assert l2["error"] == "wrong_input" and l2["step"] > l["step"]
    with pytest.raises(LoginError) as e:                         # 지난 단계의 입력은 받지 않는다(두 번 제출 방지)
        m.input("naver", lid, l["step"], {"id": USER, "password": PW})
    assert e.value.code == "stale_step"
    m.input("naver", lid, l2["step"], {"id": USER, "password": "wrong-again"})
    l3 = wait(m, lid, {"need_input"})
    m.input("naver", lid, l3["step"], {"id": USER, "password": "wrong-third"})
    done = wait(m, lid, {"failed"})
    assert done["reason"] == "wrong_input" and world["submits"] == 3 and not world.get("cookies")


def test_same_step_is_submitted_once_and_inputs_are_validated(world):
    m = make(world)
    lid = m.start("naver", "password")["login_id"]
    l = wait(m, lid, {"need_input"})
    for bad in ({"id": USER}, {"id": USER, "password": PW, "extra": "x"}, {"id": "", "password": PW}, {"id": USER, "password": 1}):
        with pytest.raises(LoginError) as e:
            m.input("naver", lid, l["step"], bad)
        assert e.value.code == "invalid_input"
    m.input("naver", lid, l["step"], {"id": USER, "password": PW})
    with pytest.raises(LoginError):
        m.input("naver", lid, l["step"], {"id": USER, "password": PW})
    wait(m, lid, {"logged_in"})
    assert world["submits"] == 1
    with pytest.raises(LoginError) as e:
        m.get("naver", "l_nope")
    assert e.value.code == "unknown_login"
    with pytest.raises(LoginError) as e:
        m.start("daum")
    assert e.value.code == "unknown_site"


def test_captcha_is_relayed_to_the_user_not_solved(world):
    world["mode"] = "captcha"
    m = make(world)
    lid = m.start("naver", "password")["login_id"]
    l = wait(m, lid, {"need_input"})
    m.input("naver", lid, l["step"], {"id": USER, "password": PW})
    c = wait(m, lid, {"need_input"})
    assert [f["name"] for f in c["fields"]] == ["password", "captcha"] and c["image"]["b64"] == "UE5H"
    m.input("naver", lid, c["step"], {"password": PW, "captcha": "AB12"})
    wait(m, lid, {"logged_in"})


def test_real_extra_check_page_is_relayed_with_its_question_then_two_factor(world):
    # 실기기 첫 시도에서 unsupported_page로 멈춘 화면: 그림 + 물음 + "정답을 입력해 주세요"
    world["mode"] = "extra"
    m = make(world)
    lid = m.start("naver", "password")["login_id"]
    l = wait(m, lid, {"need_input"})
    m.input("naver", lid, l["step"], {"id": USER, "password": PW})
    c = wait(m, lid, {"need_input"})
    assert c["fields"] == [{"name": "captcha", "label": "정답을 입력해 주세요", "secret": False}] and c["image"]["b64"] == "UE5H"
    # 실기기에서 물음이 다른 줄에 묻혀 보이지 않았다 → 물음만 뽑아 준다
    assert c["message"] == "그림을 보고 답해 주세요: 가장 가격이 싼 물건의 이름은 무엇입니까?" and "error" not in c
    m.input("naver", lid, c["step"], {"captcha": "0000"})
    c2 = wait(m, lid, {"need_input"})
    assert c2["error"] == "wrong_input" and c2["step"] > c["step"] and "가장 가격이 싼 물건" in c2["message"]
    m.input("naver", lid, c2["step"], {"captcha": "1234"})
    wait(m, lid, {"need_approval"})
    world["approved"] = True
    wait(m, lid, {"logged_in"})


def test_device_prompt_is_asked_as_a_choice(world):
    world["mode"] = "device"
    m = make(world)
    lid = m.start("naver", "password")["login_id"]
    l = wait(m, lid, {"need_input"})
    m.input("naver", lid, l["step"], {"id": USER, "password": PW})
    c = wait(m, lid, {"need_choice"})
    assert c["choices"] == ["등록", "등록안함"] and "새로운 기기" in c["text"]
    with pytest.raises(LoginError):
        m.input("naver", lid, c["step"], {"choice": "아무거나"})
    m.input("naver", lid, c["step"], {"choice": "등록안함"})
    wait(m, lid, {"logged_in"})
    assert world["choice"] == "등록안함"


def test_blocked_unknown_page_expiry_cancel_and_crash_end_cleanly(world):
    for mode, reason in (("blocked", "blocked"), ("weird", "unsupported_page")):
        world.update(mode=mode, cookies=set())
        m = make(world)
        lid = m.start("naver", "password")["login_id"]
        l = wait(m, lid, {"need_input"})
        m.input("naver", lid, l["step"], {"id": USER, "password": PW})
        assert wait(m, lid, {"failed"})["reason"] == reason
    world["mode"] = "plain"
    m = make(world)
    lid = m.start("naver", "qr")["login_id"]
    wait(m, lid, {"need_approval"})
    world["qr_expired"] = True
    assert wait(m, lid, {"expired"})["expires_in_s"] == 0
    world["qr_expired"] = False
    lid = m.start("naver", "qr")["login_id"]
    wait(m, lid, {"need_approval"})
    assert m.cancel("naver", lid)["state"] == "cancelled" and world["open"] == 0
    world["crash"] = True
    lid = m.start("naver", "qr")["login_id"]
    done = wait(m, lid, {"failed"})
    assert done["reason"] == "browser_error" and "secret-page-text" not in str(done)


def test_new_login_cancels_previous_and_starts_are_rate_limited(world):
    m = make(world)
    first = m.start("naver", "qr")["login_id"]
    wait(m, first, {"need_approval"})
    second = m.start("naver", "qr")["login_id"]
    assert m.get("naver", first)["state"] == "cancelled"
    wait(m, second, {"need_approval"})
    from agent.sites.login import START_LIMIT
    for _ in range(START_LIMIT - 2):
        m.start("naver", "qr")
    with pytest.raises(LoginError) as e:
        m.start("naver", "qr")
    assert e.value.code == "rate_limited" and world["open"] <= 1


def test_check_restores_saved_login_and_logout_removes_it(world, tmp_path):
    m = make(world, tmp_path)
    assert m.check("naver")[0]["status"] == "logged_out" and world["users"] == []   # 보관한 것이 없으면 브라우저도 열지 않는다
    lid = m.start("naver", "qr")["login_id"]
    wait(m, lid, {"need_approval"})
    world["approved"] = True
    wait(m, lid, {"logged_in"})
    world["approved"] = False
    assert m.check("naver")[0]["status"] == "logged_in"          # 빈 브라우저에 보관한 상태를 되살려 확인
    assert "cookies_import" in world["ops"] and world["ops"].index("cookies_import") < len(world["ops"]) - 2
    world["server_expired"] = True                                # 사이트 쪽에서 로그인이 만료됨
    assert m.check("naver")[0]["status"] == "logged_out" and world["store"].get("app.naver") is None
    world["server_expired"] = False
    world["store"].set("app.naver", '[{"name": "T_AUT"}, {"name": "T_SES"}]')
    assert m.logout("naver")[0]["status"] == "logged_out" and world["store"].get("app.naver") is None
    assert m.check("naver")[0]["status"] == "logged_out"


# ---------------------------------------------------------------- API

import threading  # noqa: E402

import httpx  # noqa: E402

from agent.api.dialog import DialogManager  # noqa: E402
from agent.api.jobs import JobManager  # noqa: E402
from agent.api.server import make_server  # noqa: E402

TOKEN = "t" * 32
H = {"X-App-Token": TOKEN}


@pytest.fixture
def sapi(world):
    sites = make(world)
    mgr = JobManager(lambda job, hooks: None)

    class NoModel:
        name = "none"

        def generate(self, *a, **k):
            raise AssertionError("모델을 부르면 안 된다")
    http = make_server(mgr, TOKEN, port=0, dialog=DialogManager(NoModel(), mgr), sites=sites)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{http.server_address[1]}"
    mgr.shutdown()
    http.shutdown()


def poll(base, lid, want, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        l = httpx.get(f"{base}/v1/sites/naver/login/{lid}", headers=H).json()["login"]
        if l["state"] in want:
            return l
        time.sleep(0.02)
    raise AssertionError(l)


def test_api_site_login_password_flow_never_echoes_input(sapi, world):
    base = sapi
    world["mode"] = "twofactor"
    assert "site_login" in httpx.get(f"{base}/v1/health", headers=H).json()["features"]
    assert httpx.get(f"{base}/v1/sites").status_code == 401
    assert httpx.get(f"{base}/v1/sites", headers=H).json()["sites"][0]["methods"] == ["qr", "password"]
    r = httpx.post(f"{base}/v1/sites/naver/login", headers=H, json={"method": "password"})
    lid = r.json()["login"]["login_id"]
    l = poll(base, lid, {"need_input"})
    url = f"{base}/v1/sites/naver/login/{lid}/input"
    assert httpx.post(url, headers=H, json={"step": l["step"] + 5, "values": {"id": USER, "password": PW}}).json()["error"]["code"] == "stale_step"
    bad = httpx.post(url, headers=H, json={"step": l["step"], "values": {"id": USER}})
    assert bad.status_code == 400 and USER not in bad.text
    r = httpx.post(url, headers=H, json={"step": l["step"], "values": {"id": USER, "password": PW}})
    assert r.status_code == 200 and PW not in r.text and USER not in r.text
    assert httpx.post(url, headers=H, json={"step": l["step"], "values": {"id": USER, "password": PW}}).status_code == 409
    assert poll(base, lid, {"need_approval"})["message"]
    world["approved"] = True
    assert PW not in str(poll(base, lid, {"logged_in"}))
    assert httpx.get(f"{base}/v1/sites", headers=H).json()["sites"][0]["status"] == "logged_in"
    assert httpx.post(f"{base}/v1/sites/naver/check", headers=H).json()["sites"][0]["status"] == "logged_in"
    assert httpx.post(f"{base}/v1/sites/naver/logout", headers=H).json()["sites"][0]["status"] == "logged_out"
    assert httpx.get(f"{base}/v1/sites/naver/login/l_unknown1", headers=H).json()["error"]["code"] == "unknown_login"
    assert httpx.post(f"{base}/v1/sites/daum/login", headers=H, json={}).json()["error"]["code"] == "unknown_site"
    assert httpx.post(f"{base}/v1/sites/naver/login", headers=H, json={"method": "sms"}).status_code == 400


def test_api_qr_cancel_and_chat_trigger_only_for_apps_that_can_draw_it(sapi, world):
    base = sapi
    lid = httpx.post(f"{base}/v1/sites/naver/login", headers=H, json={}).json()["login"]["login_id"]
    l = poll(base, lid, {"need_approval"})
    assert l["method"] == "qr" and l["qr"]["mime"] == "image/png" and l["number"] == "70"
    assert httpx.post(f"{base}/v1/sites/naver/login/{lid}/cancel", headers=H).json()["login"]["state"] == "cancelled"
    # 대화창: 4 이상을 보낸 앱에만 로그인 화면을 열라는 action이 간다(모델을 부르지 않는다)
    r = httpx.post(f"{base}/v1/messages", headers={**H, "X-Api-Version": "4"}, json={"text": "네이버 로그인해줘"}).json()
    assert r["action"] == {"type": "site_login", "site": "naver"} and r["replies"] == [{"text": "네이버 로그인 화면을 열게요.", "by": "mac"}]
    assert httpx.post(f"{base}/v1/messages", headers={**H, "X-Api-Version": "3"}, json={"text": "네이버 로그인해줘"}).status_code == 500


def test_captcha_question_extraction_falls_back_when_no_question_mark():
    from agent.sites.login import captcha_question
    assert captcha_question("본문 바로가기\n네이버\n보안을 위해 추가 확인을 해주세요\n아래 그림의 문자를 입력해 주세요\n새로고침\n확인\n고객센터", "네이버") \
        == "보안을 위해 추가 확인을 해주세요 아래 그림의 문자를 입력해 주세요"
    assert captcha_question("영수증의 가게 전화번호 뒤 4자리는 무엇입니까? [?]에 들어갈 숫자를 입력해 주세요.\n© NAVER Corp.", "네이버") \
        == "영수증의 가게 전화번호 뒤 4자리는 무엇입니까? [?]에 들어갈 숫자를 입력해 주세요."


def _setup_world(imap_on=True):
    w = {"page": "blank", "clicks": [], "typed": [], "imap_on": imap_on, "created": False, "dialog": [], "connected": []}

    def pages():
        radio = lambda on: '[e37] radio "사용함"' + (' = "선택됨"' if on else "") + '\n[e38] radio "사용 안 함"' + ("" if on else ' = "선택됨"')
        return {
            "mail": ('[e33] link "POP3/SMTP 설정 선택됨"\n[e34] link "IMAP/SMTP 설정"', "설정"),
            "imap": (radio(w["imap_on"]) + '\n[e49] button "취소"\n[e50] button "저장"', "IMAP/SMTP 사용"),
            "security": ('text "비밀번호"\n[e1] button "관리"\ntext "2단계 인증"\n[e2] button "관리"', "기본보안설정"),
            "recheck": ('[e1] heading "비밀번호 재확인"\n[e2] textbox "비밀번호"\n[e3] button "확인"', "비밀번호 재확인"),
            "manage": ('[e1] heading "2단계 인증 관리"\n[e2] radio "아웃룩"\n[e3] radio "직접 입력"\n[e4] textbox ""\n[e5] button "생성하기"\n'
                       '[e6] textbox ""' + (' = "ABCD1234EFGH"' if w["created"] else "") + '\n[e7] link "삭제"', "2단계 인증 관리"),
        }

    class B:
        def request(self, op, args=None):
            args = args or {}
            pg = w["page"]
            if op == "cookies_import":
                return {"count": 2}
            if op == "navigate":
                w["page"] = "mail" if "mail.naver.com" in args["url"] else "security"
            elif op == "dialog_policy":
                w["dialog"].append(args.get("accept"))
            elif op == "secret_type":
                w["typed"].append((pg, args["ref"], args["text"]))
            elif op == "field_secret":
                return {"value": {"e4": "AIagent", "e6": "ABCD1234EFGH" if w["created"] else ""}.get(args["ref"], "")}
            elif op == "click":
                w["clicks"].append((pg, args["ref"]))
                if pg == "mail" and args["ref"] == "e34":
                    w["page"] = "imap"
                elif pg == "imap" and args["ref"] == "e37":
                    w["pending_on"] = True
                elif pg == "imap" and args["ref"] == "e50":
                    w["imap_on"] = w.pop("pending_on", w["imap_on"])
                elif pg == "security" and args["ref"] == "e2":
                    w["page"] = "recheck"
                elif pg == "recheck":
                    w["page"] = "manage" if w["typed"][-1][2] == PW else "recheck"
                elif pg == "manage" and args["ref"] == "e5":
                    w["created"] = True
            elif op == "observe":
                return {"url": "https://nid.naver.com/x", "text": "URL: https://nid.naver.com/x\n제목: t\n" + pages()[pg][0]}
            elif op == "extract_text":
                return {"text": pages()[pg][1] + ("\n다시 한 번 입력해주세요 재확인" if pg == "recheck" else "")}
            elif op == "cookie_names":
                return {"names": ["NID_AUT", "NID_SES"]}
            return {}

    class SB:
        @contextlib.contextmanager
        def session(self, uid):
            yield B()

        def delete_user_data(self, uid):
            return True

    class FakeMailer:
        def accounts(self):
            return [{"provider": "naver", "address": a, "status": "ready"} for a, _ in w["connected"]]

        def connect(self, address, password, make_default=False):
            w["connected"].append((address, password))
            return [{"provider": "naver", "address": address, "status": "ready", "default": True}]
    return w, SB(), FakeMailer()


def test_mail_setup_creates_app_password_only_after_approval_and_stores_it(world):
    """네이버 메일 연결(D54): 주소 → IMAP 확인 → 비밀번호 재확인(사용자) → 승인 → 생성 → 메일 계정으로 연결."""
    from agent.sites.login import NAVER_MAIL
    w, sb, mailer = _setup_world(imap_on=True)
    store = MemoryBlobs()
    m = SiteLoginManager(sb, store, {"naver": SITE, "naver_mail": NAVER_MAIL}, poll_s=0.02, mailer=mailer)
    lid = m.start("naver_mail", "password")["login_id"]
    assert wait2(m, lid, {"failed"})["reason"] == "not_logged_in"            # 네이버 로그인이 먼저다
    store.set("app.naver", '[{"name": "NID_AUT"}]')
    lid = m.start("naver_mail", "password")["login_id"]
    l = wait2(m, lid, {"need_input"})
    assert [f["name"] for f in l["fields"]] == ["id"]
    m.input("naver_mail", lid, l["step"], {"id": "Test-User"})                # 아이디만 넣어도 주소로 본다
    l = wait2(m, lid, {"need_input"}, since=l["step"])
    assert [f["name"] for f in l["fields"]] == ["password"] and "다시 물어요" in l["message"]
    assert ("imap", "e50") not in w["clicks"]                                 # 이미 켜져 있으면 설정을 건드리지 않는다
    assert ("security", "e2") in w["clicks"] and ("security", "e1") not in w["clicks"]   # '2단계 인증' 옆의 관리만
    m.input("naver_mail", lid, l["step"], {"password": PW})
    c = wait2(m, lid, {"need_choice"})
    assert c["choices"] == ["만들기", "취소"] and "test-user@naver.com" in c["text"] and not w["created"]   # 승인 전에는 만들지 않는다
    m.input("naver_mail", lid, c["step"], {"choice": "만들기"})
    done = wait2(m, lid, {"logged_in"})
    assert w["connected"] == [("test-user@naver.com", "ABCD1234EFGH")] and ("manage", "e4", "AIagent") in w["typed"]
    assert "ABCD1234EFGH" not in str(done) and "ABCD1234EFGH" not in str(vars(m._logins[lid]))   # 만든 비밀번호는 남기지 않는다
    assert w["dialog"][-2:] == [True, False]                                  # 확인 창 수락은 생성 순간에만
    assert m.status()[1] == {**m.status()[1], "site": "naver_mail", "status": "logged_in"}


def test_mail_setup_declined_or_imap_off(world):
    from agent.sites.login import NAVER_MAIL
    w, sb, mailer = _setup_world(imap_on=False)
    store = MemoryBlobs()
    store.set("app.naver", '[{"name": "NID_AUT"}]')
    m = SiteLoginManager(sb, store, {"naver": SITE, "naver_mail": NAVER_MAIL}, poll_s=0.02, mailer=mailer)
    lid = m.start("naver_mail", "password")["login_id"]
    l = wait2(m, lid, {"need_input"})
    m.input("naver_mail", lid, l["step"], {"id": "test-user@naver.com"})
    c = wait2(m, lid, {"need_choice"})
    assert c["choices"] == ["바꾸기", "취소"] and "IMAP/SMTP" in c["text"] and not w["imap_on"]
    m.input("naver_mail", lid, c["step"], {"choice": "바꾸기"})
    l = wait2(m, lid, {"need_input"}, since=c["step"])
    assert w["imap_on"] and ("imap", "e50") in w["clicks"]                    # 승인 뒤 '사용함' → 저장
    m.input("naver_mail", lid, l["step"], {"password": PW})
    c = wait2(m, lid, {"need_choice"}, since=l["step"])
    m.input("naver_mail", lid, c["step"], {"choice": "취소"})
    assert wait2(m, lid, {"cancelled"})["state"] == "cancelled" and not w["created"] and w["connected"] == []


def wait2(m, lid, want, timeout=3.0, since=-1):
    end = time.time() + timeout
    while time.time() < end:
        l = m.get("naver_mail", lid)
        if l["state"] in want and l["step"] > since:
            return l
        time.sleep(0.01)
    raise AssertionError(l)


def test_sites_learned_by_address_show_the_name_the_user_called_them():
    # 앱 D142: GET /v1/sites의 name에 주소가 그대로 왔다 — 사용자가 부른 이름이 있으면 그것, 주소는 host로
    import dataclasses
    from agent.sites.login import display_name, generic_site
    s = generic_site("jobs-example-com", "jobs.example.com", "https://jobs.example.com/", "")
    assert display_name(s) == "jobs.example.com"
    assert display_name(dataclasses.replace(s, aliases=("채용사이트",))) == "채용사이트"


def test_www_and_bare_host_are_the_same_site():
    # 실기기 2026-11-18 브이월드: 사이트는 vworld.kr로 적혀 있고 돌아온 화면은 www.vworld.kr — 다른 곳으로 보고 3분 기다린 뒤 실패로 끝냈다
    from agent.sites.login import _same_site
    assert _same_site("www.vworld.kr", "vworld.kr") and _same_site("vworld.kr", "www.vworld.kr")
    assert not _same_site("easysign.anyid.go.kr", "vworld.kr") and not _same_site("", "vworld.kr")


def test_an_address_learned_site_can_be_moved_to_the_dedicated_chrome_and_stays_there(tmp_path):
    # D127: 컨테이너 브라우저를 사이트가 계속 의심할 때(실기기 claude.ai) 전용 크롬 길로 옮긴다 — 파일에 적힌 대로 읽고 다시 적어도 남는다
    import json
    from agent.sites.login import load_generic_sites
    p = tmp_path / "s.json"
    p.write_text(json.dumps([{"key": "claude-com", "name": "클로드", "login_url": "https://claude.ai/login", "check_url": "https://claude.ai/", "backend": "chrome"}]))
    s = load_generic_sites(p)["claude-com"]
    assert s.backend == "chrome" and s.generic
