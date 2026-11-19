"""내 공간(D87) 채우기 측정: 말로 만든 카드가 실제로 채워지는가. 시나리오: evaluation/phase8_space_fill.jsonl

말 하나로 카드를 만들고(판단 모델), 그 카드를 실제로 채운다(대화 판단 → 공개 정보 웹 검색 → 블록으로 옮기기).
판정: 채워졌나(state), 첫 블록의 종류가 그 내용에 맞나, 비서의 사정 설명·꾸밈 글자·주소가 카드에 없나, 글이 카드에 들어갈 길이인가.
값이 사실과 맞는지는 판정하지 않는다(답에 없는 숫자가 든 블록은 코드가 이미 버린다).
사용법: .venv/bin/python scripts/bench_space_fill.py [--only sf-01]
공개 정보만 묻는다(합성 문장). Claude API와 웹 검색을 부른다(카드 하나에 약 $0.02). 서버와 함께 돌려도 된다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from agent.api.dialog import DialogManager
from agent.api.jobs import JobManager
from agent.api.routines import RoutineStore
from agent.api.space import CANNOT_WORDS, Space, SpaceError

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    for line in (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def texts(blocks: list[dict]) -> list[str]:
    out = []
    for b in blocks:
        out += [str(b.get(k, "")) for k in ("text", "label", "value", "note")]
        for i in b.get("items", []):
            out += [i.get("title", ""), i.get("subtitle", ""), i.get("trailing", ""), i.get("label", ""), i.get("value", "")]
    return [t for t in out if t]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    load_env()
    from agent.providers.claude import ClaudeProvider, has_api_key
    from agent.providers.qwen import QwenProvider
    model = os.environ.get("DIALOG_MODEL", "")
    if not model or not has_api_key():
        print("오류: DIALOG_MODEL 또는 API 키가 없다(값은 출력하지 않는다)", file=sys.stderr)
        return 2
    cloud = ClaudeProvider(model=model, think=False, effort=os.environ.get("DIALOG_EFFORT", "low"), timeout=30.0, cache_system=True, max_retries=1)
    web = ClaudeProvider(model=os.environ.get("DIALOG_WEB_MODEL") or model, think=False, effort="low", timeout=40.0, max_retries=1)
    local = QwenProvider(model=os.environ.get("QWEN_MODEL", "qwen3:14b"), tool_mode="json", think=False, num_ctx=8192, timeout=120.0)
    mgr = JobManager(lambda job, hooks: None)
    dlg = DialogManager(local, mgr, cloud=cloud, web=web)
    dlg.real_only = True
    cases = [json.loads(l) for l in (ROOT / "evaluation" / "phase8_space_fill.jsonl").read_text().splitlines() if l.strip()]
    cases = [c for c in cases if not args.only or c["id"] in args.only.split(",")]
    out_path = ROOT / "runs" / f"spacefill-{time.strftime('%Y%m%d-%H%M%S')}-{model}.jsonl"
    out_path.parent.mkdir(exist_ok=True)
    ok = 0
    times = []
    try:
        with out_path.open("w") as log:
            for case in cases:
                sp = Space(None, cloud, ask=dlg.ask_for_space, routines=RoutineStore(None), local=local)
                bad, shape, why = [], [], ""
                t0 = time.perf_counter()
                try:
                    made = sp.edit(case["say"])["space"]["cards"]
                    if len(made) != 1 or made[0]["type"] != "custom":
                        bad.append(f"카드가 하나 만들어지지 않음({[c['type'] for c in made]})")
                    else:
                        card = sp.refresh()["space"]["cards"][0]
                        why = card.get("why") or ""
                        blocks = (card.get("data") or {}).get("blocks") or []
                        shape = [b["kind"] for b in blocks]
                        if card.get("state") != case["expect"]:
                            bad.append(f"state {card.get('state')}({why})")
                        elif case["expect"] == "ok":
                            if not blocks:
                                bad.append("블록 없음")
                            elif blocks[0]["kind"] not in case["first"]:
                                bad.append(f"첫 블록 {blocks[0]['kind']}(기대 {'/'.join(case['first'])})")
                            if case.get("year") and any(b["kind"] == "date" and b["date"] < time.strftime("%Y-%m-%d") for b in blocks):
                                bad.append("지난 날짜")
                            if case.get("has") and case["has"] not in shape:
                                bad.append(f"{case['has']} 블록 없음")
                            ts = texts(blocks)
                            if any(w in t for t in ts for w in CANNOT_WORDS):
                                bad.append("사정 설명이 들어 있음")
                            if any(x in t for t in ts for x in ("**", "http", "@", "\n- ")):
                                bad.append("꾸밈·주소가 들어 있음")
                            if any(b["kind"] == "text" and len(b["text"]) > 160 for b in blocks):
                                bad.append("글이 김")
                            if len([b for b in blocks if b["kind"] != "trend"]) > 3:
                                bad.append(f"블록 {len(blocks)}개")
                except SpaceError as e:
                    bad.append(f"오류 {e.code}")
                dt = time.perf_counter() - t0
                times.append(dt)
                ok += not bad
                first = ""
                if shape:
                    b0 = blocks[0]
                    first = b0.get("value") or b0.get("text") or b0.get("date") or " / ".join(
                        (i.get("title") or f"{i.get('label')} {i.get('value')}") for i in b0.get("items", [])[:3])
                print(f"- {case['id']} {'✅' if not bad else '❌'} {dt:.0f}초 | {case['say'][:22]} → {shape} {first[:60]}" + (f" | {'; '.join(bad)}" if bad else ""))
                log.write(json.dumps({"id": case["id"], "say": case["say"], "ok": not bad, "bad": bad, "shape": shape, "why": why, "s": round(dt, 1)}, ensure_ascii=False) + "\n")
    finally:
        mgr.shutdown()
    times.sort()
    print(f"\n채워진 카드 {ok}/{len(cases)}, 카드 하나 중앙 {times[len(times) // 2]:.0f}초·가장 긴 {times[-1]:.0f}초. 원본: {out_path.relative_to(ROOT)}")
    return 0 if ok == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
