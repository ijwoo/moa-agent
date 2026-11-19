"""손 넘기기(D106, 사용자 결정 2026-11-17 "진행해 보자"): 에이전트가 쓰던 브라우저 화면을 폰에 보여 주고, 사람이 직접 누르고 쓰게 한다.

- 쓰는 때: 사람이 해야 하는 단계 — 사람 확인 퍼즐(캡차), 2단계 인증 등. 퍼즐은 **사람이 푼다**(에이전트는 그동안 아무것도 누르지 않는다).
- 흐름: 작업이 질문 카드("직접 해 주세요" — 다 했어요 / 그만하기)로 멈추고, 카드에 손 넘기기 id가 붙는다. 앱은
  GET /v1/handover/{id}/frame(JPEG)으로 화면을 받고 POST /v1/handover/{id}/input으로 터치·글자를 보낸다. "다 했어요"로 답하면 에이전트가 이어 간다.
- 지키는 것: 열린 동안만(질문이 끝나면 닫힌다), 5분 넘게 아무 입력이 없으면 닫힌다. 화면은 기록하지 않고 앱으로만 보낸다.
  입력은 정해진 꼴(탭·끌기·스크롤·글자·몇 가지 키)만. 이 길로 들어온 값은 기록하지 않는다.
"""

from __future__ import annotations

import secrets
import threading
import time

IDLE_S = 300


class HandoverStore:
    def __init__(self, clock=time.monotonic):
        self._open: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.clock = clock

    def open(self, browser, reason: str, host: str) -> str:
        hid = "h_" + secrets.token_urlsafe(12)
        with self._lock:
            self._open[hid] = {"b": browser, "reason": reason[:120], "host": host[:100], "last": self.clock()}
        return hid

    def close(self, hid: str) -> None:
        with self._lock:
            self._open.pop(hid, None)

    def _get(self, hid: str) -> dict | None:
        with self._lock:
            h = self._open.get(hid)
            if h is not None and self.clock() - h["last"] > IDLE_S:
                self._open.pop(hid, None)
                return None
            return h

    def frame(self, hid: str) -> dict | None:
        h = self._get(hid)
        if h is None:
            return None
        return h["b"].request("hand_frame", {"quality": 55})

    def input(self, hid: str, args: dict) -> bool:
        h = self._get(hid)
        if h is None:
            return False
        h["b"].request("hand_input", args)
        h["last"] = self.clock()
        return True

    def public(self, hid: str) -> dict | None:
        h = self._get(hid)
        if h is None:
            return None
        return {"id": hid, "reason": h["reason"], "host": h["host"], "frame": f"/v1/handover/{hid}/frame", "input": f"/v1/handover/{hid}/input",
                "idle_close_s": IDLE_S}


STORE = HandoverStore()


def hand_over(b, ask, reason: str, host: str, on_open=None) -> bool:
    """브라우저 b를 사람에게 넘기고 "다 했어요"를 기다린다 → 사람이 다 했다고 했나. ask: 작업의 질문 훅(없으면 넘길 수 없다).
    on_open(public): 질문 카드에 붙일 손 넘기기 정보를 작업에 건넨다."""
    if ask is None:
        return False
    hid = STORE.open(b, reason, host)
    try:
        if on_open is not None:
            on_open(STORE.public(hid))
        ans = ask(f"{reason}\n'브라우저 열기'로 화면을 열어 직접 해 주세요. 끝나면 '다 했어요'를 눌러 주세요.", ["다 했어요", "그만하기"])
        return isinstance(ans, str) and ans.strip().startswith("다 했어요")
    finally:
        STORE.close(hid)
        if on_open is not None:
            on_open(None)
