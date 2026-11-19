"""Playwright AI 모드 접근성 스냅샷 → 에이전트 관찰 결과 형식 (Phase 2 Observation과 같은 형식).

입력 예 (page.locator("body").aria_snapshot(mode="ai")):
    - generic [active] [ref=e1]:
      - navigation [ref=e2]:
        - link "홈" [ref=e3] [cursor=pointer]:
          - /url: /
      - searchbox "상품 검색" [ref=e4]
      - paragraph [ref=e6]: 합계 64,900원
출력:
    [e3] link "홈"
    [e4] searchbox "상품 검색"
    [e6] paragraph "합계 64,900원"

표준 라이브러리만 쓴다 (컨테이너와 호스트 테스트 양쪽에서 import).
"""

from __future__ import annotations

import os
import re

# 구조만 나타내는 역할: 줄을 만들지 않는다 (자식은 그대로 처리).
STRUCTURAL_ROLES = frozenset({
    "generic", "navigation", "main", "banner", "contentinfo", "complementary", "region",
    "search", "form", "list", "listitem", "group", "article", "section", "document",
    "table", "rowgroup", "row", "separator", "none", "presentation", "LabelText",
})
# 이름과 별도로 '현재 값'을 갖는 입력 역할: [e3] textbox "받는 사람" = "test-a@mail.test"
VALUE_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton", "slider"})
# 이름 없이 나오면 버리는 역할
SKIP_IF_EMPTY = frozenset({"img", "cell", "columnheader", "rowheader", "status"})

MAX_LABEL = 200
# 모델에 주는 줄 수 한도. 실제 사이트는 화면이 커서(네이버 받은메일함 200줄 초과) 세션별로 OBS_MAX_LINES로 늘릴 수 있다.
# 기본값은 그대로 둔다(합성 사이트 측정을 바꾸지 않게)
MAX_LINES = int(os.environ.get("OBS_MAX_LINES", "200"))

_LINE = re.compile(
    r'^\s*-\s+(?P<role>[A-Za-z]+)'
    r'(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?'
    r'(?P<attrs>(?:\s+\[[^\]]*\])*)'
    r'(?::\s*(?P<text>.*))?$'
)
_TEXT_LINE = re.compile(r'^\s*-\s+text:\s*(?P<text>.*)$')
# Playwright ref: 첫 문서는 e12, 이동 후 문서는 f1e12처럼 프레임 번호가 붙는다 (1.64.1 실측).
_REF = re.compile(r"\[ref=((?:f\d+)?e\d+)\]")
_PROP = re.compile(r"^\s*-\s+/[a-z]+:")  # - /url: ... 같은 속성 줄


_QUOTED = re.compile(r"^(?P<lead>\s*-\s+)'(?P<body>(?:[^']|'')*)'(?P<rest>:.*|\s*)$")


def _unquote(raw: str) -> str:
    """스냅샷(YAML)의 한 줄에서 작은따옴표로 통째로 싸인 항목을 푼다.

    이름에 ": "가 들어간 요소는 줄 전체가 작은따옴표로 싸여 나온다 — `- 'button "마감일: 없음" [ref=e2]'`, 자식이 있으면 `- 'link "가격: …" [ref=e5]':`.
    이것을 못 읽어 그 요소가 관찰 결과에서 **통째로 빠졌다**(2026-11-15 합성 플래너로 찾음 — "마감일: 없음" 단추가 보이지 않았다.
    실제 사이트에서는 "가격: 12,000원" 링크, "Re: …" 제목, "공지: …" 머리글이 이 꼴이다). 따옴표 안의 작은따옴표는 두 번 겹쳐 나온다('').
    """
    m = _QUOTED.match(raw)
    if not m:
        return raw
    return m.group("lead") + m.group("body").replace("''", "'") + m.group("rest")


