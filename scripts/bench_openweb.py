"""일반 길 측정: 정해진 기능 없이, 에이전트 루프가 실제 공개 사이트를 스스로 열고 누르고 읽어 답하는가.

사용법:
    .venv/bin/python scripts/bench_openweb.py --driver claude-haiku-5-5 [--tasks t1,t2] [--max-steps 20]
    .venv/bin/python scripts/bench_openweb.py --driver local

지키는 것 (사용자 규칙):
- 로그인 없는 일회용 격리 브라우저. 브라우저를 꾸미지 않는다. 공개 https 사이트만 연다.
- 사이트가 막으면(403·418·429, 차단 안내) 그 사이트로는 더 가지 않는다. 캡차는 풀지 않는다.
- 무엇을 바꾸는 동작(결제·주문·제출·신청 …)은 승인 대상이고, 이 측정은 승인하지 않는다 — 읽고 찾는 일만 잰다.
- 검색 사이트의 결과 화면과 네이버는 열지 않는다(검색은 search_web 도구가 후보 주소만 준다).
- 사이트마다 요청을 적게: 과제 하나에 사이트 하나, 단계 한도.
과제의 답은 공개 정보다. 원본: runs/openweb-<시각>.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent.browser.client import BrowserError  # noqa: E402
from agent.core.loop import AgentLoop, Limits  # noqa: E402
from agent.providers.claude import PRICES, ClaudeProvider, cost_usd  # noqa: E402
from agent.providers.qwen import QwenProvider  # noqa: E402
from agent.sandbox.manager import SandboxManager  # noqa: E402
from agent.sites.public import Guarded, OpenWebPolicy, make_search  # noqa: E402
from agent.tools.browser import agent_registry  # noqa: E402
from agent.tools.validate import Policy  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GAVE_UP = ("찾지 못", "확인하지 못", "확인할 수 없", "찾을 수 없", "알 수 없", "막았", "차단", "불가능")   # 끝냈지만 답을 못 얻었다는 말

# (id, 과제, 답에 있어야 할 낱말 중 하나 — 없으면 사람이 본다)
TASKS = [
    ("t1", "한국어 위키백과(ko.wikipedia.org)에서 '세종'(조선의 임금) 문서를 찾아 태어난 해를 알려줘.", ["1397"]),
    ("t2", "국립중앙박물관 홈페이지(www.museum.go.kr)에서 관람 시간 안내를 찾아 수요일에 몇 시까지 여는지 알려줘.", ["21", "9시"]),
    ("t3", "파이썬 공식 문서(docs.python.org)에서 str.removeprefix가 몇 버전에 추가됐는지 찾아줘.", ["3.9"]),
    ("t4", "arxiv.org에서 'Attention Is All You Need' 논문을 검색해 첫 번째 저자와 제출 연도를 알려줘.", ["Vaswani"]),
    ("t5", "깃허브(github.com)의 python/cpython 저장소에서 가장 최근 릴리스(태그) 이름을 알려줘.", ["v3.", "3."]),
    ("t6", "교보문고(www.kyobobook.co.kr)에서 한강의 '채식주의자'를 검색해 종이책 정가를 알려줘.", ["원"]),
    ("t7", "해커뉴스(news.ycombinator.com) 첫 페이지에서 지금 1위 글의 제목과 점수를 알려줘.", ["points", "점"]),
    ("t8", "한국은행 홈페이지(www.bok.or.kr)에서 지금의 기준금리를 찾아줘.", ["%"]),
    ("t9", "서울도서관 홈페이지(lib.seoul.go.kr)에서 평일 운영 시간을 찾아줘.", ["시"]),
    ("t10", "기상청 날씨누리(www.weather.go.kr)에서 부산의 내일 날씨 예보를 찾아줘.", ["부산"]),
]

# 더 어려운 묶음: 입력·고르기가 필요한 조회 화면, 스크립트가 무거운 사이트, 막을 법한 사이트, 두 사이트 견주기
HARD = [
    ("h1", "코레일 승차권 예매 사이트(www.korail.com)에서 내일 서울에서 부산으로 가는 첫 KTX의 출발 시각을 찾아줘. 예매는 하지 않는다.", [":"]),
    ("h2", "고속버스 통합예매(www.kobus.co.kr)에서 내일 서울경부에서 부산으로 가는 첫차 시각을 찾아줘. 예매는 하지 않는다.", [":"]),
    ("h3", "메가박스(www.megabox.co.kr)에서 코엑스 지점의 내일 상영 영화 제목을 세 개 알려줘.", ["1."]),
    ("h4", "다나와(www.danawa.com)에서 '로지텍 MX Keys Mini'를 검색해 최저가를 알려줘.", ["원"]),
    ("h5", "쿠팡(www.coupang.com)에서 '생수 2L 6개'를 검색해 첫 번째 상품의 이름과 가격을 알려줘.", ["원"]),
    ("h6", "정부24(www.gov.kr)에서 주민등록표 등본을 인터넷으로 발급받을 때 수수료가 얼마인지 찾아줘.", ["무료", "원"]),
    ("h7", "교보문고(www.kyobobook.co.kr)와 알라딘(www.aladin.co.kr)에서 한강의 '채식주의자' 종이책 판매가를 각각 찾아 어디가 더 싼지 알려줘.", ["알라딘"]),
    ("h8", "국립중앙도서관(www.nl.go.kr)에서 박경리의 '토지'를 검색해 소장 자료가 있는지 알려줘.", ["토지"]),
    ("h9", "인천국제공항(www.airport.kr)의 오늘 출발 항공편 안내에서 지금 목록 맨 위 항공편의 목적지와 편명을 알려줘.", [""]),
    ("h10", "유튜브(www.youtube.com)에서 'Python tutorial for beginners'를 검색해 첫 번째 영상의 제목과 채널 이름을 알려줘.", [""]),
]


# 조작이 많이 드는 공개 과제(2026-11-15): 지역 고르기·정렬 바꾸기·탭 옮기기·표 읽기·걸러 보기. 예매·티켓 사이트와 전에 막힌 사이트는 넣지 않는다.
# 답은 그날의 값이라 낱말로만 거칠게 본다(사람이 한 번 더 본다).
HARD2 = [
    ("n1", "기상청 날씨누리(www.weather.go.kr)에서 강원도 강릉의 모레 최저기온 예보를 찾아줘.", ["℃", "도"]),
    ("n2", "국립중앙도서관(www.nl.go.kr)에서 박경리의 '토지'를 검색하고, 결과를 발행연도 최신순으로 정렬했을 때 맨 위 자료의 제목과 발행연도를 알려줘.", ["토지"]),
    ("n3", "깃허브(github.com)의 python/cpython 저장소에서 열린 이슈를 댓글 많은 순으로 정렬했을 때 맨 위 이슈의 제목을 알려줘.", [""]),
    ("n4", "PyPI(pypi.org)에서 requests 패키지의 릴리스 기록을 열어 2.0.0 버전이 올라온 날짜를 찾아줘.", ["2013"]),
    ("n5", "공공데이터포털(www.data.go.kr)에서 '미세먼지'를 검색해 오픈 API가 몇 건 나오는지 알려줘.", ["건"]),
    ("n6", "영화진흥위원회 통합전산망(www.kobis.or.kr)의 일별 박스오피스에서 가장 최근 날짜의 1위 영화 제목을 알려줘.", [""]),
    ("n7", "알라딘(www.aladin.co.kr)에서 '채식주의자'를 검색하고 판매량순으로 정렬했을 때 맨 위 책의 제목과 판매가를 알려줘.", ["원"]),
    ("n8", "MDN(developer.mozilla.org)에서 Array.prototype.flat 문서의 브라우저 호환성 표를 보고 Safari가 몇 버전부터 지원하는지 알려줘.", ["12"]),
    ("n9", "인천국제공항(www.airport.kr)의 오늘 출발 항공편 안내에서 목적지를 도쿄(나리타)로 걸러 맨 위 항공편의 편명과 출발 시각을 알려줘.", [":"]),
    ("n10", "유튜브(www.youtube.com)에서 Veritasium 채널의 동영상 탭을 열어 가장 최근 영상의 제목과 조회수를 알려줘.", ["회", "views", "조회"]),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--driver", required=True, help="local | claude-sonnet-5-5 | claude-haiku-5-5")
    ap.add_argument("--tasks", default="", help="쉼표로 고른 과제 id (없으면 전부)")
    ap.add_argument("--set", default="easy", choices=["easy", "hard", "hard2"], help="easy: 읽기·간단한 찾기 10개, hard: 조회 화면·무거운 사이트 10개(절반이 막는 곳), hard2: 조작이 많이 드는 공개 과제 10개")
    ap.add_argument("--plain", action="store_true", help="예전 측정처럼: 복잡한 화면용 도구·운전 규칙 없이(견줘 볼 때)")
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--headed", action="store_true", help="컨테이너 안 가상 화면에 창 있는 브라우저로(자동화 표시는 그대로). 없으면 창 없는 모드")
    ap.add_argument("--max-seconds", type=float, default=0, help="과제 하나의 시간 한도(0이면 운전하는 쪽에 맞춘 기본값)")
    args = ap.parse_args()
    local = args.driver == "local"
    want = [t for t in args.tasks.split(",") if t]
    tasks = [t for t in {"easy": TASKS, "hard": HARD, "hard2": HARD2}[args.set] if not want or t[0] in want]
    from agent.sites.public import BROWSE_RULES, answer_style, note_refused, use_refused
    refused = use_refused(Path.home() / ".local" / "share" / "local-ai-agent" / "sites_refused.json")   # 전에 막았던 곳은 이 측정에서도 열지 않는다
    sandbox = SandboxManager(network="bridge", max_concurrent=1, persist_profile=False,
                             env={"OBS_MAX_LINES": "260", **({"BROWSER_HEADED": "1"} if args.headed else {})})
    web = ClaudeProvider(model="claude-haiku-5-5", think=False, effort="low", timeout=40.0, max_retries=1)   # 검색 도구용
    log_path = ROOT / "runs" / f"openweb-{datetime.now():%Y%m%d-%H%M%S}-{args.set}-{args.driver}.jsonl"
    log_path.parent.mkdir(exist_ok=True)
    limits = Limits(max_steps=args.max_steps, max_seconds=args.max_seconds or (600.0 if local else 180.0))
    print(f"## 일반 길 측정 ({datetime.now():%Y-%m-%d %H:%M}) — 운전: {args.driver}, 브라우저: {'창 모드' if args.headed else '창 없음'}, 과제 {len(tasks)}개, 단계 한도 {limits.max_steps}, 시간 한도 {limits.max_seconds:.0f}초")
    done = 0
    with log_path.open("w") as log:
        for tid, task, must in tasks:
            if local:
                prov = QwenProvider(model="qwen3:14b", tool_mode="json", think=True, num_ctx=8192, timeout=300.0, default_max_tokens=3000)
            else:
                prov = ClaudeProvider(model=args.driver, think=False, effort="low", timeout=60.0)
            steps = []
            denied = []
            t0 = time.perf_counter()
            status, summary, hits, blocked, errors = "error", "", {}, [], []
            try:
                with sandbox.session("openweb-" + args.driver.replace(".", "-")) as raw:
                    b = Guarded(raw, refused, on_warned=note_refused)

                    def approver(req):   # 무엇을 바꾸는 동작은 이 측정에서 승인하지 않는다
                        denied.append(getattr(req, "element_label", "") or "")
                        return False
                    loop = AgentLoop(prov, b, agent_registry(rich=not args.plain and not local), OpenWebPolicy(allowed_hosts=frozenset()), {}, approver,
                                     limits=limits, on_step=steps.append)
                    loop.search = make_search(web)
                    now = datetime.now()
                    rules = "" if args.plain or local else f"\n\n{BROWSE_RULES}\n{answer_style(None)}"   # 실제 일반 길(browse_public)과 같은 지시
                    run = loop.run(f"{task}{rules}\n(지금은 {now:%Y-%m-%d} {'월화수목금토일'[now.weekday()]}요일 {now:%H:%M}이다.)", task_id=tid)
                    status, summary = run.status, run.summary or ""
                    hits, blocked, errors = dict(b.hits), sorted(b.blocked), list(b.errors)
            except Exception as e:   # 브라우저·모델 오류: 그 과제만 실패로 적는다
                summary = f"{type(e).__name__}: {str(e)[:160]}"
            dt = time.perf_counter() - t0
            ok = status == "completed" and any(m in summary for m in must) and not any(w in summary for w in GAVE_UP)
            done += ok
            tools = Counter(s.tool for s in steps if s.tool)
            usd = cost_usd(args.driver, prov.totals) if not local else 0.0
            usd_search = cost_usd("claude-haiku-5-5", web.totals)
            for k in web.totals:
                web.totals[k] = 0
            rec = {"id": tid, "driver": args.driver, "status": status, "ok": ok, "seconds": round(dt, 1), "steps": len(steps),
                   "tools": dict(tools), "hosts": hits, "blocked": blocked, "denied": denied, "usd": round(usd, 4),
                   "usd_search": round(usd_search, 4), "summary": summary[:600], "errors": errors,
                   "trace": [{"tool": s.tool, "check": s.check_status, "result": s.result_status, "url": (s.url or "")[:120]} for s in steps]}
            log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            log.flush()
            print(f"- {tid} {'✅' if ok else '❌'} {status} {dt:.0f}초 {len(steps)}단계 ${usd + usd_search:.3f} "
                  f"사이트 {hits} 막힘 {blocked or '없음'} | {' '.join(summary.split())[:200]}", flush=True)
            time.sleep(3)
    print(f"\n- 맞게 끝낸 과제 {done}/{len(tasks)} (자동 판정 — 답은 사람이 한 번 더 본다). 원본: {log_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
