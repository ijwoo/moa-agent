"""활동(D103): 앱의 '활동' 탭과 전역 검색이 쓰는 맥 쪽 기록.

- 작업은 서버 메모리에만 있어 다시 뜨면 사라진다 → 끝난 작업을 짧게 파일(activity.json, 0600)에 남긴다.
  남기는 것: 제목·길(skill)·상태·결과 종류·카드 제목/부제, 그리고 **개인 결과가 아닐 때만** 답의 첫 줄.
  로그인한 사이트에서 읽은 결과(private)는 내용을 남기지 않는다(사이트 읽기의 원칙 그대로).
- 활동 목록 = 지금 사람을 기다리는 작업 + 남긴 작업 기록 + 예약 실행 기록, 최근 것부터.
- 검색 = 맥에 있는 것만(작업 기록·예약·예약 실행 기록). 회의 원문은 폰에만 있으니 폰이 찾는다(D7).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

PATH = Path(os.path.expanduser("~/.local/share/local-ai-agent/activity.json"))
KEEP = 300
LINE = 200
WAITING = ("awaiting_approval", "awaiting_input", "send_pending")


class ActivityLog:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or PATH)
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items[-KEEP:], f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def record(self, job, status: str, result: dict) -> None:
        rep = result.get("report") or {}
        card = rep.get("card") or {}
        private = bool(rep.get("private"))
        done = [str(x) for x in rep.get("done") or [] if str(x).strip()]
        line = "" if private else " ".join((done[0] if done else (rep.get("blocked") or [""])[0] or "").split())[:LINE]
        item = {"id": job.id, "at": job.updated_at, "created_at": job.created_at, "title": job.title or job.task[:30],
                "skill": job.skill, "status": status, "outcome": result.get("outcome"), "private": private,
                "card_kind": card.get("kind"), "card_title": card.get("title"), "card_subtitle": card.get("subtitle"), "line": line}
        if job.conversation_id:
            item["conversation_id"] = job.conversation_id
        with self._lock:
            items = [x for x in self._load() if x.get("id") != job.id]
            items.append({k: v for k, v in item.items() if v not in (None, "")})
            self._save(items)

    def all(self) -> list[dict]:
        with self._lock:
            return self._load()


def _ts(item: dict) -> float:
    """작업 시각(UTC)·예약 시각(맥의 시간대)이 섞여 있다 — 글자가 아니라 시각으로 견준다."""
    try:
        return datetime.fromisoformat(str(item.get("at"))).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _task_item(x: dict) -> dict:
    return {"kind": "task", "id": x["id"], "at": x.get("at"), "title": x.get("title"), "status": x.get("status"),
            **{k: x[k] for k in ("outcome", "card_kind", "card_title", "card_subtitle", "line", "conversation_id", "skill", "private") if k in x}}


def _run_item(r: dict) -> dict:
    replies = [str(x.get("text") if isinstance(x, dict) else x) for x in r.get("replies") or []]
    line = " ".join(" ".join(r.get("task_result") or replies).split())[:LINE]
    out = {"kind": "routine", "id": r.get("id"), "at": r.get("ran_at") or r.get("due_at"), "title": r.get("title"),
           "status": r.get("task_status") or r.get("status"), "routine_id": r.get("routine_id"), "line": line}
    for k in ("task_id", "conversation_id"):
        if r.get(k):
            out[k] = r[k]
    return out


def activity(log: ActivityLog, live: list[dict], runs: list[dict], limit: int = 50) -> dict:
    """→ {"waiting": [지금 기다리는 작업], "items": [작업·예약 실행, 최근 것부터]}"""
    waiting = [{"kind": "task", "id": t["id"], "at": t.get("updated_at"), "title": t.get("title") or t.get("task", "")[:30], "status": t["status"],
                **({"conversation_id": t["conversation_id"]} if t.get("conversation_id") else {})} for t in live if t.get("status") in WAITING]
    wait_ids = {w["id"] for w in waiting}
    items = [_task_item(x) for x in log.all() if x.get("id") not in wait_ids] + [_run_item(r) for r in runs]
    items.sort(key=_ts, reverse=True)
    return {"waiting": waiting, "items": items[:limit]}


def search(q: str, log: ActivityLog, runs: list[dict], routines: list[dict], limit: int = 30) -> list[dict]:
    """맥에 있는 것에서 낱말 찾기(대소문자·띄어쓰기 무시). 회의는 폰이 찾는다."""
    words = [w for w in q.lower().split() if w]
    if not words:
        return []

    def hit(*texts) -> str | None:
        joined = " ".join(" ".join(str(t or "").split()) for t in texts)
        low = joined.lower()
        if not all(w in low for w in words):
            return None
        i = low.find(words[0])
        start = max(0, i - 40)
        return ("…" if start else "") + joined[start:start + 140] + ("…" if start + 140 < len(joined) else "")

    out = []
    for x in log.all():
        s = hit(x.get("title"), x.get("card_title"), x.get("card_subtitle"), x.get("line"))
        if s is not None:
            out.append({**_task_item(x), "snippet": s})
    for r in runs:
        it = _run_item(r)
        s = hit(it.get("title"), it.get("line"))
        if s is not None:
            out.append({**it, "snippet": s})
    for r in routines:
        s = hit(r.get("title"), r.get("prompt"), r.get("notes"))
        if s is not None:
            out.append({"kind": "routine_def", "id": r.get("id"), "title": r.get("title"), "snippet": s, "at": r.get("last_run_at")})
    out.sort(key=_ts, reverse=True)
    return out[:limit]
