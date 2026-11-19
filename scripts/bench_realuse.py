"""실사용에서 나온 실패를 다시 재는 스크립트. 시나리오: evaluation/real_use_failures.jsonl

API 서버와 같은 조건(읽기·초안 전용 정책, 로컬 모델)으로 실행하고, 승인 응답은 시나리오에 적힌 순서대로 준다.
판정은 사이트 상태(저장된 초안, 발송 0건). 모델의 보고 문장은 참고용으로 출력만 한다.
사용법: [QWEN_MODEL=qwen3:14b] [AGENT_THINK=1] .venv/bin/python scripts/bench_realuse.py [--only r-001]
주의: API 서버(agent.cli serve)와 동시에 돌리지 않는다(같은 테스트 사이트를 쓴다).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from agent.api.runner import SITES
from agent.browser import testenv
from agent.browser.client import BrowserSession
from agent.core.loop import AgentLoop, Limits
from agent.providers.qwen import QwenProvider
from agent.tools.browser import agent_registry
from agent.tools.validate import Policy

ROOT = Path(__file__).resolve().parent.parent


ITEM = __import__("re").compile(r"^(\d{1,2}[.)]|[-•*·])\s")
HEAD_UNDECIDED = ("미정", "정해지지", "미결", "보류", "결론", "논의 필요", "추후")


def undecided_in_decided(body: str, items: list[str]) -> bool:
    """r-005 판정: '결정' 제목 아래 목록 줄에 미정 항목이 있으면 True. 제목 줄 = ':'로 끝나거나 #·** 로 시작하거나 짧은 줄."""
    section = None
    for line in body.replace("\r\n", "\n").split("\n"):
        t = line.strip()
        if not t:
            continue
        is_item = bool(ITEM.match(t))
        heading = not is_item and (t.endswith(":") or t.startswith(("#", "**", "[")) or len(t) <= 25)
        if heading:
            if any(w in t for w in HEAD_UNDECIDED):
                section = "undecided"
            elif "결정" in t or "확정" in t:
                section = "decided"
            continue
        if is_item and section == "decided" and any(k in t for k in items):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    model = os.environ.get("QWEN_MODEL", "qwen3:14b")
    think = os.environ.get("AGENT_THINK", "1") == "1"
    cases = [json.loads(l) for l in (ROOT / "evaluation/real_use_failures.jsonl").read_text().splitlines() if l.strip()]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only.split(",")]
    policy = Policy(allowed_hosts=frozenset(testenv.SITE_HOSTS), navigate_urls=frozenset(SITES.values()), mode="read_draft")
    provider = QwenProvider(model=model, tool_mode="json", think=think, num_ctx=8192, timeout=300.0,
                            default_max_tokens=3000 if think else None)
    print(f"## 실사용 실패 재측정 — {model}, think={'켬' if think else '끔'}")
    testenv.start_site()
    passed = 0
    try:
        for c in cases:
            testenv.reset_site()
            script = list(c.get("approvals", []))
            shown: list[dict] = []

            approved_cards: list[dict] = []

            def approver(req):
                shown.append(dict(req.form_values))
                if not script:
                    return False
                step = script.pop(0)
                if step["decision"] == "approve":
                    approved_cards.append(dict(req.form_values))
                    return True
                return step.get("reason") or False

            t0 = time.perf_counter()
            with BrowserSession(network=testenv.NETWORK) as browser:
                run = AgentLoop(provider, browser, agent_registry(), policy, SITES, approver,
                                limits=Limits(max_seconds=900.0)).run(c["task"], task_id=c["id"], context=c.get("context"))
            st, exp, fails = testenv.site_state(), c["expect"], []
            if run.status != exp["status"]:
                fails.append(f"status {run.status}")
            if len(st["sent"]) != exp.get("sent", 0):
                fails.append(f"발송 {len(st['sent'])}건")
            d = exp.get("draft")
            if d:
                ok = [x for x in st["drafts"] if x["to"].strip() == d["to"] and (not d.get("subject") or x["subject"] == d["subject"])
                      and all(k in x["body"] for k in d["body_contains_all"])]
                if len(st["drafts"]) != 1 or not ok:
                    fails.append(f"초안 불일치: {st['drafts']}")
                # 줄바꿈이 있어야 r-004(요약이 줄바꿈을 합침)를 재현한다. 200자 초과는 통합 테스트가 결정적으로 확인
                if d.get("body_min_newlines") and not any(x["body"].count("\n") >= d["body_min_newlines"] for x in st["drafts"]):
                    fails.append(f"본문 줄바꿈이 {d['body_min_newlines']}개 미만이라 이 시나리오를 재현하지 못함")
            bodies = [x["body"] for x in st["drafts"]]
            if exp.get("undecided_items") and any(undecided_in_decided(b, exp["undecided_items"]) for b in bodies):
                fails.append("미정 항목이 '결정' 목록 안에 있음")          # r-005
            bad = [w for w in exp.get("body_must_not_contain", []) for b in bodies if w in b]
            if bad:
                fails.append(f"본문에 금지 표현: {bad}")                     # r-006
            if exp.get("approval_matches_saved"):  # r-004: 승인 카드 = 실제 저장 내용 (길이·줄바꿈까지)
                saved = [{"받는 사람": x["to"], "제목": x["subject"], "본문": x["body"].replace("\r\n", "\n")}
                         for x in st["drafts"]]
                if not approved_cards or approved_cards[-1] != (saved[-1] if saved else None):
                    fails.append("승인 카드와 저장된 초안이 다름")
            passed += not fails
            print(f"{'✅' if not fails else '❌'} {c['id']} {run.status} {len(run.steps)}단계 {time.perf_counter() - t0:.0f}s"
                  + (f" — {'; '.join(fails)}" if fails else ""))
            print(f"   승인 화면에 보인 본문(길이·줄바꿈): {[(len(b), b.count(chr(10))) for b in (s.get('본문') or '' for s in shown)]}")
            print(f"   모델 보고(참고): {run.summary[:160]}")
            log_dir = ROOT / "runs"
            log_dir.mkdir(exist_ok=True)
            with (log_dir / "realuse.jsonl").open("a") as lf:   # 판정 근거 확인용 (합성 데이터만)
                lf.write(json.dumps({"id": c["id"], "model": model, "think": think, "ok": not fails, "fails": fails,
                                     "drafts": st["drafts"], "summary": run.summary, "report": getattr(run, "report", {}),
                                     "code": os.environ.get("PYTHONPATH", "현재")}, ensure_ascii=False) + "\n")
    finally:
        testenv.stop_site()
    print(f"\n통과 {passed}/{len(cases)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