def _clean(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":   # 값이 작은따옴표로 싸여 나온 경우
        s = s[1:-1].replace("''", "'")
    s = s.strip('"')
    s = s.replace('\\"', '"')
    s = " ".join(s.split())
    return s[:MAX_LABEL] + ("…" if len(s) > MAX_LABEL else "")


CONTENT_LINE = re.compile(r'^\[e\d+\] (cell|gridcell|columnheader|rowheader|row|listitem|paragraph|heading|img|article|text|caption) ')


def _fit(out: list[str]) -> list[str]:
    """줄이 한도를 넘으면 덜 중요한 줄부터 뺀다: 글 줄 → 내용 요소(표의 칸·머리글·문단 등) → 그래도 넘치면 뒤의 누를 것.
    누를 수 있는 요소(단추·링크·칸·탭·보기)는 끝까지 남긴다. 순서는 그대로.
    (D121, 실기기 2026-11-18 KOSIS: 왼쪽 메뉴 나무와 큰 표가 줄을 다 써서 새로 열린 조회조건 창의 단추가 잘렸고, 모델이 "창이 열리지 않았다"고 했다)"""
    tiers = [[i for i, l in enumerate(out) if l.startswith("[e") and not CONTENT_LINE.match(l)],   # 누를 것
             [i for i, l in enumerate(out) if CONTENT_LINE.match(l)],                             # 내용 요소
             [i for i, l in enumerate(out) if not l.startswith("[e")]]                            # 글
    keep: set[int] = set()
    for tier in tiers:
        room = MAX_LINES - len(keep)
        if room <= 0:
            break
        keep |= set(tier[:room])
    return [line for i, line in enumerate(out) if i in keep] + ['text "(관찰 결과가 길어 표·글 일부를 줄였다 — 찾는 글은 find_in_page, 표·본문은 extract_text)"']


def convert_with_refs(snapshot: str) -> tuple[list[str], dict[str, str]]:
    """관찰 결과 줄들과 {모델용 ref: Playwright ref} 대응표.

    모델용 ref는 관찰할 때마다 e1부터 새로 매긴다. Playwright 내부 ref 형식(f1e12 등)에 모델이 묶이지 않고,
    최신 관찰 결과에 없는 ref는 대응표에 없으므로 서비스가 거절할 수 있다.
    """
    out: list[str] = []
    refmap: dict[str, str] = {}
    combo_idx: int | None = None  # 직전 combobox 줄 위치: 하위 option [selected]를 값으로 붙인다
    for raw in snapshot.splitlines():
        raw = _unquote(raw)
        if len(out) >= MAX_LINES * 3:   # 아주 긴 화면: 더 읽지 않는다(아래에서 줄인다)
            break
        if _PROP.match(raw):
            continue
        m_text = _TEXT_LINE.match(raw)
        if m_text:
            t = _clean(m_text.group("text"))
            if t:
                out.append(f'text "{t}"')
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        role = m.group("role")
        if role in STRUCTURAL_ROLES:
            # 틀 요소는 건너뛰되, 그 안에 바로 적힌 글은 일반 글로 옮긴다. 실제 네이버 메일(2026-11-12 측정)에서 메일을 받은 시각이
            # 이런 요소에 적혀 있어 모델에게 넘어가지 않았고, "오늘 온 메일"을 "없다"고 답했다(요소 목록 시각 0개 / 화면 글 30개)
            t = _clean(m.group("text") or "")
            if t:
                out.append(f'text "{t}"')
            continue
        if role == "option" and combo_idx is not None and "[selected]" in (m.group("attrs") or ""):
            line = out[combo_idx]
            if " = " not in line:
                out[combo_idx] = f'{line} = "{_clean(m.group("name") or "")}"'
        if role != "option":
            combo_idx = None
        ref_m = _REF.search(m.group("attrs") or "")
        name, text = m.group("name"), m.group("text")
        value = None
        if role in VALUE_ROLES and name is not None:
            label, value = _clean(name), _clean(text or "")
        elif role in VALUE_ROLES:
            # 이름표 없는 입력 칸: 뒤의 글은 그 칸의 값이다. 이름 자리에 쓰지 않는다 — 실기기: 넣은 값이 칸 이름처럼 보였다(승인 카드 "2026-11-16: 2026-11-16"),
            # 진행 설명("'…' 칸을 채웠어요")에도 값이 이름으로 들어갈 수 있었다
            label, value = "", _clean(text or "")
        else:
            label = _clean(name or text or "")
        if role in ("radio", "checkbox", "switch") and "[checked]" in (m.group("attrs") or ""):
            value = "선택됨"   # 고른 상태를 값으로 보여 준다(설정 화면에서 지금 무엇이 켜져 있는지 알아야 한다, D54)
        attrs = m.group("attrs") or ""
        if role == "button" and "[pressed]" in attrs and not value:
            value = "켜짐"     # 눌러 켜고 끄는 단추(별표·좋아요 등): 지금 켜져 있다 — 안 보이면 눌렀는지 몰라 다시 눌러 끈다(합성 c1: 별표를 네 번 눌렀다)
        elif role in ("button", "link", "combobox") and "[expanded]" in attrs and not value:
            value = "펼쳐짐"
        if not label and role in SKIP_IF_EMPTY:
            continue
        if ref_m:
            ref = f"e{len(refmap) + 1}"
            refmap[ref] = ref_m.group(1)
            out.append(f'[{ref}] {role} "{label}"' + (f' = "{value}"' if value else ""))
            if role in ("combobox", "listbox"):
                combo_idx = len(out) - 1
        elif label:
            out.append(f'text "{label}"')
    if len(out) > MAX_LINES:
        out = _fit(out)
    return out, refmap


def convert(snapshot: str) -> list[str]:
    return convert_with_refs(snapshot)[0]


def format_observation_with_refs(url: str, title: str, snapshot: str) -> tuple[str, dict[str, str]]:
    lines, refmap = convert_with_refs(snapshot)
    return "\n".join([f"URL: {url}", f"제목: {_clean(title)}", *lines]), refmap


def format_observation(url: str, title: str, snapshot: str) -> str:
    return format_observation_with_refs(url, title, snapshot)[0]
