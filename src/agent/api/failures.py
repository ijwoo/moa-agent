"""실사용 실패 기록(2026-11-18): 끝내지 못한 작업의 '꼴'만 맥 파일에 쌓는다 — 어떤 기능·사이트에서 무엇 때문에 멈췄나.

부탁의 글·답·화면 내용·주소의 경로는 남기지 않는다(실제 내용은 기록하지 않는다). 남기는 것: 시각, 기능, 결과·까닭, 사이트 키(또는 공개 길의 호스트),
단계 수, 걸린 시간, 공개 길의 지표(막힌 곳·포기·검색으로 대신함). 이것을 보고 합성 재현 시나리오(evaluation/real_use_failures.jsonl)를 만든다.
보기: .venv/bin/python scripts/failures_report.py [--days 7]
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from pathlib import Path

PATH = Path(os.path.expanduser("~/.local/share/local-ai-agent/failures.jsonl"))
KEEP = 2000
OK = ("completed",)


class FailureLog:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or PATH)
        self._lock = threading.Lock()

    def record(self, job, status: str, result: dict, run=None) -> bool:
        outcome = (result or {}).get("outcome") or status
        if status in OK and outcome in OK:
            return False
        browse = getattr(run, "browse", None) or {}
        row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "skill": job.skill or "agent", "status": status, "outcome": outcome,
               "reason": (result or {}).get("reason") or getattr(run, "reason", None),
               "site": getattr(run, "site", None), "steps": len(job.steps),
               "secs": round(float(getattr(run, "elapsed_s", 0) or 0), 1)}
        if browse:
            row["browse"] = {k: browse.get(k) for k in ("blocked", "gave_up", "fell_back", "escalated", "retried", "error_ops", "denied") if k in browse}
        line = json.dumps(row, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            try:   # 오래된 줄은 버린다
                lines = self.path.read_text(encoding="utf-8").splitlines()
                if len(lines) > KEEP * 1.2:
                    tmp = self.path.with_suffix(".tmp")
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines[-KEEP:]) + "\n")
                    os.replace(tmp, self.path)
            except OSError:
                pass
        return True

    def rows(self, days: float = 7) -> list[dict]:
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * 86400))
        try:
            out = [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()]
        except (OSError, ValueError):
            return []
        return [r for r in out if r.get("at", "") >= since]

    def summary(self, days: float = 7) -> dict:
        rs = self.rows(days)
        return {"total": len(rs),
                "by_kind": Counter(f"{r.get('skill')}:{r.get('outcome')}" + (f"/{r['reason']}" if r.get("reason") else "") for r in rs).most_common(15),
                "by_site": Counter(r.get("site") or "-" for r in rs).most_common(10)}
