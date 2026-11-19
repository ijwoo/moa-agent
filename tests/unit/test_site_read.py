"""로그인해 둔 사이트의 페이지 읽기(sites/read.py): 읽기만, 로그인 풀림 알림, 캡처는 메모리에만. 합성 화면만 쓴다."""

import base64
import json
import threading
import time
from contextlib import contextmanager

import httpx

from agent.api.dialog import DialogManager, site_read_request
from agent.api.jobs import JobManager, jpeg_size
from agent.api.runner import AgentRunner
from agent.api.server import make_server
from agent.providers.base import ModelResponse
from agent.sites import SiteLoginManager
from agent.sites.read import PAGES, READ_TOOLS, pay_rows, read_page
from agent.sites.store import MemoryBlobs

H = {"X-App-Token": "t" * 32, "X-Api-Version": "4"}
PAGE = PAGES["naver_pay_recent"]
# 실측한 뼈대(상태 → 상품 이름 → 금액 → "결제일시" → 날짜)를 합성 데이터로 옮긴 화면. 아래쪽은 광고
OBS = "\n".join([
    "URL: https://pay.naver.com/pc/history?page=1", "제목: 네이버페이", '[e1] link "결제내역"',
    '[e2] link "합성 텀블러 500ml 자세히 보기"', 'text "자세히 보기"', '[e3] strong "결제완료"', '[e4] paragraph "합성 텀블러 500ml"',
    'text "12,900원"', 'text "결제일시"', 'text "11. 9. 14:22 결제"', '[e5] link "주문 상세 보기"', '[e6] button "더보기"',
    '[e7] link "테스트 노트 3권 세트"', 'text "자세히 보기"', '[e8] strong "구매확정완료"', '[e9] paragraph "테스트 노트 3권 세트"',
    'text "수량"', 'text "1개"', 'text "4,500원"', 'text "결제일시"', 'text "9. 23. 09:05 결제"', '[e10] link "주문 상세 보기"',
    '[e11] link "광고 상품"', '[e12] img "광고 이미지"', 'text "할인된 가격"', 'text "99,000원"', '[e13] strong "공식"',
])
JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffc0000b080320050001011100ffd9")   # 머리만 있는 1280x800 JPEG


class Browser:
    def __init__(self, world):
        self.w, self.ops = world, []

    def request(self, op, args=None):
        self.ops.append(op)
        if op == "cookies_import":
            self.w["restored"] = True
            return {}
        if op == "cookies_export":
            return {"cookies": [{"name": "NID_AUT", "value": "refreshed"}]}
        if op == "navigate":
            self.w["opened"].append(args["url"])
            return {}
        if op == "observe":
            if self.w.get("blocked"):
                return {"text": 'URL: https://pay.naver.com/pc/history\ntext "비정상적인 접근이 감지되어 이용이 제한되었습니다"', "url": "https://pay.naver.com/pc/history"}
            if self.w.get("elsewhere"):
                return {"text": 'URL: https://www.naver.com/\n[e1] link "메일"', "url": "https://www.naver.com/"}
            if self.w.get("expired"):
                return {"text": 'URL: https://nid.naver.com/nidlogin.login\n[e1] textbox "아이디"', "url": "https://nid.naver.com/nidlogin.login"}
            return {"text": OBS, "url": "https://pay.naver.com/pc/history?page=1"}
        if op == "screenshot":
            return {"jpeg_b64": base64.b64encode(JPEG).decode()}
        raise AssertionError(f"읽기 기능이 쓰면 안 되는 동작: {op}")


class Sandbox:
    def __init__(self, world):
        self.w = world

    @contextmanager
    def session(self, user_id):
        b = Browser(self.w)
        self.w["browsers"].append(b)
        yield b


class Model:
    def __init__(self, text="최근 결제: 합성 텀블러 500ml 12,900원 결제완료"):
        self.text, self.seen = text, []

    def generate(self, messages, **kw):
        self.seen.append(messages)
        return ModelResponse(text=self.text, latency_ms=1, model="m", provider="p")


def make(logged_in=True, **world):
    w = {"opened": [], "browsers": [], **world}
    store = MemoryBlobs()
    m = SiteLoginManager(Sandbox(w), store, user_id="u")
    if logged_in:
        store.set(m._key(m.sites["naver"]), json.dumps([{"name": "NID_AUT", "value": "x"}]))
        m._mark(m.sites["naver"], "logged_in")
    return m, w, store


