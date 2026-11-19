"""쇼핑(D68): 폰의 쇼핑 화면을 멀리 있는 브라우저로 쓰는 작업. 합성 데이터만 쓴다(실제 쇼핑 사이트에 접속하지 않는다)."""

import json
import threading
import time

import httpx

from agent.api.jobs import Conflict, JobManager
from agent.api.server import make_server
from agent.providers.base import ModelResponse, ProviderError
from agent.sites.shop import clean_result, list_items, pick, product_item, shape, shop_drive
from test_dialog_context import done, make_cloud, sent

TOKEN = "t" * 32
H = {"X-App-Token": TOKEN}
LIST = {"kind": "list", "url": "https://shop.example.com/search", "fields": {"items": [
    {"title": "합성 생수 2L 6개", "price": 4900, "mall": "합성마트"},
    {"title": "합성 생수 500ml 20개", "price": 7900, "mall": "가상상회"},
    {"title": "광고 합성 탄산수 12개"},
    {"title": "합성 생수 2L 12개", "price": 8900, "mall": "합성마트"}]}}
P0 = {"kind": "product", "item": 0, "url": "https://shop.example.com/p/100", "fields": {
    "title": "합성 생수 2L 6개", "price": 4900, "seller": "합성마트", "shipping": "3,000원",
    "options": [{"name": "용량", "values": ["2L 6개", "2L 12개"]}], "arrival": "내일 도착"}}
P3 = {"kind": "product", "item": 3, "url": "https://shop.example.com/p/103", "fields": {
    "title": "합성 생수 2L 12개", "price": 8900, "seller": "합성마트", "shipping": "무료"}}
WISH = "생수 2L 6개\n(사용자의 말: 생수 사야 해. 만 원 안쪽으로)"


class Phone:
    """폰 자리: 맡은 일을 적어 두고 정해 둔 결과를 차례로 돌려준다. None은 답하지 않은 것(시간 초과)."""

    def __init__(self, *results):
        self.results, self.got = list(results), []

    def __call__(self, command, wait_s):
        self.got.append(command)
        return self.results.pop(0) if self.results else None


class Picker:
    """고르기·말하기 모델 자리. 받은 메시지를 적어 둔다."""

    def __init__(self, items=(0, 3), text="1번이 조건에 맞아요. 배송비 3,000원이 붙어요.", fail=False):
        self.items, self.text, self.fail, self.seen = list(items), text, fail, []

    def generate(self, messages, json_schema=None, **kw):
        self.seen.append(messages)
        if self.fail:
            raise ProviderError("연결 실패")
        out = json.dumps({"items": self.items}) if json_schema else self.text
        return ModelResponse(text=out, latency_ms=1, model="c", provider="claude")


def said(model) -> str:
    return "\n".join(m.content for msgs in model.seen for m in msgs)


def test_search_pick_open_and_cards_carry_the_values_the_phone_read():
    phone, model = Phone({"result": "ok", "views": [LIST]}, {"result": "ok", "views": [P0, P3]}), Picker()
    run = shop_drive(phone, model, "생수 2L 6개", WISH)
    assert phone.got == [{"type": "search", "query": "생수 2L 6개"}, {"type": "open", "items": [0, 3]}]
    items = run.report["items"]
    assert run.status == "completed" and run.report["by"] == "claude" and run.report["done"] == [model.text]
    assert items[0] == {"id": "i1", "title": "합성 생수 2L 6개", "url": "https://shop.example.com/p/100", "price": 4900,
                        "mall": "합성마트", "shipping": "3,000원", "detail": "용량: 2L 6개, 2L 12개 · 내일 도착"}
    assert items[1]["id"] == "i2" and items[1]["price"] == 8900 and "detail" not in items[1]
    # Claude로 나간 것: 사려는 것과 공개 상품 줄뿐. 주소는 나가지 않는다
    assert "생수 사야 해" in said(model) and "합성 생수 2L 6개 — 4,900원" in said(model) and "https://" not in said(model)


def test_pick_keeps_only_distinct_in_range_items_and_falls_back_when_the_model_fails():
    items = list_items(LIST)
    assert items[2] == {"title": "광고 합성 탄산수 12개"} and len(items) == 4
    assert pick(Picker([3, 3, 9, -1, 0, 1, 2]), WISH, items) == [3, 0, 1]      # 겹침·범위 밖은 버리고 3개까지
    assert pick(Picker([True, "0"]), WISH, items) == [0, 1, 3]                  # 꼴이 다르면 못 고른 것 → 가격이 읽힌 앞쪽부터
    assert pick(Picker(fail=True), WISH, items) == [0, 1, 3] and pick(None, WISH, items) == [0, 1, 3]
    assert pick(Picker([]), WISH, items) == [0, 1, 3]
    assert pick(Picker([0]), WISH, [{}, {}]) == []                              # 읽힌 줄이 없으면 열지 않는다


