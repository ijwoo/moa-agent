#!/bin/zsh
# 로그인할 때 에이전트 서버가 스스로 뜨게 한다(D81, 사용자 결정 2026-11-16). 사용법: scripts/install_autostart.sh [remove|status]
# 설치하는 것: ~/Library/LaunchAgents/dev.moa.agent.plist 하나. 기록: ~/Library/Logs/local-ai-agent/server.log
LABEL=dev.moa.agent
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/Library/Logs/local-ai-agent"
DOMAIN="gui/$(id -u)"
case "${1:-install}" in
  remove)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
    rm -f "$PLIST"
    echo "지웠다: $PLIST (서버도 멈췄다)" ;;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit code" || echo "설치돼 있지 않다" ;;
  install)
    mkdir -p "$LOGS" && chmod 700 "$LOGS"
    sed -e "s|__REPO__|$REPO|g" -e "s|__LOGS__|$LOGS|g" "$REPO/deploy/$LABEL.plist" > "$PLIST"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
    pkill -f "agent.cli serve" 2>/dev/null; sleep 2     # 손으로 띄운 서버가 있으면 내린다(같은 포트)
    launchctl bootstrap "$DOMAIN" "$PLIST" && echo "설치했다: $PLIST" ;;
  *) echo "사용법: $0 [install|remove|status]"; exit 2 ;;
esac
