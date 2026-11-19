"""예약해 둔 일(루틴, D80): 사용자가 정한 때에 사용자가 정한 말을 대신 건넨다.

- 예약은 사용자가 만든다(대화에서 "매일 아침 8시에 …" 또는 앱의 화면). 모델이 혼자 만들지 않고, 예약된 일이 돌면서 다른 예약을 만들지도 못한다.
- 도는 것은 "그 말을 사용자가 지금 한 것"과 같다 — 같은 대화 판단·같은 안전장치를 지난다. 무엇을 바꾸는 일(보내기·누르기)은
  예약으로 돌아도 **승인 카드에서 멈춘다**(사용자가 알림을 보고 승인해야 나간다). 승인 없이 나가는 것은 없다.
- 맥이 그 시각에 켜져 있어야 한다. 놓친 때는 GRACE_S 안이면 늦게라도 한 번 돌리고, 더 지났으면 건너뛰고 "놓쳤다"고 적는다.
- 파일(0600)에는 예약(제목·할 말·지침·때·켜짐)과, 돌린 기록(그때의 답 포함 — 사용자 결정 2026-11-16)을 적는다.
  기록은 서버가 다시 떠도 남는다. 예약을 지우면 그 예약의 기록도 지운다. 기록은 MAX_RUNS개까지.
- 지침(notes): 예약마다 붙여 두는 긴 설명(무엇을 어떻게 볼지). 돌 때 할 말 뒤에 붙어 간다 — 사용자가 쓴 것이다.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

MAX_ROUTINES = 20
MAX_TIMES = 4            # 하루에 도는 횟수(한 예약)
MAX_PROMPT, MAX_TITLE = 300, 40
MAX_NOTES = 4000         # 예약에 붙여 두는 지침(여러 줄)
MAX_REPLY_CHARS = 6000   # 기록에 남기는 답 한 번의 글자 수
GRACE_S = 60 * 60        # 놓친 때를 늦게라도 돌리는 한도
TICK_S = 20.0
MAX_RUNS = 200           # 남겨 두는 실행 기록(전체)
BUSY_RETRY_S, BUSY_TRIES = 120.0, 5
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_KO = "월화수목금토일"
TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
ONCE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T]([01]?\d|2[0-3]):([0-5]\d)$")


class RoutineError(Exception):
    """code: invalid_when | invalid_input | too_many | not_found | past"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def parse_when(text: str, now: datetime | None = None) -> dict:
    """때를 적는 정해진 꼴 → {"kind", "times", "days", "at"}. 꼴이 아니면 RoutineError("invalid_when").

    - "daily 07:30" / "daily 11:00,17:30"
    - "weekly mon,wed,fri 09:00"
    - "once 2026-12-05 10:00"  (지난 때면 past)
    """
    parts = " ".join(str(text or "").lower().split()).split(" ")
    kind = parts[0] if parts else ""

    def times(s: str) -> list[str]:
        out = []
        for t in s.split(","):
            m = TIME.match(t.strip())
            if not m:
                raise RoutineError("invalid_when")
            out.append(f"{int(m.group(1)):02d}:{m.group(2)}")
        out = sorted(dict.fromkeys(out))
        if not out or len(out) > MAX_TIMES:
            raise RoutineError("invalid_when")
        return out

    if kind == "daily" and len(parts) == 2:
        return {"kind": "daily", "times": times(parts[1]), "days": [], "at": ""}
    if kind == "weekly" and len(parts) == 3:
        days = sorted({DAYS.index(d) for d in parts[1].split(",") if d in DAYS})
        if not days or len(days) != len([d for d in parts[1].split(",") if d]):
            raise RoutineError("invalid_when")
        return {"kind": "weekly", "times": times(parts[2]), "days": days, "at": ""}
    if kind == "once" and len(parts) == 3:
        m = ONCE.match(f"{parts[1]} {parts[2]}")
        if not m:
            raise RoutineError("invalid_when")
        try:
            at = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
        except ValueError:
            raise RoutineError("invalid_when") from None
        if at <= (now or datetime.now()):
            raise RoutineError("past")
        return {"kind": "once", "times": [], "days": [], "at": at.strftime("%Y-%m-%dT%H:%M")}
    raise RoutineError("invalid_when")


