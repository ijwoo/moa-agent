"""통화 중(D116): 앱이 client_features에 voice_call을 보내면 말 규칙이 붙고, 말풍선마다 읽을 글(speak), 끝난 작업마다 report.speak."""

from agent.api import cloud
from agent.api.dialog import _stamp, speakable
from agent.api.jobs import speak_line


def test_speakable_strips_emoji_markdown_and_links():
    assert speakable("네! **결과** 바로 읽어 올게요 📊\n- 첫째 https://example.com/x") == "네! 결과 바로 읽어 올게요 첫째"


def test_stamp_adds_speak_only_in_a_call():
    out = _stamp({"replies": [{"text": "확인해 볼게요 👍"}]}, voice=True)
    assert out["replies"][0]["speak"] == "확인해 볼게요"
    assert "speak" not in _stamp({"replies": [{"text": "확인해 볼게요 👍"}]})["replies"][0]


def test_voice_rule_reaches_the_prompt_only_in_a_call():
    kw = dict(history=[], contacts={}, me={}, results={}, context=None, send_context=False, capabilities="", tone=None, now=None,
              state=[], pages={}, sites={}, mail_brief=False)
    on, _ = cloud.outgoing("채널 어때?", voice=True, **kw)
    off, _ = cloud.outgoing("채널 어때?", **kw)
    assert "통화 중" in on[0].content and "승인 카드" in on[0].content and "통화 중" not in off[0].content


def test_speak_line_prefers_given_then_done_and_cuts_at_a_sentence():
    assert speak_line({"report": {"done": ["x"]}}, "주어진 문장이에요.") == "주어진 문장이에요."
    assert speak_line({"report": {"done": ["**결제 3건**을 찾았어요 🙂"]}}) == "결제 3건을 찾았어요"
    long = "가" * 100 + "요. " + "나" * 100
    assert speak_line({"report": {"done": [long]}}) == "가" * 100 + "요."
    assert speak_line({"summary": "끝냈어요", "report": {}}) == "끝냈어요"
    assert speak_line({"report": {"card": {"kind": "failed", "title": "로그인이 풀렸어요"}, "done": []}}) == "로그인이 풀렸어요"


def test_speakable_drops_symbols_laughs_and_tildes():
    # 실기기 2026-11-18(빌드 86): 통화에서 "이모지를 읽어"
    assert speakable("좋아요 ⭐⏰ ▶ 다음 → 끝 ✅ ㅋㅋ ^^ 그럼요~") == "좋아요 다음 끝 그럼요"
    assert speakable("조회수 · 구독자") == "조회수, 구독자"
    assert speakable("👍🏻 네!") == "네!"
