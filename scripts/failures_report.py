"""실사용 실패의 꼴 요약(내용 없음). 사용: .venv/bin/python scripts/failures_report.py [--days 7] [--rows]"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent.api.failures import FailureLog

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=float, default=7)
ap.add_argument("--rows", action="store_true")
a = ap.parse_args()
log = FailureLog()
s = log.summary(a.days)
print(f"최근 {a.days:g}일 끝내지 못한 작업: {s['total']}건")
for k, n in s["by_kind"]:
    print(f"  {n:3}  {k}")
print("사이트별:", ", ".join(f"{k} {n}" for k, n in s["by_site"]))
if a.rows:
    for r in log.rows(a.days):
        print(json.dumps(r, ensure_ascii=False))