def test_values_are_taken_only_when_they_have_the_right_shape():
    assert clean_result({"result": "weird"}) is None and clean_result("ok") is None
    r = clean_result({"result": "limited", "status": 429, "views": [LIST, {"kind": "order", "fields": {}}, "x"], "skipped": [{"item": 2, "reason": "ad"}]})
    assert r["status"] == 429 and len(r["views"]) == 1 and r["skipped"] == [{"item": 2, "reason": "ad"}]
    assert product_item({"kind": "product", "url": "http://shop.example.com/p/1", "fields": {"title": "합성"}}, 1) is None     # https가 아니면 카드로 만들지 않는다
    assert product_item({"kind": "product", "url": "https://shop.example.com/p/1", "fields": {"title": " "}}, 1) is None
    it = product_item({"kind": "product", "url": "https://shop.example.com/p/1", "fields": {"title": "합성 컵", "price": "4,900원", "seller": 3}}, 2)
    assert it == {"id": "i2", "title": "합성 컵", "url": "https://shop.example.com/p/1"}   # 숫자가 아닌 가격은 쓰지 않는다(지어 쓰지 않는다)


def test_blocked_site_stops_the_task_and_no_more_commands_are_sent():
    phone = Phone({"result": "limited", "status": 418, "views": []}, {"result": "ok", "views": [P0]})
    run = shop_drive(phone, Picker(), "생수", WISH)
    assert run.status == "site_blocked" and "418" in run.summary and len(phone.got) == 1
    # 목록은 읽고 상품 화면에서 막혔다: 읽은 목록으로 답하고 막힌 것을 알린다
    phone = Phone({"result": "ok", "views": [LIST]}, {"result": "blocked", "views": []})
    run = shop_drive(phone, Picker(), "생수", WISH)
    assert run.status == "completed" and "합성 생수 2L 6개 — 4,900원" in run.report["done"][0] and "items" not in run.report
    assert run.report["blocked"] == ["쇼핑 화면이 로그인·확인 화면으로 넘어가서 멈췄어요."] and len(phone.got) == 2
    # 일부만 열고 제한됐다: 연 것은 카드로, 제한은 blocked에
    run = shop_drive(Phone({"result": "ok", "views": [LIST]}, {"result": "limited", "status": 429, "views": [P0]}), Picker(), "생수", WISH)
    assert run.status == "completed" and len(run.report["items"]) == 1 and run.report["blocked"]


def test_silent_phone_is_a_timeout_and_bad_or_empty_results_do_not_invent_anything():
    run = shop_drive(Phone(None), Picker(), "생수", WISH)
    assert run.status == "timeout" and "응답하지 않았어요" in run.report["blocked"][0]
    run = shop_drive(Phone({"result": "ok", "views": [LIST]}, None), Picker(), "생수", WISH)
    assert run.status == "timeout"
    run = shop_drive(Phone({"oops": 1}), Picker(), "생수", WISH)
    assert run.status == "browser_failed" and run.report["done"] == []
    run = shop_drive(Phone({"result": "refused", "reason": "closed"}), Picker(), "생수", WISH)
    assert run.status == "browser_failed"
    run = shop_drive(Phone({"result": "nothing", "views": []}), Picker(), "생수", WISH)
    assert run.status == "completed" and "결과가 없었어요" in run.summary
    run = shop_drive(Phone({"result": "ok", "views": [{"kind": "list", "url": "https://shop.example.com/s", "fields": {"items": []}}]}), Picker(), "생수", WISH)
    assert run.status == "browser_failed"                                        # ok라고 왔지만 읽은 줄이 없다
    # 상품 화면을 하나도 못 열었다(광고·경유 주소뿐): 목록에서 읽은 것만 글로, 카드는 없다
    run = shop_drive(Phone({"result": "ok", "views": [LIST]}, {"result": "ok", "views": [], "skipped": [{"item": 0, "reason": "ad"}]}), Picker(), "생수", WISH)
    assert run.status == "completed" and "items" not in run.report and "열지 못했어요" in run.summary
    # Claude가 조건에 맞는 것이 없다고 봤다: 열지 않고 목록만
    phone = Phone({"result": "ok", "views": [LIST]})
    model = Picker([], text="조건에 맞는 게 안 보여요.")
    model.items = [9]   # 범위 밖만 골랐다 → 맞는 것이 없다
    run = shop_drive(phone, model, "생수", WISH)
    assert run.status == "completed" and len(phone.got) == 1 and "items" not in run.report
    # 말을 못 썼다(Claude 실패): 정해 둔 문장, 카드는 그대로
    class Half(Picker):
        def generate(self, messages, json_schema=None, **kw):
            if not json_schema:
                raise ProviderError("연결 실패")
            return super().generate(messages, json_schema=json_schema, **kw)
    run = shop_drive(Phone({"result": "ok", "views": [LIST]}, {"result": "ok", "views": [P0]}), Half(), "생수", WISH)
    assert run.status == "completed" and run.report["by"] == "mac" and run.report["items"][0]["price"] == 4900


