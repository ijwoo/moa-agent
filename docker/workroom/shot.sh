#!/bin/sh
# shot <page.html> <out.png> [폭 390] [높이 844] [배율 3] — 작업실 안의 HTML 파일을 폰 크기 PNG로 찍는다(D104).
# 루트 파일시스템이 읽기 전용이라 브라우저의 프로필·캐시는 /tmp에 둔다. 컨테이너가 격리를 맡으므로 브라우저 자체 샌드박스는 끈다
# (비루트·권한 0인 컨테이너 안에서는 브라우저 샌드박스가 뜨지 않는다).
set -e
[ -f "$1" ] || { echo "HTML 파일이 없다: $1" >&2; exit 2; }
W=${3:-390}; H=${4:-844}; S=${5:-3}
mkdir -p /tmp/chrome
exec chromium --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --hide-scrollbars --no-first-run \
  --disable-extensions --disable-background-networking --disable-sync --metrics-recording-only --mute-audio \
  --user-data-dir=/tmp/chrome --force-device-scale-factor="$S" --window-size="$W,$H" \
  --virtual-time-budget=3000 --screenshot="$(realpath -m "$2")" "file://$(realpath "$1")" 2>/dev/null
