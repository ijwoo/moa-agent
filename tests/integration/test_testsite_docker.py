"""테스트 사이트 + 브라우저 컨테이너 연결. RUN_INTEGRATION=1 + colima 실행 + 이미지 빌드 필요."""

import os
import subprocess

import pytest

from agent.browser import testenv
from agent.browser.container import IMAGE, run_args

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="RUN_INTEGRATION=1 필요"),
]

BROWSER_SCRIPT = r"""
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(chromium_sandbox=True)
    pg = b.new_page()
    pg.goto("http://shop.test/"); print("TITLE", pg.title())
    print("HAS_SEARCHBOX", 'searchbox "상품 검색"' in pg.locator("body").aria_snapshot())
    pg.goto("http://shop.test/cart"); pg.get_by_role("button", name="결제하기").click()
    pg.wait_for_url("**/order/done"); print("ORDER_PAGE", pg.title())
    try:
        pg.goto("http://example.com/", timeout=5000); print("EXTERNAL reachable")
    except Exception:
        print("EXTERNAL blocked")
    b.close()
"""


@pytest.fixture(scope="module")
def site():
    testenv.start_site()
    yield
    testenv.stop_site()


def test_browser_reaches_site_not_internet_and_state_changes(site):
    testenv.reset_site()
    r = subprocess.run(
        ["docker", "run", "-i", *run_args(network=testenv.NETWORK), IMAGE, "python3", "-"],
        input=BROWSER_SCRIPT, capture_output=True, text=True, timeout=120,
    )
    out = r.stdout
    assert "TITLE 테스트 쇼핑몰" in out, out + r.stderr
    assert "HAS_SEARCHBOX True" in out
    assert "ORDER_PAGE 주문 완료" in out
    assert "EXTERNAL blocked" in out
    st = testenv.site_state()
    assert st["orders"] == [{"items": {"m100": 1, "t30": 1}, "total": 64900}]


def test_network_is_internal():
    testenv.ensure_network()
    r = subprocess.run(["docker", "network", "inspect", testenv.NETWORK, "--format", "{{.Internal}}"],
                       capture_output=True, text=True)
    assert r.stdout.strip() == "true"


def test_site_container_is_hardened(site):
    r = subprocess.run(["docker", "inspect", testenv.SITE_CONTAINER, "--format",
                        "{{.Config.User}} {{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}}"],
                       capture_output=True, text=True)
    assert r.stdout.split()[:2] == ["pwuser", "true"]
    assert "ALL" in r.stdout
