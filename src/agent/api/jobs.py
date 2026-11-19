"""작업 관리 (Phase 8 축소판, D40): 요청 → 진행 상태 → 승인 대기(초안 내용) → 승인/거부 → 결과.

- 작업은 한 번에 하나씩 실행한다(로컬 모델이 하나). 나머지는 대기열에서 기다리고, 대기열이 차면 Busy.
- 승인 대기는 길어질 수 있으므로 앱은 작업 ID로 상태를 조회한다. 승인 대기 시간은 작업 시간 제한에 넣지 않는다.
- 상태는 메모리에만 있다(서버를 재시작하면 사라진다). 앱이 보낸 회의 발췌(context)는 조회 응답에 넣지 않고 로그에도 남기지 않는다.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..core.approval import draft_sha256

TERMINAL = ("completed", "needs_input", "failed", "cancelled")
MAX_ANSWER_CHARS = 1000
SITE_NAMES = {"mail.test": "테스트 메일", "intranet.test": "테스트 인트라넷", "shop.test": "테스트 쇼핑몰",
              "booking.test": "테스트 회의실 예약"}
UNANSWERED_NOTE = ("사용자가 {m}분 동안 답하지 않았다. 질문의 답이 없어도 할 수 있는 부분부터 진행하고, "
                   "답이 없어 하지 못한 부분은 finish의 blocked에 이유와 함께 적는다. 같은 질문을 다시 하지 않는다.")


def _josa(word: str, with_final: str, without_final: str) -> str:
    """받침 있으면 with_final(을·이), 없으면 without_final(를·가). 한글이 아니면 '을(를)' 형태."""
    ch = word.rstrip("'\" )")[-1:] if word else ""
    if not ("가" <= ch <= "힣"):
        return f"{with_final}({without_final})"
    return with_final if (ord(ch) - 0xAC00) % 28 else without_final


MAX_ATTACHMENTS = 4   # 긴 결과 화면은 창 높이씩 나눠 3장까지 + 1(2026-11-17)
ATTACHMENT_TTL_S = 24 * 3600.0


def jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    """JPEG 머리에서 (가로, 세로)를 읽는다. 못 읽으면 (None, None)."""
    i, n = 2, len(data)
    if data[:2] != b"\xff\xd8":
        return None, None
    while i + 9 < n:
        if data[i] != 0xFF:
            return None, None
        marker, size = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + size
    return None, None


def narrate(entry) -> str | None:
    """단계 기록 → 사람이 읽는 진행 설명. 입력한 내용(초안 본문 등)은 넣지 않고 칸·버튼 이름만 쓴다."""
    status = getattr(entry, "result_status", None) or getattr(entry, "check_status", None)
    tool, label = getattr(entry, "tool", None), getattr(entry, "label", None)
    if status in ("policy_denied",):
        return "할 수 없는 동작이라 다른 방법을 찾고 있어요"
    if status not in ("ok",):
        return None
    if tool == "compose_mail":
        return "메일을 보냈어요"
    if tool == "navigate":
        from urllib.parse import urlsplit
        host = urlsplit((getattr(entry, "args", None) or {}).get("url", "") or "").hostname or ""
        name = SITE_NAMES.get(host, "사이트")
        return f"{name}{_josa(name, '을', '를')} 열었어요"
    if tool == "click":
        return f"'{label}'{_josa(label, '을', '를')} 눌렀어요" if label else "화면의 항목을 눌렀어요"
    if tool == "type":
        return f"'{label}' 칸을 채웠어요" if label else "입력칸을 채웠어요"
    if tool == "select_option":
        return f"'{label}'에서 골랐어요" if label else "항목을 골랐어요"
    if tool == "extract_text":
        return "화면 내용을 읽었어요"
    if tool == "scroll":
        return "화면을 넘겨 봤어요"
    if tool == "shop_search":
        return "쇼핑 검색 결과를 읽었어요"
    if tool == "shop_open":
        return "상품 화면을 열어 읽었어요"
    if tool == "shop_api":
        return "쇼핑 검색 결과를 받았어요"
    return None
SEND_FAIL_TEXT = {
    "auth_failed": "메일 계정에 로그인하지 못했어요(앱 비밀번호 확인이 필요해요).",
    "not_set": "메일 계정이 아직 연결되지 않았어요.",
    "unreachable": "메일 서버에 연결하지 못했어요.",
    "rate_limited": "메일 발송 횟수 제한에 걸렸어요.",
    "not_allowed": "받는 분의 주소를 확인하지 못해 보내지 않았어요(내 주소, 연락처에 있는 분, 직접 말씀하신 주소로만 보내요).",
    "rejected": "메일 서버가 메일을 받지 않았어요.",
}
MAX_TASK_CHARS = 2000
MAX_CONTEXT_CHARS = 8000
MAX_REASON_CHARS = 500



SPEAK_CHARS = 160


def speak_line(result: dict, given: str | None = None) -> str:
    """끝난 작업을 소리로 알릴 한두 문장(통화 중, D116). 실행한 쪽이 준 것(카드처럼 숫자를 코드가 고른 것)이 먼저,
    없으면 카드의 제목·부제 → 해낸 것 첫 줄 → 요약. 이모지·링크·마크다운은 뺀다."""
    from .dialog import speakable
    rep = result.get("report") or {}
    card = rep.get("card") if isinstance(rep.get("card"), dict) else {}
    cands = [given or ""]
    if card.get("kind") == "failed":
        cands.append(" ".join(x for x in (card.get("title"), card.get("subtitle")) if isinstance(x, str)))
    cands += [next((x for x in rep.get("done") or [] if isinstance(x, str) and x.strip()), ""),
              " ".join(x for x in (card.get("title"), card.get("subtitle")) if isinstance(x, str)),
              next((x for x in rep.get("blocked") or [] if isinstance(x, str) and x.strip()), ""), str(result.get("summary") or "")]
    for c in cands:
        t = speakable(c)
        if t:
            if len(t) > SPEAK_CHARS:   # 문장 끝에서 자른다
                cut = max(t.rfind(e, 0, SPEAK_CHARS) for e in (". ", "요 ", "다 "))
                t = t[:cut + 1] if cut > 40 else t[:SPEAK_CHARS]
            return t.strip()
    return ""

class Busy(Exception):
    pass


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


class Gone(Exception):
    """캡처가 지워졌거나 기한이 지났다."""


class HashMismatch(Conflict):
    """승인 요청에 실린 sha256이 고정한 초안의 해시와 다르다 (앱이 본 초안과 서버의 초안이 다름)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Hooks:
    on_step: Callable[[Any], None]
    approver: Callable[[Any], "bool | str"]
    cancel_check: Callable[[], bool]
    set_plan: Callable[[list[str]], None]
    ask: Callable[..., "str | None"] | None = None   # (질문, 선택지) → 답. 작업 담당이 막혔을 때 (3단계)
    send_gate: Callable[[Any], bool] | None = None   # 승인 뒤 '보내기 전 대기'(취소 가능). False면 보내지 않는다 (4단계)
    drive: Callable[..., "dict | None"] | None = None   # (할 일, 기다릴 초) → 폰의 결과. 폰의 쇼핑 화면을 멀리 있는 브라우저로 쓴다 (D68)


