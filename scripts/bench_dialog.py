"""대화 담당 측정 (D42 2단계): 메시지마다 none / ask / task / route_claude를 맞게 고르는가.

사용법:
    .venv/bin/python scripts/bench_dialog.py [--configs off,on] [--model qwen|claude-haiku-5-5] [--repeat 1]
판정:
    - 유형 정확도(accept에 있는 유형도 정답), 혼동표
    - 위험한 오분류: 정답이 none·route_claude·ask인데 task (원하지 않은 작업 생성)
    - 주소: expect_address가 브리프에 있는가, no_task_address 시나리오에서 작업이 만들어졌는가(추측 주소)
    - ask의 선택지(expect_choices), 안전장치(guard)가 바꾼 횟수, 지연
합성 데이터만 사용한다. 원본: runs/dialog-<시각>.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_phase4 as b4  # noqa: E402

from agent.api.dialog import CLAIM, DEFAULT_CAPABILITIES, NEGATION, PROMISE, SEND_CAPABILITIES, decide  # noqa: E402
from agent.api.dialog import said_host  # noqa: E402
from agent.api.cloud import cloud_decide  # noqa: E402
from agent.providers.claude import PRICES, ClaudeProvider, cost_usd, has_api_key  # noqa: E402
from agent.providers.qwen import QwenProvider  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOCAL_MODEL = __import__("os").environ.get("QWEN_MODEL", "qwen3:14b")
TYPES = ("none", "route_claude", "task", "ask")
# --send 측정용 가상 본인 주소 (실제 설정에서는 서비스 이름이 네이버·지메일로 붙는다)
ME = {"나(사용자 본인)": "test-me@mail.test", "나(네이버)": "test-me@mail.test", "나(지메일)": "test-me2@mail.test"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", default="off,on", help="think off|on|hybrid(끔으로 고르고 task·'발췌 있는 none'만 켬으로 재판단)")
    ap.add_argument("--model", default="qwen", choices=["qwen", *PRICES])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--tone", default=None, choices=["friendly", "formal"], help="대화 담당 말투(D48). 없으면 기본")
    ap.add_argument("--context-prefix", default=None,
                    help="발췌(context)가 있는 시나리오의 발췌 맨 앞에 붙일 줄(앱이 붙이는 머리 줄 측정용). \\n은 줄바꿈")
    ap.add_argument("--send", action="store_true",
                    help="본인 주소 발송을 켠 요청처럼 측정(4단계): '나'의 가상 주소를 연락처에 넣고 발송 능력 설명을 쓴다")
    ap.add_argument("--head", default="local", choices=["local", "cloud"],
                    help="cloud: Claude가 판단하는 길(api/cloud.py, B안). --model에 Claude 모델을 준다")
    ap.add_argument("--context-mode", default="handle", choices=["handle", "send"],
                    help="cloud에서 회의 자료를 어떻게 주나 — handle: 붙어 있다는 사실만, send: 내용까지")
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high"], help="Claude 호출의 effort")
    ap.add_argument("--set", default="dev", choices=["dev", "holdout", "holdout2", "holdout3", "realuse", "chat", "send", "context", "web", "all"],
                    help="dev: phase8_dialog.jsonl(프롬프트를 고칠 때 본 세트), holdout: 고친 뒤 처음 보는 세트")
    ap.add_argument("--shop-api", action="store_true", help="쇼핑을 공식 검색 API 길로(D71): 모든 문장에 shop을 고를 수 있게 하고 API 길의 지시를 붙인다")
    ap.add_argument("--web", action="store_true", help="cloud에서 공개 정보 웹 검색(web_lookup)을 고를 수 있게 한다(D60). 검색은 하지 않고 판단만 잰다")
    args = ap.parse_args()
    claude = args.model != "qwen"
    if claude and not has_api_key():
        print("오류: .env에 ANTHROPIC_API_KEY가 없다 (값은 출력하지 않는다)", file=sys.stderr)
        return 4
    files = {"dev": ["phase8_dialog.jsonl"], "holdout": ["phase8_dialog_holdout.jsonl"],
             "holdout2": ["phase8_dialog_holdout2.jsonl"], "realuse": ["phase8_dialog_realuse.jsonl"],
             "holdout3": ["phase8_dialog_holdout3.jsonl"],
             "chat": ["phase8_dialog_chat.jsonl"],
             "send": ["phase8_dialog_send.jsonl"], "context": ["phase8_dialog_context.jsonl"], "web": ["phase8_dialog_web.jsonl"],
             "all": ["phase8_dialog.jsonl", "phase8_dialog_holdout.jsonl", "phase8_dialog_holdout2.jsonl"]}[args.set]
    cases = [json.loads(l) for f in files for l in (ROOT / "evaluation" / f).read_text().splitlines() if l.strip()]
    if args.context_prefix:
        pre = args.context_prefix.replace("\\n", "\n")
        cases = [{**c, "context": pre + c["context"]} if c.get("context") else c for c in cases]
    log_path = ROOT / "runs" / f"dialog-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    log_path.parent.mkdir(exist_ok=True)
    label = args.model if claude else LOCAL_MODEL
    print(f"## 대화 담당 측정 ({datetime.now():%Y-%m-%d %H:%M}) — {label}, {args.set} 시나리오 {len(cases)}개 × {args.repeat}회")
    print("| 설정 | 유형 정확도 | none | route_claude | task | ask | 원치 않은 작업 생성 | 주소 맞음 | 추측 주소로 작업 | ask 선택지 | 안전장치 작동 | 지연 p50 / p95 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    with log_path.open("w") as log:
        for cfg in args.configs.split(","):
            think = cfg == "on"
            slow = None
            if claude:
                prov = ClaudeProvider(model=args.model, think=think, effort=args.effort, cache_system=args.head == "cloud")
            else:
                mk = lambda t: QwenProvider(model=LOCAL_MODEL, tool_mode="json", think=t, num_ctx=8192, timeout=300.0,
                                            default_max_tokens=3000 if t else 800)
                prov = mk(think)
                slow = mk(True) if cfg == "hybrid" else None   # 혼합: think 끔으로 고르고 일부만 think 켬으로 재판단
            escalations = 0
            per = Counter()
            tot = Counter()
            confusion = Counter()
            unwanted = addr_ok = addr_n = guessed = choices_ok = choices_n = guards = 0
            tone_ok = tone_n = 0
            lat = []
            for _ in range(args.repeat):
                for c in cases:
                    if args.head == "cloud":
                        feat = args.set in ("context", "web")   # 새 기능(사이트 읽기·로그인·메일 브리핑)은 맥락 묶음에서만 켠다
                        d = cloud_decide(prov, c["text"], history=[tuple(h) for h in c.get("history", [])],
                                         context=c.get("context"), contacts=c.get("contacts"), tone=args.tone,
                                         capabilities=SEND_CAPABILITIES if args.send else DEFAULT_CAPABILITIES,
                                         me=ME if args.send else None, results=c.get("results"),
                                         send_context=args.context_mode == "send", now="2026-11-13 (금) 14:20",
                                         state=(["네이버 로그인: " + ("되어 있음" if c.get("state_login") else "안 되어 있음"),
                                                 "연결된 메일 계정: 네이버"] if feat else []),
                                         active=set(c.get("active", [])),
                                         pages={"naver_pay_recent": "네이버페이 결제내역"} if feat else None,
                                         sites={"naver": "네이버", "naver_mail": "네이버 메일 연결",
                                                **{k: v.split(",")[0] for k, v in c.get("inside", {}).items()}} if feat else None,
                                         inside=c.get("inside") if feat else None,
                                         mail_brief=feat, web=args.web, find=args.web, weather="weather" in c.get("abilities", []),
                                         reaction="reaction" in c.get("abilities", []),
                                         shop=args.shop_api or "shop" in c.get("abilities", []), view=c.get("view"),
                                         browse=bool(c.get("browse")), pointing=c.get("pointing"),
                                         shop_drive="api" if args.shop_api else "shop_drive" in c.get("abilities", []),
                                         new_site=feat and said_host(c["text"]) is not None)
                    else:
                        d = decide(prov, c["text"], history=[tuple(h) for h in c.get("history", [])],
                                   context=c.get("context"), contacts=c.get("contacts"), escalate=slow, tone=args.tone,
                                   capabilities=SEND_CAPABILITIES if args.send else DEFAULT_CAPABILITIES,
                                   me=ME if args.send else None)
                    escalations += d.escalated_from is not None
                    # 말투: 모델이 쓴 none·ask 답이 고른 말투를 따르는가 (formal=합니다체, friendly=해요체)
                    if args.tone and d.type in ("none", "ask") and d.reply.strip():
                        sents = [x for x in re.split(r"(?<=[.!?~])\s+", d.reply.strip()) if x]
                        last = sents[-1].rstrip(" .!?~)ㅎㅋ^")
                        tone_n += 1
                        tone_ok += (last.endswith(("니다", "니까", "십시오", "시오")) if args.tone == "formal"
                                    else not last.endswith(("니다", "니까")))
                    ok = d.type == c["expect"] or d.type in c.get("accept", [])
                    # 실사용 실패 시나리오의 추가 판정 (답 내용)
                    if any(w in d.reply for w in c.get("reply_must_not_contain", [])):
                        ok = False
                    if c.get("reply_must_contain_any") and not any(w in d.reply for w in c["reply_must_contain_any"]):
                        ok = False
                    if c.get("reply_must_not_promise") and PROMISE.search(d.reply):
                        ok = False
                    if c.get("must_not_claim_about"):   # 그 낱말이 든 문장에서 "된다"고 했는가(부정이 없는 주장) — 낱말만 보던 판정이 맞는 답을 틀렸다고 셌다
                        for sent in re.split(r"[.!?\n]+", d.reply):
                            if any(w in sent.lower() for w in c["must_not_claim_about"]) and CLAIM.search(sent) and not NEGATION.search(sent):
                                ok = False
                    if c.get("no_task") and d.type in ("task", "site_read"):
                        ok = False
                    if c.get("expect_reaction") is not None and bool(d.reaction) != c["expect_reaction"]:
                        ok = False   # 반응 이모지를 달 말인가(D64)
                    if c.get("no_task") and d.type == "browse":
                        ok = False
                    if d.type == "browse" and c.get("question_any") and not any(w in d.question for w in c["question_any"]):
                        ok = False
                    if c.get("no_web") and d.type == "web_lookup":
                        ok = False
                    if d.type == "web_lookup" and c.get("expect_find") is not None and d.find != c["expect_find"]:
                        ok = False   # 직접 찾는 작업으로 할 말인가(D61)
                    if d.type in ("web_lookup", "shop"):   # 검색어: 풀어 쓴 낱말이 있는가, 넣으면 안 되는 것이 없는가
                        if c.get("question_must_contain_any") and not any(w in d.question for w in c["question_must_contain_any"]):
                            ok = False
                        if any(w in d.question for w in c.get("question_must_not_contain", [])):
                            ok = False
                    if c.get("expect_target") is not None and d.target != c["expect_target"] and (c["expect_target"] or d.type == "weather"):
                        ok = False
                    if c.get("expect_when") and d.type == "weather" and d.question not in c["expect_when"]:
                        ok = False
                    if c.get("expect_sources") is not None and d.type == "task" and sorted(d.sources) != sorted(c["expect_sources"]):
                        ok = False
                    if c.get("expect_address") and d.type == "task" and d.brief and c["expect_address"] not in " ".join(d.brief.targets):
                        ok = False
                    btxt = " ".join([d.title, d.brief.purpose, d.brief.instructions, *d.brief.targets]) if d.brief else ""
                    if any(w in btxt for w in c.get("brief_must_not_contain", [])):
                        ok = False
                    if c.get("brief_must_contain_any") and not any(w in btxt for w in c["brief_must_contain_any"]):
                        ok = False
                    # r-005d: '결정된 사항' 틀에 미정 항목을 섞지 않고 나눠 쓰라고 지시했는가
                    if c.get("brief_separates_undecided") and d.brief is not None:
                        ins = d.brief.instructions
                        sep = ("미정 사항", "미정인 ", "정해지지 않은", "미결 사항", "나눠", "나누어", "구분", "따로", "별도")
                        # '아직 결정되지 않음'은 미정 표시다 — '결정'이라는 낱말만 보고 틀렸다고 하지 않는다(머리 줄 측정에서 발견)
                        if re.search(r"결정(?!되지\s*않|하지\s*않|되지\s*못|을\s*못)", ins) and not any(w in ins for w in sep):
                            ok = False
                    per[c["expect"]] += ok
                    tot[c["expect"]] += 1
                    confusion[(c["expect"], d.type)] += 1
                    lat.append(d.latency_ms)
                    guards += d.guard is not None
                    if d.type == "task" and c["expect"] != "task" and "task" not in c.get("accept", []):
                        unwanted += 1
                    brief_text = json.dumps(d.brief.model_dump(), ensure_ascii=False) if d.brief else ""
                    if c.get("expect_address"):
                        addr_n += 1
                        addr_ok += c["expect_address"] in brief_text
                    if c.get("no_task_address") and d.type == "task":
                        guessed += 1
                    if c.get("expect_choices"):
                        choices_n += 1
                        choices_ok += d.type == "ask" and len(d.choices) >= 2
                    log.write(json.dumps({"config": cfg, "id": c["id"], "expect": c["expect"], "got": d.type,
                                          "raw_type": d.raw_type, "guard": d.guard, "escalated_from": d.escalated_from, "ok": ok, "reply": d.reply,
                                          "choices": d.choices, "title": d.title, "target": d.target, "sources": d.sources,
                                          "replies": d.replies, "question": d.question, "reaction": d.reaction,
                                          "brief": d.brief.model_dump() if d.brief else None,
                                          "ms": round(d.latency_ms), "error": d.error}, ensure_ascii=False) + "\n")
                    log.flush()
                    print(f"  {'✅' if ok else '❌'} {c['id']} {cfg} 기대 {c['expect']} → {d.type}"
                          f"{' (guard ' + d.guard + ')' if d.guard else ''} {d.latency_ms / 1000:.1f}s",
                          file=sys.stderr, flush=True)
            n = sum(tot.values())
            cell = lambda t: f"{per[t]}/{tot[t]}"
            if args.head == "cloud":
                other = ", ".join(f"{t} {per[t]}/{tot[t]}" for t in sorted(tot) if t not in TYPES)
                print(f"  - Claude 판단({args.model}, effort {args.effort}, 회의 자료 {args.context_mode})" + (f" — 새 유형: {other}" if other else ""), flush=True)
            print(f"| {'혼합' if cfg == 'hybrid' else 'think ' + cfg} | **{sum(per.values())}/{n}** | {cell('none')} | {cell('route_claude')} | {cell('task')} | "
                  f"{cell('ask')} | {unwanted} | {addr_ok}/{addr_n} | {guessed} | {choices_ok}/{choices_n} | {guards} | "
                  f"{b4.pct(lat, .5) / 1000:.1f}s / {b4.pct(lat, .95) / 1000:.1f}s |", flush=True)
            if slow is not None:
                none_fast = [l for l, c in zip(lat, cases * args.repeat) if c["expect"] == "none" and not c.get("context")]
                print(f"  - 혼합: 재판단 {escalations}/{n}회, 발췌 없는 일상 대화 지연 p50 {b4.pct(none_fast, .5) / 1000:.1f}s", flush=True)
            if args.tone:
                print(f"  - 말투({args.tone}): 모델이 쓴 none·ask 답 {tone_ok}/{tone_n}이 고른 말투", flush=True)
            wrong = {k: v for k, v in confusion.items() if k[0] != k[1]}
            if wrong:
                print("  - 오분류(기대→결과): " + ", ".join(f"{a}→{b} {v}" for (a, b), v in sorted(wrong.items())), flush=True)
            if claude:
                t = prov.totals
                print(f"  - 토큰: 호출 {t['calls']}회, 입력 {t['input']:,} + 캐시 읽기 {t['cache_read']:,}, 출력 {t['output']:,} "
                      f"→ 약 ${cost_usd(args.model, t):.4f}", flush=True)
    print(f"\n- 원본: {log_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
