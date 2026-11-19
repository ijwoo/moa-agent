"""브라우저 컨테이너 실행 옵션 (D15, D16). 이 목록 밖의 권한은 주지 않는다.

검증 (2026-11-10, colima VM Ubuntu 24.04, Docker 29.5.2):
  이 옵션으로 Chromium chrome://sandbox → "You are adequately sandboxed."
  (PID·네트워크 네임스페이스, seccomp-BPF 켬, 렌더러는 별도 사용자 네임스페이스)
  컨테이너 프로세스 자체의 실효 권한은 0 (CapEff=0). SYS_CHROOT는 bounding set에만 남아
  Chromium이 만든 사용자 네임스페이스 안에서의 chroot에만 쓰인다.
선행 조건: VM에 AppArmor 프로필 `local-ai-agent-browser` 로드 (colima provision, docker/browser/README.md)
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
import os as _os

# 기본 이미지. BROWSER_IMAGE로 바꿀 수 있다(실험용 — 공식 크롬을 넣은 이미지 등, D86). 코드·설정이 정한 값만
IMAGE = _os.environ.get("BROWSER_IMAGE") or "local-ai-agent/browser:0.2"
SECCOMP_PROFILE = REPO_ROOT / "docker" / "browser" / "seccomp_chromium.json"
APPARMOR_PROFILE = "local-ai-agent-browser"


def run_args(
    *,
    network: str = "none",
    memory: str = _os.environ.get("BROWSER_MEMORY", "2g"),   # 1g → 2g(D121, 실기기 2026-11-18: 에어코리아 첫 화면에서 "Page crashed" — 2g에서 열렸다). 한도일 뿐 평소 쓰는 양은 같다
    cpus: str = "2",
    pids_limit: int = 256,
    shm_size: str = "512m",
) -> list[str]:
    """`docker run` 옵션 목록 (이미지·명령 제외). Docker 소켓은 절대 마운트하지 않는다."""
    return [
        "--rm", "--init",
        "--user", "pwuser",
        "--cap-drop", "ALL",
        "--cap-add", "SYS_CHROOT",
        "--security-opt", "no-new-privileges",
        "--security-opt", f"seccomp={SECCOMP_PROFILE}",
        "--security-opt", f"apparmor={APPARMOR_PROFILE}",
        # 루트 파일시스템은 읽기 전용, 쓰기는 크기 제한 tmpfs에만 (Chromium 프로필·임시 파일)
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
        "--tmpfs", "/home/pwuser:rw,nosuid,nodev,size=256m,uid=1001,gid=1001",
        "--network", network,
        "--memory", memory,
        "--cpus", cpus,
        "--pids-limit", str(pids_limit),
        "--shm-size", shm_size,
    ]
