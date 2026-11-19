"""맥의 진짜 크롬(에이전트 전용 프로필)을 에이전트가 열어 조작할 수 있는지 재 본다 — 구글 계정 서비스(유튜브 스튜디오 등)의 길을 찾기 위한 측정.

왜: 격리 컨테이너의 브라우저로는 구글 로그인이 안 된다(구글이 자동화된 브라우저의 로그인을 받지 않는다 — 2026-11-15 실기기).
    꾸미거나 우회하지 않고 되는 길이 있는지 본다: **로그인은 사람이 진짜 크롬에서 손으로**, 그 뒤 에이전트가 그 프로필을 **자동화 표시를 단 채로** 연다.

    1) 로그인(사람이, 맥 앞에서):  python scripts/try_real_chrome.py login
       전용 프로필로 보통의 크롬 창이 뜬다(자동화 없음). 거기서 구글에 직접 로그인하고 창을 닫는다.
    2) 확인(에이전트가):            python scripts/try_real_chrome.py check [주소]
       같은 프로필을 Playwright로 연다(navigator.webdriver = true 그대로). 로그인된 화면인지와 화면의 꼴만 적는다(내용은 적지 않는다).

전용 프로필: ~/.local/share/local-ai-agent/chrome-agent-profile (저장소 밖, 평소 쓰는 크롬 프로필과 다르다).
이 스크립트는 측정용이다. 엔진에는 아직 붙이지 않았다. 프록시·탐지 회피는 쓰지 않는다. 막히면 거기서 멈춘다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

PROFILE = Path.home() / ".local" / "share" / "local-ai-agent" / "chrome-agent-profile"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SHAPE_JS = """() => ({
  webdriver: navigator.webdriver === true,
  ua: navigator.userAgent.replace(/[0-9.]+/g, 'N').slice(0, 80),
  title_len: document.title.length,
  text_len: (document.body ? document.body.innerText : '').length,
  sign_in_link: !!document.querySelector('a[href*="ServiceLogin"], a[href*="accounts.google.com"], ytd-button-renderer a[href*="signin"]'),
  avatar: !!document.querySelector('#avatar-btn, button#avatar-btn, img#img[alt], ytcp-entity-avatar, #account-button'),
  videos: document.querySelectorAll('ytd-rich-item-renderer, ytd-video-renderer, ytcp-video-row').length,
  blocked_words: ['unusual traffic', '비정상적인 트래픽', "Couldn't sign you in", '로그인할 수 없음', '안전하지 않을 수'].filter(w => (document.body ? document.body.innerText : '').includes(w)).length,
})"""


def login() -> int:
    PROFILE.mkdir(parents=True, exist_ok=True, mode=0o700)
    print(f"전용 프로필로 보통의 크롬을 엽니다: {PROFILE}\n구글에 직접 로그인한 뒤 그 창을 닫아 주세요. (자동화 없음 — 이 스크립트는 아무것도 누르지 않습니다)")
    subprocess.run([CHROME, f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check", "https://accounts.google.com/"])
    return 0


def check(url: str) -> int:
    from playwright.sync_api import sync_playwright
    PROFILE.mkdir(parents=True, exist_ok=True, mode=0o700)
    rec = {"asked": urlsplit(url).hostname}
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=False,
                                                    args=["--no-first-run", "--no-default-browser-check"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            t0 = time.time()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
            final = urlsplit(page.url)
            rec.update(status=resp.status if resp else None, landed=f"{final.hostname}{final.path[:40]}", seconds=round(time.time() - t0, 1),
                       **page.evaluate(SHAPE_JS))
            rec["logged_in"] = bool(rec["avatar"]) and not rec["sign_in_link"] and final.hostname != "accounts.google.com"
        finally:
            ctx.close()
    print(json.dumps(rec, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "login":
        sys.exit(login())
    if cmd == "check":
        sys.exit(check(sys.argv[2] if len(sys.argv) > 2 else "https://www.youtube.com/"))
    print(__doc__)
    sys.exit(2)
