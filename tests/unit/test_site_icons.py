"""사이트 아이콘(D110): 로그인 상태를 보관할 때 그 화면에서 받아 64px PNG로 두고, /v1/sites/{site}/icon으로 준다."""

import base64
import shutil
import struct
import threading

import httpx
import pytest

from agent.sites.icons import IconStore
from agent.sites.login import site_host

from test_site_login import H, TOKEN, make, wait, world  # noqa: F401 — 합성 로그인 사이트를 같이 쓴다

pytestmark = pytest.mark.skipif(shutil.which("sips") is None, reason="macOS sips가 있어야 그림을 바꾼다")


def ico(w=16):
    px = bytes([0, 0, 255, 255]) * (w * w)
    mask = b"\x00" * (4 * w)
    bih = struct.pack("<IiiHHIIiiII", 40, w, w * 2, 1, 32, 0, len(px) + len(mask), 0, 0, 0, 0)
    img = bih + px + mask
    return struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(img), 22) + img


def test_store_converts_to_png_and_retries_failures_only_after_a_day(tmp_path):
    now = [1000.0]
    s = IconStore(tmp_path, clock=lambda: now[0])
    assert s.wants("naver") and s.get("naver") is None
    assert not s.save("naver", b"not an image", "image/png")          # 그림이 아니면 버린다
    assert not s.save("../x", ico(), "image/x-icon") and not s.wants("../x")   # 이상한 키는 받지 않는다

    class B:
        def __init__(self, got):
            self.got, self.n = got, 0

        def request(self, op, args=None):
            assert op == "site_icon"
            self.n += 1
            return self.got

    miss = B({"type": None})
    assert not s.grab(miss, "naver") and miss.n == 1
    assert not s.grab(miss, "naver") and miss.n == 1                  # 하루 안에는 다시 묻지 않는다
    now[0] += 24 * 3600
    hit = B({"type": "image/x-icon", "data": base64.b64encode(ico()).decode()})
    assert s.grab(hit, "naver")
    png = s.get("naver")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and (tmp_path / "naver.png").stat().st_mode & 0o777 == 0o600
    assert not s.wants("naver") and not s.grab(hit, "naver") and hit.n == 1   # 있으면 다시 받지 않는다


def test_host_is_the_page_you_land_on_after_login(world):
    m = make(world)
    rows = {r["site"]: r for r in m.status()}
    for key, s in m.sites.items():
        assert rows[key]["host"] == site_host(s) and rows[key]["host"] == rows[key]["host"].lower()


def test_check_grabs_icon_once_and_api_serves_it(world, tmp_path):
    from agent.api.dialog import DialogManager
    from agent.api.jobs import JobManager
    from agent.api.server import make_server

    m = make(world, tmp_path)
    m.icons = IconStore(tmp_path / "icons")
    world["icon"] = {"type": "image/x-icon", "data": base64.b64encode(ico()).decode()}
    lid = m.start("naver", "qr")["login_id"]
    wait(m, lid, {"need_approval"})
    world["approved"] = True
    wait(m, lid, {"logged_in"})
    assert world["ops"].count("site_icon") == 1 and m.icons.get("naver")
    m.check("naver")
    assert world["ops"].count("site_icon") == 1                       # 이미 있으면 다시 받지 않는다

    jm = JobManager(lambda job, hooks: None)

    class NoModel:
        name = "none"

        def generate(self, *a, **k):
            raise AssertionError

    http = make_server(jm, TOKEN, port=0, dialog=DialogManager(NoModel(), jm), sites=m)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http.server_address[1]}"
    try:
        assert httpx.get(f"{base}/v1/sites/naver/icon").status_code == 401
        r = httpx.get(f"{base}/v1/sites/naver/icon", headers=H)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
        assert httpx.get(f"{base}/v1/sites/daum/icon", headers=H).json()["error"]["code"] == "icon_not_found"
        assert httpx.get(f"{base}/v1/sites", headers=H).json()["sites"][0]["host"]
    finally:
        jm.shutdown()
        http.shutdown()