def _clock(t: str) -> str:
    h, m = int(t[:2]), t[3:]
    return f"{'오전' if h < 12 else '오후'} {h % 12 or 12}:{m}"


def describe(when: dict) -> str:
    """사람이 읽는 때: "매일 오전 7:30", "매주 월·수 오전 9:00", "2026년 12월 5일 오전 10:00에 한 번"."""
    if when["kind"] == "once":
        at = datetime.strptime(when["at"], "%Y-%m-%dT%H:%M")
        return f"{at.year}년 {at.month}월 {at.day}일 {_clock(at.strftime('%H:%M'))}에 한 번"
    ts = ", ".join(_clock(t) for t in when["times"])
    if when["kind"] == "weekly":
        return f"매주 {'·'.join(DAY_KO[d] for d in when['days'])} {ts}"
    return f"매일 {ts}"


@dataclass
class Routine:
    id: str
    title: str
    prompt: str                 # 그때 건넬 말(사용자가 지금 한 것처럼 대화 판단에 간다)
    kind: str                   # once | daily | weekly
    times: list[str] = field(default_factory=list)   # "HH:MM"
    days: list[int] = field(default_factory=list)    # weekly: 0=월 … 6=일
    at: str = ""                # once: "YYYY-MM-DDTHH:MM" (맥의 시각)
    enabled: bool = True
    created_at: float = 0.0
    checked_at: float = 0.0     # 이 시각까지의 때는 다뤘다(돌렸거나 건너뛰었다)
    last_run_at: float = 0.0
    last_status: str = ""       # done | task | needs_reply | needs_login | failed | busy | missed
    notes: str = ""             # 지침: 돌 때 할 말 뒤에 붙는 긴 설명(사용자가 쓴 것)

    def said(self) -> str:
        """돌 때 건네는 말: 할 말 + (있으면) 지침."""
        return self.prompt + (f"\n\n[이 예약에 내가 적어 둔 지침]\n{self.notes}" if self.notes.strip() else "")

    @property
    def when(self) -> dict:
        return {"kind": self.kind, "times": self.times, "days": self.days, "at": self.at}

    def due_between(self, start: float, end: float) -> float | None:
        """(start, end] 사이에 든 가장 늦은 때. 없으면 None."""
        if self.kind == "once":
            at = datetime.strptime(self.at, "%Y-%m-%dT%H:%M").timestamp()
            return at if start < at <= end else None
        best = None
        day = datetime.fromtimestamp(end).replace(hour=0, minute=0, second=0, microsecond=0)
        for back in range(0, 8):
            d = day - timedelta(days=back)
            if self.kind == "weekly" and d.weekday() not in self.days:
                continue
            for t in self.times:
                at = d.replace(hour=int(t[:2]), minute=int(t[3:])).timestamp()
                if start < at <= end and (best is None or at > best):
                    best = at
            if best is not None or d.timestamp() < start:
                break
        return best

    def next_after(self, now: float) -> float | None:
        if not self.enabled:
            return None
        if self.kind == "once":
            at = datetime.strptime(self.at, "%Y-%m-%dT%H:%M").timestamp()
            return at if at > now and at > self.checked_at else None
        day = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        for ahead in range(0, 9):
            d = day + timedelta(days=ahead)
            if self.kind == "weekly" and d.weekday() not in self.days:
                continue
            for t in self.times:
                at = d.replace(hour=int(t[:2]), minute=int(t[3:])).timestamp()
                if at > now:
                    return at
        return None

    def public(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        nxt = self.next_after(now)
        iso = lambda ts: datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None
        return {"id": self.id, "title": self.title, "prompt": self.prompt, "notes": self.notes, "enabled": self.enabled,
                "schedule": {"kind": self.kind, **({"times": self.times} if self.times else {}),
                             **({"days": [DAYS[d] for d in self.days]} if self.days else {}), **({"at": self.at} if self.at else {})},
                "when_text": describe(self.when), "next_run_at": iso(nxt), "last_run_at": iso(self.last_run_at),
                "last_status": self.last_status or None, "done": self.kind == "once" and nxt is None and bool(self.checked_at >= self.created_at and self.last_status)}


def _clean(text: str, limit: int) -> str:
    return " ".join("".join(ch for ch in str(text or "") if ch.isprintable() or ch in "\n ").split())[:limit].strip()


def _clean_notes(text: str) -> str:
    """지침: 줄바꿈은 두고 제어 글자만 뗀다."""
    lines = ["".join(ch for ch in ln if ch.isprintable() or ch == "\t").rstrip() for ln in str(text or "").replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip()[:MAX_NOTES]


class RoutineStore:
    """예약 목록. 파일(0600)에는 예약만 적는다."""

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time):
        self.path, self.clock = (Path(path) if path else None), clock
        self._lock = threading.RLock()
        self._items: dict[str, Routine] = {}
        if self.path is not None and self.path.exists():
            try:
                for r in json.loads(self.path.read_text()):
                    try:
                        item = Routine(**{k: r[k] for k in Routine.__dataclass_fields__ if k in r})
                        if item.kind in ("once", "daily", "weekly") and item.id and item.prompt:
                            self._items[item.id] = item
                    except (TypeError, KeyError):
                        continue
            except (ValueError, OSError):
                pass

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump([asdict(r) for r in self._items.values()], f, ensure_ascii=False, indent=1)

    def list(self) -> list[Routine]:
        with self._lock:
            return sorted(self._items.values(), key=lambda r: r.created_at)

    def get(self, rid: str) -> Routine:
        with self._lock:
            if rid not in self._items:
                raise RoutineError("not_found")
            return self._items[rid]

    def add(self, title: str, prompt: str, when: dict, notes: str = "") -> Routine:
        title, prompt = _clean(title, MAX_TITLE), _clean(prompt, MAX_PROMPT)
        if not prompt:
            raise RoutineError("invalid_input")
        with self._lock:
            if len(self._items) >= MAX_ROUTINES:
                raise RoutineError("too_many")
            now = self.clock()
            r = Routine("rt_" + secrets.token_urlsafe(8), title or prompt[:20], prompt, when["kind"], list(when.get("times") or []),
                        list(when.get("days") or []), when.get("at") or "", True, now, now, notes=_clean_notes(notes))
            self._items[r.id] = r
            self._save()
            return r

    def update(self, rid: str, *, enabled: bool | None = None, title: str | None = None, prompt: str | None = None,
               when: dict | None = None, notes: str | None = None) -> Routine:
        with self._lock:
            r = self.get(rid)
            if notes is not None:
                r.notes = _clean_notes(notes)
            if title is not None:
                r.title = _clean(title, MAX_TITLE) or r.title
            if prompt is not None:
                if not _clean(prompt, MAX_PROMPT):
                    raise RoutineError("invalid_input")
                r.prompt = _clean(prompt, MAX_PROMPT)
            if when is not None:
                r.kind, r.times, r.days, r.at = when["kind"], list(when.get("times") or []), list(when.get("days") or []), when.get("at") or ""
                r.checked_at = self.clock()
            if enabled is not None and enabled != r.enabled:
                r.enabled = bool(enabled)
                r.checked_at = self.clock()   # 꺼져 있던 동안 지난 때를 켜자마자 돌리지 않는다
            self._save()
            return r

    def delete(self, rid: str) -> None:
        with self._lock:
            self.get(rid)
            del self._items[rid]
            self._save()

    def mark(self, rid: str, *, checked_at: float | None = None, status: str | None = None, ran: bool = False) -> None:
        with self._lock:
            r = self._items.get(rid)
            if r is None:
                return
            if checked_at is not None:
                r.checked_at = checked_at
            if status is not None:
                r.last_status = status
            if ran:
                r.last_run_at = self.clock()
            self._save()

    def find(self, name: str) -> list[Routine]:
        """제목으로 찾기(대화에서 "아침 브리핑 꺼줘"): 똑같은 제목, 없으면 그 말이 든 제목."""
        n = "".join(str(name or "").lower().split())
        if not n:
            return []
        items = self.list()
        exact = [r for r in items if "".join(r.title.lower().split()) == n]
        return exact or [r for r in items if n in "".join(r.title.lower().split()) or "".join(r.title.lower().split()) in n]


class Scheduler:
    """때가 된 예약을 하나씩 돌린다. run(routine) → {"status", "conversation_id"?, "replies"?, "task_id"?}"""

    def __init__(self, store: RoutineStore, run: Callable[[Routine], dict], notify: Callable[[dict], None] | None = None,
                 clock: Callable[[], float] = time.time, tick_s: float = TICK_S, grace_s: float = GRACE_S,
                 busy_retry_s: float = BUSY_RETRY_S, sleep: Callable[[float], None] = time.sleep, runs_path: Path | None = None):
        self.store, self.run, self.notify, self.clock = store, run, notify, clock
        self.tick_s, self.grace_s, self.busy_retry_s, self._sleep = tick_s, grace_s, busy_retry_s, sleep
        self.runs: list[dict] = []
        self.runs_path = Path(runs_path) if runs_path else None   # 실행 기록 파일(0600). 없으면 메모리에만
        if self.runs_path is not None and self.runs_path.exists():
            try:
                self.runs = [x for x in json.loads(self.runs_path.read_text()) if isinstance(x, dict) and x.get("id")][-MAX_RUNS:]
            except (ValueError, OSError):
                self.runs = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._retry: dict[str, tuple[float, int, float]] = {}   # 예약 id → (다시 해 볼 때, 해 본 횟수, 원래의 때)
        self._thread: threading.Thread | None = None

    def start(self) -> "Scheduler":
        self._thread = threading.Thread(target=self._loop, name="routines", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:   # 예약 하나의 오류로 시간표가 멈추지 않게: 종류만 적는다
                sys.stderr.write(f"[routine] {time.strftime('%Y-%m-%d %H:%M:%S')} tick error={type(e).__name__}\n")
            self._stop.wait(self.tick_s)

    def _record(self, r: Routine, due: float, status: str, out: dict | None = None) -> dict:
        rec = {"id": "rr_" + secrets.token_urlsafe(8), "routine_id": r.id, "title": r.title, "status": status,
               "due_at": datetime.fromtimestamp(due).astimezone().isoformat(timespec="seconds"),
               "ran_at": datetime.fromtimestamp(self.clock()).astimezone().isoformat(timespec="seconds"),
               **{k: v for k, v in (out or {}).items() if k in ("conversation_id", "replies", "task_id", "action") and v}}
        if rec.get("replies"):
            rec["replies"] = [{**x, "text": str(x.get("text", ""))[:MAX_REPLY_CHARS]} for x in rec["replies"] if isinstance(x, dict)][:8]
        with self._lock:
            self.runs.append(rec)
            del self.runs[:-MAX_RUNS]
            self._save_runs()
        sys.stderr.write(f"[routine] {time.strftime('%Y-%m-%d %H:%M:%S')} id={r.id} kind={r.kind} status={status} "
                         f"late={max(0, int(self.clock() - due))}s\n")   # 제목·할 말·답은 적지 않는다
        return rec

    def tick(self) -> list[dict]:
        """지금 때가 된 예약을 돌린다(시험에서는 직접 부른다). 돌린 기록들을 돌려준다."""
        now, out = self.clock(), []
        for r in self.store.list():
            if not r.enabled:
                self._retry.pop(r.id, None)
                continue
            due, tries = None, 0
            if r.id in self._retry:
                at, tries, due0 = self._retry[r.id]
                if now < at:
                    continue
                due = due0
                del self._retry[r.id]
            else:
                due = r.due_between(r.checked_at, now)
                if due is None:
                    continue
                self.store.mark(r.id, checked_at=now)
                if now - due > self.grace_s:   # 맥이 꺼져 있었다 등: 너무 늦었으면 돌리지 않는다
                    self.store.mark(r.id, status="missed")
                    out.append(self._record(r, due, "missed"))
                    continue
            try:
                got = self.run(r)
            except Exception as e:
                sys.stderr.write(f"[routine] {time.strftime('%Y-%m-%d %H:%M:%S')} id={r.id} run error={type(e).__name__}\n")
                got = {"status": "failed"}
            status = got.get("status") or "failed"
            if status == "busy" and tries + 1 < BUSY_TRIES:   # 다른 일이 도는 중: 조금 뒤에 다시
                self._retry[r.id] = (now + self.busy_retry_s, tries + 1, due)
                continue
            self.store.mark(r.id, status=status, ran=True)
            rec = self._record(r, due, status, got)
            out.append(rec)
            if self.notify is not None and status != "task":   # 작업이 된 것은 작업의 알림이 따로 간다
                try:
                    self.notify(rec)
                except Exception:
                    pass
        return out

    def _save_runs(self) -> None:
        """기록을 파일에 적는다(잠근 채로 부른다). 못 적어도 도는 것은 계속된다."""
        if self.runs_path is None:
            return
        try:
            self.runs_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.runs_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.runs, f, ensure_ascii=False)
        except OSError as e:
            sys.stderr.write(f"[routine] {time.strftime('%Y-%m-%d %H:%M:%S')} runs save error={type(e).__name__}\n")

    def run_now(self, rid: str) -> None:
        """그 예약을 지금 한 번 돌린다(사용자가 카드의 "지금 가져오기"를 눌렀다). 뒤에서 돌고, 기록·알림은 때가 되어 돈 것과 같다. 없으면 RoutineError."""
        r = self.store.get(rid)

        def go():
            try:
                got = self.run(r)
            except Exception as e:
                sys.stderr.write(f"[routine] {time.strftime('%Y-%m-%d %H:%M:%S')} id={r.id} run error={type(e).__name__}\n")
                got = {"status": "failed"}
            status = got.get("status") or "failed"
            self.store.mark(r.id, status=status, ran=True)
            rec = self._record(r, self.clock(), status, got)
            if self.notify is not None and status != "task":
                try:
                    self.notify(rec)
                except Exception:
                    pass
        threading.Thread(target=go, name="routine-now", daemon=True).start()

    def recent(self, limit: int = 20, routine_id: str | None = None) -> list[dict]:
        with self._lock:
            runs = [x for x in self.runs if routine_id is None or x.get("routine_id") == routine_id]
            return list(reversed(runs[-limit:]))

    def find_run(self, run_id: str) -> dict | None:
        with self._lock:
            return next((x for x in self.runs if x.get("id") == run_id), None)

    def task_finished(self, task_id: str, status: str, lines: list[str]) -> None:
        """예약이 만든 작업이 끝났다: 그 결과를 기록에 붙인다(작업은 서버가 다시 뜨면 사라지므로)."""
        with self._lock:
            rec = next((x for x in self.runs if x.get("task_id") == task_id), None)
            if rec is None:
                return
            rec["task_status"] = status
            rec["task_result"] = [str(x)[:MAX_REPLY_CHARS] for x in lines if str(x).strip()][:8]
            self._save_runs()

    def task_followup(self, conversation_id: str | None, task_id: str, status: str, lines: list[str]) -> dict | None:
        """예약이 만든 작업이 멈췄다가(로그인이 풀림·질문) **같은 대화에서 이어 한 작업**이 끝났다: 그 결과를 그 실행 기록에 붙인다 → 그 기록(없으면 None).
        실기기 2026-11-16: 예약이 돌 때 로그인이 풀려 있었고, 사용자가 다시 로그인해 이어 읽은 결과가 기록에도 카드에도 들어가지 않았다."""
        if not conversation_id or status != "completed":
            return None
        with self._lock:
            rec = next((x for x in reversed(self.runs) if x.get("conversation_id") == conversation_id
                        and (x.get("status") in ("needs_login", "needs_reply") or x.get("task_status") in ("failed", "needs_input"))), None)
            if rec is None:
                return None
            rec["task_id"], rec["task_status"], rec["status"] = task_id, status, "task"
            rec["task_result"] = [str(x)[:MAX_REPLY_CHARS] for x in lines if str(x).strip()][:8]
            self._save_runs()
            return dict(rec)

    def forget(self, routine_id: str) -> None:
        """예약을 지웠다: 그 예약의 기록도 지운다."""
        with self._lock:
            self.runs = [x for x in self.runs if x.get("routine_id") != routine_id]
            self._save_runs()
