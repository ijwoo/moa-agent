"""D122(사용자 결정 2026-11-18): 공개 길의 가벼운 운전자가 포기하면, 검색으로 넘어가기 전에 더 센 모델이 같은 브라우저에서 한 번 더 한다."""

from contextlib import contextmanager

from test_agent_loop import C, FakeBrowser, Scripted

from agent.sites import public

PAGE = 'URL: https://air.example/\n[e1] textbox "검색어 입력 : 새창열림"\n[e2] button "검색"'


class Box:
    def __init__(self):
        self.b = FakeBrowser(PAGE)

    @contextmanager
    def session(self, user, env=None):
        yield self.b


def run(driver_calls, strong_calls, monkeypatch=None):
    weak, strong = Scripted(driver_calls), (Scripted(strong_calls) if strong_calls is not None else None)
    r, _ = public.browse_public(Box(), weak, None, "강남구 측정소의 어제 PM10 최고값", strong=strong)
    return r, weak, strong


def test_a_stronger_driver_tries_once_more_when_the_light_one_gives_up():
    long = "강남구 측정소의 11월 17일 PM10 최고값은 15시 62㎍/㎥예요. " + "시간별 값을 표에서 읽어 비교했어요. " * 8
    r, weak, strong = run([C("observe"), C("finish", summary="강남구 측정소를 찾지 못했어요.")],
                          [C("observe"), C("finish", summary=long)])
    assert r.browse["escalated"] is True and r.summary.startswith("강남구 측정소의 11월 17일")
    assert strong.seen and any("앞서 이 일을 하다 멈췄다" in (m.content or "") for m in strong.seen[0])


def test_no_second_try_when_the_light_driver_succeeds_or_no_strong_driver():
    long = "답이에요. " * 60
    r, _, strong = run([C("observe"), C("finish", summary=long)], [C("finish", summary="안 쓰임")])
    assert r.browse["escalated"] is False and r.browse["retried"] is False and not strong.seen
    # (D124) 센 모델이 없으면 같은 운전자가 한 번 더 — 멈추며 적은 방법부터 해 보라고
    weak = Scripted([C("observe"), C("finish", summary="찾지 못했어요.", blocked=["단지명으로 다시 검색해야 해요"]),
                     C("observe"), C("finish", summary="답이에요. " * 60)])
    r, _ = public.browse_public(Box(), weak, None, "대치동 전세 최고가")
    assert r.browse["escalated"] is False and r.browse["retried"] is True and r.summary.startswith("답이에요")
    assert any("멈추며 적은 것: 단지명으로 다시 검색해야 해요" in (m.content or "") for m in weak.seen[-1])


def test_sites_refused_on_earlier_days_do_not_block_the_second_try(monkeypatch):
    # 실기기 2026-11-18 15:51: 전에 막은 사이트 목록(정부24 등)이 있어 "이번에 막혔다"로 잘못 보고 한 번도 넘기지 않았다
    monkeypatch.setattr(public, "_refused", {"gov.kr", "korail.com"})
    long = "답이에요. " * 60
    r, _, strong = run([C("observe"), C("finish", summary="찾지 못했어요.")], [C("observe"), C("finish", summary=long)])
    assert r.browse["escalated"] is True and strong.seen


def test_a_long_answer_whose_first_line_says_it_could_not_is_a_give_up():
    long = "강남구 측정소의 최고값은 확인하지 못했어요.\n" + "- 주소 팝업으로만 연결됐어요. " * 20
    r, _, strong = run([C("observe"), C("finish", summary=long)], [C("observe"), C("finish", summary="답이에요. " * 60)])
    assert r.browse["escalated"] is True and strong.seen


def test_a_web_answer_that_came_out_in_english_is_asked_again_in_korean():
    # 실기기 2026-11-18(실거래가): 검색으로 대신 낸 답이 영어였다
    from agent.api.cloud import web_answer

    class P:
        def __init__(self):
            self.n, self.last_results = 0, []

        def search(self, system, question, max_uses=2):
            self.n += 1
            if self.n == 1:
                return "I'm sorry, but I couldn't verify the exact answer from the official site today.", [], 1, 10
            return "공식 사이트에서는 확인하지 못했어요.", [], 1, 10
    p = P()
    bubbles, _, _ = web_answer(p, "잠실동 최고가 거래")
    assert p.n == 2 and "확인하지 못했어요" in bubbles[0]


def test_values_read_on_the_site_are_kept_when_falling_back_to_search():
    # 실기기 2026-11-18 기상청: 10일치를 표에서 읽고도 검색 답으로 통째로 바뀌었다
    from agent.sites.public import after_giving_up
    said, came = after_giving_up("31일 중 10일만 확인했어요. 나머지는 확인하지 못했어요.", False,
                                 lambda: "10월 12일 30.1도였어요. 기사 기준이에요.", ["10월 1~10일 최고기온: 1일 24.3, 5일 26.1, 10일 22.8"])
    assert came == "search" and "사이트에서 직접 확인한 것:" in said and "5일 26.1" in said
