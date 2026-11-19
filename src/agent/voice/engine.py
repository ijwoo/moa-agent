"""통화 목소리(D117): 서버 쪽. 맥의 전용 가상환경에서 도는 일꾼(tts_worker.py)에게 글을 주고 조각(WAV)을 받아 둔다.

- 켜기: `.env`의 `VOICE_TTS=1` + `TTS_PYTHON`(mlx-audio가 깔린 가상환경의 python). 목소리 샘플은 `VOICE_REF`(wav)·`VOICE_REF_TEXT`(그 말의 글) —
  기본 `~/.local/share/local-ai-agent/voice/sample/bright_v4.{wav,txt}`. 샘플·만든 음성은 맥에만, 파일로 남기지 않는다(메모리 10분).
- 일꾼은 처음 부를 때 띄우고(모델 올리기 약 3초), 15분 쓰지 않으면 내린다(메모리 약 5GB).
- 앱: `POST /v1/voice {"text"}` → `{"id"}`, `GET /v1/voice/{id}/{n}` → n번째 조각(audio/wav). 아직이면 10초까지 기다리고,
  끝났으면 204. 통화 중 대화 응답의 말풍선에는 미리 시작한 `voice_id`가 붙는다.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

DIR = Path(os.path.expanduser("~/.local/share/local-ai-agent/voice"))
WORKER = Path(__file__).with_name("tts_worker.py")
MAX_CHARS = 400
KEEP_S = 600          # 만든 조각을 메모리에 두는 시간
MAX_ITEMS = 40
IDLE_S = 900          # 이만큼 안 쓰면 일꾼을 내린다
READY_S = 120         # 모델을 올려 데우기까지 기다리는 한도


class VoiceError(Exception):
    """code: not_found | unavailable"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Item:
    def __init__(self, text: str):
        self.text, self.segs, self.done, self.error = text, [], False, None
        self.t0, self.first = time.time(), None


class VoiceEngine:
    def __init__(self, python: str, ref_wav: str, ref_text: str, worker_cmd: list[str] | None = None):
        self.python, self.ref_wav, self.ref_text = python, ref_wav, ref_text
        self.worker_cmd = worker_cmd   # 테스트용: 일꾼 대신 띄울 명령
        self.items: dict[str, _Item] = {}
        self.cond = threading.Condition()
        self.proc: subprocess.Popen | None = None
        self.ready = False
        self.last_use = time.time()
        self._wlock = threading.Lock()
        threading.Thread(target=self._reaper, daemon=True).start()

    @classmethod
    def from_env(cls) -> "VoiceEngine | None":
        if os.environ.get("VOICE_TTS") != "1":
            return None
        py = os.path.expanduser(os.environ.get("TTS_PYTHON", "~/.local/share/local-ai-agent/tts-venv/bin/python"))
        wav = os.path.expanduser(os.environ.get("VOICE_REF", str(DIR / "sample" / "bright_v4.wav")))
        txt = os.path.expanduser(os.environ.get("VOICE_REF_TEXT", str(DIR / "sample" / "bright_v4.txt")))
        if not (Path(py).is_file() and Path(wav).is_file() and Path(txt).is_file()):
            sys.stderr.write(f"[voice-tts] {time.strftime('%Y-%m-%d %H:%M:%S')} off: python·샘플 중 없는 것이 있다\n")
            return None
        return cls(py, wav, txt)

    # ── 일꾼 ──
    def _ensure(self) -> None:
        with self._wlock:
            if self.proc is not None and self.proc.poll() is None:
                return
            cmd = self.worker_cmd or [self.python, "-I", str(WORKER), self.ref_wav, self.ref_text]
            self.ready = False
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         text=True, bufsize=1)
            threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
            sys.stderr.write(f"[voice-tts] {time.strftime('%Y-%m-%d %H:%M:%S')} worker start\n")

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            with self.cond:
                if ev.get("ready"):
                    self.ready = True
                it = self.items.get(ev.get("id") or "")
                if it is not None:
                    if "b64" in ev:
                        try:
                            it.segs.append(base64.b64decode(ev["b64"]))
                        except ValueError:
                            pass
                        if it.first is None:
                            it.first = time.time() - it.t0
                    if ev.get("done") or ev.get("error"):
                        it.done, it.error = True, ev.get("error")
                        sys.stderr.write(f"[voice-tts] {time.strftime('%Y-%m-%d %H:%M:%S')} chars={len(it.text)} segs={len(it.segs)} "
                                         f"first={(it.first or 0):.2f}s total={time.time() - it.t0:.2f}s error={it.error}\n")
                self.cond.notify_all()
        with self.cond:   # 일꾼이 끝났다: 기다리던 것은 실패로
            for it in self.items.values():
                if not it.done:
                    it.done, it.error = True, "worker_exit"
            self.ready = False
            self.cond.notify_all()

    def _send(self, msg: dict) -> None:
        with self._wlock:
            if self.proc is None or self.proc.poll() is not None:
                raise VoiceError("unavailable")
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def _reaper(self) -> None:
        while True:
            time.sleep(30)
            now = time.time()
            with self.cond:
                for k in [k for k, v in self.items.items() if now - v.t0 > KEEP_S]:
                    del self.items[k]
                busy = any(not v.done for v in self.items.values())
            if not busy and now - self.last_use > IDLE_S:
                self.stop()

    def stop(self) -> None:
        with self._wlock:
            if self.proc is not None and self.proc.poll() is None:
                self.proc.stdin.close()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                sys.stderr.write(f"[voice-tts] {time.strftime('%Y-%m-%d %H:%M:%S')} worker stop (idle)\n")
            self.proc = None

    # ── 앱이 부르는 것 ──
    def warm(self) -> None:
        self.last_use = time.time()
        self._ensure()

    def start(self, text: str) -> str:
        text = " ".join((text or "").split())[:MAX_CHARS]
        if not text:
            raise VoiceError("unavailable")
        self.last_use = time.time()
        self._ensure()
        vid = "v" + secrets.token_urlsafe(9)
        with self.cond:
            if len(self.items) >= MAX_ITEMS:
                del self.items[min(self.items, key=lambda k: self.items[k].t0)]
            self.items[vid] = _Item(text)
        self._send({"id": vid, "text": text})
        return vid

    def segment(self, vid: str, n: int, wait_s: float = 10.0) -> bytes | None:
        """n번째 조각. 끝나서 더 없으면 None. 아직 없으면 wait_s까지 기다리고 그래도 없으면 VoiceError('not_ready')."""
        self.last_use = time.time()
        end = time.time() + wait_s
        with self.cond:
            while True:
                it = self.items.get(vid)
                if it is None:
                    raise VoiceError("not_found")
                if n < len(it.segs):
                    return it.segs[n]
                if it.done:
                    if it.error and not it.segs:
                        raise VoiceError("unavailable")
                    return None
                left = end - time.time()
                if left <= 0:
                    raise VoiceError("not_ready")
                self.cond.wait(left)

    def cancel(self, vid: str) -> None:
        with self.cond:
            it = self.items.get(vid)
            if it is None:
                raise VoiceError("not_found")
            if not it.done:   # 끝난 것으로 둔다 — 기다리던 GET은 받은 데까지 주고 204(실기기 2026-11-18: 시작 전에 취소하면 일꾼이 done을 안 보내 GET이 10초 뒤 409)
                it.done = True
                self.cond.notify_all()
        try:
            self._send({"cancel": vid})
        except VoiceError:
            pass
