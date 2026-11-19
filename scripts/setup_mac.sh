#!/bin/zsh
# moa-agent 맥 설치(한 번). 다시 돌려도 된다 — 이미 된 것은 건너뛴다.
# 하는 일: 필요한 도구 확인 → colima(컨테이너) 시작 → 브라우저 격리 프로필 적재 → 컨테이너 이미지 빌드 → 파이썬 가상환경 → .env 준비
# 하지 않는 일: API 키 넣기(사용자가 .env에 직접), 사이트 로그인(사람이 앱에서), 자동 시작 등록(마지막에 안내)
# 사용: scripts/setup_mac.sh [--with-workroom]
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
WITH_WORKROOM=0
for a in "$@"; do
  case $a in
    --with-workroom) WITH_WORKROOM=1 ;;
    *) echo "모르는 옵션: $a"; exit 2 ;;
  esac
done

say() { print -P "%F{green}==>%f $*"; }
need() { command -v "$1" >/dev/null 2>&1 || { print -P "%F{red}없음:%f $1 — $2"; exit 1; }; }

[[ $(uname) == Darwin ]] || { echo "맥(macOS)용 설치 스크립트입니다."; exit 1; }
need brew "https://brew.sh 에서 Homebrew를 먼저 설치하세요"
PY=$(command -v python3.14 || true)
[[ -n $PY ]] || { say "python 3.14 설치"; brew install python@3.14; PY=$(command -v python3.14); }
for t in colima docker; do command -v $t >/dev/null || { say "$t 설치"; brew install $t; }; done
command -v ollama >/dev/null || print -P "%F{yellow}참고:%f 로컬 모델(Qwen3)을 쓰려면 ollama가 필요합니다: brew install ollama && ollama pull qwen3:14b"

say "colima 시작"
colima status >/dev/null 2>&1 || colima start --cpu 4 --memory 8

say "브라우저 격리 프로필(AppArmor) 적재"
colima ssh -- sudo apparmor_parser -r -W "$ROOT/docker/browser/apparmor_browser" || \
  print -P "%F{yellow}경고:%f AppArmor 프로필을 적재하지 못했습니다 — docker/browser/README.md 참고"
print "   colima를 다시 켤 때마다 자동으로 적재하려면 ~/.colima/default/colima.yaml의 provision에 위 명령을 넣으세요(docker/browser/README.md)."

say "브라우저 이미지 빌드"
docker image inspect local-ai-agent/browser:0.2 >/dev/null 2>&1 || docker build -t local-ai-agent/browser:0.2 docker/browser
if (( WITH_WORKROOM )); then
  say "작업실 이미지 빌드(모델이 쓴 코드를 격리해 실행하는 곳)"
  docker image inspect local-ai-agent/workroom:0.2 >/dev/null 2>&1 || docker build -t local-ai-agent/workroom:0.2 docker/workroom
fi

say "파이썬 가상환경"
[[ -d .venv ]] || "$PY" -m venv .venv
.venv/bin/pip install -q -e ".[dev]"

if [[ ! -f .env ]]; then
  cp .env.example .env && chmod 600 .env
  say ".env를 만들었습니다 — 쓸 기능의 키·설정을 직접 넣으세요(키는 저장소에 올리지 않습니다)"
fi
# 앱 토큰(AGENT_APP_TOKEN)은 서버가 처음 뜰 때 스스로 만들어 .env에 넣는다(값은 출력하지 않는다)

say "빠른 점검(단위 테스트)"
.venv/bin/pytest -q tests/unit 2>&1 | tail -1

cat <<EOF

다 됐습니다. 서버 띄우기:
  .venv/bin/python -m agent.cli serve          # 127.0.0.1:8765
맥에 로그인하면 스스로 뜨게:
  scripts/install_autostart.sh install
폰(모아 앱)에서 붙으려면 같은 tailnet의 주소가 필요합니다(예: tailscale serve) — README의 "폰에서 붙기".
EOF
