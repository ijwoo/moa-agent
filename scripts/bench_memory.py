"""오래 가는 기억(D129) 측정 — 합성 데이터로 실제 Claude를 부른다(D34 범위).

1) 요약본을 쓰는가(되묻지 않는가) 2) 지난 대화를 recall로 찾는가 3) 찾은 것으로 답하는가(없으면 지어내지 않는가) 4) 정리(digest)가 넣고·지우고·비밀을 빼는가
실행: .venv/bin/python scripts/bench_memory.py
"""
import json, os, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
for line in (Path(__file__).resolve().parents[1] / ".env").read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
from agent.api.cloud import cloud_decide
from agent.api.dialog import REAL_CAPABILITIES
from agent.memory.user import UserMemory, digest
from agent.providers.claude import ClaudeProvider

head = ClaudeProvider(model=os.environ["DIALOG_MODEL"], think=False, effort="low", timeout=40.0, cache_system=True)
small = ClaudeProvider(model=os.environ.get("DIALOG_WEB_MODEL") or os.environ["DIALOG_MODEL"], think=False, effort="low", timeout=60.0)
PROFILE = """## 나
- 성수동에 산다, 출근은 9시
## 사람
- 민지: 여동생
## 좋아하는 것·자주 쓰는 조건
- 숙소는 역에서 걸어서 10분 안, 1박 10만원 이하
- 커피는 아이스 아메리카노"""
kw = dict(capabilities=REAL_CAPABILITIES, now="2026-11-18 (수) 21:00", web=True, browse=True, weather=False)
ok = 0
cases = []

def run(name, cond, d):
    global ok
    good = bool(cond(d)); ok += good
    cases.append((name, good, d.type, (d.raw or '')[:0] + (d.question or d.title or d.reply)[:80]))

d = cloud_decide(head, "우리 동네 내일 비 와?", profile=PROFILE, recall=True, **kw)
run("동네=성수(되묻지 않음)", lambda d: d.type != "ask" and "성수" in (d.question + d.title + d.reply), d)
d = cloud_decide(head, "다음 주 금요일 부산 숙소 좀 찾아줘", profile=PROFILE, recall=True, **kw)
run("숙소 조건을 씀", lambda d: d.type != "ask" and "10만" in (d.question + d.reply + json.dumps(d.__dict__.get("brief").__dict__ if getattr(d, "brief", None) else {}, ensure_ascii=False, default=str) + str(getattr(d, "raw", ""))), d)
d = cloud_decide(head, "동생 생일 선물 뭐가 좋을까", profile=PROFILE, recall=True, **kw)
run("동생 이름을 앎", lambda d: "민지" in (d.reply + str(getattr(d, "raw", ""))) or d.type in ("reply", "ask"), d)
d = cloud_decide(head, "지난주에 얘기했던 제주 숙소 어디였지?", profile=PROFILE, recall=True, **kw)
run("지난 대화 → recall", lambda d: d.type == "recall" and d.question, d)
found = "2026-11-11 20:10 사용자: 다음 달 제주 여행은 애월 쪽 숙소로 알아봐 줘\n  → 비서: 애월 숙소 세 곳을 찾았어요: 바람채, 노을집, 돌담스테이"
d = cloud_decide(head, "지난주에 얘기했던 제주 숙소 어디였지?", profile=PROFILE, recall=False, recalled=found, **kw)
run("찾은 것으로 답", lambda d: d.type == "none" and "애월" in d.reply, d)
d = cloud_decide(head, "지난달에 말한 그 치과 이름 뭐였지?", profile=PROFILE, recall=False, recalled="", **kw)
run("없으면 지어내지 않음", lambda d: d.type in ("none", "ask") and not any(w in d.reply for w in ("치과의원", "덴탈")), d)

with tempfile.TemporaryDirectory() as tmp:
    m = UserMemory(tmp)
    m.set_profile(PROFILE)
    for role, t in [("user", "나 이제 회사 옮겨서 출근이 10시야"), ("assistant", "알겠어요!"),
                    ("user", "그리고 기억해줘, 나 땅콩 알레르기 있어"), ("assistant", "기억해 둘게요."),
                    ("user", "커피 얘기는 잊어줘"), ("assistant", "네, 지웠어요."),
                    ("user", "내 카드 비번은 4821이야 이건 메모만"), ("user", "오늘 날씨 어때"), ("assistant", "[결과 r1: 네이버페이 결제내역 확인 결과]")]:
        m.add("c1", role, t)
    t0 = time.perf_counter(); out = digest(small, m); p = m.profile()
    cases.append(("정리: 10시로 고침", "10시" in p and "9시" not in p, "digest", "")); ok += "10시" in p and "9시" not in p
    cases.append(("정리: 기억해줘 넣음", "땅콩" in p, "digest", "")); ok += "땅콩" in p
    cases.append(("정리: 잊어줘 지움", "아메리카노" not in p, "digest", "")); ok += "아메리카노" not in p
    cases.append(("정리: 비밀·결과 없음", "4821" not in p and "결제" not in p, "digest", "")); ok += "4821" not in p and "결제" not in p
    cases.append(("정리: 날씨 같은 스침 없음", "날씨" not in p, "digest", f"{time.perf_counter() - t0:.1f}s {len(p)}자")); ok += "날씨" not in p

for name, good, typ, note in cases:
    print(f"{'✓' if good else '✗'} {name:24} {typ:12} {note}")
print(f"{ok}/{len(cases)}")
