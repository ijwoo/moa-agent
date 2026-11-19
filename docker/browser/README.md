# 브라우저 샌드박스 컨테이너

| 파일 | 내용 |
|---|---|
| `Dockerfile` | Playwright 1.64.1 공식 이미지 + Python `playwright==1.64.1`, `pwuser`로 실행 (D14) |
| `seccomp_chromium.json` | moby 기본 seccomp(커밋 `4b8214f`) + `clone`·`unshare`·`setns` 허용. `scripts/make_seccomp_profile.py`로 생성 (D16) |
| `apparmor_browser` | Docker 29.5.2 `docker-default` + `userns` 허용 (ABI 4.0, `network unix` 명시) (D16) |
| `sandbox_check.py` | 컨테이너 안에서 Chromium 샌드박스 상태 점검 |

## 준비 (한 번)
```sh
colima start                     # VM 시작 시 AppArmor 프로필이 자동 로드됨 (아래 provision)
docker build -t local-ai-agent/browser:0.1 docker/browser
```
`~/.colima/default/colima.yaml`의 provision 설정 (원본 백업: `colima.yaml.bak-20261110`):
```yaml
provision:
  - mode: system
    script: apparmor_parser -r -W <저장소 경로>/docker/browser/apparmor_browser
```
저장소 위치를 옮기면 이 경로도 바꿔야 한다. 확인: `colima ssh -- sudo aa-status | grep local-ai-agent`

## 실행 옵션
코드의 기준은 `src/agent/browser/container.py`의 `run_args()`. 요약:
`--user pwuser --cap-drop ALL --cap-add SYS_CHROOT --security-opt no-new-privileges
--security-opt seccomp=docker/browser/seccomp_chromium.json --security-opt apparmor=local-ai-agent-browser
--network none --memory 1g --cpus 2 --pids-limit 256 --shm-size 512m --init` (Docker 소켓 마운트 없음)

## 점검
```sh
RUN_INTEGRATION=1 .venv/bin/pytest tests/integration/test_browser_sandbox.py
```
