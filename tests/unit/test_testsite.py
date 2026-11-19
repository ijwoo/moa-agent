"""합성 테스트 사이트 단위 테스트 (프로세스 안에서 서버 실행, Docker 불필요)."""

import importlib.util
import threading
from pathlib import Path

import httpx
import pytest

_path = Path(__file__).resolve().parents[2] / "testsite" / "server.py"
_spec = importlib.util.spec_from_file_location("testsite_server", _path)
site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site)


@pytest.fixture(scope="module")
def base_url():
    srv = site.make_server("127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def reset():
    site.reset_state()


def get(base, host, path, **kw):
    return httpx.get(base + path, headers={"Host": host}, **kw)


def post(base, host, path, data=None):
    return httpx.post(base + path, headers={"Host": host}, data=data or {}, follow_redirects=False)


def state(base):
    return httpx.get(base + "/_test/state").json()


def test_unknown_host_404(base_url):
    assert get(base_url, "evil.example", "/").status_code == 404


def test_shop_home_lists_products_and_cart(base_url):
    r = get(base_url, "shop.test", "/")
    assert r.status_code == 200
    assert "aria-label='상품 검색'" in r.text
    assert "장바구니 (2)" in r.text
    assert "무선 마우스 M100 — 19,900원" in r.text


def test_search_escapes_query(base_url):
    r = get(base_url, "shop.test", "/search", params={"q": "<script>마우스"})
    assert "<script>" not in r.text and "&lt;script&gt;" in r.text


def test_checkout_records_order_and_clears_cart(base_url):
    r = post(base_url, "shop.test", "/checkout")
    assert r.status_code == 303 and r.headers["location"] == "/order/done"
    s = state(base_url)
    assert s["orders"] == [{"items": {"m100": 1, "t30": 1}, "total": 64900}]
    assert s["cart"] == {}


def test_injection_text_present_on_product_page(base_url):
    assert "evil.example" in get(base_url, "shop.test", "/product/m100").text


def test_mail_send_and_draft(base_url):
    post(base_url, "mail.test", "/send", {"to": "test-a@mail.test", "subject": "회의록", "body": "본문"})
    post(base_url, "mail.test", "/draft", {"to": "x@mail.test", "subject": "초안", "body": ""})
    s = state(base_url)
    assert s["sent"] == [{"to": "test-a@mail.test", "subject": "회의록", "body": "본문"}]
    assert s["drafts"][0]["subject"] == "초안"
    assert "회의록" in get(base_url, "mail.test", "/sent").text


def test_mail_delete(base_url):
    post(base_url, "mail.test", "/inbox/m2/delete")
    s = state(base_url)
    assert [m["id"] for m in s["inbox"]] == ["m1", "m3", "m42"]
    assert s["deleted"][0]["id"] == "m2"


def test_mail_detail_and_missing(base_url):
    assert "분기 보고서" in get(base_url, "mail.test", "/inbox/m1").text
    assert get(base_url, "mail.test", "/inbox/nope").status_code == 404


def test_booking_requires_date_and_time(base_url):
    assert post(base_url, "booking.test", "/room/reserve", {"room": "대회의실"}).status_code == 400
    r = post(base_url, "booking.test", "/room/reserve", {"room": "대회의실", "date": "2026-11-17", "time": "15:00"})
    assert r.status_code == 303
    assert state(base_url)["reservations"] == [{"room": "대회의실", "date": "2026-11-17", "time": "15:00"}]


def test_intranet_notice(base_url):
    assert "연말 휴무 안내" in get(base_url, "intranet.test", "/notice").text
    assert "12월 29일" in get(base_url, "intranet.test", "/notice/3").text


def test_login_stores_nothing(base_url):
    before = state(base_url)
    post(base_url, "shop.test", "/login", {"id": "a", "pw": "b"})
    assert state(base_url) == before


def test_reset(base_url):
    post(base_url, "shop.test", "/checkout")
    httpx.post(base_url + "/_test/reset")
    assert state(base_url)["cart"] == {"m100": 1, "t30": 1}


def test_session_cookie_login_is_per_cookie(base_url):
    r = httpx.get(base_url + "/_test/login", params={"user": "test-a"}, headers={"Host": "shop.test"})
    cookie = r.headers["set-cookie"].split(";")[0]
    assert "Max-Age=86400" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    with_cookie = httpx.get(base_url + "/", headers={"Host": "shop.test", "Cookie": cookie}).text
    without = get(base_url, "shop.test", "/").text
    assert "test-a 님으로 로그인됨" in with_cookie and "로그인됨" not in without
    assert httpx.get(base_url + "/_test/login", params={"user": "<x>"}, headers={"Host": "shop.test"}).status_code == 400


def test_drafts_page_and_inbox_override(base_url):
    r = post(base_url, "mail.test", "/draft", {"to": "test-b@mail.test", "subject": "매출 자료 확인", "body": "b"})
    assert r.headers["location"] == "/drafts"
    page_ = get(base_url, "mail.test", "/drafts").text
    assert "임시 저장된 메일 1건" in page_ and "test-b@mail.test" in page_
    mails = [{"id": "x1", "from": "박대리", "subject": "매출 자료 송부", "date": "오늘", "body": "첨부"}]
    assert httpx.post(base_url + "/_test/inbox", json=mails).status_code == 200
    assert "매출 자료 송부" in get(base_url, "mail.test", "/inbox").text and state(base_url)["inbox"] == mails
    assert httpx.post(base_url + "/_test/inbox", content="[1]").status_code == 400
