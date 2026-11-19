"""대화 맥락 작은 측정: 앞에서 한 말 기억(R1), 앞 답을 가리키는 말 풀이(R2). 가상 데이터만.

작업 결과가 대화 기록에 남아 있다고 가정하고(지금 서버는 남기지 않는다) 대화 담당 decide()를 그대로 부른다.
실행: .venv/bin/python scripts/bench_context.py   (혼합 2회 + think 켬 1회, 약 6분)
"""
import json, sys, time, re, os
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
from agent.api.dialog import decide
from agent.providers.qwen import QwenProvider

MODEL = os.environ.get("QWEN_MODEL_BENCH", "qwen3:14b")
PAY = ("네이버페이 결제내역 최근 3건이에요.\n1. 상품: 무선 마우스 | 금액: 23,900원 | 결제일시: 11. 10. 09:12 결제 | 상태: 결제완료\n"
       "2. 상품: 기계식 키보드 | 금액: 54,000원 | 결제일시: 11. 8. 20:40 결제 | 상태: 구매확정완료\n"
       "3. 상품: 충전 케이블 | 금액: 9,900원 | 결제일시: 11. 5. 13:05 결제 | 상태: 구매확정완료")
PAYH = [("user", "네이버페이 결제내역에서 최근에 결제한 3건을 알려줘"), ("assistant", PAY)]
CTX = "## 제품 회의 (2026-11-10)\n요약: 베타 일정과 출시 준비 논의(가상)\n[00:03] 오팀장: 베타 테스트는 2주간 진행하기로 확정합니다. 가격표 최종본은 한주임이 금요일까지 공유해 주세요."
FILL = [("user", "오늘 날씨 좋네"), ("assistant", "네, 산책하기 좋은 날이에요."), ("user", "ㅋㅋ 고마워"), ("assistant", "별말씀을요!")]
CASES = [
 ("R1-1 이름", [("user", "내 동생 이름은 민지야"), ("assistant", "민지 님이군요, 기억해 둘게요.")] + FILL, "아까 말한 내 동생 이름 뭐였지?", None, ["민지"], "none"),
 ("R1-2 약속", [("user", "다음 주 화요일 3시에 치과 예약했어"), ("assistant", "알겠어요.")] + FILL + FILL, "치과 몇 시였지?", None, ["3시"], "none"),
 ("R1-3 사정", [("user", "나 오늘 쉬는 날이야"), ("assistant", "푹 쉬세요!"), ("user", "점심은 벌써 먹었어"), ("assistant", "잘 하셨어요.")], "나 오늘 회사 가야 하나?", None, ["쉬는"], "none"),
 ("R2-1 둘째", PAYH, "두 번째 거 얼마였지?", None, ["54,000"], "none"),
 ("R2-2 최댓값", PAYH, "그중에 제일 비싼 건 뭐야?", None, ["키보드"], "none"),
 ("R2-3 합계", PAYH, "셋 다 합치면 얼마야?", None, ["87,800"], "none"),
 ("R2-4 조건", PAYH + FILL, "아까 그 내역에서 구매확정 안 된 건 뭐였어?", None, ["마우스"], "none"),
 ("R2-5 회의자료와 섞임", PAYH, "두 번째 거 얼마였지?", CTX, ["54,000"], "none"),
 ("R2-6 이어받기", [("user", "토요일에 영화랑 전시 중에 뭐가 나을까"), ("assistant", "전시는 입장료가 15,000원, 영화는 14,000원이에요. 날씨가 좋으면 전시를 추천해요.")], "그럼 더 싼 걸로 두 장이면 얼마야?", None, ["28,000"], "none"),
 ("R2-7 이거 메일로(회의자료 있음)", PAYH, "이거 정리해서 메일 초안으로 써줘", CTX, ["마우스", "키보드", "결제"], "task"),
]

def mk(t): return QwenProvider(model=MODEL, tool_mode="json", think=t, num_ctx=8192, timeout=300.0, default_max_tokens=3000 if t else 800)

def run(cfg, reps):
    fast, slow = (mk(cfg == "on"), mk(True) if cfg == "hybrid" else None)
    ok_n = tot = 0; lat = []
    for name, hist, text, ctx, must, want in CASES:
        for r in range(reps):
            t = time.perf_counter()
            d = decide(fast, text, history=hist, context=ctx, escalate=slow)
            s = time.perf_counter() - t
            blob = d.reply + " " + (d.title or "") + " " + (json.dumps(d.brief.__dict__, ensure_ascii=False, default=str) if d.brief else "")
            hit = any(m in blob.replace(" ", "") or m in blob for m in must)
            meeting = bool(re.search("베타|가격표|오팀장", blob))
            ok = hit and not (ctx and name.startswith("R2-7") and meeting and not hit)
            ok_n += ok; tot += 1; lat.append(s)
            print(json.dumps({"cfg": cfg, "case": name, "rep": r + 1, "type": d.type, "raw": d.raw_type, "guard": d.guard, "esc": d.escalated_from,
                              "s": round(s, 1), "ok": ok, "meeting_words": meeting, "reply": blob.strip()[:160]}, ensure_ascii=False), flush=True)
    lat.sort()
    print(f"## {cfg}: {ok_n}/{tot}, 지연 중앙값 {lat[len(lat)//2]:.1f}s, 최대 {lat[-1]:.1f}s", flush=True)

for cfg, reps in [("hybrid", 2), ("on", 1)]:
    run(cfg, reps)
