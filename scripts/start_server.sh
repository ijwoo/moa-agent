#!/bin/zsh
# 맥에 로그인하면 launchd가 이 스크립트를 띄운다(deploy/dev.moa.agent.plist, D81).
# 컨테이너(colima)와 로컬 모델 서버(ollama)가 떠 있지 않으면 띄우고, API 서버를 앞에서 돌린다 — 서버가 죽으면 launchd가 다시 띄운다.
# 비밀 값은 여기 없다: 토큰·키는 서버가 .env와 키체인에서 직접 읽는다.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$(dirname "$0")/.." || exit 1
stamp() { date "+%Y-%m-%d %H:%M:%S"; }

if ! colima status >/dev/null 2>&1; then
  echo "[start] $(stamp) colima 시작"
  colima start >/dev/null 2>&1 || echo "[start] $(stamp) colima를 띄우지 못함(브라우저 작업은 안 된다)"
fi
if ! pgrep -x ollama >/dev/null 2>&1; then
  echo "[start] $(stamp) ollama 시작"
  nohup ollama serve >/dev/null 2>&1 &
  for i in 1 2 3 4 5 6 7 8 9 10; do
    curl -s -o /dev/null -m 2 http://127.0.0.1:11434/api/version && break
    sleep 1
  done
fi
echo "[start] $(stamp) API 서버 시작"
exec .venv/bin/python -m agent.cli serve