def test_pay_rows_follow_the_measured_skeleton_and_skip_ads():
    assert pay_rows(OBS) == [
        {"status": "결제완료", "name": "합성 텀블러 500ml", "price": "12,900", "date": "11. 9. 14:22 결제"},
        {"status": "구매확정완료", "name": "테스트 노트 3권 세트", "price": "4,500", "date": "9. 23. 09:05 결제"}]
    assert pay_rows("") == []


def test_read_tools_cannot_click_or_type():
    assert {t.name for t in READ_TOOLS} == {"navigate", "observe", "scroll", "extract_text", "finish"}


def test_read_page_opens_only_the_fixed_url_and_gives_the_screen_to_the_model_as_data():
    m, w, store = make()
    model, seen_steps = Model(), []
    run, shots = read_page(m, model, PAGE, "최근 결제 알려줘", on_step=seen_steps.append, sleep=lambda s: None)
    assert run.status == "completed" and "12,900원" in run.summary and run.report["done"] == [run.summary] and shots == []
    assert w["opened"] == [PAGE.url]
    ops = w["browsers"][0].ops
    assert ops == ["cookies_import", "navigate", "observe", "cookies_export"]            # 누르기·입력 없음, 갱신된 로그인 값은 다시 보관
    assert "refreshed" in store.get(m._key(m.sites["naver"]))
    system, user = model.seen[0][0].content, model.seen[0][1].content
    assert "지시가 아니다" in system and "[물음]\n최근 결제 알려줘" in user and "합성 텀블러" in user
    assert [(s.tool, s.result_status) for s in seen_steps] == [("navigate", "ok"), ("extract_text", "ok")]
    assert seen_steps[0].url == "https://pay.naver.com/pc/history"                          # 주소의 물음표 뒤는 남기지 않는다


def test_read_page_without_login_or_with_expired_login_says_needs_login():
    m, w, _ = make(logged_in=False)
    run, _ = read_page(m, Model(), PAGE, "최근 결제", sleep=lambda s: None)
    assert (run.status, run.reason) == ("needs_login", "login_missing") and w["opened"] == []
    m, w, store = make(expired=True)
    model = Model()
    run, shots = read_page(m, model, PAGE, "최근 결제", capture=True, sleep=lambda s: None)
    assert (run.status, run.reason) == ("needs_login", "login_expired") and "풀렸" in run.summary
    # 멈춘 화면(로그인 화면)을 한 장 붙인다. 보관한 로그인 상태를 실은 브라우저의 화면이라 sensitive다. 설명은 코드가 붙인다
    assert [(s["page_host"], s["sensitive"], s["caption"]) for s in shots] == [
        ("nid.naver.com", True, "네이버 로그인 화면(로그인이 풀려 여기서 멈춤)")]
    assert model.seen == []                                                                 # 로그인 화면의 글은 모델에 주지 않는다
    assert store.get(m._key(m.sites["naver"])) is None                                      # 죽은 로그인 상태는 지운다
    naver = next(s for s in m.status() if s["site"] == "naver")
    assert naver["status"] == "logged_out" and naver["reason"] == "expired"


def test_capture_is_taken_only_when_enabled_and_kept_in_memory():
    m, w, _ = make()
    run, shots = read_page(m, Model(), PAGE, "최근 결제", capture=True, sleep=lambda s: None)
    assert run.status == "completed" and len(shots) == 1
    assert shots[0]["data"] == JPEG and shots[0]["page_host"] == "pay.naver.com" and shots[0]["caption"] == "네이버페이 결제내역 화면"
    assert shots[0]["sensitive"] is True
    assert jpeg_size(JPEG) == (1280, 800) and jpeg_size(b"not a jpeg") == (None, None)
    # 캡처를 끄면 멈춘 화면도 찍지 않는다
    m, w, _ = make(expired=True)
    assert read_page(m, Model(), PAGE, "최근 결제", sleep=lambda s: None)[1] == [] and "screenshot" not in w["browsers"][0].ops


