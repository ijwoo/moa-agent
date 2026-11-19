"""로그인된 실제 네이버 메일을 로컬 모델이 '읽을' 수 있는지 측정 (D52 다음 단계). 읽기만 한다.

- 사용자가 앱에서 직접 로그인해 보관한 로그인 상태를 새 브라우저에 되살려 쓴다(없으면 중단).
- 정책: mail.naver.com만, 읽기·초안 전용(read_draft) — 보내기·삭제 같은 버튼은 누를 수 없다. 승인 요청은 모두 거절한다.
- **메일 내용은 출력·기록하지 않는다**(실제 개인 데이터, D7). 판정은 코드가 화면 글과 대조해 참/거짓·숫자만 남긴다.
사용법: .venv/bin/python scripts/bench_naver_read.py [--only n-01]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from agent.cli import _make_sites
from agent.core.loop import AgentLoop, Limits
from agent.providers.qwen import QwenProvider
from agent.sites import NAVER
from agent.tools.browser import agent_registry
from agent.tools.observation import Observation
from agent.tools.validate import Policy

ROOT = Path(__file__).resolve().parent.parent
HOME = "https://mail.naver.com/"
TASKS = [
    ("n-01", "list", "네이버 메일의 받은메일함에서 맨 위에 있는 메일의 제목을 알려줘."),
    ("n-02", "open", "네이버 메일의 받은메일함에서 맨 위에 있는 메일을 열어서 내용을 두 문장으로 요약해줘."),
    ("n-03", "search", "네이버 메일에서 '네이버'로 메일을 검색해서 결과 맨 위에 있는 메일의 제목을 알려줘."),
    # 사용자 요청(2026-11-12): 루틴 브리핑에 쓸 '오늘 온 메일 요약'
    ("n-04", "today", "네이버 메일의 받은메일함에서 오늘 온 메일들을 확인하고, 누가 어떤 메일을 보냈는지 메일마다 한 줄씩 요약해줘. "
                      "오늘 온 메일이 없으면 없다고 말해줘."),
]
TODAY_TIME = re.compile(r"(오전|오후)\s*\d{1,2}:\d{2}|\b\d{1,2}:\d{2}\b")   # 목록에서 오늘 온 메일은 날짜 대신 시각으로 보인다(추정)
WORD = re.compile(r"[가-힣A-Za-z0-9]{2,}")


class Recorder:
    """브라우저 요청을 지나가며 화면 글을 모아 둔다(판정용, 메모리에만)."""

    def __init__(self, inner):
        self.inner, self.labels, self.texts, self.urls, self.obs_chars = inner, [], [], [], []
        self.today: list[int] = []     # 화면마다 '오전/오후 H:MM'(오늘 받은 시각 표시) 수
        self.rows: list[list[tuple[str, str]]] = []   # 화면마다 [(제목, 날짜·시각 표시)] — 판정용, 출력하지 않는다(날짜 표시만 출력)

    def request(self, op, args=None):
        r = self.inner.request(op, args)
        if op == "observe":
            o = Observation.parse(r["text"])
            self.labels.append([e.label.strip() for e in o.elements.values() if e.role in ("link", "heading") and len(e.label.strip()) >= 6])
            self.obs_chars.append(len(r["text"]))
            self.rows.append(mail_rows(r["text"]))
            self.today.append(len(TODAY.findall(r["text"])))
            self.urls.append(r.get("url") or "")
        if op == "extract_text":
            self.texts.append(r.get("text", ""))
        return r


TODAY = re.compile(r"(?:오전|오후)\s*\d{1,2}:\d{2}")   # 네이버 메일 목록: 오늘 온 메일은 '오전/오후 시각', 그 전은 날짜로 보인다(사용자 확인)
DATE = re.compile(r"\d{4}\.\s?\d{1,2}\.\s?\d{1,2}\.?|(?:오전|오후)\s*\d{1,2}:\d{2}|\b\d{1,2}:\d{2}\b|\b\d{1,2}\.\d{1,2}\.?(?!\d)")


def mail_rows(obs_text: str) -> list[tuple[str, str]]:
    """받은메일함 관찰 결과에서 메일 줄을 뽑는다: (제목 링크의 글, 그 줄의 받은 시각 표시).

    실측한 뼈대: checkbox … link(제목, 14자 이상) … 그 뒤 같은 줄 안에 받은 시각 글('오전 11:30' 또는 '11.11 21:34').
    제목은 링크의 글 그대로 쓴다(앞뒤에 붙는 말이 있어도 겹침 비율로 판정한다).
    """
    lines = [l.strip() for l in obs_text.splitlines()]
    rows: list[list[str]] = []
    for l in lines:
        m = re.match(r'^\[e\d+\]\s+(\S+)\s+"(.*?)"', l)
        if m and m.group(1) == "checkbox":
            rows.append(["", ""])          # 새 메일 줄 시작
        elif m and m.group(1) == "link" and len(m.group(2)) >= 14 and rows and not rows[-1][0]:
            rows[-1][0] = m.group(2)
        elif l.startswith('text "') and rows and rows[-1][0] and not rows[-1][1]:
            t = TODAY.search(l) or re.search(r"\d{1,2}\.\d{1,2}\.?\s+\d{1,2}:\d{2}|\d{4}\.\s?\d{1,2}\.\s?\d{1,2}", l)
            if t and len(l) <= 30:
                rows[-1][1] = t.group(0)
    return [(t, d) for t, d in rows if t]


def best_match(answer: str, titles: list[str]) -> tuple[int | None, float]:
    """답에 가장 길게 겹치는 제목의 순번과, 그 제목 길이 대비 겹친 길이(0~1)."""
    from difflib import SequenceMatcher
    best, score = None, 0.0
    for i, t in enumerate(titles):
        if len(t) < 4:
            continue
        m = SequenceMatcher(None, answer, t, autojunk=False).find_longest_match(0, len(answer), 0, len(t))
        r = m.size / len(t)
        if r > score:
            best, score = i, r
    return best, round(score, 2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    model = os.environ.get("QWEN_MODEL", "qwen3:14b")
    provider = QwenProvider(model=model, tool_mode="json", think=True, num_ctx=8192, timeout=300.0, default_max_tokens=3000)
    policy = Policy(allowed_hosts=frozenset({"mail.naver.com"}), navigate_urls=frozenset({HOME}), mode="read_draft")
    sites = _make_sites()
    print(f"## 네이버 메일 읽기 측정 — {model}, think 켬 (내용은 출력하지 않는다)")
    try:
        for tid, kind, task in TASKS:
            if args.only and tid not in args.only.split(","):
                continue
            t0 = time.perf_counter()
            with sites.sandbox.session(sites._uid(NAVER)) as raw:
                if not sites.restore(raw, NAVER):
                    print("보관한 로그인 상태가 없다. 앱에서 먼저 로그인한다.")
                    return 3
                b = Recorder(raw)
                loop = AgentLoop(provider, b, agent_registry(), policy, {"네이버 메일": HOME}, lambda req: False,
                                 limits=Limits(max_seconds=600.0, max_steps=14))
                run = loop.run(task, task_id=tid)
                final_text = raw.request("extract_text", {"ref": None}).get("text", "")
            answer = " ".join([run.summary, *run.report.get("done", [])])
            seen = [l for page in b.labels for l in page]
            hit = next((l for l in seen if l in answer), None)                 # 답에 화면에 있던 제목(링크 글)이 그대로 들어 있는가
            first = b.labels[0] if b.labels else []
            words = set(WORD.findall(answer))
            pool = " ".join([final_text, *b.texts])
            overlap = round(sum(w in pool for w in words) / max(1, len(words)), 2)   # 답의 낱말 중 실제 화면 글에 있던 비율
            paths = [urlsplit(u).path for u in b.urls]
            mentioned = len({l for l in seen if l in answer})                    # 답에 그대로 들어 있는 화면의 제목(링크 글) 수
            today_guess = len(TODAY_TIME.findall(b.texts[0] if b.texts else final_text))   # 화면 글에서 '시각'으로 보이는 것의 수(오늘 온 메일 수 추정)
            page = next((r for r in b.rows if r), []) if kind != "search" else (b.rows[-1] if b.rows else [])
            rank, score = best_match(answer, [t for t, _ in page])
            n_match = sum(best_match(answer, [t])[1] >= 0.6 for t, _ in page)      # 답에 60% 이상 겹쳐 들어 있는 제목 수
            res = {"id": tid, "kind": kind, "status": run.status, "steps": len(run.steps), "s": round(time.perf_counter() - t0),
                   "tools": [s.tool for s in run.steps], "step_status": [s.result_status or s.check_status for s in run.steps],
                   "answer_chars": len(run.summary), "title_in_answer": hit is not None,
                   "title_rank_on_first_page": (first.index(hit) if hit in first else None),
                   "answer_words_seen_on_page": overlap, "opened_mail": any("/read" in p for p in paths),
                   "searched": any("search" in p for p in paths), "obs_chars_max": max(b.obs_chars or [0]),
                   "left_mail_host": any(urlsplit(u).hostname not in ("mail.naver.com", None) for u in b.urls),
                   "approvals_asked": len(run.approvals), "llm_calls": run.llm_calls,
                   "titles_mentioned": mentioned, "today_times_on_page_guess": today_guess,
                   "answer_lines": len([l for l in run.summary.splitlines() if l.strip()]),
                   "blocked": len(run.report.get("blocked", [])),
                   "rows_on_page": len(page), "best_title_rank": rank, "best_title_overlap": score, "titles_matched_60": n_match,
                   "says_none": bool(re.search(r"없", answer)),
                   "today_rows_in_observation": (b.today[0] if b.today else 0),
                   "matched_ranks": [i for i, (t, _) in enumerate(page) if best_match(answer, [t])[1] >= 0.6],
                   "row_time_kinds_first12": ["오늘" if TODAY.search(d) else ("날짜" if d else "?") for _, d in page[:12]],
                   "today_rows": sum(bool(TODAY.search(d)) for _, d in page),
                   "today_matched": sum(bool(TODAY.search(d)) and best_match(answer, [t])[1] >= 0.6 for t, d in page),
                   "not_today_matched": sum(not TODAY.search(d) and best_match(answer, [t])[1] >= 0.6 for t, d in page)}
            strict = lambda t: best_match(answer, [t])[1] >= 0.95     # 제목이 거의 글자 그대로(번호만 다른 비슷한 제목을 가르려고)
            res["today_exact"] = sum(bool(TODAY.search(d)) and strict(t) for t, d in page)
            res["not_today_exact"] = sum(not TODAY.search(d) and strict(t) for t, d in page)
            # 다시 판정할 때 쓰는 원본(답·제목·시각)은 이 맥의 파일에만 둔다(권한 600, runs/는 git 제외). 출력하지 않는다
            priv = ROOT / "runs" / "naver_read_private.jsonl"
            fd = os.open(priv, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps({"id": tid, "answer": answer, "rows": page}, ensure_ascii=False) + "\n")
            print(json.dumps(res, ensure_ascii=False), flush=True)
            with (ROOT / "runs" / "naver_read.jsonl").open("a") as f:   # 숫자·참거짓만. 내용 없음
                f.write(json.dumps({**res, "model": model}, ensure_ascii=False) + "\n")
    finally:
        sites.sandbox.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
