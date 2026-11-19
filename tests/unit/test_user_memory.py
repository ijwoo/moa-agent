import json

from agent.memory.user import UserMemory, digest, scrub, IDLE_S, SOON_S


def test_secrets_are_scrubbed_before_anything_is_kept():
    t = scrub("비밀번호는 hunter22 이고 카드 1234-5678-9012-3456, 주민 900101-1234567, 인증번호 482913, 키 sk-abcdefghijklmnopqrstu")
    for bad in ("hunter22", "1234-5678-9012-3456", "900101-1234567", "482913", "sk-abcdefghij"):
        assert bad not in t
    assert scrub("내일 9시에 강남역에서 만나") == "내일 9시에 강남역에서 만나"


def test_archive_search_finds_old_turns_with_the_reply(tmp_path):
    now = [1_000_000.0]
    m = UserMemory(tmp_path, clock=lambda: now[0])
    m.add("c1", "user", "다음 달 제주 여행은 애월 쪽 숙소로 알아봐 줘")
    m.add("c1", "assistant", "애월 숙소 세 곳을 찾았어요")
    m.add("c2", "user", "오늘 날씨 어때")
    hits = m.search("제주 숙소 어디였지")
    assert hits and "애월" in hits[0]["text"] and "세 곳" in hits[0]["next"]
    assert m.search("없는말") == []
    assert m.stats()["turns"] == 3


def test_digest_runs_after_idle_or_soon_after_remember(tmp_path):
    now = [1_000_000.0]
    m = UserMemory(tmp_path, clock=lambda: now[0])
    m.add("c1", "user", "나 출근은 9시야")
    assert not m.due()
    now[0] += IDLE_S
    assert m.due()
    m2 = UserMemory(tmp_path / "b", clock=lambda: now[0])
    m2.add("c1", "user", "이거 기억해줘: 커피는 아이스 아메리카노")
    now[0] += SOON_S
    assert m2.due()


class FakeModel:
    def __init__(self, profile):
        self.profile, self.seen = profile, None

    def generate(self, msgs, **kw):
        self.seen = msgs
        class R:
            text = json.dumps({"profile": self.profile, "changes": ["출근 시간"]})
        return R()


def test_digest_rewrites_the_profile_scrubs_it_and_keeps_one_undo(tmp_path):
    now = [1_000_000.0]
    m = UserMemory(tmp_path, clock=lambda: now[0])
    m.set_profile("## 나\n- 개발자")
    m.add("c1", "user", "나 출근은 9시야")
    fm = FakeModel("## 나\n- 개발자\n- 출근 9시\n- 비밀번호는 abc123")
    assert digest(fm, m)["changes"] == ["출근 시간"]
    assert "출근 9시" in m.profile() and "abc123" not in m.profile()
    assert "출근은 9시" in fm.seen[1].content and "개발자" in fm.seen[1].content
    assert digest(fm, m) is None                     # 새 대화가 없으면 부르지 않는다
    assert "출근" not in m.undo_profile()            # 한 단계 되돌리기
    assert oct((tmp_path / "profile.md").stat().st_mode)[-3:] == "600"


def test_undo_without_a_previous_version_is_an_error(tmp_path):
    import pytest
    m = UserMemory(tmp_path)
    with pytest.raises(LookupError):
        m.undo_profile()
    m.set_profile("a")
    with pytest.raises(LookupError):   # 처음 쓴 것에는 전 것이 없다
        m.undo_profile()
    m.set_profile("b")
    assert m.undo_profile() == "a" and m.undo_profile() == "b"   # 한 단계 — 다시 누르면 둘 사이를 오간다


def test_forget_all(tmp_path):
    m = UserMemory(tmp_path)
    m.set_profile("## 나\n- x")
    m.add("c", "user", "안녕")
    m.forget_all()
    assert m.profile() == "" and m.stats()["turns"] == 0


def test_the_profile_goes_with_each_turn_and_recall_is_offered():
    from agent.api.cloud import outgoing
    msgs, _ = outgoing("주말에 뭐 할까", history=[], contacts={}, me={}, results={}, context=None, send_context=False,
                       capabilities="", tone=None, now="2026-11-18", state=[], pages={}, sites={}, mail_brief=False,
                       profile="## 좋아하는 것\n- 등산", recall=True)
    assert "등산" in msgs[1].content and "recall —" in msgs[0].content and "다시 묻지 않고" in msgs[0].content
    msgs, _ = outgoing("주말에 뭐 할까", history=[], contacts={}, me={}, results={}, context=None, send_context=False,
                       capabilities="", tone=None, now="2026-11-18", state=[], pages={}, sites={}, mail_brief=False)
    assert "기억하는 것" not in msgs[1].content and "recall —" not in msgs[0].content
