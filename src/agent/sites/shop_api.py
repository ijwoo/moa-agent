"""쇼핑: 공식 쇼핑 검색 API로 상품을 찾아 카드로 정리한다 (D71). 브라우저를 열지 않는다 — 폰에도 맥에도 화면이 뜨지 않는다.

**주의 (2026-11-15에 확인): 아래 fetch가 부르는 네이버 검색 API의 쇼핑 검색은 2026-07-31 24:00에 종료됐다**(개발자센터 공지 2026-07-27 —
'Search API' 중 쇼핑·책·학술정보 제공 종료, 기존 이용자도 8월 1일부터 못 쓴다. 대체 API 없음). 이 길은 서버에서 켜지 않는다.
이 모듈을 만들 때 문서 페이지만 읽고 공지를 읽지 않았고, 실제로 불러 보지도 않았다 — 사용자가 알려 줘서 알았다.
shop_find(검색 함수를 받아 카드로 정리하는 흐름)는 원천과 무관하므로 남겨 둔다. fetch는 다른 원천으로 바꾸기 전에는 쓰지 않는다.

- 왜: 네이버 쇼핑은 맥의 브라우저를 막았고(D65), 폰의 앱 안 브라우저에는 첫 요청 405·로그인 화면·보안문자를 냈다(D68 실기기 4번, 카드 0장).
  사용자가 처음에 정한 대로("브라우저로 못 얻으면 … 공식 쇼핑 검색 API를 쓸지 내가 정한다") 공식 API로 바꿨다.
- 나가는 것: 검색어(Sonnet이 쓴 상품 낱말)만 네이버 검색 API로 간다. 열어 볼 것 고르기·견준 말은 Claude(haiku) — 상품 이름·가격·판매처만 본다(공개 상품 정보).
  상품 주소·그림 주소는 Claude로 보내지 않는다. 카드에만 싣는다.
- 값은 API가 준 그대로다. **배송비·옵션·도착일은 API에 없다** — 지어 쓰지 않고 카드에서 확인하게 한다. 가격은 lprice(가격비교 상품이면 여러 판매처의 최저가).
- 중고·단종·판매예정 상품(productType 4~12)과 렌탈·해외직구는 뺀다.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable

from .public import is_public_https
from .shop import MAX_OPEN, MAX_URL, _lines, _one, pick, say

API_URL = "https://openapi.naver.com/v1/search/shop.json"
DISPLAY = 20            # 한 번에 받는 상품 수(고르는 모델이 보는 목록)
SHOW_WHEN_NONE = 5      # 조건에 맞는 것이 없다고 봤을 때 글로 보여 주는 줄 수
_TAG = re.compile(r"<[^>]{0,40}>")


class ShopApiError(Exception):
    """쇼핑 검색을 하지 못했다. code: not_set(키 없음) | auth(키가 틀렸거나 검색 API 권한 없음) | limited(하루 한도·요청 제한) | unreachable | bad_response."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def fetch(query: str, client_id: str, client_secret: str, *, get: Callable | None = None, timeout: float = 10.0) -> list[dict]:
    """쇼핑 검색 API를 한 번 부른다 → items(원본 그대로). 키는 헤더로만 가고 기록하지 않는다."""
    if not client_id or not client_secret:
        raise ShopApiError("not_set")
    if get is None:
        import httpx
        get = httpx.get
    try:
        r = get(API_URL, params={"query": query, "display": DISPLAY, "start": 1, "sort": "sim", "exclude": "used:rental:cbshop"},
                headers={"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}, timeout=timeout)
    except Exception:   # 연결 오류의 글에는 주소·헤더가 섞일 수 있어 종류만 남긴다
        raise ShopApiError("unreachable") from None
    if r.status_code in (401, 403):
        raise ShopApiError("auth")
    if r.status_code == 429:
        raise ShopApiError("limited")
    if r.status_code != 200:
        raise ShopApiError("bad_response")
    try:
        items = r.json().get("items")
    except (ValueError, AttributeError):
        raise ShopApiError("bad_response") from None
    return items if isinstance(items, list) else []


def rows(items: list) -> list[dict]:
    """API의 items → 목록 줄. 꼴이 맞는 것만: 이름, 공개 https 주소, 0보다 큰 가격. 일반 상품(productType 1~3)만."""
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = _one(html.unescape(_TAG.sub("", str(it.get("title") or ""))), 120)
        url = it.get("link") if isinstance(it.get("link"), str) else ""
        try:
            price, ptype = int(it.get("lprice") or 0), int(it.get("productType") or 0)
        except (TypeError, ValueError):
            continue
        if not title or price <= 0 or price >= 10**10 or ptype not in (1, 2, 3) or len(url) > MAX_URL or not is_public_https(url):
            continue
        row = {"title": title, "url": url, "price": price}
        mall = _one(html.unescape(str(it.get("mallName") or "")), 40)
        if mall:
            row["mall"] = mall
        image = it.get("image") if isinstance(it.get("image"), str) else ""
        if image and len(image) <= MAX_URL and is_public_https(image):
            row["image"] = image
        if ptype == 1:   # 가격비교 상품: 값은 여러 판매처 가운데 최저가다(그 판매처의 값이 아니다)
            row["detail"] = "가격비교 최저가"
        out.append(row)
    return out


def shop_find(search: Callable[[str], list], model, query: str, wish: str, *, tone: str | None = None, on_step=None, cancel_check=None):
    """검색 → 맞는 것 고르기 → 카드와 견준 말. search(검색어) → API의 items(못 하면 ShopApiError).

    RunResult: completed(report.items 카드 + report.done 견준 말. 결과가 없을 때도 그렇게 말하고 completed), search_failed(검색을 못 함, reason에 종류 — 앱은 provider_error를 '맥의 모델을 못 불렀다'로 보여 줘서 이름을 따로 둔다).
    """
    from ..core.loop import RunResult, StepLog

    t0 = time.perf_counter()
    steps: list = []

    def log(tool: str, status: str) -> None:
        e = StepLog(len(steps) + 1, tool, {}, "ok", status, 0, 0.0, 0.0, None, label="")
        steps.append(e)
        if on_step is not None:
            on_step(e)

    def done(status: str, summary: str, **kw):
        return RunResult(status, summary, steps=steps, elapsed_s=time.perf_counter() - t0, **kw)

    q = " ".join(str(query).split())[:80]
    try:
        listed = rows(search(q)) if q else []
    except ShopApiError as e:
        log("shop_api", "error")
        why = {"not_set": "쇼핑 검색 키가 아직 없어요. 맥에 키를 넣은 뒤 다시 말씀해 주세요.",
               "auth": "쇼핑 검색 키가 맞지 않거나 검색 API 권한이 없어요.",
               "limited": "쇼핑 검색의 오늘 한도를 넘었어요. 내일 다시 해 주세요."}.get(e.code, "지금은 쇼핑 검색을 하지 못했어요. 잠시 뒤에 다시 해 주세요.")
        return done("search_failed", why, reason=e.code, report={"done": [], "blocked": [why]})
    log("shop_api", "ok" if listed else "error")
    if cancel_check is not None and cancel_check():
        return done("cancelled", "작업을 취소했어요", report={"done": [], "blocked": []})
    if not listed:
        text = "쇼핑 검색 결과가 없었어요. 다른 낱말로 다시 말씀해 주세요."
        return done("completed", text, report={"done": [text], "blocked": [], "by": "mac"})
    seen = [{k: v for k, v in r.items() if k not in ("url", "image")} for r in listed]   # 고르는 모델은 이름·가격·판매처만 본다
    order = pick(model, wish, seen)
    if not order:   # 조건에 맞는 것이 없다고 봤다: 앞쪽 몇 줄만 글로
        text, by = say(model, wish, seen[:SHOW_WHEN_NONE], tone, opened=False, api=True)
        text = "조건에 딱 맞는 상품은 찾지 못했어요. 검색 결과의 앞쪽은 이래요." if by == "mac" else text
        return done("completed", text, report={"done": [text + "\n" + _lines(seen[:SHOW_WHEN_NONE], 1)], "blocked": [], "by": by})
    items = [{"id": f"i{n}", **listed[i]} for n, i in enumerate(order[:MAX_OPEN], 1)]
    text, by = say(model, wish, [{k: v for k, v in it.items() if k not in ("url", "image")} for it in items], tone, opened=True, api=True)
    return done("completed", text, report={"done": [text], "blocked": [], "items": items, "by": by})
