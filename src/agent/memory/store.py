"""사용자별 기억 저장소: SQLite 파일 하나 + 글자 2-gram BM25 검색 (표준 라이브러리만).

검색 방식 선택(D30): 한국어는 조사·어미가 붙어 단어 단위 일치가 약하다("회의에서" ≠ "회의").
형태소 분석기·임베딩 모델을 새로 설치하지 않고, 글자 2-gram으로 부분 일치를 잡는다. 개인 회의 기록 규모에서는 전체를 훑어도 충분하다(측정).
저장 암호화는 미구현(D27과 같음) — 합성 데이터만 넣는다.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from .schema import Hit, Item, Meeting, Utterance

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (id TEXT PRIMARY KEY, title TEXT, date TEXT, topic TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS utterances (id TEXT PRIMARY KEY, meeting_id TEXT, idx INTEGER, speaker TEXT,
                                       start_s REAL, end_s REAL, text TEXT);
CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id TEXT, kind TEXT, text TEXT,
                                  assignee TEXT, due_text TEXT, done INTEGER, sources TEXT);
CREATE TABLE IF NOT EXISTS task_results (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, task TEXT,
                                         status TEXT, summary TEXT);
"""
_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)
K1, B = 1.2, 0.75
# 결정·할 일은 발화 원문보다 정리된 정보라 같은 점수면 먼저 보여 준다.
KIND_BOOST = {"decision": 1.15, "todo": 1.15, "question": 1.1, "summary": 1.0, "utterance": 1.0}


def bigrams(text: str) -> list[str]:
    s = _STRIP.sub("", text.lower())
    return [s[i:i + 2] for i in range(len(s) - 1)] if len(s) > 1 else ([s] if s else [])


