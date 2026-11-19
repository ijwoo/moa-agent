"""로그인해 둔 실제 사이트의 화면을 로컬 모델이 '읽고 답하는' 일 측정 (첫 브라우저 읽기 기능 후보). 읽기만 한다.

대상: 네이버페이 결제내역(pay.naver.com/pc/history) — 한 화면에 결제 건이 줄 단위로 있다.
두 방식을 같은 물음으로 잰다.
- loop: 에이전트 루프. 모델이 도구를 고른다. 도구는 읽기용만 준다(navigate·observe·scroll·extract_text·finish — 누르기·입력 없음).
- page: 코드가 정해진 주소를 열고 화면 글 전체를 모델에 한 번 주어 답만 받는다.
- rows: page와 같지만 코드가 화면에서 결제 줄만 뽑아(광고·메뉴 없음) 모델에 준다.

**화면 내용·답은 출력하지 않는다**(실제 개인 데이터). 판정은 코드가 화면에서 뽑은 줄(상품 이름·금액·상태·날짜)과 답을 대조해
숫자·참거짓만 남긴다. 다시 판정할 때 쓰는 원본은 이 맥의 runs/site_read_private.jsonl(권한 600, git 제외)에만 둔다.
사용법: .venv/bin/python scripts/bench_site_read.py [--only p-01] [--arm loop,page,rows] [--repeat 3]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit

from agent.cli import _make_sites
from agent.core.loop import AgentLoop, Limits
from agent.providers.base import Message
from agent.providers.qwen import QwenProvider
from agent.sites import NAVER
from agent.sites.read import PAGE_PROMPT, PAGES, READ_TOOLS, pay_rows, screen_for_model
from agent.tools.spec import ToolRegistry
from agent.tools.validate import Policy

ROOT = Path(__file__).resolve().parent.parent
URL = "https://pay.naver.com/pc/history"
SITES = {"네이버페이 결제내역": URL}
TASKS = [
    ("p-01", 3, "네이버페이 결제내역에서 가장 최근에 결제한 3건의 상품 이름, 금액, 상태를 알려줘."),
    ("p-02", 1, "네이버페이 결제내역에서 가장 최근에 결제한 것 하나의 상품 이름과 금액, 결제한 날짜를 알려줘."),
    ("p-03", 0, "네이버페이 결제내역 첫 화면에서 상태가 구매확정완료인 결제 건이 있는지, 있으면 상품 이름과 금액을 알려줘."),
]
PRICE = re.compile(r"\d{1,3}(?:,\d{3})+|\d{4,}")
DONE = "구매확정"


def overlap(answer: str, name: str) -> float:
    """상품 이름 가운데 답에 그대로 이어져 나온 가장 긴 부분의 비율(0~1)."""
    if len(name) < 2:
        return 0.0
    m = SequenceMatcher(None, answer, name, autojunk=False).find_longest_match(0, len(answer), 0, len(name))
    return round(m.size / len(name), 2)


def named(answer: str, name: str) -> bool:
    if len(name) < 8:   # 짧은 이름은 통째로 나와야 한다(5자 이름이 다른 상품과 3자만 겹쳐도 60%가 되는 오판정을 막는다)
        return name in answer
    return overlap(answer, name) >= 0.6 or (len(name) > 16 and overlap(answer, name[:16]) >= 0.9)   # 긴 이름은 앞부분만 말해도 인정


def date_in(answer: str, date: str) -> bool:
    m = re.match(r"\s*(?:\d{2,4}\.\s*)?(\d{1,2})\.\s*(\d{1,2})\.", date)
    return bool(m and re.search(rf"(?<!\d)0?{m.group(1)}\s*[./월-]\s*0?{m.group(2)}(?!\d)", answer))


def judge(tid: str, k: int, answer: str, rows: list[dict], page_text: str) -> dict:
    prices_on_page = {p.replace(",", "") for p in PRICE.findall(page_text)}
    said = {p.replace(",", "") for p in PRICE.findall(answer)}
    out = {"rows_on_page": len(rows), "wrong_prices": len({p for p in said if p not in prices_on_page and len(p) >= 4}),
           "rows_named": sum(named(answer, r["name"]) for r in rows)}
    if tid == "p-03":
        want = [r for r in rows if r["status"].startswith(DONE)]
        names = {r["name"] for r in want}
        others = [r for r in rows if not r["status"].startswith(DONE) and r["name"] not in names]   # 같은 이름의 다른 건은 가를 수 없어 뺀다
        out.update(want_on_page=len(want), want_named=sum(named(answer, r["name"]) for r in want),
                   want_prices=sum(r["price"].replace(",", "") in said for r in want),
                   others_named=sum(named(answer, r["name"]) for r in others), says_none=bool(re.search(r"없", answer)))
        out["ok"] = (out["want_named"] == len(want) and out["others_named"] == 0 and out["wrong_prices"] == 0) if want \
            else out["says_none"]
        return out
    top = rows[:k]
    out.update(asked=k, names_ok=sum(named(answer, r["name"]) for r in top),
               prices_ok=sum(r["price"].replace(",", "") in said for r in top),
               status_ok=sum(r["status"] in answer or (r["status"].startswith(DONE) and DONE in answer) for r in top),
               date_ok=sum(date_in(answer, r["date"]) for r in top),
               later_rows_named=sum(named(answer, r["name"]) for r in rows[k:]))
    need = ("names_ok", "prices_ok", "status_ok") if tid == "p-01" else ("names_ok", "prices_ok", "date_ok")
    out["ok"] = len(top) == k and all(out[n] == k for n in need) and out["wrong_prices"] == 0 and \
        (tid != "p-02" or out["later_rows_named"] == 0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--arm", default="loop,page")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    model = os.environ.get("QWEN_MODEL", "qwen3:14b")
    provider = QwenProvider(model=model, tool_mode="json", think=True, num_ctx=8192, timeout=300.0, default_max_tokens=3000)
    policy = Policy(allowed_hosts=frozenset({"pay.naver.com"}), navigate_urls=frozenset({URL}), mode="read_draft")
    sites = _make_sites()
    print(f"## 사이트 읽기 측정(네이버페이 결제내역) — {model}, think 켬 (내용은 출력하지 않는다)", flush=True)
    priv = ROOT / "runs" / "site_read_private.jsonl"
    try:
        for rep in range(args.repeat):
            for tid, k, task in TASKS:
                if args.only and tid not in args.only.split(","):
                    continue
                for arm in args.arm.split(","):
                    t0 = time.perf_counter()
                    res: dict = {"id": tid, "arm": arm, "rep": rep + 1}
                    with sites.sandbox.session("bench:site:naver") as b:
                        if not sites.restore(b, NAVER):
                            print("보관한 로그인 상태가 없다. 앱에서 먼저 로그인한다.")
                            return 3
                        if arm == "loop":
                            loop = AgentLoop(provider, b, ToolRegistry(READ_TOOLS), policy, SITES, lambda req: False,
                                             limits=Limits(max_seconds=600.0, max_steps=8))
                            run = loop.run(task, task_id=tid)
                            answer = " ".join([run.summary, *run.report.get("done", [])])
                            res.update(status=run.status, steps=len(run.steps), tools=[s.tool for s in run.steps],
                                       step_status=[s.result_status or s.check_status for s in run.steps], llm_calls=run.llm_calls)
                            obs = b.request("observe")
                        else:
                            b.request("navigate", {"url": URL})
                            time.sleep(1.5)
                            obs = b.request("observe")
                            r = provider.generate([Message(role="system", content=PAGE_PROMPT),
                                                   Message(role="user", content=f"[물음]\n{task}\n\n[화면]\n" + screen_for_model(
                                                       PAGES["naver_pay_recent"], obs["text"], use_rows=arm == "rows"))],
                                                  temperature=0.0, max_tokens=3000)
                            answer = (r.text or "").strip()
                            res.update(status="done" if answer else "empty", llm_calls=1)
                        host = urlsplit(obs.get("url") or "").hostname
                        rows = pay_rows(obs["text"])
                    res.update(s=round(time.perf_counter() - t0), answer_chars=len(answer), on_pay_host=host == "pay.naver.com",
                               obs_chars=len(obs["text"]), **judge(tid, k, answer, rows, obs["text"]))
                    fd = os.open(priv, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    with os.fdopen(fd, "a") as f:
                        f.write(json.dumps({"id": tid, "arm": arm, "answer": answer, "rows": rows}, ensure_ascii=False) + "\n")
                    print(json.dumps(res, ensure_ascii=False), flush=True)
                    with (ROOT / "runs" / "site_read.jsonl").open("a") as f:   # 숫자·참거짓만. 내용 없음
                        f.write(json.dumps({**res, "model": model}, ensure_ascii=False) + "\n")
    finally:
        sites.sandbox.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