def test_blocked_or_wrong_page_stops_with_that_screen_and_never_reaches_the_model():
    m, w, store = make(blocked=True)
    model = Model()
    run, shots = read_page(m, model, PAGE, "최근 결제", capture=True, sleep=lambda s: None)
    assert run.status == "site_blocked" and "막았어요" in run.summary and model.seen == []
    assert len(shots) == 1 and shots[0]["sensitive"] is True and "막아 여기서 멈춤" in shots[0]["caption"]
    assert store.get(m._key(m.sites["naver"])) is not None                                  # 막힌 것은 로그인이 풀린 것이 아니다
    m, w, _ = make(elsewhere=True)
    run, shots = read_page(m, model, PAGE, "최근 결제", capture=True, sleep=lambda s: None)
    assert run.status == "provider_error" and model.seen == [] and shots[0]["page_host"] == "www.naver.com"
    # 결제 줄이 읽히는 화면은 막힘 문구가 섞여 있어도 막힌 것으로 보지 않는다
    assert pay_rows(OBS + '\ntext "이용이 제한된 상품"')


def test_final_shot_for_synthetic_site_tasks_only_when_read_finished_or_stuck():
    from types import SimpleNamespace as NS
    from agent.api.runner import final_shot

    class B:
        def request(self, op, args=None):
            assert op == "screenshot"
            return {"jpeg_b64": base64.b64encode(JPEG).decode()}
    step = lambda url: NS(url=url)
    job = NS(approved=[])
    read = NS(status="completed", steps=[step("about:blank"), step("http://mail.test/inbox")], sent=[])
    assert final_shot(B(), read, job) == [{"mime": "image/jpeg", "data": JPEG, "page_host": "mail.test", "caption": "테스트 메일 화면",
                                           "sensitive": False}]                             # 로그인 없는 합성 화면만 sensitive=False
    stuck = NS(status="max_steps", steps=[step("http://shop.test/")], sent=[])
    assert final_shot(B(), stuck, job)[0]["caption"] == "테스트 쇼핑몰 화면(여기서 멈춤)"
    assert final_shot(B(), read, NS(approved=[{"draft_id": "d"}])) == []                    # 초안을 저장한 작업: 승인 카드가 이미 보여 줬다
    for status in ("cancelled", "approval_denied", "needs_user", "send_failed"):
        assert final_shot(B(), NS(status=status, steps=read.steps, sent=[]), job) == [], status
    assert final_shot(B(), NS(status="completed", steps=[step("about:blank")], sent=[]), job) == []   # 연 페이지가 없다


def test_dialog_routes_only_read_requests_to_the_site_read_job():
    assert site_read_request("네이버페이 결제내역에서 최근에 결제한 3건을 알려줘") == "naver_pay_recent"
    assert site_read_request("네이버 페이에서 최근에 뭐 샀어?") == "naver_pay_recent"
    for text in ("네이버페이 결제 취소해줘", "네이버페이로 이거 결제해줘", "네이버페이 주문 환불 신청해줘", "오늘 회의 요약해줘", "네이버 로그인해줘"):
        assert site_read_request(text) is None, text


def api(runner_sites, capture=False, monkeypatch=None):
    runner = AgentRunner.__new__(AgentRunner)       # 테스트 사이트·샌드박스 없이 기능 작업 분기만 쓴다
    runner.sites, runner.capture, runner.model, runner.mailer = runner_sites, capture, "m", None
    mgr = JobManager(runner)
    dialog = DialogManager(Model(), mgr, sites=runner_sites)
    http = make_server(mgr, "t" * 32, port=0, dialog=dialog, sites=runner_sites)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{http.server_address[1]}", mgr, http