_NOTES_LOCK = __import__("threading").Lock()


@dataclass
class Job:
    id: str
    task: str
    use_plan: bool = False
    title: str | None = None       # 대화 담당이 만든 작업이면 짧은 제목과 브리프 (D42)
    brief: dict | None = None
    conversation_id: str | None = None
    questions: bool = False        # 앱이 X-Api-Version: 2로 질문 방식(awaiting_input)을 켰는가. 아니면 v1(needs_input으로 끝남)
    send_enabled: bool = False     # 앱이 X-Api-Version: 3으로 본인 주소 발송(send_pending 등)을 켰는가
    skill: str | None = None       # 기능 파일 id로 정해진 길을 가는 작업(예: naver_pay_recent → sites/read.py)
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    plan: list[str] | None = None
    steps: list[dict] = field(default_factory=list)
    approval: dict | None = None
    result: dict | None = None
    approvals_count: int = 0
    approved: list[dict] = field(default_factory=list)   # 사용자가 승인한 초안 내용 (모델의 보고와 별개의 사실 기록)
    narration: list[dict] = field(default_factory=list)  # [{time, text}] 사람이 읽는 진행 설명
    question: dict | None = None                         # awaiting_input일 때 작업 담당의 질문
    notes: list = field(default_factory=list)            # 작업이 도는 동안 사용자가 같은 대화에서 덧붙인 말(작업실이 다음 걸음에 꺼내 간다)
    steers: list = field(default_factory=list)           # 덧붙인 말의 기록 [{time, text, applied}] — 앱의 "덧붙인 말 받음 · 반영 전/반영함"
    unanswered: list[str] = field(default_factory=list)  # 시간 안에 답을 못 받은 질문
    send: dict | None = None                             # send_pending일 때 보낼 메일과 취소 가능 시각
    _sending: bool = False                               # 대기가 끝나 보내기 시작했다 → 취소할 수 없다
    _answer: str | None = None
    _qevent: threading.Event = field(default_factory=threading.Event)
    _context: str | None = None
    _event: threading.Event = field(default_factory=threading.Event)
    _decision: "bool | str | None" = None
    _approved_sha: str | None = None
    _cancel: bool = False
    _attachments: dict = field(default_factory=dict)     # id → (mime, 바이트, 만료 시각 epoch, 사이트 키). 메모리에만 둔다
    _data: str | None = None                             # 사이트 읽기에서 코드가 뽑은 목록(개인 데이터). 메모리에만, 응답에 없다
    browser: dict | None = None                          # 폰의 쇼핑 화면에 지금 맡긴 일 {"id", "type", …} (running일 때만 내보낸다)
    _bevent: Any = None                                  # 그 결과가 오면 켜지는 신호
    _bresult: dict | None = None
    _bdone: str | None = None                            # 마지막으로 결과를 받은 일의 id(같은 결과가 다시 와도 한 번만 받는다)
    _bseq: int = 0
    _send_to: str | None = None                          # 받는 사람이 남인 발송 작업의 주소(D77) — 대화 쪽 코드가 정한다. 응답에 싣지 않는다
    _send_from: str | None = None                        # 그때 보낼 내 계정(없으면 기본 계정)
    _private: bool = False                               # 맥락(_context)이 읽은 결과(개인 데이터)다 → 기억 저장소에 남기지 않는다

    def add_note(self, text: str) -> None:
        with _NOTES_LOCK:
            self.notes = [*self.notes, text][-10:]
            self.steers = [*self.steers, {"time": _now(), "text": text[:200], "applied": False}][-10:]
            self.narration.append({"time": _now(), "text": "덧붙인 말을 받았어요 — 지금 단계가 끝나면 반영해요"})

    def take_notes(self) -> list:
        with _NOTES_LOCK:
            out, self.notes = list(self.notes), []
            if out:
                self.steers = [{**x, "applied": True} for x in self.steers]
                self.narration.append({"time": _now(), "text": "덧붙인 말을 반영하고 있어요"})
        return out

    def public(self) -> dict:
        d = {"id": self.id, "status": self.status, "task": self.task, "created_at": self.created_at,
             "updated_at": self.updated_at, "steps": list(self.steps)}
        if self.title is not None:
            d["title"] = self.title
        if self.skill:   # 어느 길의 작업인지(web_find | web_browse | shop_drive | 사이트 읽기 기능의 이름). 일반 작업(메일 쓰기 등)에는 없다
            d["skill"] = self.skill
        if self.brief is not None:
            d["brief"] = self.brief
        if self.conversation_id is not None:
            d["conversation_id"] = self.conversation_id
        d["narration"] = list(self.narration)
        if self.steers:   # 작업이 도는 동안 사용자가 덧붙인 말(applied: 작업이 받아 갔다)
            d["steers"] = [dict(x) for x in self.steers]
        if self.status == "queued" and getattr(self, "_wait_reason", ""):
            d["wait_reason"] = self._wait_reason   # 줄 선 까닭 한 줄(같은 자원을 쓰는 앞 작업 등)
        if self.status == "awaiting_input" and self.question is not None:
            d["question"] = self.question
        if self.plan is not None:
            d["plan"] = self.plan
        if self.status == "awaiting_approval" and self.approval is not None:
            d["approval"] = self.approval
        if self.status == "send_pending" and self.send is not None:
            d["send"] = {**self.send, "now": _now()}
        if self.status == "running" and self.browser is not None:
            d["browser"] = self.browser   # 폰이 지금 할 일(검색·열기). 폰이 하고 POST …/browser로 결과를 돌려준다 (D68)
        if self.status not in TERMINAL and getattr(self, "_progress", None):
            d["progress"] = dict(self._progress)   # 오래 도는 일의 지금 상태(실행한 쪽이 실을 때만)
        if self.result is not None:
            d["result"] = self.result
        return d


