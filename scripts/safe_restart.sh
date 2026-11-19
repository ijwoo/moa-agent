#!/bin/zsh
# 서버를 다시 띄운다 — 그런데 사람을 기다리거나 오래 도는 작업이 있으면 멈추고 알린다(2026-11-17: 개발 중 다시 띄우다 돌던 작업을 끊었다).
# 그래도 띄우려면: scripts/safe_restart.sh --force
cd "$(dirname "$0")/.."
TOK=$(grep '^AGENT_APP_TOKEN=' .env | cut -d= -f2-)
H=$(grep -o 'TOKEN_HEADER = "[^"]*"' src/agent/api/server.py | cut -d'"' -f2)
busy=$(curl -s -m 5 -H "$H: $TOK" "http://127.0.0.1:8765/v1/tasks" | python3 -c "
import json,sys
try: ts=json.loads(sys.stdin.read(), strict=False)['tasks']
except Exception: ts=[]
b=[t for t in ts if t.get('status') in ('queued','running','awaiting_approval','awaiting_input','send_pending')]
print(len(b), ' '.join((t.get('title') or '')[:20].replace(' ','_') for t in b))")
n=${busy%% *}
if [[ "$1" != "--force" && "$n" != "0" && -n "$n" ]]; then
  echo "돌고 있는 작업이 있어 다시 띄우지 않았다: ${busy#* }" >&2
  exit 3
fi
launchctl kickstart -k gui/$(id -u)/dev.moa.agent && echo restarted