def shop_runner(job, hooks):
    return shop_drive(hooks.drive, None, job.task, job.task, on_step=hooks.on_step, wait_s=job_wait[0], cancel_check=hooks.cancel_check)


job_wait = [5.0]


def wait_browser(mgr, jid, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        pub = mgr.get(jid).public()
        if pub.get("browser") or pub["status"] not in ("queued", "running"):
            return pub
        time.sleep(0.01)
    raise AssertionError("폰이 할 일이 실리지 않았다")


def test_task_carries_the_command_and_takes_the_result_once():
    job_wait[0] = 5.0
    mgr = JobManager(shop_runner)
    try:
        job = mgr.submit("생수 2L 6개", None, title="쇼핑 화면에서 찾기", skill="shop_drive")
        pub = wait_browser(mgr, job.id)
        assert pub["status"] == "running" and pub["browser"] == {"id": "b1", "type": "search", "query": "생수 2L 6개"}
        for bad in ("b9", "b0", ""):
            try:
                mgr.browser_result(job.id, bad, {"id": bad, "result": "ok", "views": [LIST]})
                raise AssertionError("받으면 안 된다")
            except Conflict:
                pass
        mgr.browser_result(job.id, "b1", {"id": "b1", "result": "ok", "views": [LIST]})
        mgr.browser_result(job.id, "b1", {"id": "b1", "result": "limited", "views": []})     # 같은 결과가 다시 와도 처음 것만 쓴다
        pub = wait_browser(mgr, job.id)
        while pub.get("browser", {}).get("id") != "b2":
            pub = wait_browser(mgr, job.id)
        assert pub["browser"] == {"id": "b2", "type": "open", "items": [0, 1, 3]}
        mgr.browser_result(job.id, "b1", {"id": "b1", "result": "ok"})                        # 바로 앞 일의 결과가 늦게 또 왔다: 조용히 받는다
        mgr.browser_result(job.id, "b2", {"id": "b2", "result": "ok", "views": [P0, P3]})
        end = done(mgr, job.id).public()
        assert end["status"] == "completed" and "browser" not in end
        assert [i["id"] for i in end["result"]["report"]["items"]] == ["i1", "i2"] and "by" not in end["result"]["report"]
        assert [n["text"] for n in end["narration"]][1:3] == ["쇼핑 검색 결과를 읽었어요", "상품 화면을 열어 읽었어요"]
        try:
            mgr.browser_result(job.id, "b3", {"id": "b3", "result": "ok"})                    # 끝난 작업에 온 새 결과
            raise AssertionError("받으면 안 된다")
        except Conflict:
            pass
    finally:
        mgr.shutdown()


def test_task_times_out_when_the_phone_is_silent_and_cancel_ends_the_wait():
    job_wait[0] = 0.3
    mgr = JobManager(shop_runner)
    try:
        job = mgr.submit("생수", None, title="쇼핑 화면에서 찾기", skill="shop_drive")
        end = done(mgr, job.id).public()
        assert end["status"] == "failed" and end["result"]["outcome"] == "timeout" and "browser" not in end
        assert "응답하지 않았어요" in end["result"]["report"]["blocked"][0]
        job_wait[0] = 30.0
        job = mgr.submit("생수", None, title="쇼핑 화면에서 찾기", skill="shop_drive")
        wait_browser(mgr, job.id)
        mgr.cancel(job.id)
        t0 = time.time()
        while mgr.get(job.id).status != "cancelled" and time.time() - t0 < 3:
            time.sleep(0.01)
        end = mgr.get(job.id).public()
        assert end["status"] == "cancelled" and "browser" not in end and end["result"]["report"]["blocked"] == []   # 취소는 '폰이 답하지 않았다'가 아니다
        # 사이트가 막았다: 작업은 failed + site_blocked, 이유는 blocked 첫 줄에 한 번만
        job_wait[0] = 5.0
        job = mgr.submit("생수", None, title="쇼핑 화면에서 찾기", skill="shop_drive")
        wait_browser(mgr, job.id)
        mgr.browser_result(job.id, "b1", {"id": "b1", "result": "limited", "status": 429})
        end = done(mgr, job.id).public()
        assert end["result"]["outcome"] == "site_blocked" and end["result"]["report"]["blocked"] == ["쇼핑 사이트가 접속을 제한했어요(HTTP 429)."]
    finally:
        mgr.shutdown()


def test_http_route_takes_the_result_and_answers_409_for_other_ids():
    job_wait[0] = 5.0
    mgr = JobManager(shop_runner)
    srv = make_server(mgr, TOKEN, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        job = mgr.submit("생수", None, title="쇼핑 화면에서 찾기", skill="shop_drive")
        wait_browser(mgr, job.id)
        url = f"{base}/v1/tasks/{job.id}/browser"
        assert httpx.post(url, headers=H, json={"result": "ok"}).status_code == 400                       # id가 없다
        r = httpx.post(url, headers=H, json={"id": "b7", "result": "ok", "views": [LIST]})
        assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"
        assert httpx.post(url, json={"id": "b1", "result": "ok"}).status_code == 401                      # 토큰 없이
        r = httpx.post(url, headers=H, json={"id": "b1", "result": "nothing", "views": []})
        assert r.status_code == 200 and r.json()["id"] == job.id
        end = done(mgr, job.id).public()
        assert end["status"] == "completed" and "결과가 없었어요" in end["result"]["report"]["done"][0]
        assert httpx.post(url, headers=H, json={"id": "b2", "result": "ok"}).status_code == 409           # 끝난 작업
        assert httpx.post(f"{base}/v1/tasks/{'x' * 12}/browser", headers=H, json={"id": "b1", "result": "ok"}).status_code == 404
    finally:
        mgr.shutdown()
        srv.shutdown()


def test_summary_with_a_number_the_phone_did_not_read_is_replaced_by_a_fixed_sentence():
    from agent.sites.shop import new_numbers
    src = "생수 2L 6개 만 원\n1. 합성 생수 — 4,900원 · 배송 3,000원"
    assert new_numbers("1번은 4,900원이고 배송비 3,000원이 붙어요. 6개 묶음이에요.", src) == []
    assert new_numbers("합계 7,900원이에요.", src) == ["7,900"] and new_numbers("4900원", src) == []
    two = lambda: Phone({"result": "ok", "views": [LIST]}, {"result": "ok", "views": [P0]})
    model = Picker([0], text="1번은 합계 7,900원이라 조건에 맞아요.")
    run = shop_drive(two(), model, "생수", WISH)
    assert run.report["by"] == "mac" and "7,900" not in run.report["done"][0] and run.report["items"][0]["price"] == 4900
    assert len(model.seen) == 3                                                   # 고르기 1번 + 말 2번(한 번 다시 쓰게 했다)
    assert "새 숫자(합계 등)를 만들지 않는다" in said(model)


SHOP = {"action": "shop", "question": "생수 2L 6개", "replies": ["쇼핑 화면에서 찾아볼게요!"]}


def test_dialog_makes_a_task_only_when_the_app_says_it_can_drive():
    dlg, mgr, sites, model, ran = make_cloud(SHOP, SHOP, SHOP)
    try:
        # 켜져 있지 않거나 앱이 shop_drive를 알리지 않으면 예전 그대로(action shop, 작업 없음)
        r = dlg.handle("생수 사야 해", abilities=frozenset({"shop", "shop_drive"}))
        assert r["action"] == {"type": "shop", "query": "생수 2L 6개"} and not ran
        dlg.shop_drive = True
        r = dlg.handle("생수 사야 해", abilities=frozenset({"shop"}))
        assert r["action"] == {"type": "shop", "query": "생수 2L 6개"} and not ran
        assert "이 앱에서는 shop이 **작업**이 된다" not in sent(dlg.cloud)
        r = dlg.handle("생수 사야 해. 만 원 안쪽으로", abilities=frozenset({"shop", "shop_drive"}))
        assert r["action"]["type"] == "task" and r["replies"] == [{"text": "쇼핑 화면에서 찾아볼게요!", "by": "claude"}]
        assert "이 앱에서는 shop이 **작업**이 된다" in sent(dlg.cloud)
        job = mgr.get(r["action"]["task"]["id"])
        assert job.skill == "shop_drive" and job.task == "생수 2L 6개" and "만 원 안쪽으로" in job._wish and job.title == "쇼핑 화면에서 찾기"
    finally:
        mgr.shutdown()


def test_cards_stay_in_the_talk_and_a_replied_card_is_pointed_out():
    dlg, mgr, sites, model, ran = make_cloud(SHOP, {"action": "reply", "replies": ["배송비는 3,000원이에요."]},
                                             {"action": "reply", "replies": ["네."]})
    job_wait[0] = 5.0
    mgr._runner = shop_runner
    dlg.shop_drive = True
    try:
        r = dlg.handle("생수 사야 해", abilities=frozenset({"shop", "shop_drive"}))
        cid, jid = r["conversation_id"], r["action"]["task"]["id"]
        wait_browser(mgr, jid)
        mgr.browser_result(jid, "b1", {"id": "b1", "result": "ok", "views": [LIST]})
        pub = wait_browser(mgr, jid)
        while pub.get("browser", {}).get("id") != "b2":
            pub = wait_browser(mgr, jid)
        mgr.browser_result(jid, "b2", {"id": "b2", "result": "ok", "views": [P0, P3]})
        done(mgr, jid)
        dlg.handle("이거 배송비 얼마야?", cid, reply_to=jid, reply_item="i1", abilities=frozenset({"shop", "shop_drive"}))
        out = sent(dlg.cloud)
        assert "[폰의 쇼핑 화면에서 찾아 답했다]" in out and "[보여 준 상품 카드: i1 합성 생수 2L 6개 4,900원 (합성마트) 배송 3,000원 · 용량: 2L 6개, 2L 12개 · 내일 도착 / i2 " in out
        assert "작업 '쇼핑 화면에서 찾기'의 상품 카드 i1 '합성 생수 2L 6개' 4,900원 (합성마트) 배송 3,000원" in out
        assert "https://shop.example.com" not in out and not dlg._convs[cid].results      # 상품 주소는 나가지 않고, 이름표(개인 결과)도 만들지 않는다
        dlg.handle("그럼 이건?", cid, reply_to=jid, reply_item="i9", abilities=frozenset({"shop", "shop_drive"}))
        assert "상품 카드 i9" not in sent(dlg.cloud)                                       # 없는 카드는 가리킨 것으로 치지 않는다
    finally:
        mgr.shutdown()


def test_phone_results_leave_a_shape_only_line_for_the_log():
    """실기기: 상품 화면을 못 열었는데 이유를 알 수 없었다. 결과의 꼴(낱말·개수)만 남기고 상품 이름·가격·주소·폰이 쓴 글은 남기지 않는다."""
    long = dict(P0, url="https://shop.example.com/p/100?" + "a" * 400)
    phone = Phone({"result": "ok", "views": [LIST]},
                  {"result": "ok", "views": [long], "skipped": [{"item": 3, "reason": "no_link"}, {"item": 1, "reason": "합성 비밀 글"}]})
    lines = []
    r = shop_drive(phone, Picker(), "생수 2L 6개", WISH, diag=lines.append)
    assert lines == ["search: result=ok status=- reason=- asked=0 list=1 rows=4/4 priced=3 openable=- product=0 card_miss=- skipped=-",
                     "open: result=ok status=- reason=- asked=2 list=0 rows=0/0 priced=0 openable=- product=1 card_miss=long_urlx1 skipped=no_linkx1,otherx1"]
    assert r.status == "completed" and "items" not in r.report            # 카드가 못 된 것은 카드로 내지 않는다
    assert not any(w in " ".join(lines) for w in ("합성", "4900", "shop.example"))
    assert shape({"type": "open", "items": [0]}, None, None) == "open: no_answer"
    assert shape({"type": "search"}, {"x": 1}, None) == "search: bad_result"
    lines.clear()
    shop_drive(Phone({"result": "limited", "status": 429, "reason": "rate", "views": []}), Picker(), "생수", WISH, diag=lines.append)
    assert lines == ["search: result=limited status=429 reason=rate asked=0 list=0 rows=0/0 priced=0 openable=- product=0 card_miss=- skipped=-"]

    def broken(line):
        raise RuntimeError("기록 실패")
    assert shop_drive(Phone({"result": "ok", "views": [LIST]}, {"result": "ok", "views": [P0]}), Picker(), "생수", WISH,
                      diag=broken).status == "completed"                    # 기록이 실패해도 일은 그대로 간다


def test_talk_knows_when_there_were_no_cards():
    """실기기: 카드가 없는 결과 뒤에 "카드를 눌러 직접 하세요"라고 답했다. 카드가 없었다는 것을 기록에 적는다."""
    dlg, mgr, sites, model, ran = make_cloud(SHOP, {"action": "reply", "replies": ["네."]})
    job_wait[0] = 5.0
    mgr._runner = shop_runner
    dlg.shop_drive = True
    try:
        r = dlg.handle("생수 사야 해", abilities=frozenset({"shop", "shop_drive"}))
        cid, jid = r["conversation_id"], r["action"]["task"]["id"]
        wait_browser(mgr, jid)
        mgr.browser_result(jid, "b1", {"id": "b1", "result": "ok", "views": [LIST]})
        pub = wait_browser(mgr, jid)
        while pub.get("browser", {}).get("id") != "b2":
            pub = wait_browser(mgr, jid)
        mgr.browser_result(jid, "b2", {"id": "b2", "result": "ok", "views": [], "skipped": [{"item": 0, "reason": "no_link"}]})
        job = done(mgr, jid)
        dlg.handle("주문하자", cid, abilities=frozenset({"shop", "shop_drive"}))
        out = sent(dlg.cloud)
        assert "[상품 카드는 없다 — 글로만 보여 줬다]" in out and "[보여 준 상품 카드: i1 합성" not in out
        assert "기록에 [상품 카드는 없다]라고 적힌 결과에는 누를 카드가 없다" in out
    finally:
        mgr.shutdown()


def test_task_state_says_which_path_it_is():
    """앱이 작업의 길을 알아야 맞는 표시를 고른다(실기기: 조회 작업에 메일 초안용 안내 줄이 붙었다). 일반 작업에는 키가 없다."""
    mgr = JobManager(shop_runner)
    try:
        j = mgr.submit("생수", None, title="쇼핑 화면에서 찾기", skill="shop_drive")
        assert j.public()["skill"] == "shop_drive"
        mgr.cancel(j.id)
        assert "skill" not in mgr.submit("메일 초안 써 줘", None).public()
    finally:
        mgr.shutdown()


def test_only_rows_the_app_can_open_are_picked_and_none_means_no_open_command():
    """앱이 줄마다 열 수 있는지를 알려 주면(can_open) 그 줄에서만 고른다. 실기기: 다른 몰 상품을 골라 열기가 헛돌았다."""
    def lst(flags):
        rows = [dict(r, can_open=f) for r, f in zip(LIST["fields"]["items"], flags)]
        return {"kind": "list", "url": LIST["url"], "fields": {"items": rows}}
    phone, model = Phone({"result": "ok", "views": [lst([False, True, False, True])]}, {"result": "ok", "views": [P3]}), Picker(items=(0, 3))
    r = shop_drive(phone, model, "생수 2L 6개", WISH)
    assert phone.got[1] == {"type": "open", "items": [3]}                    # 모델이 고른 0번은 열 수 없는 줄이라 버린다
    assert "합성 생수 2L 6개 —" not in said(model).split("[목록]")[1].split("\n\n")[0]   # 열 수 없는 줄은 고르는 모델에게 보이지 않는다
    assert r.status == "completed" and [i["title"] for i in r.report["items"]] == ["합성 생수 2L 12개"]
    phone, lines = Phone({"result": "ok", "views": [lst([False, False, False, False])]}), []
    r = shop_drive(phone, Picker(), "생수", WISH, diag=lines.append)
    assert len(phone.got) == 1 and r.status == "completed" and "바로 열 수 있는 상품 화면이 목록에 없었어요" in r.report["done"][0]
    assert "합성 생수 2L 6개 — 4,900원" in r.report["done"][0] and "openable=0" in lines[0]
    assert pick(None, WISH, list_items(lst([False, True, False, True]))) == [1, 3]   # 모델이 없어도 열 수 있는 줄에서만
    assert pick(None, WISH, list_items(LIST)) == [0, 1, 3]                           # 옛 앱(키 없음)은 예전 그대로
