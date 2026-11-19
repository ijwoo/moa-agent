"""쇼핑(D71): 공식 쇼핑 검색 API로 찾아 상품 카드로. 합성 응답만 쓴다(실제 API를 부르지 않는다)."""

import json
import threading

import httpx

from agent.api.jobs import JobManager
from agent.api.server import make_server
from agent.sites.shop_api import API_URL, ShopApiError, fetch, rows, shop_find
from test_dialog_context import done, make_cloud, sent
from test_shop_drive import Picker, said

WISH = "생수 2L 6개\n(사용자의 말: 생수 사야 해. 만 원 안쪽으로)"
ITEMS = [
    {"title": "합성 <b>생수</b> 2L 6개 &amp; 손잡이", "link": "https://shop.example.com/p/1", "image": "https://img.example.com/1.jpg",
     "lprice": "4900", "hprice": "0", "mallName": "합성마트", "productId": "1", "productType": "2", "brand": "", "maker": ""},
    {"title": "합성 생수 2L 12개", "link": "https://shop.example.com/catalog/2", "image": "http://img.example.com/2.jpg",
     "lprice": "8900", "hprice": "12000", "mallName": "네이버", "productId": "2", "productType": "1"},
    {"title": "중고 생수통", "link": "https://shop.example.com/p/3", "lprice": "1000", "mallName": "중고상회", "productType": "4"},
    {"title": "가격 없는 생수", "link": "https://shop.example.com/p/4", "lprice": "0", "mallName": "가상상회", "productType": "2"},
    {"title": "평문 주소 생수", "link": "http://shop.example.com/p/5", "lprice": "3000", "mallName": "가상상회", "productType": "2"},
    {"title": "합성 생수 500ml 20개", "link": "https://shop.example.com/p/6", "lprice": "7900", "mallName": "가상상회", "productType": "3"},
    "글자", {"title": "", "link": "https://shop.example.com/p/7", "lprice": "100", "productType": "2"},
]


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b = status, body

    def json(self):
        if self._b is None:
            raise ValueError("본문 없음")
        return self._b


def test_values_come_from_the_api_as_they_are_and_bad_rows_are_dropped():
    got = rows(ITEMS)
    assert [r["title"] for r in got] == ["합성 생수 2L 6개 & 손잡이", "합성 생수 2L 12개", "합성 생수 500ml 20개"]   # 태그를 벗기고, 중고·값 없음·평문 주소·이름 없음은 뺀다
    assert got[0] == {"title": "합성 생수 2L 6개 & 손잡이", "url": "https://shop.example.com/p/1", "price": 4900, "mall": "합성마트",
                      "image": "https://img.example.com/1.jpg"}
    assert got[1]["detail"] == "가격비교 최저가" and "image" not in got[1]                                          # 평문 그림 주소는 싣지 않는다


def test_fetch_sends_the_key_in_headers_only_and_names_the_failure_without_leaking_it():
    seen = {}

    def get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)
        return Resp(200, {"items": ITEMS})
    assert fetch("생수 2L", "id-합성", "sec-합성", get=get) == ITEMS
    assert seen["url"] == API_URL and seen["params"]["query"] == "생수 2L" and seen["params"]["exclude"] == "used:rental:cbshop"
    assert seen["headers"] == {"X-Naver-Client-Id": "id-합성", "X-Naver-Client-Secret": "sec-합성"}
    for status, code in ((401, "auth"), (403, "auth"), (429, "limited"), (500, "bad_response")):
        try:
            fetch("q", "i", "s", get=lambda *a, **k: Resp(status, {}))
            raise AssertionError("오류가 나야 한다")
        except ShopApiError as e:
            assert e.code == code and "sec" not in str(e)

    def boom(*a, **k):
        raise httpx.ConnectError("연결 실패 sec-합성")
    for call, code in ((lambda: fetch("q", "", ""), "not_set"), (lambda: fetch("q", "i", "sec-합성", get=boom), "unreachable"),
                       (lambda: fetch("q", "i", "s", get=lambda *a, **k: Resp(200, None)), "bad_response")):
        try:
            call()
            raise AssertionError("오류가 나야 한다")
        except ShopApiError as e:
            assert e.code == code and "sec-합성" not in str(e) and e.__cause__ is None
    assert fetch("q", "i", "s", get=lambda *a, **k: Resp(200, {"items": "x"})) == []


