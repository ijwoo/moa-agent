"""회의 앱의 "TXT 내보내기" 파일 → Meeting (D7).

형식:
    <제목>
    일시: <날짜·시간>
    녹음 길이: <길이>

    [요약]            ← 있을 때만
    <요약 본문>

    [대화]
    [MM:SS] <발화>    ← [H:MM:SS]도 허용. 타임스탬프 없는 줄은 앞 발화에 이어 붙인다.

실제 회의 데이터 규칙(D7): 이 파일로 만든 기록은 로컬 기억 저장소에만 둔다. 구조화는 로컬 모델로만 한다.
발화자 정보는 파일에 없으므로 추측하지 않는다(speaker=None).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .schema import Meeting, Utterance

_TS = re.compile(r"^\[(?:(\d{1,2}):)?(\d{1,3}):(\d{2})\]\s*(.*)$")
_DATE = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
MAX_BYTES = 5 * 1024 * 1024


class TxtFormatError(ValueError):
    pass


@dataclass
class ParsedTxt:
    meeting: Meeting
    app_summary: str        # 앱이 만든 [요약] 원문 (있으면)
    duration_text: str
    date_text: str


def parse_txt_export(text: str) -> ParsedTxt:
    if len(text.encode("utf-8", "ignore")) > MAX_BYTES:
        raise TxtFormatError("파일이 너무 크다 (5MB 초과)")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿").split("\n")
    nonempty = [l for l in lines if l.strip()]
    if not nonempty:
        raise TxtFormatError("빈 파일")
    title = nonempty[0].strip()
    date_text = duration_text = ""
    section = "head"
    summary_lines: list[str] = []
    utts: list[tuple[float, str]] = []
    for line in lines[lines.index(nonempty[0]) + 1:]:
        s = line.strip()
        if s == "[요약]":
            section = "summary"
            continue
        if s == "[대화]":
            section = "talk"
            continue
        if section == "head":
            if s.startswith("일시:"):
                date_text = s[3:].strip()
            elif s.startswith("녹음 길이:"):
                duration_text = s[6:].strip()
        elif section == "summary":
            summary_lines.append(line.rstrip())
        elif section == "talk":
            m = _TS.match(s)
            if m:
                h, mnt, sec, body = m.groups()
                utts.append((int(h or 0) * 3600 + int(mnt) * 60 + int(sec), body.strip()))
            elif s and utts:
                utts[-1] = (utts[-1][0], f"{utts[-1][1]} {s}".strip())
    if section != "talk" and not utts:
        raise TxtFormatError("[대화] 구역이 없다 — 회의 앱의 TXT 내보내기 파일이 아니다")
    utts = [(t, b) for t, b in utts if b]
    if not utts:
        raise TxtFormatError("[대화] 아래에 [MM:SS] 발화 줄이 없다")

    d = _DATE.search(date_text)
    date = f"{int(d.group(1)):04d}-{int(d.group(2)):02d}-{int(d.group(3)):02d}" if d else (date_text or "날짜 없음")
    # 같은 파일을 다시 가져오면 같은 id → 덮어쓴다
    mid = "t" + hashlib.sha256(f"{title}|{date_text}|{utts[0][1]}".encode()).hexdigest()[:8]
    utterances = []
    for i, (t, body) in enumerate(utts, 1):
        end = utts[i][0] if i < len(utts) else None
        utterances.append(Utterance(id=f"{mid}-u{i}", speaker=None, text=body, start_s=float(t),
                                    end_s=float(end) if end is not None else None))
    summary = "\n".join(summary_lines).strip()
    return ParsedTxt(Meeting(id=mid, title=title, date=date, utterances=utterances, summary=summary),
                     summary, duration_text, date_text)
