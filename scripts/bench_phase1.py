"""Phase 1 측정: 한국어 명령 → TaskRequest 변환의 유효율·필드 정확도·지연·메모리.

사용법:
    .venv/bin/python scripts/bench_phase1.py [--repeat N] [--set evaluation/phase1_commands.jsonl]

채점 규칙 (expected에 있는 필드만 채점):
    intent, needs_approval, needs_clarification: 정확히 일치
    target: 문자열 목록 중 하나라도 출력에 포함되면 정답 (공백 무시). null이면 출력도 null
    time_ref: 기대 문자열이 출력에 포함되면 정답 (공백 무시). null이면 출력도 null
    needs_approval은 정책 적용 전 '모델 원 출력'으로 채점한다 (정책 적용 후 값은 따로 집계)
    JSON/스키마 실패 응답은 채점 대상 필드를 모두 오답으로 센다.

결과 원본은 runs/phase1-<시각>.jsonl (git 제외)에 저장된다. 합성 명령만 사용한다.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

from agent.config import get_provider
from agent.providers.base import ProviderError
from agent.tasks.parser import parse_command

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ["intent", "target", "time_ref", "needs_approval", "needs_clarification"]


def _norm(s: str) -> str:
    return "".join(s.split())


def score_field(field: str, expected, actual) -> bool:
    if field in ("target", "time_ref"):
        if expected is None:
            return actual is None
        if actual is None:
            return False
        cands = expected if isinstance(expected, list) else [expected]
        return any(_norm(c) in _norm(actual) for c in cands)
    return expected == actual


def percentile(values: list[float], p: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


class OllamaRssSampler:
    """ollama 관련 프로세스 RSS 합계의 최댓값을 주기적으로 샘플링한다 (MB)."""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self.peak_mb = 0.0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _sample(self) -> float:
        out = subprocess.run(["ps", "-axo", "rss=,command="], capture_output=True, text=True).stdout
        total_kb = 0
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and "ollama" in parts[1] and "bench_phase1" not in parts[1]:
                total_kb += int(parts[0])
        return total_kb / 1024

    def _run(self):
        while not self._stop.is_set():
            self.peak_mb = max(self.peak_mb, self._sample())
            self._stop.wait(self.interval)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()


def ollama_ps(host: str) -> list[dict]:
    try:
        return httpx.get(f"{host}/api/ps", timeout=5).json().get("models", [])
    except Exception:
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default=str(ROOT / "evaluation/phase1_commands.jsonl"))
    ap.add_argument("--repeat", type=int, default=1, help="명령당 반복 횟수 (지연 통계용)")
    args = ap.parse_args()

    cases = [json.loads(l) for l in Path(args.set).read_text().splitlines() if l.strip()]
    provider = get_provider()
    host = str(provider._client.base_url).rstrip("/")  # type: ignore[attr-defined]

    # 콜드 로드 시간: 첫 호출(모델 적재 포함)을 따로 측정하고 통계에서 뺀다.
    t0 = time.perf_counter()
    try:
        parse_command(provider, "테스트")
    except ProviderError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 3
    cold_ms = (time.perf_counter() - t0) * 1000

    run_path = ROOT / "runs" / f"phase1-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    run_path.parent.mkdir(exist_ok=True)

    latencies: list[float] = []
    out_tokens: list[int] = []
    valid = total = 0
    field_hits = {f: 0 for f in FIELDS}
    field_total = {f: 0 for f in FIELDS}
    policy_approval_hits = policy_approval_total = 0
    failures: list[str] = []

    with OllamaRssSampler() as sampler, run_path.open("w") as log:
        for rep in range(args.repeat):
            for case in cases:
                total += 1
                exp = case["expected"]
                try:
                    r = parse_command(provider, case["input"])
                except ProviderError as e:
                    r = None
                    err = str(e)
                if r is not None and r.response is not None:
                    latencies.append(r.response.latency_ms)
                    if r.response.usage.output_tokens is not None:
                        out_tokens.append(r.response.usage.output_tokens)
                ok = r is not None and r.ok
                valid += ok
                hits = {}
                for f in FIELDS:
                    if f not in exp:
                        continue
                    field_total[f] += 1
                    hit = ok and score_field(f, exp[f], getattr(r.raw_task, f))
                    hits[f] = hit
                    field_hits[f] += hit
                if "needs_approval" in exp:
                    policy_approval_total += 1
                    policy_approval_hits += ok and r.task.needs_approval == exp["needs_approval"]
                missed = [f for f, h in hits.items() if not h]
                if missed and rep == 0:
                    failures.append(f"{case['id']}: {', '.join(missed)}")
                log.write(json.dumps({
                    "id": case["id"], "rep": rep,
                    "model": getattr(provider, "model", None), "provider": provider.name,
                    "settings": {"temperature": 0.0, "think": getattr(provider, "think", None)},
                    "valid": ok,
                    "error": (r.error if r is not None else err),
                    "output": r.raw_task.model_dump() if ok else (r.response.text if r and r.response else None),
                    "hits": hits,
                    "latency_ms": r.response.latency_ms if r and r.response else None,
                    "usage": r.response.usage.model_dump() if r and r.response else None,
                }, ensure_ascii=False) + "\n")
        loaded = ollama_ps(host)

    scored = sum(field_total.values())
    hit_sum = sum(field_hits.values())
    print(f"## Phase 1 측정 결과 ({datetime.now():%Y-%m-%d %H:%M})")
    print(f"- 모델: {getattr(provider, 'model', '?')} / {provider.name} / temperature=0, think={getattr(provider, 'think', None)}")
    print(f"- 명령 {len(cases)}개 × 반복 {args.repeat}회 = {total}회")
    print(f"- JSON·스키마 유효율: {valid}/{total} ({valid / total:.1%})")
    print(f"- 필드 정확도(전체): {hit_sum}/{scored} ({hit_sum / scored:.1%})")
    for f in FIELDS:
        if field_total[f]:
            print(f"  - {f}: {field_hits[f]}/{field_total[f]} ({field_hits[f] / field_total[f]:.1%})")
    print(f"  - needs_approval (정책 적용 후): {policy_approval_hits}/{policy_approval_total}")
    if latencies:
        print(f"- 지연: p50 {percentile(latencies, .5):.0f}ms / p95 {percentile(latencies, .95):.0f}ms "
              f"/ 최대 {max(latencies):.0f}ms (콜드 로드 포함 첫 호출 {cold_ms:.0f}ms, 통계 제외)")
    if out_tokens:
        print(f"- 출력 토큰: 평균 {statistics.mean(out_tokens):.0f}")
    print(f"- ollama 프로세스 RSS 최대: {sampler.peak_mb:.0f}MB")
    for m in loaded:
        print(f"- /api/ps: {m.get('name')} size={m.get('size', 0) / 2**30:.2f}GiB "
              f"size_vram={m.get('size_vram', 0) / 2**30:.2f}GiB context={m.get('context_length', '?')}")
    if failures:
        print("- 틀린 항목(1회차): " + "; ".join(failures))
    print(f"- 원본: {run_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