def test_cards_carry_api_values_and_the_model_never_sees_addresses():
    model = Picker(items=(0, 2), text="1번이 만 원 안쪽이에요. 배송비와 옵션은 카드를 눌러 확인해 주세요.")
    steps = []
    r = shop_find(lambda q: ITEMS, model, "생수 2L 6개", WISH, on_step=steps.append)
    assert r.status == "completed" and r.report["by"] == "claude" and [s.tool for s in steps] == ["shop_api"]
    assert r.report["items"] == [
        {"id": "i1", "title": "합성 생수 2L 6개 & 손잡이", "url": "https://shop.example.com/p/1", "price": 4900, "mall": "합성마트",
         "image": "https://img.example.com/1.jpg"},
        {"id": "i2", "title": "합성 생수 500ml 20개", "url": "https://shop.example.com/p/6", "price": 7900, "mall": "가상상회"}]
    out = said(model)
    assert "shop.example.com" not in out and "img.example.com" not in out          # 주소·그림 주소는 Claude로 가지 않는다
    assert "배송비·옵션·도착일·재고는 주어지지 않았다" in out                        # 없는 값을 짐작하지 않게 한다
    assert "합성 생수 2L 12개 — 8,900원 (네이버) · 가격비교 최저가" in out            # 고를 때는 이름·가격·판매처를 본다


def test_made_up_numbers_and_model_failure_fall_back_to_a_fixed_sentence():
    r = shop_find(lambda q: ITEMS, Picker(items=(0,), text="배송비 3,000원을 더해 합계 7,900원이에요."), "생수", WISH)
    assert r.report["done"] == ["쇼핑 검색에서 상품 1개를 골랐어요. 배송비와 옵션은 카드를 눌러 확인해 주세요."] and r.report["by"] == "mac"
    r = shop_find(lambda q: ITEMS, Picker(fail=True), "생수", WISH)                  # 모델을 못 불러도 카드는 나간다(가격이 읽힌 앞쪽부터)
    assert [i["id"] for i in r.report["items"]] == ["i1", "i2", "i3"] and r.report["by"] == "mac"
    r = shop_find(lambda q: ITEMS, None, "생수", WISH)
    assert len(r.report["items"]) == 3


def test_no_results_no_match_and_api_failures_say_so_without_inventing():
    r = shop_find(lambda q: [], Picker(), "없는상품", WISH)
    assert r.status == "completed" and "items" not in r.report and "결과가 없었어요" in r.report["done"][0]
    r = shop_find(lambda q: ITEMS, Picker(items=(99,), text="조건에 맞는 2L 6개 묶음이 없어요."), "생수", WISH)   # 고른 것이 목록에 없다 → 카드 없이 앞쪽 줄만
    assert r.status == "completed" and "items" not in r.report and "1. 합성 생수 2L 6개 & 손잡이 — 4,900원 (합성마트)" in r.report["done"][0]

    def fail(code):
        def f(q):
            raise ShopApiError(code)
        return f
    for code, word in (("not_set", "키가 아직 없어요"), ("auth", "키가 맞지 않거나"), ("limited", "한도"), ("unreachable", "하지 못했어요")):
        r = shop_find(fail(code), Picker(), "생수", WISH)
        assert r.status == "search_failed" and r.reason == code and word in r.summary and "items" not in r.report
    assert shop_find(lambda q: ITEMS, Picker(), "생수", WISH, cancel_check=lambda: True).status == "cancelled"


API_SHOP = {"action": "shop", "question": "생수 2L 6개", "replies": ["쇼핑에서 찾아볼게요!"]}