class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript(_SCHEMA)
        if str(path) != ":memory:":
            self.path.chmod(0o600)

    # ---- 쓰기 ----
    def add_meeting(self, m: Meeting) -> None:
        """같은 id의 회의가 있으면 통째로 바꾼다."""
        with self.db:
            for t in ("utterances", "items"):
                self.db.execute(f"DELETE FROM {t} WHERE meeting_id = ?", (m.id,))
            self.db.execute("INSERT OR REPLACE INTO meetings VALUES (?,?,?,?,?)",
                            (m.id, m.title, m.date, m.topic, m.summary))
            self.db.executemany(
                "INSERT INTO utterances VALUES (?,?,?,?,?,?,?)",
                [(u.id, m.id, i, u.speaker, u.start_s, u.end_s, u.text) for i, u in enumerate(m.utterances, 1)])
            self.db.executemany(
                "INSERT INTO items (meeting_id, kind, text, assignee, due_text, done, sources) VALUES (?,?,?,?,?,?,?)",
                [(m.id, it.kind, it.text, it.assignee, it.due_text, int(it.done), json.dumps(it.sources))
                 for it in m.items])

    def add_task_result(self, task: str, status: str, summary: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO task_results (created_at, task, status, summary) VALUES (?,?,?,?)",
                            (datetime.now().isoformat(timespec="seconds"), task, status, summary))

    # ---- 읽기 ----
    def meetings(self) -> list[tuple[str, str, str]]:
        return self.db.execute("SELECT id, title, date FROM meetings ORDER BY date").fetchall()

    def get_utterances(self, ids: list[str]) -> list[Utterance]:
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        rows = self.db.execute(
            f"SELECT id, speaker, text, start_s, end_s FROM utterances WHERE id IN ({q}) ORDER BY meeting_id, idx", ids
        ).fetchall()
        return [Utterance(id=r[0], speaker=r[1], text=r[2], start_s=r[3], end_s=r[4]) for r in rows]

    def items(self, kind: str | None = None, only_open: bool = False) -> list[tuple[str, Item]]:
        sql = "SELECT meeting_id, kind, text, assignee, due_text, done, sources FROM items"
        cond, args = [], []
        if kind:
            cond.append("kind = ?"); args.append(kind)
        if only_open:
            cond.append("done = 0")
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        return [(r[0], Item(kind=r[1], text=r[2], assignee=r[3], due_text=r[4], done=bool(r[5]),
                            sources=json.loads(r[6]))) for r in self.db.execute(sql, args)]

    def task_results(self, limit: int = 20) -> list[tuple[str, str, str, str]]:
        return self.db.execute(
            "SELECT created_at, task, status, summary FROM task_results ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def _docs(self) -> list[Hit]:
        meta = {r[0]: (r[1], r[2], r[3], r[4]) for r in
                self.db.execute("SELECT id, title, date, topic, summary FROM meetings")}
        docs: list[Hit] = []
        for mid, (title, date, topic, summary) in meta.items():
            if summary:
                docs.append(Hit(kind="summary", text=f"{topic}. {summary}" if topic else summary, meeting_id=mid,
                                meeting_title=title, date=date, sources=[], score=0))
        for uid, mid, speaker, text in self.db.execute("SELECT id, meeting_id, speaker, text FROM utterances"):
            title, date = meta[mid][0], meta[mid][1]
            docs.append(Hit(kind="utterance", text=text, meeting_id=mid, meeting_title=title, date=date,
                            sources=[uid], score=0, speaker=speaker))
        for mid, it in self.items():
            title, date = meta[mid][0], meta[mid][1]
            docs.append(Hit(kind=it.kind, text=it.text, meeting_id=mid, meeting_title=title, date=date,
                            sources=it.sources, score=0, assignee=it.assignee))
        return docs

    def search(self, query: str, k: int = 6, kinds: tuple[str, ...] | None = None) -> list[Hit]:
        docs = [d for d in self._docs() if kinds is None or d.kind in kinds]
        if not docs:
            return []
        # 색인 문서 = 회의 제목 + 발화자/담당자 + 본문 (제목으로 묻는 질문을 잡기 위해)
        toks = [Counter(bigrams(f"{d.meeting_title} {d.speaker or ''} {d.assignee or ''} {d.text}")) for d in docs]
        n = len(docs)
        avg = sum(sum(t.values()) for t in toks) / n
        df: Counter = Counter()
        for t in toks:
            df.update(t.keys())
        q = Counter(bigrams(query))
        scored = []
        for d, t in zip(docs, toks):
            dl = sum(t.values())
            s = 0.0
            for g in q:
                f = t.get(g, 0)
                if f:
                    idf = math.log(1 + (n - df[g] + 0.5) / (df[g] + 0.5))
                    s += idf * f * (K1 + 1) / (f + K1 * (1 - B + B * dl / avg))
            if s > 0:
                scored.append(d.model_copy(update={"score": round(s * KIND_BOOST[d.kind], 4)}))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:k]

    def close(self) -> None:
        self.db.close()


def format_hits(hits: list[Hit]) -> str:
    """모델에 줄 기억 발췌. 각 줄에 근거 번호를 붙인다."""
    label = {"utterance": "발화", "decision": "결정", "todo": "할 일", "question": "미해결", "summary": "요약"}
    lines = []
    for h in hits:
        who = f" {h.speaker}:" if h.speaker else (f" (담당 {h.assignee})" if h.assignee else "")
        src = ",".join(h.sources) if h.sources else h.meeting_id
        lines.append(f"[{src}] {h.date} {h.meeting_title} · {label[h.kind]}{who} {h.text}")
    return "\n".join(lines)


def user_store_path(user_id: str) -> Path:
    """사용자별 기억 파일 경로 (data/는 git 제외). 사용자 id는 해시 키로만 경로에 쓴다."""
    from ..browser.container import REPO_ROOT
    from ..sandbox.manager import user_key
    return REPO_ROOT / "data" / "users" / user_key(user_id) / "memory.db"
