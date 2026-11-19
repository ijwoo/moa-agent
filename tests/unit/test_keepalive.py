"""로그인 살려 두기(D105): 로그인돼 있다고 아는 사이트만 확인, 작업이 돌면 건너뜀, 풀리면 하루 한 번 알림, 기록은 0600."""

from types import SimpleNamespace

from agent.api.push import payload
from agent.sites.keepalive import Health, KeepAlive


class Sites:
    def __init__(self, status, after):
        self._s, self._after, self.checked = status, after, []
        self.sites = {k: SimpleNamespace(backend="sandbox") for k in status}

    def status(self):
        return [{"site": k, "status": v} for k, v in self._s.items()]

    def check(self, key):
        self.checked.append(key)
        self._s[key] = self._after[key]
        return self.status()


class Jobs:
    def __init__(self, busy=False):
        self.b = busy

    def list(self, limit=50):
        return [{"status": "running"}] if self.b else [{"status": "completed"}]


class Push:
    def __init__(self):
        self.sent = []

    def notify(self, kind, task_id, conv, key=""):
        self.sent.append((kind, task_id, key))


def test_checks_logged_in_sites_and_alerts_once_when_expired(tmp_path):
    s = Sites({"jobsite": "logged_in", "naver": "logged_out", "naver_mail": "logged_in", "vworld": "logged_in"},
              {"jobsite": "logged_out", "vworld": "logged_in"})
    p = Push()
    out = KeepAlive(s, Jobs(), p, health=Health(tmp_path / "h.json")).once()
    assert out == {"jobsite": "expired", "vworld": "ok"} and s.checked == ["jobsite", "vworld"]   # 풀린 줄 아는 것·메일 설정은 열지 않는다
    assert p.sent and p.sent[0][:2] == ("login_expired", "site:jobsite")
    h = Health(tmp_path / "h.json").load()
    assert len(h["jobsite"]["expired"]) == 1 and h["vworld"]["checks"] == 1 and (tmp_path / "h.json").stat().st_mode & 0o777 == 0o600


def test_skips_while_a_task_is_running(tmp_path):
    s = Sites({"vworld": "logged_in"}, {"vworld": "logged_out"})
    assert KeepAlive(s, Jobs(busy=True), None, health=Health(tmp_path / "h.json")).once() == {"vworld": "skipped"} and s.checked == []


def test_site_alert_payload_carries_only_the_site_key():
    p = payload("login_expired", "site:jobsite", None)
    assert p["site"] == "jobsite" and p["type"] == "login_expired" and "로그인이 풀린" in p["aps"]["alert"]["body"]


def test_a_site_that_expires_within_the_interval_is_checked_more_often(tmp_path):
    # 실기기 2026-11-18: 교육 사이트가 16:56 살아 있다가 19:17에 풀려 있었다(6시간 간격 사이) → 그 사이트만 틈의 절반으로
    now = [100_000.0]
    s = Sites({"edu": "logged_in", "naver": "logged_in"}, {"edu": "logged_in", "naver": "logged_in"})
    h = Health(tmp_path / "h.json")
    k = KeepAlive(s, Jobs(), None, health=h, clock=lambda: now[0])
    assert k.once(due_only=True) == {"edu": "ok", "naver": "ok"}
    now[0] += 3600
    assert k.once(due_only=True) == {}                             # 아직 차례가 아니다(6시간)
    now[0] += 3600
    s._after["edu"] = "logged_out"
    s._s["edu"] = "logged_in"
    k.once()                                                       # 2시간 만에 풀린 것을 봤다
    assert h.every("edu", k.every_s) == 3600 and h.every("naver", k.every_s) == k.every_s
    s._s["edu"], s._after["edu"] = "logged_in", "logged_in"        # 다시 로그인했다
    now[0] += 3600
    assert set(k.once(due_only=True)) == {"edu"}                   # 1시간 뒤 edu만 차례
