import pytest

from agent.memory.txt_import import TxtFormatError, parse_txt_export

SAMPLE = """﻿주간 회의 (합성)
일시: 2026. 11. 7. 오후 3:00
녹음 길이: 12분 30초

[요약]
다음 회의 일정을 정했다.
- 11월 17일 오후 3시

[대화]
[00:05] 다음 주간 회의 일정을 정하겠습니다.
[00:42] 11월 17일 오후 3시가 괜찮습니다.
그리고 장소는 소회의실 A로 하죠.
[1:02:03] 마무리하겠습니다.
"""


def test_parse_full_export():
    p = parse_txt_export(SAMPLE)
    m = p.meeting
    assert m.title == "주간 회의 (합성)" and m.date == "2026-11-07"
    assert p.duration_text == "12분 30초" and "11월 17일" in p.app_summary
    assert [u.text for u in m.utterances] == [
        "다음 주간 회의 일정을 정하겠습니다.",
        "11월 17일 오후 3시가 괜찮습니다. 그리고 장소는 소회의실 A로 하죠.",
        "마무리하겠습니다.",
    ]
    assert [u.start_s for u in m.utterances] == [5.0, 42.0, 3723.0]
    assert m.utterances[0].end_s == 42.0 and m.utterances[-1].end_s is None
    assert all(u.speaker is None for u in m.utterances)
    assert m.utterances[1].id == f"{m.id}-u2" and m.id.startswith("t")


def test_same_file_gives_same_id_and_summary_is_optional():
    no_summary = SAMPLE.replace("[요약]\n다음 회의 일정을 정했다.\n- 11월 17일 오후 3시\n\n", "")
    a, b = parse_txt_export(no_summary), parse_txt_export(no_summary)
    assert a.meeting.id == b.meeting.id and a.app_summary == ""
    assert parse_txt_export("제목\n일시: 날짜 미상\n[대화]\n[00:01] 안녕").meeting.date == "날짜 미상"


@pytest.mark.parametrize("bad", ["", "제목만", "제목\n일시: x\n본문만 있고 구역 없음", "제목\n[대화]\n타임스탬프 없는 줄"])
def test_rejects_non_export_files(bad):
    with pytest.raises(TxtFormatError):
        parse_txt_export(bad)
