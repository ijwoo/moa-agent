"""통화 목소리 일꾼(D117): 맥의 전용 가상환경(TTS_PYTHON — mlx-audio)에서 따로 도는 프로세스.

서버(src/agent/voice/engine.py)가 표준 입력으로 한 줄 JSON을 주면 사용자의 목소리 샘플을 참고로 음성을 만들어
조각마다 표준 출력으로 내보낸다. 모델은 Qwen3-TTS 1.7B Base(Apache-2.0, 맥 안에서만 — 글·목소리가 밖으로 나가지 않는다).

    입력: {"id": "v…", "text": "…"} | {"cancel": "v…"}
    출력: {"ready": true} | {"id", "seg": n, "b64": WAV} | {"id", "done": true, "segs": n, "ms": …} | {"id", "error": "…"}

이 파일은 서버의 가상환경에서 import하지 않는다(mlx가 없다). 표준 라이브러리 + mlx-audio·numpy·soundfile만.
"""

from __future__ import annotations

import base64
import io
import json
import os
import queue
import sys
import threading
import time

MODEL = os.environ.get("TTS_MODEL", "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16")
INTERVAL = float(os.environ.get("TTS_STREAM_INTERVAL", "1.0"))   # 조각 길이(초) — 짧을수록 첫 소리가 빠르다


def say(**kw) -> None:
    sys.stdout.write(json.dumps(kw, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    import numpy as np
    import soundfile as sf
    from mlx_audio.tts.utils import load_model

    ref, ref_text = sys.argv[1], open(sys.argv[2], encoding="utf-8").read().strip()
    model = load_model(MODEL)
    for _ in model.generate(text="네.", ref_audio=ref, ref_text=ref_text, lang_code="korean"):   # 데우기
        pass
    jobs: queue.Queue = queue.Queue()
    cancelled: set[str] = set()

    def reader() -> None:
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg.get("cancel"), str):
                cancelled.add(msg["cancel"])
            elif isinstance(msg.get("id"), str) and isinstance(msg.get("text"), str):
                jobs.put(msg)
        jobs.put(None)   # 서버가 닫았다

    threading.Thread(target=reader, daemon=True).start()
    say(ready=True)
    while True:
        job = jobs.get()
        if job is None:
            return
        vid, t0, n = job["id"], time.time(), 0
        if vid in cancelled:
            continue
        try:
            for r in model.generate(text=job["text"], ref_audio=ref, ref_text=ref_text, lang_code="korean",
                                    stream=True, streaming_interval=INTERVAL):
                if vid in cancelled:
                    break
                buf = io.BytesIO()
                sf.write(buf, np.array(r.audio), r.sample_rate, format="WAV", subtype="PCM_16")
                say(id=vid, seg=n, b64=base64.b64encode(buf.getvalue()).decode())
                n += 1
            say(id=vid, done=True, segs=n, ms=int((time.time() - t0) * 1000))
        except Exception as e:   # 내용 없이 종류만
            say(id=vid, error=type(e).__name__)


if __name__ == "__main__":
    main()
