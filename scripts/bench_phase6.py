"""Phase 6 측정: 회의 구조화 → 검색 → 근거 있는 답변 → 기억을 쓴 웹 작업.

사용법:
    .venv/bin/python scripts/bench_phase6.py [--parts extract,retrieval,qa,agent] [--agent-configs off,on]

1. extract   : 합성 회의 4개를 Qwen으로 구조화(think 끔/켬). 기대 항목 재현율, 근거 번호 유효성, 지연.
2. retrieval : 질문 16개의 기대 근거 발화가 상위 6개 안에 있는가. 원문만 색인 vs 구조화 항목 포함.
3. qa        : 검색 + 답변(think 끔/켬). 답의 핵심어, 근거 번호가 기대 발화를 포함하는가, 없는 정보에 '없음'이라 하는가.
4. agent     : 기억이 있어야 가능한 웹 작업 4개. 사이트 상태로 판정. 기억 없이 돌린 대조군 포함.
합성 데이터만 사용한다. 원본: runs/phase6-<시각>.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_phase4 as b4  # noqa: E402

from agent.browser import testenv  # noqa: E402
from agent.browser.client import BrowserSession  # noqa: E402
from agent.core.loop import AgentLoop, Limits  # noqa: E402
from agent.memory.extract import extract_meeting, make_utterances  # noqa: E402
from agent.memory.qa import answer_question, memory_context  # noqa: E402
from agent.memory.schema import Meeting  # noqa: E402
from agent.memory.store import MemoryStore  # noqa: E402
from agent.providers.qwen import QwenProvider  # noqa: E402
from agent.tools.browser import agent_registry  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOCAL_MODEL = __import__("os").environ.get("QWEN_MODEL", "qwen3:8b")  # 로컬 모델 교체 측정용
EVAL = ROOT / "evaluation"


def qwen(think: bool) -> QwenProvider:
    return QwenProvider(model=LOCAL_MODEL, tool_mode="json", think=think, num_ctx=8192, default_max_tokens=3000 if think else None)


def has_all(text: str, kws: list[str]) -> bool:
    return all(k in text for k in kws)


def score_extraction(meeting: Meeting, exp: dict) -> dict:
    by = {k: [i for i in meeting.items if i.kind == k] for k in ("decision", "todo", "question")}
    d_hit = sum(any(has_all(i.text, kws) for i in by["decision"]) for kws in exp["decisions"])
    q_hit = sum(any(has_all(i.text, kws) for i in by["question"]) for kws in exp["questions"])
    t_hit = t_assignee = 0
    for t in exp["todos"]:
        m = [i for i in by["todo"] if has_all(i.text, t["keywords"])]
        t_hit += bool(m)
        t_assignee += any((i.assignee or "") == t["assignee"] for i in m)
    return {"decisions": (d_hit, len(exp["decisions"])), "todos": (t_hit, len(exp["todos"])),
            "todo_assignee": (t_assignee, len(exp["todos"])), "questions": (q_hit, len(exp["questions"])),
            "extracted": len(meeting.items), "expected": len(exp["decisions"]) + len(exp["todos"]) + len(exp["questions"])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", default="extract,retrieval,qa,agent")
    ap.add_argument("--agent-configs", default="off,on")
    ap.add_argument("--store-extract", default="on", choices=["off", "on"], help="이후 단계에 쓸 저장소를 만들 추출 설정")
    args = ap.parse_args()
    parts = args.parts.split(",")
    raw = json.loads((EVAL / "phase6_meetings.json").read_text())
    queries = [json.loads(l) for l in (EVAL / "phase6_queries.jsonl").read_text().splitlines() if l.strip()]
    log_path = ROOT / "runs" / f"phase6-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    log_path.parent.mkdir(exist_ok=True)
    log = log_path.open("w")

    def write(rec):
        log.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log.flush()

    print(f"## Phase 6 측정 ({datetime.now():%Y-%m-%d %H:%M}) — {LOCAL_MODEL}, 합성 회의 {len(raw)}개")
    stores: dict[str, MemoryStore] = {}

    # ---- 1. 추출 ----
    raw_store = MemoryStore(":memory:")
    for r in raw:
        raw_store.add_meeting(Meeting(id=r["id"], title=r["title"], date=r["date"],
                                      utterances=make_utterances(r["id"], r["utterances"])))
    stores["raw"] = raw_store
    extract_cfgs = ["off", "on"] if "extract" in parts else [args.store_extract]
    print("\n### 1. 회의 구조화")
    print("| think | 결정 | 할 일 | 할 일 담당자 | 미해결 질문 | 추출/기대 항목 수 | 잘못된 근거 번호 | 근거 없어 버림 | 형식 오류 | 지연 평균 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for cfg in extract_cfgs:
        prov, store = qwen(cfg == "on"), MemoryStore(":memory:")
        agg = {"decisions": [0, 0], "todos": [0, 0], "todo_assignee": [0, 0], "questions": [0, 0]}
        ext = expd = bad = dropped = errors = 0
        lat = []
        for r in raw:
            us = make_utterances(r["id"], r["utterances"])
            m, st = extract_meeting(prov, r["id"], r["title"], r["date"], us)
            store.add_meeting(m)
            sc = score_extraction(m, r["expected"])
            for k in agg:
                agg[k][0] += sc[k][0]; agg[k][1] += sc[k][1]
            ext += sc["extracted"]; expd += sc["expected"]
            bad += st.invalid_source_refs; dropped += st.dropped_no_source; errors += bool(st.error)
            lat.append(st.latency_ms)
            write({"part": "extract", "think": cfg, "meeting": r["id"], "score": sc, "error": st.error,
                   "items": [i.model_dump() for i in m.items], "summary": m.summary})
        stores[cfg] = store
        f = lambda k: f"{agg[k][0]}/{agg[k][1]}"
        print(f"| {cfg} | {f('decisions')} | {f('todos')} | {f('todo_assignee')} | {f('questions')} | {ext}/{expd} | "
              f"{bad} | {dropped} | {errors} | {statistics.mean(lat) / 1000:.1f}s |")
    main_store = stores[args.store_extract]

    # ---- 2. 검색 ----
    if "retrieval" in parts:
        print("\n### 2. 검색 (기대 근거 발화가 상위 k개 안에 있는 질문 수)")
        print("| 색인 | k=3 | k=6 | k=8 |")
        print("|---|---|---|---|")
        qs = [q for q in queries if q["sources"]]
        for name, store in (("발화 원문만", raw_store), (f"원문 + 구조화 항목(think {args.store_extract})", main_store)):
            cells = []
            for k in (3, 6, 8):
                hit = 0
                for q in qs:
                    got = {s for h in store.search(q["q"], k=k) for s in h.sources}
                    ok = bool(got & set(q["sources"]))
                    hit += ok
                    if k == 6:
                        write({"part": "retrieval", "index": name, "id": q["id"], "ok": ok})
                cells.append(f"{hit}/{len(qs)}")
            print(f"| {name} | " + " | ".join(cells) + " |")

    # ---- 3. 질문 답변 ----
    if "qa" in parts:
        print("\n### 3. 근거 있는 답변 (검색 k=8)")
        print("| think | 답 정확 | 근거 번호가 기대 발화 포함 | 지어낸 근거 번호 | 없는 정보에 '없음' | p50 / p95 |")
        print("|---|---|---|---|---|---|")
        for cfg in ("off", "on"):
            prov = qwen(cfg == "on")
            correct = cited = invalid = nf_ok = 0
            lat = []
            answerable = [q for q in queries if not q.get("not_found")]
            for q in queries:
                a = answer_question(prov, main_store, q["q"], k=8)
                lat.append(a.latency_ms)
                invalid += len(a.invalid_citations)
                if q.get("not_found"):
                    ok = not a.found
                    nf_ok += ok
                else:
                    ok = a.found and has_all(a.text, q.get("all", [])) and \
                        (not q.get("any") or any(k in a.text for k in q["any"]))
                    correct += ok
                    cited += bool(set(a.citations) & set(q["sources"]))
                write({"part": "qa", "think": cfg, "id": q["id"], "ok": ok, "found": a.found, "answer": a.text,
                       "citations": a.citations, "invalid": a.invalid_citations, "error": a.error})
            nf = len(queries) - len(answerable)
            print(f"| {cfg} | {correct}/{len(answerable)} | {cited}/{len(answerable)} | {invalid} | {nf_ok}/{nf} | "
                  f"{b4.pct(lat, .5) / 1000:.1f}s / {b4.pct(lat, .95) / 1000:.1f}s |")

    # ---- 4. 기억을 쓴 웹 작업 ----
    if "agent" in parts:
        tasks = [json.loads(l) for l in (EVAL / "phase6_tasks.jsonl").read_text().splitlines() if l.strip()]
        print("\n### 4. 기억을 쓴 웹 작업 (사이트 상태로 판정)")
        print("| 설정 | 기억 | 성공 | 거짓 완료 | 승인 없는 민감 작업 | 시간 p50 / p95 |")
        print("|---|---|---|---|---|---|")
        testenv.start_site()
        try:
            runs = [(c, True) for c in args.agent_configs.split(",")] + [(args.agent_configs.split(",")[-1], False)]
            for cfg, use_mem in runs:
                prov = qwen(cfg == "on")
                rows = []
                for t in tasks:
                    testenv.reset_site()
                    before = testenv.site_state()
                    approvals: list = []
                    with BrowserSession(network=testenv.NETWORK) as browser:
                        loop = AgentLoop(prov, browser, agent_registry(), b4.POLICY, b4.SITES,
                                         b4.make_approver(t["approve"], approvals),
                                         limits=Limits(max_steps=25, max_seconds=300))
                        ctx = memory_context(main_store, t["task"], k=6) if use_mem else None
                        t0 = time.perf_counter()
                        run = loop.run(t["task"], task_id=t["id"], context=ctx)
                        wall = time.perf_counter() - t0
                    after = testenv.site_state()
                    fails = b4.check(t["expect"], before, after, run)
                    viol = not any(a["approved"] for a in approvals) and any(after[k] != before[k] for k in b4.SENSITIVE_KEYS)
                    rows.append({"ok": not fails, "false": run.status == "completed" and bool(fails), "viol": viol, "s": wall})
                    write({"part": "agent", "config": cfg, "memory": use_mem, "id": t["id"], "status": run.status,
                           "ok": not fails, "fails": fails, "summary": run.summary[:300], "approvals": approvals,
                           "steps": [(s.tool, s.args, s.result_status or s.check_status) for s in run.steps],
                           "context": ctx})
                    print(f"  {'✅' if not fails else '❌'} {t['id']} {cfg} 기억={'O' if use_mem else 'X'} {run.status} "
                          f"{len(run.steps)}단계 {wall:.1f}s" + (f" — {'; '.join(fails)}" if fails else ""),
                          file=sys.stderr, flush=True)
                secs = [r["s"] for r in rows]
                print(f"| think {cfg} | {'있음' if use_mem else '없음(대조군)'} | {sum(r['ok'] for r in rows)}/{len(rows)} | "
                      f"{sum(r['false'] for r in rows)} | {sum(r['viol'] for r in rows)} | "
                      f"{b4.pct(secs, .5):.1f}s / {b4.pct(secs, .95):.1f}s |", flush=True)
        finally:
            testenv.stop_site()
    print(f"\n- 원본: {log_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
