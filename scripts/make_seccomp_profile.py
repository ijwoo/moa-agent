"""Chromium 샌드박스용 seccomp 프로필 생성.

기반: Docker(moby) 기본 프로필 — 최신 보안 기본값(io_uring 차단 등)을 유지한다.
추가: Chromium 자체 샌드박스(사용자 네임스페이스)에 필요한 clone·unshare·setns만 CAP_SYS_ADMIN 없이 허용.

Playwright 저장소의 seccomp_profile.json을 쓰지 않는 이유 (2026-11-10 비교, D16):
  오래된 moby 기본값 기반이라 clone3·landlock 등 47개 syscall 규칙이 없고, io_uring 3종을 허용한다.

사용법:
    curl -sfL -o /tmp/x/moby_default.json \\
      https://raw.githubusercontent.com/moby/profiles/<커밋>/seccomp/default.json
    .venv/bin/python scripts/make_seccomp_profile.py /tmp/x/moby_default.json docker/browser/seccomp_chromium.json
"""

from __future__ import annotations

import json
import sys

EXTRA_ALLOW = ["clone", "unshare", "setns"]


def build(moby: dict) -> dict:
    if moby.get("defaultAction") != "SCMP_ACT_ERRNO":
        raise SystemExit("예상과 다른 기본 프로필: defaultAction이 SCMP_ACT_ERRNO가 아님")
    profile = json.loads(json.dumps(moby))  # 깊은 복사
    profile["syscalls"].append({
        "names": EXTRA_ALLOW,
        "action": "SCMP_ACT_ALLOW",
        "comment": "Chromium sandbox: unprivileged user/pid/net namespaces (local-ai-agent D16)",
    })
    return profile


def main() -> int:
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as f:
        moby = json.load(f)
    with open(dst, "w") as f:
        json.dump(build(moby), f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"생성: {dst} (기반 syscall 규칙 {len(moby['syscalls'])}개 + 추가 {EXTRA_ALLOW})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