Runner = Callable[[Job, Hooks], Any]   # RunResult 비슷한 객체(status, summary, question)를 돌려준다


class JobManager:
    def __init__(self, runner: Runner, max_queue: int = 5, approval_timeout_s: float = 1800.0, keep: int = 50,
                 send_delay_s: float = 10.0):
        self.card_maker = None   # (job, result) → 답변 카드(cards.make_card). cli가 붙인다
        self.failures = None     # 끝내지 못한 작업의 꼴(api/failures.py). cli가 붙인다
        self.activity = None     # 끝난 작업을 짧게 남기는 기록(activity.ActivityLog, D103). cli가 붙인다
        self._runner = runner
        self.send_delay_s = send_delay_s   # 승인 뒤 보내기 전 대기(이때 취소 가능)
        self.max_queue = max_queue
        self.approval_timeout_s = approval_timeout_s
        self.keep = keep
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        # 서로 다른 일은 함께 돈다(사용자 결정 2026-11-18): 같은 자원(브라우저·작업실 폴더)을 쓰는 작업끼리만 차례를 지킨다
        self.parallel = max(1, int(os.environ.get("JOB_PARALLEL", "3")))
        self._pool = ThreadPoolExecutor(max_workers=self.parallel, thread_name_prefix="agent-job")
        self._busy: set[str] = set()          # 지금 쓰고 있는 자원
        self._waiting: list[Job] = []         # 자원·자리를 기다리는 작업(들어온 차례)
        self.on_status = None   # (job, status) → None. 상태가 바뀔 때 부른다(푸시 알림, D44). 실패해도 작업은 계속된다

    # ---- 조회 ----
    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise NotFound(job_id)
        return job

    def list(self, limit: int = 20, conversation_id: str | None = None) -> list[dict]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if conversation_id is None or j.conversation_id == conversation_id]
        out = []
        for j in reversed(jobs[-limit:]):
            d = {"id": j.id, "status": j.status, "task": j.task, "title": j.title, "created_at": j.created_at,
                 "updated_at": j.updated_at}
            if j.conversation_id is not None:
                d["conversation_id"] = j.conversation_id
            out.append(d)
        return out

    # ---- 제출 ----
    def submit(self, task: str, context: str | None = None, use_plan: bool = False,
               title: str | None = None, brief: dict | None = None, conversation_id: str | None = None,
               questions: bool = False, send: bool = False, fail: "tuple[str, str] | None" = None,
               skill: str | None = None, private: bool = False, extra: dict | None = None) -> Job:
        """fail=(outcome, reason): 실행하지 않고 바로 실패로 끝나는 작업(예: 메일 계정이 없어 보낼 수 없음 → 앱이 계정 연결을 띄운다).
        extra: 작업 담당이 쓸 값({"_site": …, "_tone": …} — 밑줄로 시작하는 이름만). 작업이 돌기 전에 붙인다
        (만든 뒤에 붙이면 작업 스레드가 먼저 읽을 수 있다 — 실측: 사이트 이름이 빈 채로 시작됐다)."""
        with self._lock:
            active = sum(j.status not in TERMINAL for j in self._jobs.values())
            if active >= self.max_queue + 1:
                raise Busy()
            if title is None:   # v1(/v1/tasks) 작업: 요청 앞부분을 제목으로
                one = " ".join(task.split())
                title = one if len(one) <= 30 else one[:29] + "…"
            job = Job(id=secrets.token_urlsafe(12), task=task, use_plan=use_plan, title=title, brief=brief,
                      conversation_id=conversation_id, questions=questions, send_enabled=send, skill=skill, _context=context,
                      _private=private)
            for k, v in (extra or {}).items():
                if k.startswith("_"):
                    setattr(job, k, v)
            self._jobs[job.id] = job
            done = [k for k, j in self._jobs.items() if j.status in TERMINAL]
            for k in done[: max(0, len(self._jobs) - self.keep)]:
                del self._jobs[k]
            if fail is not None:
                outcome, reason = fail
                text = SEND_FAIL_TEXT.get(reason, "메일을 보내지 못했어요") + " 보낸 메일은 없어요."
                job.result = {"outcome": outcome, "reason": reason, "summary": text, "approved_drafts": [],
                              "report": {"done": [], "blocked": [text], "sources": [], **({"sent": []} if send else {})}}
                job.narration.append({"time": _now(), "text": "작업을 끝내지 못했어요"})
                job._context = None
                job.status = "failed"
                return job
        with self._lock:
            self._waiting.append(job)
        self._dispatch()
        return job

    @staticmethod
    def resource(job: Job) -> str:
        """작업이 쓰는 자원. 같은 자원끼리만 차례를 지킨다.
        작업실은 그 대화의 폴더(전용 크롬은 드라이버끼리 프로필 잠금이 따로 있다), 그 밖은 사용자의 브라우저 한 벌."""
        if job.skill == "workroom":
            return f"workroom:{getattr(job, '_project', '') or job.id}"
        return "browser"

    WAIT_TEXT = {"browser": "브라우저를 쓰는 앞 작업이 끝나면 시작해요", "workroom": "같은 작업실에서 만드는 앞 작업이 끝나면 시작해요"}

    def _dispatch(self) -> None:
        start = []
        with self._lock:
            for job in list(self._waiting):
                res = self.resource(job)
                if not job._cancel:
                    if res in self._busy:
                        job._wait_reason = self.WAIT_TEXT[res.split(":")[0]]
                        continue
                    if len(self._busy) >= self.parallel:
                        job._wait_reason = f"함께 할 수 있는 작업({self.parallel}개)이 차서 하나가 끝나면 시작해요"
                        continue
                    self._busy.add(res)
                    job._res = res
                job._wait_reason = ""
                self._waiting.remove(job)
                start.append(job)
        for job in start:
            self._pool.submit(self._run_slot, job)

    def _run_slot(self, job: Job) -> None:
        try:
            self._run(job)
        finally:
            with self._lock:
                self._busy.discard(getattr(job, "_res", None))
            self._dispatch()

    @staticmethod
    def _say(job: Job, text: str | None) -> None:
        if text and (not job.narration or job.narration[-1]["text"] != text):
            job.narration.append({"time": _now(), "text": text})

    def _touch(self, job: Job, status: str | None = None) -> None:
        if status:
            job.status = status
        job.updated_at = _now()
        if status and self.on_status is not None:
            try:
                self.on_status(job, status)
            except Exception:
                pass

    def _run(self, job: Job) -> None:
        if job._cancel:
            job.result = {"outcome": "cancelled", "summary": "시작 전에 취소됨"}
            return self._touch(job, "cancelled")
        self._touch(job, "running")
        self._say(job, "작업을 시작했어요")

        def on_step(entry) -> None:
            args = entry.args or {}
            brief = {k: (v if not isinstance(v, str) else v[:80]) for k, v in args.items() if k != "summary"}
            job.steps.append({"n": len(job.steps) + 1, "tool": entry.tool, "args": brief,
                              "status": entry.result_status or entry.check_status, "url": entry.url})
            self._say(job, narrate(entry))
            self._touch(job)

        def pause_shot() -> dict | None:
            """승인·질문으로 멈춘 그때의 화면(실행한 쪽이 job._pause_shot에 둔 것)을 캡처로 올리고 그 정보를 돌려준다.
            실기기 2026-11-16: 어느 리뷰를 고치려는지 화면을 보지 못한 채 승인을 물었다. 메모리에만 두고 보고의 캡처와 같이 만료된다."""
            a, job._pause_shot = getattr(job, "_pause_shot", None), None
            if not a or not isinstance(a.get("data"), bytes):
                return None
            job._pause_n = getattr(job, "_pause_n", 0) + 1
            aid, exp = f"p{job._pause_n}", time.time() + ATTACHMENT_TTL_S
            w, h = jpeg_size(a["data"])
            job._attachments[aid] = (a.get("mime", "image/jpeg"), a["data"], exp, a.get("site"))
            return {"id": aid, "type": "screenshot", "mime": a.get("mime", "image/jpeg"), "width": w, "height": h, "bytes": len(a["data"]),
                    "page_host": a.get("page_host"), "caption": str(a.get("caption") or "")[:60], "sensitive": True,
                    "expires_at": datetime.fromtimestamp(exp, timezone.utc).isoformat(timespec="seconds"),
                    "path": f"/v1/tasks/{job.id}/attachments/{aid}"}

        def approver(req) -> "bool | str":
            job.approvals_count += 1
            job._decision, job._approved_sha, job._event = None, None, threading.Event()
            # 초안 고정(D42): 승인 화면에 보낸 원문과 그 해시. 이 값과 같은 해시로만 승인할 수 있다
            fields = dict(req.form_values)
            kind = getattr(req, "kind", "draft") or "draft"
            sha = draft_sha256(kind, req.element_label, req.url, fields)
            job.approval = {
                "approval_id": secrets.token_urlsafe(8), "kind": kind, "action": req.element_label,
                "url": req.url, "draft_id": getattr(req, "draft_id", None) or "d_" + secrets.token_urlsafe(9),
                "sha256": sha, "fields": fields, "requested_at": _now(),
                "expires_in_s": int(self.approval_timeout_s),
            }
            shot = pause_shot()
            if shot:
                job.approval["attachment"] = shot   # 누르려는 그 화면
            if kind == "send" and fields.get("받는 사람") != fields.get("보내는 사람"):
                job.approval["external"] = True   # 받는 사람이 내가 아니다(D77) — 앱이 카드에 눈에 띄게 그릴 수 있게
            if getattr(req, "external", False):
                job.approval["external"] = True   # 채널에 올리기(D98) — 바깥에 보이는 일
            self._say(job, "메일을 보내도 될지 승인을 기다리고 있어요" if kind == "send"
                      else f"'{str(req.element_label)[:20]}'을(를) 눌러도 될지 승인을 기다리고 있어요" if kind == "action"
                      else "초안을 저장해도 될지 승인을 기다리고 있어요")
            self._touch(job, "awaiting_approval")
            deadline = time.monotonic() + self.approval_timeout_s
            while not job._event.wait(0.2):
                if job._cancel or time.monotonic() > deadline:
                    job.approval = None
                    job._expired = not job._cancel
                    if not job._cancel:
                        self._say(job, "승인을 기다리다 시간이 지나 그 동작은 하지 않아요")
                    self._touch(job, "running")
                    return False
            decision = job._decision
            if decision is True and not (job._approved_sha and hmac.compare_digest(job._approved_sha, sha)):
                decision = False   # 승인한 해시가 고정한 초안과 다르면 승인이 아니다 (decide에서 이미 막지만 한 번 더)
            if decision is True:
                ap = job.approval
                job.approved.append({"draft_id": ap["draft_id"], "sha256": sha, "kind": kind, "action": ap["action"],
                                     "url": ap["url"], "fields": ap["fields"]})
            job.approval = None
            # 기다린다는 줄이 마지막에 남지 않게(실기기 2026-11-18: 승인한 뒤에도 "승인을 기다리고 있어요"와 '멈추기'만 보였다)
            self._say(job, "승인을 받아 이어서 진행해요" if decision is True else "승인하지 않아 그 동작은 하지 않아요")
            self._touch(job, "running")
            return decision if decision is not None else False

        def ask(question: str, choices: list[str] | None = None) -> "str | None":
            """작업 담당의 질문을 앱으로 올리고 답을 기다린다. 승인과 같은 만료 시간 뒤에는 할 수 있는 부분부터 진행하게 한다."""
            if not job.questions:   # v1 앱은 awaiting_input을 모른다(실패로 처리) → 예전처럼 needs_input으로 끝낸다
                return None
            job._answer, job._qevent = None, threading.Event()
            job.question = {"question_id": secrets.token_urlsafe(8), "text": question,
                            "choices": [c for c in (choices or [])][:4], "asked_at": _now(),
                            "expires_in_s": int(self.approval_timeout_s)}
            shot = pause_shot()
            if shot:
                job.question["attachment"] = shot   # 묻는 그때의 화면
            if getattr(job, "_handover", None):
                job.question["handover"] = job._handover   # 손 넘기기(D106): 앱이 이 화면을 열어 사람이 직접 한다
            self._say(job, "확인할 것이 있어 질문을 보냈어요")
            self._touch(job, "awaiting_input")
            deadline = time.monotonic() + self.approval_timeout_s
            while not job._qevent.wait(0.2):
                if job._cancel:
                    job.question = None
                    self._touch(job, "running")
                    return None
                if time.monotonic() > deadline:
                    job.unanswered.append(question)
                    job.question = None
                    self._say(job, "답이 없어 할 수 있는 부분부터 진행해요")
                    self._touch(job, "running")
                    return UNANSWERED_NOTE.format(m=int(self.approval_timeout_s // 60))
            answer = job._answer
            job.question = None
            self._say(job, "답을 받아 이어서 진행해요")
            self._touch(job, "running")
            return answer

        def send_gate(draft) -> bool:
            """승인 뒤 보내기 전 대기. 이 동안 취소하면 보내지 않는다. 대기가 끝나면 더는 취소할 수 없다."""
            until = datetime.now(timezone.utc) + timedelta(seconds=self.send_delay_s)
            at = until.isoformat(timespec="seconds")
            job.send = {"draft_id": draft.draft_id, "sha256": draft.sha256, "from": draft.sender, "to": draft.to,
                        "fields": dict(draft.fields), "send_at": at, "cancel_until": at,
                        **({"external": True} if getattr(draft, "external", False) else {})}   # 받는 사람이 내가 아니다(D77) — 대기 카드에서도 알 수 있게
            self._say(job, "곧 메일을 보내요. 지금은 취소할 수 있어요")
            self._touch(job, "send_pending")
            deadline = time.monotonic() + self.send_delay_s
            while time.monotonic() < deadline:
                if job._cancel:
                    job.send = None
                    job.approved = [d for d in job.approved if d.get("draft_id") != draft.draft_id]
                    self._touch(job, "running")
                    return False
                time.sleep(0.05)
            with self._lock:
                if job._cancel:   # 마지막 순간의 취소도 받아 준다
                    job.send = None
                    job.approved = [d for d in job.approved if d.get("draft_id") != draft.draft_id]
                    self._touch(job, "running")
                    return False
                job._sending = True
            job.send = None
            self._say(job, "메일을 보내고 있어요")
            self._touch(job, "running")
            return True

        def drive(command: dict, wait_s: float = 300.0) -> "dict | None":
            """폰의 쇼핑 화면에 일 하나를 맡기고 결과를 기다린다. 시간 안에 안 오거나 취소되면 None."""
            job._bseq += 1
            job._bresult, job._bevent = None, threading.Event()
            job.browser = {"id": f"b{job._bseq}", **command}
            self._touch(job)
            deadline = time.monotonic() + wait_s
            try:
                while not job._bevent.wait(0.2):
                    if job._cancel or time.monotonic() > deadline:
                        return None
                return job._bresult
            finally:
                job._bdone = job.browser["id"] if job._bresult is not None else job._bdone
                job.browser = None
                self._touch(job)

        hooks = Hooks(on_step, approver, lambda: job._cancel, lambda steps: setattr(job, "plan", steps), ask, send_gate, drive)
        try:
            run = self._runner(job, hooks)
            outcome = getattr(run, "status", "failed")
            result = {"outcome": outcome, "summary": getattr(run, "summary", "")}
            if outcome == "completed":
                status = "completed"
            elif outcome == "needs_user":
                status = "needs_input"
                result["question"] = getattr(run, "question", None) or result["summary"]
            elif outcome == "cancelled" or (job._cancel and not job._sending):
                status, result["outcome"] = "cancelled", "cancelled"
            else:
                status = "failed"
                if outcome == "approval_denied" and getattr(job, "_expired", False):
                    result["outcome"] = "approval_expired"
            # 승인된 초안은 모델의 요약이 아니라 승인 화면의 내용 그대로 돌려준다 (실사용 실패 r-001)
            approved = list(job.approved) if outcome not in ("approval_mismatch",) else []
            # 단추 누르기 승인(kind action — 로그인한 사이트 안에서 하기 D72)은 초안이 아니다: 초안 목록에 넣지 않고 누른 단추의 이름만 따로 준다
            result["approved_drafts"] = [d for d in approved if d.get("kind") != "action"]
            if any(d.get("kind") == "action" for d in approved):
                result["approved_actions"] = [str(d.get("action", ""))[:40] for d in approved if d.get("kind") == "action"]
            if status == "needs_input":
                result["choices"] = list(getattr(run, "question_choices", []) or [])
            rep = getattr(run, "report", None) or {}
            blocked = list(rep.get("blocked") or [])
            blocked += [f"답을 받지 못한 질문: {q}" for q in job.unanswered if not any(q in b for b in blocked)]
            sources = []
            for st in job.steps:   # 실제로 연 페이지 (모델이 쓴 것이 아니라 단계 기록에서)
                if st.get("url") and st.get("status") == "ok" and st["url"] not in sources and st["url"] != "about:blank":
                    sources.append(st["url"])
            if rep.get("from_search"):
                sources = []   # 답이 연 사이트에서 온 것이 아니다(검색이 썼거나, 하지 못했다): 그 사이트의 "열기"를 답 아래에 붙이지 않는다
            if job.skill == "site_do":
                # 로그인한 화면의 주소는 싣지 않는다(경로에 작업·문서 번호가 들 수 있고, 앱은 sources를 누를 수 있는 링크로 그린다): 그 사이트의 첫 주소만
                from urllib.parse import urlsplit
                # 거쳐 간 곳을 줄줄이 붙이지 않는다(실기기 2026-11-16: "열기" 줄이 서너 개 붙었다): 마지막에 있던 곳 하나만
                sources = list(dict.fromkeys(f"{u.scheme}://{u.netloc}/" for u in map(urlsplit, sources) if u.scheme in ("http", "https") and u.netloc))[-1:]
            done = list(rep.get("done") or [])
            if not done and status == "completed" and result["summary"]:
                done = [result["summary"]]
            if outcome == "verify_failed":
                # 검증에서 거절된 완료 보고의 '한 일'은 확인되지 않은 주장이다 → 보여 주지 않고 이유를 blocked에
                done = []
                why = result["summary"].rsplit("[검증 실패:", 1)[-1].rstrip("] ").strip() if "[검증 실패:" in result["summary"] else ""
                blocked.insert(0, f"완료 보고가 검증을 통과하지 못함{': ' + why if why else ''}")
            sent = list(getattr(run, "sent", None) or [])
            if outcome == "send_failed":
                result["reason"] = getattr(run, "reason", None) or "unreachable"
                done = []
                blocked.insert(0, SEND_FAIL_TEXT.get(result["reason"], "메일을 보내지 못했어요") + " 보낸 메일은 없어요.")
            # 보낸 메일이 없으면 '보냈다'는 승인 기록도 남기지 않는다(승인했지만 발송에 실패한 경우)
            sent_ids = {x.get("draft_id") for x in sent}
            result["approved_drafts"] = [d for d in result["approved_drafts"]
                                         if d.get("kind") != "send" or d.get("draft_id") in sent_ids]
            if outcome == "site_blocked":
                done = []
                if result["summary"] not in blocked:   # 막힌 까닭이 이미 보고 첫 줄에 있으면 두 번 싣지 않는다
                    blocked.insert(0, result["summary"])
            if outcome == "needs_login":   # 사이트 로그인이 없거나 풀렸다 → 앱이 다시 로그인 단추를 그릴 수 있게
                result["reason"] = getattr(run, "reason", None) or "login_missing"
                if getattr(run, "site", None):
                    result["action"] = {"type": "site_login", "site": run.site, "resume": True}
                done = []
                blocked.insert(0, result["summary"])
            result["report"] = {"done": done, "blocked": blocked, "sources": sources[:10]}
            links = [l for l in (rep.get("links") or []) if isinstance(l, dict) and l.get("url")][:5]
            if links:   # 공개 정보 찾기: 맥의 브라우저가 실제로 열어 읽은 페이지(제목·주소·사이트·대표 그림 주소) (D61)
                result["report"]["links"] = links
            if run.status == "search_failed":   # 쇼핑 검색 API를 부르지 못했다: 종류를 싣는다(not_set | auth | limited | unreachable | bad_response) (D71)
                result["reason"] = getattr(run, "reason", None) or "unreachable"
            items = [i for i in (rep.get("items") or []) if isinstance(i, dict) and i.get("title") and i.get("url")][:8]
            if items:   # 쇼핑: 폰이 상품 화면에서 읽어 온 상품 카드(값은 읽은 그대로) (D68)
                result["report"]["items"] = items
            if rep.get("by") in ("claude", "local"):   # 공개 정보 찾기의 답을 쓴 곳(다른 작업의 보고는 모두 맥이 만든다)
                result["report"]["by"] = rep["by"]
            if getattr(run, "private", False):
                result["report"]["private"] = True
            if isinstance(rep.get("card"), dict):   # 실행한 쪽이 정해진 꼴로 만든 카드(D101) — 카드 만들기를 건너뛴다
                result["report"]["card"] = rep["card"]
            # 화면 캡처: 실행한 쪽이 상황에 맞게 고른 것(읽은 화면·멈춘 화면)을 4장까지(긴 화면은 나눠 찍은 여러 장). 사용자가 취소한 작업에는 남기지 않는다
            shots = [a for a in (getattr(run, "attachments", None) or []) if isinstance(a.get("data"), bytes)][:MAX_ATTACHMENTS]
            if status == "cancelled":
                shots = []
            if shots:
                exp = time.time() + ATTACHMENT_TTL_S
                metas = []
                for i, a in enumerate(shots, 1):
                    aid = f"a{i}"
                    w, h = (a.get("width"), a.get("height")) if a.get("width") else jpeg_size(a["data"])
                    job._attachments[aid] = (a.get("mime", "image/jpeg"), a["data"], exp, a.get("site"))
                    metas.append({"id": aid, "type": a.get("type") or "screenshot", "mime": a.get("mime", "image/jpeg"), "width": w, "height": h,
                                  "bytes": len(a["data"]), "page_host": a.get("page_host"),
                                  "caption": str(a.get("caption") or "")[:60], "sensitive": bool(a.get("sensitive", True)),
                                  "expires_at": datetime.fromtimestamp(exp, timezone.utc).isoformat(timespec="seconds"),
                                  "path": f"/v1/tasks/{job.id}/attachments/{aid}"})
                result["report"]["attachments"] = metas
            if job.send_enabled:
                result["report"]["sent"] = sent
            if self.card_maker is not None and status in ("completed", "failed", "needs_input") and "card" not in result["report"]:
                try:   # 답변 카드(사용자 지시 2026-11-16): 결과를 정해진 꼴의 카드 한 장으로. 못 만들어도 결과는 그대로 간다
                    card = self.card_maker(job, result)
                    if card:
                        result["report"]["card"] = card
                except Exception as e:
                    sys.stderr.write(f"[card] {time.strftime('%Y-%m-%d %H:%M:%S')} make error={type(e).__name__}\n")
            result["report"]["speak"] = speak_line(result, rep.get("speak"))   # 통화 중에 앱이 읽을 한두 문장(D116)
            self._say(job, {"completed": "작업을 마쳤어요", "needs_input": "확인이 필요해 멈췄어요",
                            "cancelled": "작업을 취소했어요"}.get(status, "작업을 끝내지 못했어요"))
            job.result = result
            self._touch(job, status)
            if self.activity is not None:
                try:
                    self.activity.record(job, status, result)
                except Exception as e:
                    sys.stderr.write(f"[activity] {time.strftime('%Y-%m-%d %H:%M:%S')} record error={type(e).__name__}\n")
            if getattr(self, "failures", None) is not None:   # 끝내지 못한 작업의 꼴만(내용 없음) — 실사용 실패 모으기
                try:
                    self.failures.record(job, status, result, run)
                except Exception as e:
                    sys.stderr.write(f"[failures] {time.strftime('%Y-%m-%d %H:%M:%S')} record error={type(e).__name__}\n")
        except Exception as e:  # 내부 오류: 내용은 노출하지 않고 종류만
            job.result = {"outcome": "internal_error", "summary": f"내부 오류: {type(e).__name__}"}
            self._touch(job, "failed")
        finally:
            job._context = None

    # ---- 캡처(보고의 attachments) ----
    def attachment(self, job_id: str, aid: str) -> tuple[str, bytes]:
        """(mime, 바이트). 작업이 없으면 NotFound, 캡처가 없거나 기한이 지났으면 Gone."""
        job = self.get(job_id)
        item = job._attachments.get(aid)
        if item is None or time.time() > item[2]:
            job._attachments.pop(aid, None)
            raise Gone(aid)
        return item[0], item[1]

    def drop_attachments(self, site: str | None = None) -> int:
        """기한이 지난 캡처와, site를 주면 그 사이트의 캡처를 모두 지운다(로그아웃 = 동의 철회)."""
        n, now = 0, time.time()
        with self._lock:
            jobs = list(self._jobs.values())
        for j in jobs:
            for aid in [k for k, v in j._attachments.items() if now > v[2] or (site is not None and v[3] == site)]:
                del j._attachments[aid]
                n += 1
        return n

    # ---- 승인·취소 ----
    def decide(self, job_id: str, approval_id: str, decision: str, reason: str | None = None,
               sha256: str | None = None) -> Job:
        """sha256: 앱이 보여 준 초안의 해시. 주면 고정한 초안의 해시와 같아야 한다(다르면 HashMismatch).
        생략하면 approval_id가 가리키는 고정 초안(그 해시)을 승인한 것으로 본다(v1 호환)."""
        job = self.get(job_id)
        ap = job.approval
        if job.status != "awaiting_approval" or ap is None:
            raise Conflict("승인 대기 중이 아니다")
        if not secrets.compare_digest(str(approval_id), ap["approval_id"]):
            raise Conflict("approval_id가 현재 승인 요청과 다르다")
        if "received" in ap:
            raise Conflict("이 승인 요청에는 이미 응답했다. 처리 중이다")
        if sha256 is not None and not hmac.compare_digest(str(sha256), ap["sha256"]):
            raise HashMismatch("sha256이 고정한 초안의 해시와 다르다. 승인 화면을 다시 불러온다")
        ap["received"] = decision
        if decision == "approve":
            job._approved_sha = ap["sha256"]
            job._decision = True
        else:
            r = (reason or "").strip()[:MAX_REASON_CHARS]
            job._decision = r if r else False   # 사유가 있으면 고쳐서 다시 시도, 없으면 중단
        job._event.set()
        return job

    def answer(self, job_id: str, question_id: str, answer: str) -> Job:
        job = self.get(job_id)
        q = job.question
        if job.status != "awaiting_input" or q is None:
            raise Conflict("질문 대기 중이 아니다")
        if not secrets.compare_digest(str(question_id), q["question_id"]):
            raise Conflict("question_id가 현재 질문과 다르다")
        if "received" in q:
            raise Conflict("이 질문에는 이미 답했다. 처리 중이다")
        q["received"] = True
        job._answer = answer.strip()[:MAX_ANSWER_CHARS]
        job._qevent.set()
        return job

    def browser_result(self, job_id: str, command_id: str, result: dict) -> Job:
        """폰이 맡은 일의 결과를 받는다. 지금 맡긴 일의 id가 아니면 Conflict(이미 넘어갔다) — 같은 결과가 다시 온 것은 그대로 받는다."""
        job = self.get(job_id)
        with self._lock:
            cur = job.browser
            if job.status == "running" and cur is not None and cur.get("id") == command_id and job._bevent is not None:
                if job._bresult is None:
                    job._bresult = result
                    job._bevent.set()
                return job
            if command_id and command_id == job._bdone:
                return job
        raise Conflict("그 일은 이미 끝났거나 지금 맡긴 일이 아니다")

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        with self._lock:
            if job.status in TERMINAL:
                raise Conflict("이미 끝난 작업이다")
            if job._sending:
                raise Conflict("이미 발송했다. 취소할 수 없다")
            job._cancel = True
            waiting = job in self._waiting
            if waiting:   # 아직 자기 차례를 기다리던 작업: 바로 취소로(앞 작업이 끝날 때까지 '대기'로 보이지 않게)
                self._waiting.remove(job)
        if waiting:
            job.result = {"outcome": "cancelled", "summary": "시작 전에 취소됨"}
            self._touch(job, "cancelled")
        return job

    def shutdown(self) -> None:
        for j in list(self._jobs.values()):
            j._cancel = True
        self._pool.shutdown(wait=False, cancel_futures=True)
