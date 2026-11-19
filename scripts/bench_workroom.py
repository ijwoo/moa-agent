"""작업실 루프 측정(D96 2단계): 합성 과제를 끝까지 돌려 걸음·시간·비용·결과를 잰다.

    .venv/bin/python scripts/bench_workroom.py [--model claude-sonnet-5-5] [--task short]

결과 파일은 ~/.local/share/local-ai-agent/workroom/projects/bench-…/ 에 남는다. 측정 기록은 runs/workroom-*.jsonl.
과제의 내용은 모두 지어낸 것이다(개인 정보 없음).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent.providers.claude import ClaudeProvider, load_env_file  # noqa: E402
from agent.workroom.loop import WorkroomLoop  # noqa: E402
from agent.workroom.manager import open_project  # noqa: E402

TASKS = {
    "short": ("10초 안팎의 세로(1080x1920) 소개 영상을 만들어 줘. 주제는 '바다의 소금은 어디서 왔을까'.\n"
              "- 그림 3장을 직접 그려서(파이썬으로 — 남의 사진은 쓰지 않는다) 장면으로 쓴다.\n"
              "- 한국어 나레이션 2~3문장(edge-tts), 화면 아래에 자막.\n"
              "- 다 만들면 장면 시트를 만들어 직접 보고 검수한 뒤 /work/out/final.mp4로 끝낸다."),
    # 문서 만들기(README 예시, 2026-11-19): 자료는 아래에 지어낸 것만 — 개인 정보·바깥 자료 없음
    "doc": ("A4 한 장짜리 보고서 PDF를 만들어 줘. 제목 '동네 카페 3곳 11월 매출 비교'(가상의 카페, 숫자는 아래 그대로).\n"
            "자료(지어낸 값, 단위 만 원): 숲길카페 1주 412·2주 455·3주 398·4주 520 / 모퉁이커피 1주 380·2주 362·3주 401·4주 433 / "
            "바다빵집카페 1주 290·2주 344·3주 375·4주 410.\n"
            "- 맨 위에 결론 3줄(가장 많이 판 곳, 가장 많이 오른 곳, 4주 합계 순위), 가운데 주별 표(합계 행 포함), 아래 주별 선 그래프 하나.\n"
            "- 계산은 코드로 하고 표·글의 숫자가 자료와 맞는지 직접 확인한다. 한글 글꼴(나눔·Noto CJK)로, 깔끔한 업무 문서 모양.\n"
            "- PDF 첫 쪽을 PNG로 찍어 view_image로 직접 보고 글자 깨짐·겹침을 고친 뒤 /work/out/report.pdf와 /work/out/report.png로 끝낸다."),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--task", default="short", choices=list(TASKS))
    ap.add_argument("--max-usd", type=float, default=3.0)
    a = ap.parse_args()
    load_env_file()
    provider = ClaudeProvider(model=a.model, effort="medium")
    searcher = ClaudeProvider(model="claude-haiku-5-5")
    search = lambda q: searcher.search("공개 정보를 짧게 답한다.", q)[:2]
    name = f"bench-{a.task}-{datetime.now():%m%d-%H%M}"
    t0 = time.time()
    with open_project(name) as room:
        loop = WorkroomLoop(room, provider, search, max_usd=a.max_usd,
                            on_step=lambda s: print(f"  {'✓' if s.ok else '✗'} {s.tool:<11} {s.seconds:5.1f}s  {s.brief}", flush=True))
        out = loop.run(TASKS[a.task])
        gate = room.gate_log()
        probe = {}
        for f in out.files:
            if f.endswith(".mp4"):
                r = room.run(f"ffprobe -v error -show_entries format=duration:stream=codec_type,width,height -of json '{f}'", 30)
                probe[f] = r.output[:400]
    hosts = sorted({l.split()[2].rsplit(":", 1)[0] + " " + l.split()[3] for l in gate.splitlines() if l.startswith("[egress]") and "listen" not in l})
    rec = {"at": datetime.now().isoformat(timespec="seconds"), "model": a.model, "task": a.task, "status": out.status,
           "steps": len(out.steps), "failed_steps": sum(not s.ok for s in out.steps), "tools": [s.tool for s in out.steps],
           "usd": round(out.usd, 3), "seconds": round(out.seconds), "files": out.files, "probe": probe, "egress": hosts,
           "project": name}
    Path("runs").mkdir(exist_ok=True)
    with open(f"runs/workroom-{datetime.now():%Y%m%d-%H%M%S}.jsonl", "w") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps({k: v for k, v in rec.items() if k != "tools"}, ensure_ascii=False, indent=1))
    print("\n요약:\n" + out.summary)


if __name__ == "__main__":
    main()
