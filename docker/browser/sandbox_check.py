"""컨테이너 안에서 실행: Chromium 자체 샌드박스 상태를 출력한다 (통합 테스트·수동 점검용).

출력 예:
    SANDBOX_STATUS ok            ← chrome://sandbox가 "adequately sandboxed"
    RENDERER_USERNS separate     ← 렌더러가 브라우저와 다른 사용자 네임스페이스
"""

import os

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(chromium_sandbox=True, channel="chromium")
    page = b.new_page()
    page.goto("chrome://sandbox")
    page.wait_for_timeout(500)
    text = page.locator("body").inner_text()
    print("SANDBOX_STATUS", "ok" if "adequately sandboxed" in text else "NOT_SANDBOXED")

    mine = os.readlink("/proc/self/ns/user")
    renderer_ns = set()
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                if b"--type=renderer" in f.read():
                    renderer_ns.add(os.readlink(f"/proc/{pid}/ns/user"))
        except OSError:
            pass
    if not renderer_ns:
        print("RENDERER_USERNS none_found")
    else:
        print("RENDERER_USERNS", "separate" if mine not in renderer_ns else "SAME")
    b.close()
