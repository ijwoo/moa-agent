"""통화 목소리(D117): 일꾼에게 글을 주고 조각을 받아 둔다 — 가짜 일꾼으로(모델 없이)."""

import base64
import json
import sys

import pytest

from agent.voice.engine import VoiceEngine, VoiceError

FAKE = r'''
import sys, json, base64
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    m = json.loads(line)
    if "cancel" in m:
        continue
    if m["text"] == "실패":
        print(json.dumps({"id": m["id"], "error": "RuntimeError"}), flush=True); continue
    for i, w in enumerate(m["text"].split()):
        print(json.dumps({"id": m["id"], "seg": i, "b64": base64.b64encode(("RIFF" + w).encode()).decode()}), flush=True)
    print(json.dumps({"id": m["id"], "done": True, "segs": i + 1}), flush=True)
'''


@pytest.fixture
def eng(tmp_path):
    f = tmp_path / "fake.py"
    f.write_text(FAKE)
    e = VoiceEngine("py", "ref.wav", "ref.txt", worker_cmd=[sys.executable, str(f)])
    yield e
    e.stop()


def test_segments_in_order_then_end(eng):
    vid = eng.start("안녕 하세요")
    assert eng.segment(vid, 0) == "RIFF안녕".encode()
    assert eng.segment(vid, 1) == "RIFF하세요".encode()
    assert eng.segment(vid, 2) is None          # 끝났다 → 204


def test_unknown_and_failed(eng):
    with pytest.raises(VoiceError) as e:
        eng.segment("vnope123", 0)
    assert e.value.code == "not_found"
    vid = eng.start("실패")
    with pytest.raises(VoiceError) as e:
        eng.segment(vid, 0)
    assert e.value.code == "unavailable"        # 앱은 폰 목소리로 읽는다


def test_stamp_starts_voice_for_each_reply(eng):
    from agent.api.dialog import _stamp
    out = _stamp({"replies": [{"text": "네 들려요 👍"}]}, voice=True, tts=eng)
    r = out["replies"][0]
    assert r["speak"] == "네 들려요" and r["voice_id"].startswith("v") and eng.segment(r["voice_id"], 0) == "RIFF네".encode()
    assert "voice_id" not in _stamp({"replies": [{"text": "네"}]}, voice=False, tts=eng)["replies"][0]


def test_from_env_off_without_flag(monkeypatch):
    monkeypatch.delenv("VOICE_TTS", raising=False)
    assert VoiceEngine.from_env() is None


def test_cancel_before_any_segment_ends_waiting_reads_at_once():
    # 실기기 2026-11-18: 통화를 걸자마자 끊어 첫 조각 전에 취소 — 기다리던 GET이 10초 뒤 409였다. 취소하면 곧바로 끝(204)
    import threading
    import time as _t
    from agent.voice.engine import VoiceEngine
    eng = VoiceEngine("py", "w", "t", worker_cmd=["sh", "-c", "read x; sleep 30"])
    vid = eng.start("안녕하세요")
    got = {}
    th = threading.Thread(target=lambda: got.setdefault("v", eng.segment(vid, 0, wait_s=10.0)))
    t0 = _t.time()
    th.start()
    _t.sleep(0.2)
    eng.cancel(vid)
    th.join(timeout=5)
    assert got.get("v", "x") is None and _t.time() - t0 < 3
    eng.stop()