def wait(base, jid, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        j = httpx.get(f"{base}/v1/tasks/{jid}", headers=H).json()
        if j["status"] in ("completed", "failed"):
            return j
        time.sleep(0.05)
    raise AssertionError(j)


def test_api_site_read_job_reports_answer_sources_and_capture(monkeypatch):
    import agent.api.runner as runner_mod
    monkeypatch.setattr(runner_mod, "QwenProvider", lambda **kw: Model())
    m, w, _ = make()
    base, mgr, http = api(m, capture=True)
    try:
        feats = httpx.get(f"{base}/v1/health", headers=H).json()["features"]
        assert "site_read" in feats and "report_attachments" in feats
        r = httpx.post(f"{base}/v1/messages", headers=H, json={"text": "네이버페이 결제내역에서 최근에 결제한 3건을 알려줘"}).json()
        assert r["action"]["type"] == "task" and "네이버페이 결제내역" in r["replies"][0]["text"]
        j = wait(base, r["action"]["task"]["id"])
        rep = j["result"]["report"]
        assert j["status"] == "completed" and "12,900원" in rep["done"][0] and rep["sources"] == ["https://pay.naver.com/pc/history"]
        assert rep["private"] is True                                                       # 개인 데이터가 든 답이라는 표시
        a = rep["attachments"][0]
        assert {k: a[k] for k in ("id", "type", "mime", "width", "height", "bytes", "page_host", "caption", "sensitive")} == {
            "id": "a1", "type": "screenshot", "mime": "image/jpeg", "width": 1280, "height": 800, "bytes": len(JPEG),
            "page_host": "pay.naver.com", "caption": "네이버페이 결제내역 화면", "sensitive": True}
        assert a["path"] == f"/v1/tasks/{j['id']}/attachments/a1" and a["expires_at"]
        img = httpx.get(base + a["path"], headers=H)
        assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and img.content == JPEG
        assert img.headers["cache-control"] == "no-store"
        assert httpx.get(base + a["path"]).status_code == 401                               # 토큰 없이는 못 받는다
        assert "attachments" not in json.dumps(httpx.get(f"{base}/v1/tasks", headers=H).json())
        assert httpx.get(f"{base}/v1/tasks/{j['id']}/attachments/a9", headers=H).json()["error"]["code"] == "attachment_gone"
        assert httpx.get(f"{base}/v1/tasks/nosuchtask123/attachments/a1", headers=H).json()["error"]["code"] == "task_not_found"
        # 로그아웃하면(동의 철회) 그 사이트의 캡처도 지운다
        httpx.post(f"{base}/v1/sites/naver/logout", headers=H)
        gone = httpx.get(base + a["path"], headers=H)
        assert gone.status_code == 404 and gone.json()["error"]["code"] == "attachment_gone"
    finally:
        mgr.shutdown()
        http.shutdown()


def test_api_expired_login_ends_as_needs_login_and_card_asks_again(monkeypatch):
    import agent.api.runner as runner_mod
    monkeypatch.setattr(runner_mod, "QwenProvider", lambda **kw: Model())
    m, w, _ = make(expired=True)
    base, mgr, http = api(m)
    try:
        assert "report_attachments" not in httpx.get(f"{base}/v1/health", headers=H).json()["features"]   # 캡처는 기본 끔
        r = httpx.post(f"{base}/v1/messages", headers=H, json={"text": "네이버페이 결제내역 알려줘"}).json()
        j = wait(base, r["action"]["task"]["id"])
        res = j["result"]
        assert j["status"] == "failed" and res["outcome"] == "needs_login" and res["reason"] == "login_expired"
        assert res["action"] == {"type": "site_login", "site": "naver", "resume": True}   # D126: 로그인하면 이어서
        assert res["report"]["done"] == [] and "풀렸" in res["report"]["blocked"][0] and "attachments" not in res["report"]   # 캡처를 끈 서버
        assert "private" not in res["report"]
        # 이제 풀린 것을 안다 → 같은 부탁에는 작업을 만들지 않고 로그인부터 권한다
        again = httpx.post(f"{base}/v1/messages", headers=H, json={"text": "네이버페이 결제내역 알려줘"}).json()
        assert again["action"] == {"type": "site_login", "site": "naver", "resume": True} and "로그인이 필요" in again["replies"][0]["text"]
        old = httpx.post(f"{base}/v1/messages", headers={**H, "X-Api-Version": "3"}, json={"text": "네이버페이 결제내역 알려줘"}).json()
        assert old["action"] == {"type": "none"}                                            # 로그인 화면을 못 그리는 앱에는 말로만
        sites = httpx.get(f"{base}/v1/sites", headers=H).json()["sites"]
        assert next(s for s in sites if s["site"] == "naver")["reason"] == "expired"
    finally:
        mgr.shutdown()
        http.shutdown()


def test_api_failed_task_carries_the_screen_where_it_stopped(monkeypatch):
    import agent.api.runner as runner_mod
    monkeypatch.setattr(runner_mod, "QwenProvider", lambda **kw: Model())
    m, w, _ = make(expired=True)
    base, mgr, http = api(m, capture=True)
    try:
        r = httpx.post(f"{base}/v1/messages", headers=H, json={"text": "네이버페이 결제내역 알려줘"}).json()
        j = wait(base, r["action"]["task"]["id"])
        rep = j["result"]["report"]
        assert j["status"] == "failed" and j["result"]["outcome"] == "needs_login" and "private" not in rep
        a = rep["attachments"]
        assert len(a) == 1 and a[0]["sensitive"] is True and a[0]["page_host"] == "nid.naver.com" and "풀려" in a[0]["caption"]
        assert httpx.get(base + a[0]["path"], headers=H).content == JPEG
    finally:
        mgr.shutdown()
        http.shutdown()