def api_runner(job, hooks):
    return shop_find(lambda q: ITEMS, Picker(items=(0, 1)), job.task, getattr(job, "_wish", job.task), on_step=hooks.on_step, cancel_check=hooks.cancel_check)


def test_dialog_makes_an_api_task_whatever_the_app_can_do_and_talk_keeps_the_cards_without_addresses():
    dlg, mgr, sites, model, ran = make_cloud(API_SHOP, {"action": "reply", "replies": ["4,900원이에요."]})
    mgr._runner = api_runner
    dlg.shop_api = True
    try:
        r = dlg.handle("생수 사야 해. 만 원 안쪽으로")                                   # 앱이 아무 능력도 알리지 않아도 된다(화면을 열지 않는다)
        assert r["action"]["type"] == "task" and r["action"]["task"]["skill"] == "shop_api" and "browser" not in r["action"]["task"]
        cid, jid = r["conversation_id"], r["action"]["task"]["id"]
        job = done(mgr, jid)
        pub = mgr.get(jid).public()
        assert pub["result"]["outcome"] == "completed" and [i["id"] for i in pub["result"]["report"]["items"]] == ["i1", "i2"]
        assert pub["result"]["report"]["items"][0]["image"] == "https://img.example.com/1.jpg"
        assert "쇼핑 검색 결과를 받았어요" in [n["text"] for n in pub["narration"]]
        out0 = sent(dlg.cloud)
        assert "쇼핑 검색으로 상품을 찾아" in out0 and "사용자의 폰이 쇼핑 화면을 열어" not in out0   # 지시도 API 길의 것
        dlg.handle("이거 얼마야?", cid, reply_to=jid, reply_item="i1")
        out = sent(dlg.cloud)
        assert "[쇼핑 검색으로 찾아 답했다 — 배송비·옵션은 모른다]" in out
        assert "[보여 준 상품 카드: i1 합성 생수 2L 6개 & 손잡이 4,900원 (합성마트) / i2 합성 생수 2L 12개 8,900원 (네이버) · 가격비교 최저가]" in out
        assert "상품 카드 i1 '합성 생수 2L 6개 & 손잡이' 4,900원 (합성마트)" in out
        assert "shop.example.com" not in out and "img.example.com" not in out and not dlg._convs[cid].results
    finally:
        mgr.shutdown()


def test_health_says_shop_api_only_when_on_and_the_phone_paths_can_be_off():
    dlg, mgr, sites, model, ran = make_cloud(API_SHOP)
    dlg.shop_view = False
    srv = make_server(mgr, "t" * 32, port=0, dialog=dlg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/v1/health"
    try:
        f = httpx.get(url, headers={"X-App-Token": "t" * 32}).json()["features"]
        assert "shop_api" not in f and "shop_view" not in f and "shop_drive" not in f
        dlg.shop_api = True
        assert "shop_api" in httpx.get(url, headers={"X-App-Token": "t" * 32}).json()["features"]
    finally:
        srv.shutdown()
        mgr.shutdown()


def test_a_failed_search_ends_the_task_with_its_own_outcome_and_the_reason():
    """앱은 provider_error를 "맥의 모델을 부르지 못했어요"로 보여 준다 → 쇼핑 검색 실패는 이름을 따로 두고 종류를 싣는다."""
    def runner(job, hooks):
        def fail(q):
            raise ShopApiError("auth")
        return shop_find(fail, None, job.task, job.task, on_step=hooks.on_step)
    mgr = JobManager(runner)
    try:
        j = mgr.submit("생수", None, skill="shop_api")
        pub = done(mgr, j.id).public() if False else None
        import time
        for _ in range(200):
            if j.status in ("completed", "failed"):
                break
            time.sleep(0.01)
        pub = j.public()
        assert pub["status"] == "failed" and pub["result"]["outcome"] == "search_failed" and pub["result"]["reason"] == "auth"
        assert pub["result"]["report"]["blocked"] == ["쇼핑 검색 키가 맞지 않거나 검색 API 권한이 없어요."] and "items" not in pub["result"]["report"]
    finally:
        mgr.shutdown()
