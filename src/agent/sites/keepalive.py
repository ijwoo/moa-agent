"""로그인 살려 두기(D105): 로그인해 둔 사이트를 몇 시간마다 한 번 열어 세션을 이어 가고, 풀린 것을 일보다 먼저 알아챈다.

- 하는 일은 기존 '로그인 확인'(SiteLoginManager.check)뿐이다: 로그인돼 있어야 열리는 화면을 열어 보고, 살아 있으면 갱신된 값을 다시 보관한다.
  사람이 가끔 들어가 보는 것과 같다 — 누르거나 입력하지 않는다.
- 작업이 돌고 있거나 로그인 중이면 이번 차례는 건너뛴다. 전용 크롬 사이트는 다른 운전과 같은 프로필 잠금을 잡고 연다.
- 살아 있다가 풀리면 알림(login_expired)을 보낸다 — 앱이 그 사이트의 다시 로그인 카드를 띄우고, 아이디·비밀번호로 들어간 사이트(via=password)는
  폰 키체인(Face ID)의 값으로 채운다(D105, 사용자 결정 B). 맥은 그 값을 보관하지 않는다.
- 기록: 사이트마다 마지막 점검 시각·풀린 시각을 맥 파일(site_health.json, 0600)에 — 어느 사이트가 약한지 본다.
- 사이트마다 간격을 맞춘다(2026-11-18): 살아 있던 것을 본 뒤 6시간 안에 풀린 사이트(실기기: 교육 사이트)는 그 틈의 절반으로 줄인다(30분 아래로는 안 간다).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

PATH = Path(os.path.expanduser("~/.local/share/local-ai-agent/site_health.json"))
SKIP = ("naver_mail",)
MIN_EVERY_S = 30 * 60   # 사이트마다 줄인 간격의 하한
TICK_S = 15 * 60        # 차례가 된 사이트가 있는지 살피는 간격
BUSY = ("queued", "running", "awaiting_approval", "awaiting_input", "send_pending")


def _profile_lock():
    """전용 크롬 프로필 잠금(운전 스크립트와 같은 파일). 지금 다른 일이 쓰고 있으면 None."""
    import fcntl
    from ..browser import hostchrome
    f = open(str(hostchrome.PROFILE).rstrip("/") + ".agent.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


class Health:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or PATH)
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def every(self, site: str, default: float) -> float:
        v = (self.load().get(site) or {}).get("every")
        return float(v) if isinstance(v, (int, float)) and v > 0 else default

    def due(self, site: str, default: float, now: float) -> bool:
        last = (self.load().get(site) or {}).get("last_check")
        return not isinstance(last, (int, float)) or now - last >= self.every(site, default) - 60

    def note(self, site: str, event: str, at: float | None = None, default_every: float | None = None) -> None:
        """event: ok | expired | relogin_ok | relogin_failed"""
        at = at or time.time()
        with self._lock:
            d = self.load()
            s = d.setdefault(site, {"checks": 0, "expired": []})
            s["last_check"] = at
            s["checks"] = int(s.get("checks", 0)) + (1 if event in ("ok", "expired") else 0)
            if event == "ok":
                s["last_ok"] = at
            if event == "expired":
                s["expired"] = (s.get("expired") or [])[-19:] + [at]
                if s.get("last_ok") and default_every:   # 살아 있던 것을 본 뒤 이만큼 만에 풀렸다 → 그 사이트는 더 자주 연다
                    gap = at - float(s["last_ok"])
                    if 0 < gap < default_every:
                        s["every"] = max(MIN_EVERY_S, min(float(s.get("every") or default_every), gap / 2))
            else:
                s.setdefault("expired", [])
            if event.startswith("relogin"):
                s["last_relogin"] = {"at": at, "ok": event == "relogin_ok"}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, self.path)


class KeepAlive:
    def __init__(self, sites, jobs, push=None, every_h: float = 6.0, health: Health | None = None, sleep=time.sleep, clock=time.time):
        self.sites, self.jobs, self.push = sites, jobs, push
        self.every_s = every_h * 3600
        self.health = health or Health()
        self._sleep, self._clock = sleep, clock
        self._stop = threading.Event()

    def busy(self) -> bool:
        try:
            return any(t.get("status") in BUSY for t in self.jobs.list(limit=50))
        except Exception:
            return True

    def once(self, due_only: bool = False) -> dict:
        """한 차례: 로그인돼 있다고 아는 사이트를 하나씩 확인한다 → {사이트: ok|expired|skipped}. due_only면 차례가 된 사이트만."""
        out: dict[str, str] = {}
        before = {x["site"]: x for x in self.sites.status()}
        for key, row in before.items():
            if key in SKIP or row.get("status") != "logged_in":
                continue
            if due_only and not self.health.due(key, self.every_s, self._clock()):
                continue
            if self.busy() or getattr(self.sites, "_busy", lambda: False)():   # 작업이 돌거나 로그인 중이면 건너뛴다
                out[key] = "skipped"
                continue
            site = self.sites.sites.get(key)
            lock = None
            if getattr(site, "backend", "") == "chrome":
                lock = _profile_lock()
                if lock is None:
                    out[key] = "skipped"
                    continue
            try:
                after = {x["site"]: x for x in self.sites.check(key)}
            except Exception as e:
                out[key] = "skipped"
                sys.stderr.write(f"[keepalive] {time.strftime('%Y-%m-%d %H:%M:%S')} site={key} error={type(e).__name__}\n")
                continue
            finally:
                if lock is not None:
                    lock.close()
            now = (after.get(key) or {}).get("status")
            if now == "logged_in":
                out[key] = "ok"
                self.health.note(key, "ok", at=self._clock())
            elif now == "logged_out":
                out[key] = "expired"
                self.health.note(key, "expired", at=self._clock(), default_every=self.every_s)
                if self.push is not None:   # 풀렸다: 일보다 먼저 알린다(하루에 한 번까지 — 같은 날 같은 사이트는 한 번)
                    self.push.notify("login_expired", f"site:{key}", None, time.strftime("%Y-%m-%d"))
            else:
                out[key] = "skipped"
        if out:
            sys.stderr.write(f"[keepalive] {time.strftime('%Y-%m-%d %H:%M:%S')} " + " ".join(f"{k}={v}" for k, v in out.items()) + "\n")
        return out

    def start(self) -> None:
        if self.every_s <= 0:
            return

        def loop():
            self._sleep(600)   # 서버가 막 떴을 때는 쉬게 둔다
            while not self._stop.is_set():
                try:
                    self.once(due_only=True)   # 사이트마다 제 간격(기본 6시간, 잘 풀리는 곳은 짧게)이 된 것만
                except Exception as e:
                    sys.stderr.write(f"[keepalive] {time.strftime('%Y-%m-%d %H:%M:%S')} error={type(e).__name__}\n")
                self._stop.wait(min(TICK_S, self.every_s))
        threading.Thread(target=loop, name="keepalive", daemon=True).start()
