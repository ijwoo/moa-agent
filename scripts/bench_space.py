"""내 공간(D87) 측정: 말로 화면을 짜는 판단을 실제 판단 모델로 잰다. 시나리오: evaluation/phase8_space.jsonl

판정은 모델의 말이 아니라 **받아들여진 구성**(카드 종류·크기·차례)으로 한다. 카드의 내용은 채우지 않는다(구성만 잰다).
사용법: .venv/bin/python scripts/bench_space.py [--only sp-01] [--repeat 1] [--model claude-sonnet-5-5]
서버와 함께 돌려도 된다(테스트 사이트를 쓰지 않는다). 합성 문장만 쓴다. Claude API를 부른다(문장 하나에 한 번).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from agent.api.routines import RoutineStore, parse_when
from agent.api.space import Space, SpaceError

ROOT = Path(__file__).resolve().parent.parent
APP_CARDS = ["calendar_today", "weather", "meeting_todos"]


def load_env() -> None:
    for line in (ROOT / ".env").read_text().splitlines() if (ROOT / ".env").exists() else []:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def check(exp: dict, before: list[dict], out: dict) -> list[str]:
    """기대와 다른 점들(없으면 통과)."""
    cards = out["space"]["cards"]
    types = [c["type"] for c in cards]
    size = {c["type"]: c["size"] for c in cards}
    bad = []
    for t in exp.get("has", []):
        if t not in types:
            bad.append(f"{t} 없음")
    for t in exp.get("absent", []):
        if t in types:
            bad.append(f"{t} 남아 있음")
    if "count" in exp and len(cards) != exp["count"]:
        bad.append(f"카드 {len(cards)}장(기대 {exp['count']})")
    if "min_count" in exp and len(cards) < exp["min_count"]:
        bad.append(f"카드 {len(cards)}장(기대 {exp['min_count']}장 이상)")
    if "first" in exp and (not types or types[0] != exp["first"]):
        bad.append(f"맨 위가 {types[:1]}")
    if "last" in exp and (not types or types[-1] != exp["last"]):
        bad.append(f"맨 아래가 {types[-1:]}")
    for t, s in exp.get("size", {}).items():
        if size.get(t) != s:
            bad.append(f"{t} 크기 {size.get(t)}(기대 {s})")
    if "custom" in exp and types.count("custom") != exp["custom"]:
        bad.append(f"말로 만든 카드 {types.count('custom')}장(기대 {exp['custom']})")
    if "small_custom" in exp and sum(1 for c in cards if c["type"] == "custom" and c["size"] == "small") != exp["small_custom"]:
        bad.append("작은 말 카드 수가 다름")
    if "adjacent_small" in exp:
        a, b = exp["adjacent_small"]
        ok = a in types and b in types and abs(types.index(a) - types.index(b)) == 1 and size[a] == size[b] == "small"
        if not ok:
            bad.append(f"{a}·{b}가 작게 나란하지 않음")
    if exp.get("unchanged") and ([(c["id"], c["size"]) for c in cards] != [(c["id"], c["size"]) for c in before] or out["changes"]):
        bad.append("구성이 바뀜")
    if exp.get("changed") and not out["changes"]:
        bad.append("바뀐 것이 없음")
    if exp.get("say") and not out["say"].strip():
        bad.append("못 한 까닭을 말하지 않음")
    if "asking" in exp and bool(out.get("asking")) != exp["asking"]:
        bad.append("되물었어야 함" if exp["asking"] else "되물으면 안 됨")
    if "actions" in exp and sum(1 for c in cards if c.get("action")) != exp["actions"]:
        bad.append(f"단추가 붙은 카드 {sum(1 for c in cards if c.get('action'))}장(기대 {exp['actions']})")
    if exp.get("kept_ids") and not {c["id"] for c in before} <= {c["id"] for c in cards}:
        bad.append("말하지 않은 카드가 새로 만들어짐(id가 바뀜)")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--model", default="")
    args = ap.parse_args()
    load_env()
    from agent.providers.claude import ClaudeProvider, has_api_key
    model = args.model or os.environ.get("DIALOG_MODEL", "")
    if not model or not has_api_key():
        print("오류: DIALOG_MODEL 또는 API 키가 없다(값은 출력하지 않는다)", file=sys.stderr)
        return 2
    cloud = ClaudeProvider(model=model, think=False, effort=os.environ.get("DIALOG_EFFORT", "low"), timeout=30.0, cache_system=True, max_retries=1)
    scenarios = [json.loads(l) for l in (ROOT / "evaluation" / "phase8_space.jsonl").read_text().splitlines() if l.strip()]
    scenarios = [s for s in scenarios if not args.only or s["id"] in args.only.split(",")]
    out_path = ROOT / "runs" / f"space-{time.strftime('%Y%m%d-%H%M%S')}-{model}.jsonl"
    out_path.parent.mkdir(exist_ok=True)
    ok = total = 0
    times = []
    with out_path.open("w") as log:
        for _ in range(args.repeat):
            for sc in scenarios:
                store = RoutineStore(None)
                for name in sc.get("routines", []):
                    store.add(name, "합성 예약", parse_when("daily 08:00"))
                sp = Space(None, cloud, routines=store)
                for i, step in enumerate(sc["steps"], 1):
                    before = sp.public()["cards"]
                    t0 = time.perf_counter()
                    try:
                        res = sp.edit(step["say"], APP_CARDS)
                        bad = check(step["expect"], before, res)
                    except SpaceError as e:
                        res, bad = {"space": {"cards": before}, "changes": [], "say": ""}, [f"오류 {e.code}"]
                    dt = time.perf_counter() - t0
                    times.append(dt)
                    total += 1
                    ok += not bad
                    shape = [(c["type"].replace("_today", ""), c["size"][0]) for c in res["space"]["cards"]]
                    print(f"- {sc['id']}.{i} {'✅' if not bad else '❌'} {dt:.1f}초 | {step['say'][:34]} → {shape}" + (f" | {'; '.join(bad)}" if bad else ""))
                    log.write(json.dumps({"id": sc["id"], "step": i, "say": step["say"], "ok": not bad, "bad": bad, "shape": shape, "changes": res["changes"],
                                          "said": res["say"], "s": round(dt, 2)}, ensure_ascii=False) + "\n")
    times.sort()
    print(f"\n맞게 한 걸음 {ok}/{total}, 한 걸음 중앙 {times[len(times) // 2]:.1f}초·가장 긴 {times[-1]:.1f}초. 원본: {out_path.relative_to(ROOT)}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
