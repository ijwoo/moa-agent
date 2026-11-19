"""D126: 로그인한 사이트에서 표 내려받기 — 처음 한 번 묻고, '이 사이트는 허락'이면 기억한다. 거절하면 내려받지 않는다."""

import pytest

from agent.browser.client import BrowserError
from agent.sites.inside import ALLOW_ONCE_DL, ALLOW_SITE_DL, NO_DL, AskBeforeDownload, downloads_allowed


class B:
    def __init__(self):
        self.ops = []

    def request(self, op, args=None):
        self.ops.append((op, args))
        return {"table_id": "t1"} if op == "download_table" else {}


class Site:
    key, name = "data-go-kr", "공공데이터포털"


class M:
    def __init__(self, path):
        self.download_ok_path = path


def test_first_download_asks_and_a_site_yes_is_remembered(tmp_path):
    m, b, asked = M(tmp_path / "ok.json"), B(), []
    w = AskBeforeDownload(b, Site, m, lambda q, c: asked.append(q) or ALLOW_SITE_DL, False)
    assert w.request("download_table", {"ref": "e1"}) == {"table_id": "t1"}
    assert asked and ("dialog_policy", {"downloads_ok": True}) in b.ops and downloads_allowed(m, "data-go-kr")
    w.request("download_table", {"ref": "e2"})
    assert len(asked) == 1                                                   # 같은 작업에서 다시 묻지 않는다


def test_once_is_not_remembered_and_no_refuses(tmp_path):
    m = M(tmp_path / "ok.json")
    w = AskBeforeDownload(B(), Site, m, lambda q, c: ALLOW_ONCE_DL, False)
    w.request("download_table", {"ref": "e1"})
    assert not downloads_allowed(m, "data-go-kr")
    b = B()
    w = AskBeforeDownload(b, Site, m, lambda q, c: NO_DL, False)
    with pytest.raises(BrowserError):
        w.request("download_table", {"ref": "e1"})
    assert not any(op == "download_table" for op, _ in b.ops)
    assert AskBeforeDownload(b, Site, m, None, False).request("observe") == {}   # 다른 요청은 그대로


def test_after_done_we_wait_for_the_human_check_to_clear():
    # 실기기 2026-11-18 claude.ai: '다 했어요' 2초 뒤 확인이 아직 돌고 있었다 — 사라질 때까지 기다린다
    from agent.sites import inside
    seq = iter(["Just a moment...", "Just a moment...", "본문"])

    class Bt:
        def request(self, op, args=None):
            return {"text": next(seq, "본문")}
    t = [0.0]
    assert inside.wait_cleared(Bt(), sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0]) is False


def test_a_site_that_kept_blocking_is_not_opened_again_and_the_phone_link_is_given(tmp_path):
    # D128(실기기 2026-11-18 claude.ai): 사람이 두 번 풀어도 사람 확인이 다시 떴다 → 다음엔 브라우저를 띄우지 않고 '폰에서 열기'
    import types
    from agent.sites import inside
    inside.use_walls(tmp_path / "walls.json")
    site = types.SimpleNamespace(key="ex-com", name="예시", check_url="https://ex.com/", login_urls={"password": "https://ex.com/login"})

    class NoOpen:
        def _open(self, *a, **k):
            raise AssertionError("열면 안 된다")
    assert inside.wall_of("ex-com") is None
    inside.note_wall("ex-com", "ex.com")
    r, shots = inside.inside_site(NoOpen(), site, None, "최근 대화 목록 보여줘", approver=None, said="최근 대화 목록 보여줘")
    assert r.status == "site_blocked" and r.report["links"][0]["url"] == "https://ex.com/"
    assert any("폰에서 예시를 열어 직접" in x and "최근 대화 목록" in x for x in r.report["blocked"])
    assert inside.wall_of("ex-com", now=__import__("time").time() + 8 * 86400) is None       # 7일 지나면 다시 해 본다
    assert inside.TRY_AGAIN.search("그래도 다시 해 봐")                                       # 사용자가 다시 해 보라면 연다
    inside.clear_wall("ex-com")
    assert inside.wall_of("ex-com") is None
    inside.use_walls(None)


def test_a_blocked_card_carries_the_phone_instruction_as_its_subtitle():
    # 앱 D110: 카드가 있으면 blocked 줄은 대화에 그리지 않는다 → '폰에서 열기' 안내를 부제로(D128)
    import types
    from agent.api.cards import make_card
    res = {"outcome": "site_blocked", "summary": "x", "report": {"blocked": ["예시가 사람 확인 화면을 띄웠어요. 그 확인은 제가 대신 누르지 않아요.",
                                                                          "폰에서 예시를 열어 직접 해 주세요: 최근 목록 보여줘"],
                                                              "sources": ["https://ex.com/"]}}
    card = make_card(types.SimpleNamespace(skill="site_do"), res, None, None)
    assert card["kind"] == "failed" and card["subtitle"].startswith("폰에서 예시를 열어") and card["actions"][0]["url"] == "https://ex.com/"
