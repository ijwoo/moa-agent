"""사이트 로그인 관리 (D52). 로그인은 로컬 모델이 아니라 이 코드가 진행하고, 사람이 필요한 곳(입력·승인·선택)은 앱으로 넘긴다.

- 아이디·비밀번호·인증 문자는 브라우저 칸에 넣고 버린다. 저장하지 않고 기록·응답·모델 입력에 넣지 않는다.
- 남는 것은 로그인된 상태(쿠키)뿐이고 **암호화 저장소(맥 키체인)** 에만 둔다(D27의 조건). 로그인용 브라우저는 일회용 프로필이라
  닫으면 컨테이너에 아무것도 남지 않는다. logout()이 보관한 상태를 지운다. 쿠키 값은 이 모듈이 받아 바로 저장소에 넣고 기록하지 않는다.
- 캡차·2단계 인증을 우회하지 않는다. 화면이 요구하는 것을 사용자에게 그대로 묻고, 모르는 화면이면 멈춘다(unsupported_page).
- 한 번에 로그인 하나. 브라우저는 로그인마다 전용 스레드 하나가 쓴다(브라우저 세션은 스레드 안전하지 않다).
"""

from __future__ import annotations

import dataclasses
import base64
import json
import os
import queue
import re
import secrets
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..tools.observation import Observation

FIELD_NAMES = ("id", "password", "captcha", "code", "choice")   # 앱과 맞춘 입력 이름. 늘릴 때는 앱에 먼저 알린다
TERMINAL = ("logged_in", "failed", "expired", "cancelled")
OVERALL_TIMEOUT_S = 600.0
KEEP_ENDED_S = 600.0
START_LIMIT, START_WINDOW_S = 12, 600.0   # 시작 횟수(제출 횟수가 아니다 — 틀린 입력은 로그인마다 3번에서 끊는다). 실기기: 다섯 번이면 시험 중에 금방 찬다
MAX_WRONG = 2
POLL_S = 1.5

# 화면 글에서 상황을 가리는 표시(네이버 화면 문구 기준, 합성 로그인 사이트도 같은 문구를 쓴다)
WRONG = ("잘못 입력", "잘못 되었습니다", "잘못되었습니다", "일치하지 않", "다시 확인해")
CAPTCHA = ("자동입력 방지", "자동 입력 방지", "보안문자", "보안 문자", "추가 확인", "정답")
# 화면의 물음을 사용자에게 옮길 때 빼는 줄(실제 네이버 추가 확인 화면: 버튼 새로고침·음성듣기·확인, 위아래의 안내 줄)
CAPTCHA_NOISE = ("새로고침", "음성듣기", "음성 듣기", "확인", "취소", "본문 바로가기")
CAPTCHA_NOISE_PART = ("스마트봇", "고객센터", "©", "NAVER Corp", "Copyright", "회원가입", "아이디 찾기", "비밀번호 찾기")


def captcha_question(text: str, site_name: str) -> str:
    """추가 확인 화면의 글에서 사용자가 답해야 할 물음만 뽑는다.

    실기기(2026-11-12): 화면 글을 통째로 옮겼더니 "본문 바로가기 … 가장 가격이 싼 물건의 이름은 무엇입니까? 스마트봇 상담 고객센터 © NAVER Corp."처럼
    물음이 다른 줄에 묻혀 사용자가 무엇을 적어야 하는지 알 수 없었다. 물음표가 든 줄을 먼저 찾고, 없으면 안내 줄을 뺀 나머지를 준다.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    lines = [l for l in lines if l not in CAPTCHA_NOISE and l != site_name and not any(w in l for w in CAPTCHA_NOISE_PART)]
    asks = [l for l in lines if "?" in l]   # 물음이 든 줄은 통째로("[?]에 들어갈 …"처럼 빈칸 표시가 섞인 물음도 있다)
    if asks:
        return " ".join(asks)[:240]
    return " ".join(lines)[:240]
APPROVAL = ("2단계 인증", "알림을 확인", "승인해 주세요", "인증 요청을 보냈", "기기에서 승인")
EXPIRED = ("시간이 초과", "시간이 만료", "유효 시간이 지났", "다시 시도해 주세요")
BLOCKED = ("비정상적인 접근", "보호조치", "보호 조치", "이용이 제한")
CHOICE_LABELS = ("등록", "등록안함", "등록 안 함", "다음에 하기", "나중에 하기", "확인", "계속")
KEEP_LOGIN = "로그인 상태 유지"


class LoginError(Exception):
    """code: unknown_site | unknown_login | stale_step | rate_limited | invalid_input | busy | site_refused(전에 이 브라우저를 막았던 사이트)"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Site:
    key: str
    name: str
    login_urls: dict[str, str]      # 방법(qr | password) → 로그인 화면 주소
    check_url: str                   # 로그인돼 있어야 열리는 페이지(아니면 로그인 화면으로 돌려보낸다)
    login_hosts: tuple[str, ...]     # 로그인 화면의 호스트
    auth_cookies: tuple[str, ...]    # 이 이름의 쿠키가 모두 있으면 로그인된 것으로 본다(값은 읽지 않는다)
    cookie_url: str                  # 쿠키를 확인할 주소
    generic: bool = False            # 공통 로그인(D70): 화면의 칸을 표준 속성으로 알아본다. auth_cookies는 비워 둔다(사용자가 끝났다고 확인)
    cloud_ok: bool = False           # 이 사이트의 로그인한 화면을 Claude가 보고 조작해도 된다고 사용자가 허락했다(D72). 사이트마다 따로
    # "chrome": 격리 컨테이너가 아니라 맥의 진짜 크롬(에이전트 전용 프로필)으로 연다(browser/hostchrome.py).
    # 자동화된 브라우저의 로그인을 받지 않는 회사(구글)의 서비스용 — 로그인은 사람이 그 프로필에서 손으로 해 두고, 로그인 상태는 그 프로필에 남는다(키체인이 아니다)
    backend: str = "sandbox"
    hosts: tuple[str, ...] = ()      # 그 사이트 안에서 다닐 수 있는 다른 호스트(예: 같은 서비스의 다른 하위 주소). 비어 있으면 첫 주소의 호스트뿐
    inside_start: str = ""           # 정해 둔 사이트(공통 로그인이 아닌 것)도 "그 안에서 하기"를 할 수 있게: 시작 화면. 비어 있으면 못 한다
    no_hosts: tuple[str, ...] = ()   # 그 안에서도 가지 않는 호스트
    no_hosts_except: tuple[str, ...] = ()   # 가지 않는 호스트 안에서도 열어도 되는 주소의 앞부분
    via: str = ""                    # 사용자가 이 사이트에 로그인할 때 고른 소셜 로그인("naver" 등). 풀렸을 때 그 방법으로 스스로 다시 로그인하는 데 쓴다(D90)
    aliases: tuple[str, ...] = ()    # 사용자가 이 사이트를 부른 이름("브이월드") — 사용자의 말에 있던 낱말만 적는다(D78). 주소·권한과 무관하다


def site_host(s: Site) -> str:
    """그 사이트의 대표 호스트(소문자) — 로그인돼 있어야 열리는 화면, 없으면 쿠키 주소·로그인 화면의 호스트. 앱이 아이콘을 찾는 데 쓴다(D110)."""
    for u in (s.check_url, s.cookie_url, *s.login_urls.values()):
        h = (urlsplit(u).hostname or "").lower() if u else ""
        if h:
            return h
    return ""


# 전용 크롬 길로 여는 사이트(꺼 둔 채로 만들어 둠 — cli가 SITE_REAL_CHROME=1일 때만 더한다). 구글 계정 로그인은 맥의 전용 프로필에서 사람이 해 둔다
YOUTUBE = Site("youtube", "유튜브", {"password": "https://www.youtube.com/"}, "https://www.youtube.com/", ("accounts.google.com",), (),
               "https://www.youtube.com/", generic=True, backend="chrome", hosts=("youtube.com",))
# 쿠팡(D91, 사용자 결정 2026-11-16): 컨테이너의 브라우저에는 로그인용 요청(아이디·비밀번호 제출, QR 발급)을 내주지 않았다(같은 회선의 보통 크롬은 받는다).
# 맥의 진짜 크롬 전용 프로필에서 사람이 손으로 로그인해 두고, 그 프로필을 자동화 표시를 단 채로 연다. 꾸미지 않는다 — 여기서도 막으면 멈춘다.
# 결제·주문 단추는 승인 카드를 거친다(결제 비밀번호·카드 번호는 넣지 않는다 — INSIDE_RULES).
# **결과(같은 날 21:17): 이 길로도 안 된다.** 사람이 그 크롬에서 로그인한 것은 남았지만, 에이전트가 그 크롬을 몰자 쿠팡 첫 화면이 "Access Denied"였다.
# 그래서 등록하지 않는다(cli) — 정의는 기록으로 남긴다. 쿠팡은 자동으로 움직이는 브라우저를 종류와 상관없이 받지 않는다.
COUPANG = Site("coupang", "쿠팡", {"password": "https://login.coupang.com/login/login.pang"}, "https://www.coupang.com/", ("login.coupang.com",), (),
               "https://www.coupang.com/", generic=True, backend="chrome", hosts=("coupang.com",), aliases=("쿠팡",))
CODE_CHROME_SITES = frozenset({"youtube", "coupang"})
MANUAL_LOGIN_SAY = ("{name}는 구글 계정으로 로그인하는데, 구글은 자동으로 움직이는 브라우저의 로그인을 받지 않아요. "
                    "맥 화면 앞에서 한 번만 직접 로그인해 주세요: 터미널에서 `.venv/bin/python -m agent.cli site login {key}` — "
                    "뜨는 크롬 창에서 로그인하고 창을 닫으면 돼요.")


# 공통 로그인(D70)에서 사용자가 고르는 말. 앱은 고른 글을 그대로 돌려보낸다
GO_ON, STOP = "계속", "그만하기"
DONE, NOT_YET = "로그인됐어요", "아직이에요"
RETRY = "다시 해 보기"
HAND_DONE = "다 했어요"   # 손 넘기기(D106) — 사람이 폰에서 확인 문제를 풀었다
# 사이트가 로그인 실패를 알리는 말(D120) — 이때 "로그인이 끝났나요?" 대신 실패라고 말하고 다시 해 보기를 둔다
LOGIN_FAILED = re.compile(r"로그인(에|이)?\s*실패|실패하였습니다|실패했습니다|인증\s*오류|login\s*failed|sign[- ]in\s*failed", re.I)
EASY_AGREE = "동의하고 인증 요청"
EASY_DONE = re.compile(r"인증\s*완료")
EASY_DONE_MAX = 5
RELOGIN_S = 30.0   # 스스로 다시 로그인해 보는 가장 긴 시간(D90)
CONFIRM_CHARS = 90
SIGNED_IN_WORDS = ("로그아웃", "logout", "log out", "sign out")


SCREEN_NOISE = ("본문 바로가기", "고객센터", "회원정보", "NAVER", "네이버 회원정보", "이용약관", "개인정보처리방침", "Copyright", "©")


def screen_gist(host: str, seen: str, buttons: list[str] | tuple = ()) -> str:
    """카드에 보여 줄 그 화면의 요지(실기기 2026-11-17: 네이버 2단계 인증 화면의 글을 "본문 바로가기 … 고객센터"까지 통째로 붙였다).
    2단계 인증·승인 대기 화면이면 할 일을 한 문장으로, 아니면 단추 이름·안내 줄을 뺀 앞부분만 짧게."""
    who = {"nid.naver.com": "네이버", "accounts.kakao.com": "카카오", "kauth.kakao.com": "카카오", "accounts.google.com": "구글",
           "appleid.apple.com": "애플"}.get(host, host)
    if any(w in seen for w in APPROVAL):
        return f"{who}가 2단계 인증 알림을 보냈어요. 휴대폰(설정한 기기)에서 승인하면 저절로 이어져요."
    t = seen
    for w in (*SCREEN_NOISE, *[b for b in buttons if len(b) >= 2]):
        t = t.replace(w, " ")
    t = " ".join(t.split())
    short = t[:CONFIRM_CHARS].rstrip() + ("…" if len(t) > CONFIRM_CHARS else "")
    return f"{who} 화면: {short}" if short else f"{who} 화면이에요."


def failed_text(host: str, seen: str) -> str:
    """사이트가 로그인 실패를 알린 때의 카드 글(D120): 그 안내를 짧게 보여 주고 다시 할지 묻는다."""
    return screen_gist(host, seen) + "\n로그인에 실패했다고 나와요. 다시 해 볼까요? (이미 로그인됐으면 '로그인됐어요')"


def confirm_text(host: str, seen: str, whole: str | None = None) -> str:
    """제출 뒤 "로그인이 끝났나요?" 카드의 글. 화면 글을 통째로 옮기지 않는다
    (실기기 2026-11-16: 사이트 첫 화면의 메뉴 글 300자가 카드를 덮었다) — 로그아웃 단추가 보이는지, 아니면 앞부분만 짧게."""
    low = (whole if whole is not None else seen).lower()   # 로그아웃 단추는 화면 글 전체에서 찾는다(앞 300자 밖에 있다)
    if any(w in low for w in SIGNED_IN_WORDS):
        return f"{host} 화면에 '로그아웃'이 보여요.\n로그인이 끝났나요?"
    return (screen_gist(host, seen) + "\n" if seen.strip() else f"{host} 화면으로 넘어갔어요.\n") + "로그인이 끝났나요?"
FIELD_LABEL = {"id": "아이디", "password": "비밀번호", "code": "인증번호", "captcha": "그림의 문자"}
# 소셜 로그인: 화면의 단추를 고르면 그 회사의 로그인 화면으로 넘어간다. 사용자가 고른 방법의 호스트는 다시 묻지 않고 받는다
PROVIDER_NAME = {"kakao": "카카오", "naver": "네이버", "google": "구글", "apple": "애플"}
PROVIDER_HOSTS = {"kakao": ("accounts.kakao.com", "kauth.kakao.com"), "naver": ("nid.naver.com",),
                  "google": ("accounts.google.com",), "apple": ("appleid.apple.com",)}
BY_PASSWORD = "아이디·비밀번호로 로그인"
BY_QR = "QR 코드로 로그인"   # 그 사이트의 앱으로 QR을 찍어 승인하는 방법(D85). 비밀번호를 넣지 않는다
QR_REFRESH, QR_MAX_REFRESH, QR_FIND_S = "새 QR 받기", 3, 8.0
APPROVAL_WAIT_S = 180.0   # 소셜 로그인의 폰 승인을 지켜보는 시간. 지나면 사용자에게 끝났는지 묻는다
WAIT_MORE = "기다리기"     # 칸 없는 중간 화면에서: 누르지 않고 더 지켜본다
RETYPE = "다시 입력"       # 제출했는데 화면이 넘어가지 않았을 때: 칸을 다시 띄운다
EASY_AUTH_WORDS = ("생년월일", "휴대폰", "이름")   # 간편인증 입력 화면에 함께 나오는 말
MAX_QUIZ = 4              # 한 로그인에서 확인 문제(보안문자)를 사용자에게 다시 보여 주는 횟수
# 로그인 회사가 이 브라우저의 로그인을 받지 않겠다고 한 화면의 말. 보이면 기다리지 않고 멈춘다(돌아가지 않는다)
REFUSED_WORDS = ("로그인할 수 없음", "브라우저 또는 앱이 안전하지 않", "This browser or app may not be secure", "Couldn’t sign you in", "Couldn't sign you in",
                 "비정상적인 접근", "비정상적인 로그인", "자동화된 요청", "unusual traffic", "접근이 제한", "이용이 제한", "차단되었습니다")
# 제출한 뒤 뜬 안내가 이런 말이면 입력이 틀린 것이 아니라 요청 자체를 받지 않은 것이다: 다시 입력을 받지 않는다
REQUEST_FAILED = ("요청에 실패", "요청을 처리할 수 없", "잠시 후 다시", "일시적인 오류", "일시적으로", "오류가 발생", "네트워크", "Request failed", "try again later")
# 자동으로 움직이는 브라우저의 로그인을 받지 않는 것으로 확인된 로그인 회사(실기기 2026-11-15: "브라우저 또는 앱이 안전하지 않을 수 있습니다"). 고를 수 있는 방법에서 뺀다
REFUSING_PROVIDERS = ("google",)
LAND_S = 8.0              # 로그인을 마치고 사이트로 돌아온 화면이 자리 잡기를 기다리는 가장 긴 시간
LAND_QUIET_S = 2.0        # 그 화면의 주소가 이만큼 그대로면 자리 잡은 것으로 본다. 돌아온 직후의 화면은 아직 로그인 표식을 받아 적는 중일 수 있다(합성: 1.5초 뒤에 적는 서비스)
CHROME_READY_TRIES = 9    # 전용 크롬 길에서는 더 여러 번(약 12초)
READY_TRIES = 3           # 보관한 로그인 상태를 싣고 연 화면이 로그아웃으로 보일 때 다시 읽어 보는 횟수(스크립트가 표식을 읽고 화면을 다시 그리기 전일 수 있다)
SETTLE_S = 10.0           # 제출한 뒤 화면이 넘어가거나 거절 안내가 뜨기를 기다리는 시간. 실기기: 1초 만에 "같은 칸이 그대로다 = 틀렸다"고 해서 맞는 비밀번호를 틀렸다고 했다
CHANGED = object()        # 물음을 띄워 둔 사이에 화면이 바뀌었다
MAX_FIELDS = 4
EXCERPT_CHARS = 300


ALIAS = re.compile(r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9 ]{0,18}[가-힣A-Za-z0-9]")
ALIAS_NOT = frozenset({"사이트", "홈페이지", "로그인", "거기", "여기", "그거", "이거", "계정", "웹사이트", "페이지"})
MAX_ALIASES = 3


def clean_alias(word: str | None) -> str | None:
    """사이트를 부르는 이름으로 적어도 되는 낱말인가: 2~20자, 한글·영문·숫자·띄어쓰기만(주소·기호 없음), 흔한 말이 아닌 것."""
    w = " ".join(str(word or "").split())
    return w if ALIAS.fullmatch(w) and w not in ALIAS_NOT else None


LOGIN_PATH = re.compile(r"log_?in|sign_?in|auth|cert|sso", re.I)   # 로그인·인증 중인 화면의 주소 꼴


def _same_site(a: str, b: str) -> bool:
    """www.를 뗀 호스트가 같은가(브이월드: vworld.kr / www.vworld.kr)."""
    strip = lambda h: (h or "").lower().removeprefix("www.")
    return bool(a) and strip(a) == strip(b)


def display_name(s: "Site") -> str:
    """앱에 보일 사이트 이름(앱 D142 요청): 주소로 배운 사이트는 이름이 주소 그대로라, 사용자가 부른 이름(aliases — 사용자의 말에 있던 낱말)이 있으면 그것.
    주소는 `host`로 따로 간다(아이콘)."""
    if s.generic and s.aliases and "." in s.name and " " not in s.name:
        return s.aliases[0][:40]
    return s.name


def generic_site(key: str, name: str, login_url: str, check_url: str, login_hosts: tuple[str, ...] = (), cloud_ok: bool = False,
                 aliases: tuple[str, ...] = (), via: str = "") -> Site:
    """공통 로그인으로 다룰 사이트. login_url: 로그인 화면(사용자·코드가 정한 주소 — 모델이 짐작하지 않는다).
    check_url: 로그인돼 있어야 열리는 화면(아니면 사이트가 로그인 화면을 보여 준다). login_hosts: 미리 아는 로그인 호스트(묻지 않고 받는다)."""
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", key):
        raise ValueError("사이트 키는 영문 소문자·숫자·_·-만")
    # check_url이 비어 있으면: 아직 모른다 — 로그인이 끝난 화면의 주소를 그때 적는다(사용자가 주소로 시작한 사이트)
    return Site(key, name, {"password": login_url}, check_url, tuple(login_hosts), (), check_url or login_url, generic=True, cloud_ok=bool(cloud_ok),
                aliases=tuple(aliases)[:MAX_ALIASES], via=via if via in PROVIDER_NAME or via == "password" else "")


PROVIDER_DOMAINS = ("kakao.com", "kakaocorp.com", "daum.net", "naver.com", "google.com", "gstatic.com", "apple.com", "icloud.com")


def _provider_domain(domain: str) -> bool:
    d = domain.lower().lstrip(".")
    return any(d == p or d.endswith("." + p) for p in PROVIDER_DOMAINS)


def _walk(tb):
    while tb is not None:
        yield tb
        tb = tb.tb_next


def site_key(host: str) -> str:
    """호스트 → 사이트 키(경로에 쓰는 값: 영문 소문자·숫자·-). 예: plan.example.com → plan-example-com"""
    return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:32].strip("-")


def load_generic_sites(path) -> dict[str, Site]:
    """공통 로그인 사이트 목록 파일(JSON: [{"key","name","login_url","check_url","login_hosts"?}])을 읽는다. 없거나 꼴이 틀리면 빈 목록.

    주소는 https만 받는다. 파일은 사용자가 정한 사이트만 담는다(모델이 주소를 짐작해 넣지 않는다).
    """
    try:
        rows = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, Site] = {}
    for r in rows if isinstance(rows, list) else []:
        try:
            if not all(isinstance(r.get(k), str) and r[k] for k in ("key", "name", "login_url")) or not isinstance(r.get("check_url", ""), str):
                continue
            if not all(urlsplit(r[k]).scheme == "https" and urlsplit(r[k]).hostname for k in ("login_url", "check_url") if r.get(k)):
                continue
            hosts = tuple(h for h in r.get("login_hosts", []) if isinstance(h, str))
            site = generic_site(r["key"], r["name"][:40], r["login_url"], r.get("check_url", ""), hosts, cloud_ok=r.get("cloud_ok") is True,
                                aliases=tuple(a for a in (clean_alias(x) for x in r.get("aliases") or [] if isinstance(x, str)) if a),
                                via=r.get("via") if isinstance(r.get("via"), str) else "")
            if r.get("backend") == "chrome":   # 전용 크롬 길로 옮긴 사이트(D127 — 컨테이너 브라우저를 사이트가 계속 의심할 때)
                site = dataclasses.replace(site, backend="chrome")
            out[r["key"]] = site
        except (AttributeError, ValueError):
            continue
    return out


NAVER = Site(
    "naver", "네이버",
    {"qr": "https://nid.naver.com/nidlogin.login?mode=qrcode&url=https%3A%2F%2Fwww.naver.com&locale=ko_KR&svctype=1",
     "password": "https://nid.naver.com/nidlogin.login?mode=form&url=https%3A%2F%2Fwww.naver.com&locale=ko_KR&svctype=1"},
    "https://mail.naver.com/", ("nid.naver.com",), ("NID_AUT", "NID_SES"), "https://www.naver.com/",
    # 그 안에서 하기(D72)를 네이버에도 켠다(D75, 사용자 결정 2026-11-16: "그냥 자유롭게 웹 만질 수 있게 해 줘 … 내가 모델을 바꾸기 전까지는 감수할게").
    # 네이버의 로그인한 화면을 Claude가 보고 운전한다. 시작은 네이버페이 화면. 쇼핑·검색 결과는 열지 않는다(이 브라우저를 막는다·자동 접근 금지 — D65), 로그인 화면도 아니다
    inside_start="https://pay.naver.com/", hosts=("naver.com",),
    no_hosts=("shopping.naver.com", "search.naver.com", "smartstore.naver.com", "brand.naver.com", "nid.naver.com"),
    # 네이버 쇼핑의 '내 쇼핑'(주문·리뷰 — 사용자 계정의 화면)은 연다(실기기 2026-11-16: 사용자가 그리로 보냈고 열렸다).
    # 쇼핑 검색·상품 화면(이 브라우저를 막은 곳)은 그대로 열지 않는다. 여기서도 막으면 멈춘다
    no_hosts_except=("https://shopping.naver.com/my/", "https://shopping.naver.com/popup/reviews/"))


# 네이버 메일 연결(D54): 로그인된 브라우저로 서버 코드가 설정 화면을 진행한다. 로그인이 아니라 '설정 흐름'이지만 앱과는 같은 형식으로 주고받는다
NAVER_SECURITY_URL = "https://nid.naver.com/user2/help/myInfoV2?m=viewSecurity&lang=ko_KR"
NAVER_MAIL = Site("naver_mail", "네이버 메일 연결", {"password": NAVER_SECURITY_URL}, "https://mail.naver.com/",
                  ("nid.naver.com",), ("NID_AUT", "NID_SES"), "https://www.naver.com/")


NAVER_MAIL_SETTINGS_URL = "https://mail.naver.com/v2/settings/smtp"
APP_PASSWORD_NAME = "AIagent"   # 네이버에 만드는 애플리케이션 비밀번호의 이름(사용 내역에 이 이름으로 보인다)


def ref_after(obs_text: str, anchor: str, role: str) -> str | None:
    """관찰 결과에서 anchor가 든 줄 뒤에 처음 나오는 role 요소의 ref. (예: radio "직접 입력" 다음의 textbox)"""
    seen = False
    for l in obs_text.splitlines():
        l = l.strip()
        if seen:
            m = re.match(r'^\[(e\d+)\]\s+(\S+)\s+"', l)
            if m and m.group(2) == role:
                return m.group(1)
        elif anchor in l:
            seen = True
    return None


def button_after(obs_text: str, text_label: str, button_label: str) -> str | None:
    """관찰 결과에서 '글(text_label)' 바로 뒤에 나오는 button_label 버튼의 ref. (예: "2단계 인증" 다음의 "관리")"""
    seen = False
    for l in obs_text.splitlines():
        l = l.strip()
        if text_label in l and not l.startswith("["):
            seen = True
        elif seen:
            m = re.match(r'^\[(e\d+)\]\s+button\s+"(.*?)"', l)
            if m and m.group(2).strip() == button_label:
                return m.group(1)
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Login:
    id: str
    site: str
    method: str
    state: str = "starting"
    step: int = 0
    message: str = ""
    error: str | None = None
    reason: str | None = None
    fields: list[dict] = field(default_factory=list)
    image: dict | None = None        # 캡차 그림
    qr: dict | None = None           # QR 그림
    number: str | None = None        # QR 화면에서 고를 숫자
    text: str | None = None          # need_choice에서 화면이 묻는 말
    host: str | None = None          # 공통 로그인: 지금 입력·선택을 받는 화면의 호스트
    choices: list[str] = field(default_factory=list)
    deadline: float = 0.0            # 이 단계가 끝나는 시각(monotonic)
    ended_at: float | None = None
    wrong: int = 0
    _inputs: "queue.Queue[tuple[int, dict] | None]" = field(default_factory=queue.Queue, repr=False)
    _cancel: bool = False
    _kind: str = ""                  # 지금 화면의 종류와 내용(바뀌면 step을 올린다)
    _kept: bool = False              # '로그인 상태 유지'를 이미 눌렀는가
    _submits: int = 0                # 입력을 제출한 횟수(제출 뒤의 화면은 언제나 새 step)
    _submitted_names: list | None = None   # 방금 제출한 입력칸 이름들
    _debug_sig: tuple | None = None
    _debug_n: int = 0

    def public(self, now: float) -> dict:
        d: dict[str, Any] = {"login_id": self.id, "site": self.site, "method": self.method, "state": self.state,
                             "step": self.step, "message": self.message,
                             "expires_in_s": max(0, int(self.deadline - now)) if self.state not in TERMINAL else 0}
        if self.error:
            d["error"] = self.error
        if self.state == "failed":
            d["reason"] = self.reason or "browser_error"
        if self.state == "need_input":
            d["fields"] = list(self.fields)
            if self.image:
                d["image"] = self.image
        if self.state == "need_approval":
            if self.qr:
                d["qr"] = self.qr
            if self.number:
                d["number"] = self.number
        if self.state == "need_choice":
            d["text"], d["choices"] = self.text or "", list(self.choices)
            if getattr(self, "handover", None):
                d["handover"] = self.handover   # 작업 질문 카드의 question.handover와 같은 꼴(D106)
        if self.host and self.state in ("need_input", "need_choice", "need_approval"):
            d["host"] = self.host   # 앱이 저장해 둔 값을 채울지 가를 때 쓴다(글에서 뽑지 않게 따로 준다)
        return d


@dataclass
class _Page:
    url: str
    obs: Observation
    text: str
    cookies: set[str]

    def boxes(self) -> list:
        return [e for e in self.obs.elements.values() if e.role == "textbox" and e.label.strip()]

    def find(self, role: str, *words: str, exact: bool = False):
        for e in self.obs.elements.values():
            if e.role == role and (e.label.strip() in words if exact else any(w in e.label for w in words)):
                return e
        return None


def _field_name(label: str) -> str | None:
    if "비밀번호" in label:
        return "password"
    if "아이디" in label:
        return "id"
    # 실기기 첫 시도(2026-11-12): 제출 뒤 "보안을 위해 추가 확인을 해주세요" 화면 — 그림(영수증) + 물음 + 칸 "정답을 입력해 주세요"
    if any(w in label for w in ("자동입력", "자동 입력", "보안", "문자", "정답")):
        return "captcha"
    if any(w in label for w in ("인증번호", "인증 번호", "OTP", "일회용")):
        return "code"
    return None


class SiteLoginManager:
    def __init__(self, sandbox, store, sites: dict[str, Site] | None = None, user_id: str = "app",
                 state_path: Path | None = None, clock=time.monotonic, poll_s: float = POLL_S, mailer=None,
                 insecure_hosts: tuple[str, ...] = (), headed: bool = False, sites_path: Path | None = None,
                 settle_s: float = SETTLE_S):
        """sandbox: SandboxManager(network="bridge", persist_profile=False). store: BlobStore(로그인 상태 보관, 기본 키체인)."""
        self.sandbox, self.store, self.sites = sandbox, store, dict(sites or {"naver": NAVER, "naver_mail": NAVER_MAIL})
        self.user_id, self.state_path, self.clock, self.poll_s = user_id, state_path, clock, poll_s
        self.mailer = mailer   # 네이버 메일 연결 흐름이 만든 애플리케이션 비밀번호를 넘겨 보관할 곳(mail.Mailer.connect)
        self.insecure_hosts = tuple(insecure_hosts)   # https가 아니어도 입력을 받는 호스트(합성 테스트 사이트 전용 — 운영에서는 비운다)
        # 로그인이 풀렸을 때, 사용자가 처음 고른 소셜 로그인으로 스스로 다시 로그인한다(D90, 사용자 결정 2026-11-16). cli가 SITE_AUTO_RELOGIN으로 켠다
        self.auto_relogin = False
        self.headed = headed   # 공통 로그인 사이트의 브라우저를 창 있는 모드로(D69 — 창 없는 브라우저는 여러 사이트가 첫 화면에서 거른다)
        self.settle_s = settle_s
        self.land_quiet_s = min(LAND_QUIET_S, 10 * poll_s)   # 테스트(짧은 poll)에서는 짧게
        # 전용 크롬 길의 사이트도 앱의 카드로 로그인을 받아 본다(사용자 결정 2026-11-16 — 맥 앞에 없어도 되게. 진짜 크롬에서는 그 회사가 받아 줄지 재 본다).
        # 받지 않으면 거기서 멈추고 손으로 하는 방법을 알린다
        self.chrome_card_login = True
        self.icons = None   # 사이트 아이콘 보관(icons.IconStore, D110) — None이면 받지 않는다
        self.chrome_profile = None   # 전용 크롬 길의 프로필 폴더(None이면 기본 — ~/.local/share/local-ai-agent/chrome-agent-profile)
        self.chrome_args: tuple[str, ...] = ()   # 그 크롬에 더할 옵션(테스트가 합성 사이트 이름을 풀 때만 쓴다)
        self.sites_path = sites_path   # 공통 로그인 사이트 목록 파일(로그인에 성공한 사이트를 적어 둔다 — 서버가 다시 떠도 남게)
        self._logins: dict[str, Login] = {}
        self._active: Login | None = None
        self._thread: threading.Thread | None = None
        self._starts: list[float] = []
        self._lock = threading.Lock()
        # 개발용 화면 기록 폴더(끄려면 SITE_LOGIN_DEBUG를 지운다). 하루 지난 기록은 시작할 때 지운다
        self.debug_dir = (Path(state_path).parent / "login-debug") if state_path is not None and \
            os.environ.get("SITE_LOGIN_DEBUG") == "1" else None
        if self.debug_dir is not None and self.debug_dir.exists():
            for d in self.debug_dir.iterdir():
                if d.is_dir() and time.time() - d.stat().st_mtime > 86400:
                    shutil.rmtree(d, ignore_errors=True)
        self._status: dict[str, dict] = {}
        if state_path is not None and Path(state_path).exists():
            try:
                self._status = json.loads(Path(state_path).read_text())
            except (ValueError, OSError):
                self._status = {}

    # ---- 상태 ----
    def _uid(self, site: Site) -> str:
        return f"{self.user_id}:site:{site.key}"

    def _key(self, site: Site) -> str:
        return f"{re.sub(r'[^a-z0-9_.-]', '-', self.user_id.lower())[:40]}.{site.key}"

    def restore(self, b, site: Site) -> bool:
        """보관한 로그인 상태를 방금 연 브라우저에 되살린다(페이지를 열기 전에). 보관한 것이 없으면 False."""
        if site.backend == "chrome":   # 전용 크롬 길: 로그인 상태는 그 프로필에 이미 있다(옮길 것이 없다)
            return True
        raw = self.store.get(self._key(site))
        if not raw:
            return False
        try:
            kept = json.loads(raw)
            if isinstance(kept, list):   # 예전 꼴: 쿠키 목록만
                kept = {"cookies": kept}
            b.request("cookies_import", {"cookies": kept.get("cookies", []), "origins": kept.get("origins", []), "session": kept.get("session")})
            return True
        except Exception:
            return False

    def _keep(self, b, site: Site) -> None:
        """로그인된 상태를 암호화 저장소에 넣는다. 값은 여기서만 지나가고 기록하지 않는다.

        공통 로그인 사이트는 그 사이트의 것만 보관한다: 소셜 로그인으로 거쳐 온 회사(카카오·네이버·구글·애플)의 쿠키는 뺀다 —
        그 회사의 로그인 상태까지 이 사이트의 이름으로 들고 다니지 않는다.
        """
        if self.icons is not None:   # 사이트 아이콘(D110): 열어 둔 그 화면에서, 아직 없을 때만
            self.icons.grab(b, site.key)
        if site.backend == "chrome":   # 전용 크롬 길: 프로필에 남는다. 꺼내 보관하지 않는다(구글의 로그인 쿠키를 다른 곳에 두지 않는다)
            return
        got = b.request("cookies_export")
        cookies = got["cookies"]
        if not site.generic:   # 정해 둔 사이트(네이버): 예전 그대로 쿠키만
            self.store.set(self._key(site), json.dumps(cookies))
            return
        cookies = [c for c in cookies if not _provider_domain(str(c.get("domain", "")))]
        # 로그인 표식을 브라우저 저장소(localStorage·sessionStorage)에 두는 서비스가 있다(실기기: 쿠키만 보관했더니 로그인 직후 다시 열자 풀려 있었다).
        # 그 사이트의 주소의 것만 보관한다(거쳐 온 로그인 회사의 것은 뺀다)
        home = (urlsplit(next(iter(site.login_urls.values()))).hostname or "").lower()
        mine = lambda origin: (urlsplit(str(origin)).hostname or "").lower() == home
        origins = [o for o in got.get("origins") or [] if mine(o.get("origin"))]
        session = got.get("session") if got.get("session") and mine(got["session"].get("origin")) else None
        self.store.set(self._key(site), json.dumps({"cookies": cookies, "origins": origins, "session": session}))

    def add_site(self, host: str) -> str:
        """사용자가 말한 주소의 사이트를 공통 로그인 대상으로 더한다 → 사이트 키. 주소는 사용자가 준 것만 쓴다(모델이 짐작하지 않는다).

        공개 https 호스트만. 이미 있으면 그 키. 로그인에 성공하기 전에는 파일에 적지 않는다(잘못 말한 주소가 남지 않게).
        """
        from .public import host_blocked_by_policy, is_public_https
        host = (host or "").lower().strip().rstrip(".")
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", host) or len(host) > 100:
            raise LoginError("invalid_input")
        for s_ in self.sites.values():   # 이미 아는 사이트(네이버 등)의 주소면 그 사이트로
            known = {*s_.login_hosts, urlsplit(next(iter(s_.login_urls.values()))).hostname or "", urlsplit(s_.cookie_url).hostname or ""}
            if host in known or any(k.endswith("." + host) for k in known if k):
                return s_.key
        from .public import was_refused
        if was_refused(host):   # 전에 이 브라우저를 막았던 사이트: 다시 열지 않는다(왜 못 여는지 알릴 수 있게 따로 구분한다)
            raise LoginError("site_refused")
        if not is_public_https(f"https://{host}/") or host_blocked_by_policy(host):
            raise LoginError("invalid_input")
        key = site_key(host)
        if not key:
            raise LoginError("invalid_input")
        with self._lock:
            if key not in self.sites:
                self.sites[key] = generic_site(key, host, f"https://{host}/", "")
        return key

    def allow_cloud(self, key: str, ok: bool = True) -> None:
        """이 사이트의 로그인한 화면을 Claude가 보고 조작해도 된다는 사용자의 허락을 적는다(끄면 지운다). 사이트마다 따로다."""
        site = self._site(key)
        if not (site.generic or site.inside_start):
            raise LoginError("invalid_input")   # 그 안에서 하기를 할 수 없는 사이트(메일 연결 흐름 등)
        import dataclasses
        site = dataclasses.replace(site, cloud_ok=bool(ok))
        with self._lock:
            self.sites[key] = site
        self._remember(site)

    def add_alias(self, key: str, word: str | None) -> bool:
        """사용자가 그 사이트를 부른 이름을 적어 둔다(다음에 이름만 말해도 알아보게, D78). 공통 로그인 사이트만.
        부르는 쪽이 '사용자의 말에 있던 낱말'임을 확인하고 준다. 이미 목록 파일에 있는 사이트면 바로 적고, 아니면 로그인에 성공할 때 함께 적힌다."""
        alias = clean_alias(word)
        with self._lock:
            site = self.sites.get(key)
            if alias is None or site is None or not site.generic or alias.lower() in {a.lower() for a in (*site.aliases, site.name, site.key)}:
                return False
            # 다른 사이트가 이미 그 이름을 쓰고 있으면 적지 않는다(이름 하나가 두 사이트를 가리키지 않게)
            if any(alias.lower() in {a.lower() for a in s_.aliases} for s_ in self.sites.values()):
                return False
            import dataclasses
            site = dataclasses.replace(site, aliases=(*site.aliases, alias)[-MAX_ALIASES:])
            self.sites[key] = site
        try:
            rows = json.loads(Path(self.sites_path).read_text()) if self.sites_path and Path(self.sites_path).exists() else []
        except (OSError, ValueError):
            rows = []
        if any(isinstance(r, dict) and r.get("key") == key for r in rows if isinstance(rows, list)):
            self._remember(site)
        return True

    def _remember(self, site: Site) -> None:
        """로그인에 성공한 공통 로그인 사이트를 목록 파일에 적는다(주소뿐 — 로그인 정보는 없다)."""
        if self.sites_path is None or not site.generic or site.key in CODE_CHROME_SITES:   # 코드에 정해 둔 전용 크롬 사이트는 파일에 적지 않는다
            return
        try:
            path = Path(self.sites_path)
            rows = json.loads(path.read_text()) if path.exists() else []
            rows = [r for r in rows if isinstance(r, dict) and r.get("key") != site.key] if isinstance(rows, list) else []
            rows.append({"key": site.key, "name": site.name, "login_url": site.login_urls["password"],
                         "check_url": site.check_url, "login_hosts": list(site.login_hosts), **({"cloud_ok": True} if site.cloud_ok else {}),
                         **({"aliases": list(site.aliases)} if site.aliases else {}), **({"via": site.via} if site.via else {}),
                         **({"backend": "chrome"} if site.backend == "chrome" else {})})
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(rows, f, ensure_ascii=False, indent=1)
        except (OSError, ValueError):
            pass

    def _site(self, key: str) -> Site:
        if key not in self.sites:
            raise LoginError("unknown_site")
        return self.sites[key]

    def _mark(self, site: Site, status: str, reason: str | None = None) -> None:
        """reason: 로그아웃된 까닭 — "expired"(쓰다가 풀린 것을 알았다). 앱의 카드 안내가 달라진다."""
        self._status[site.key] = {"status": status, "checked_at": _now_iso(), **({"reason": reason} if reason else {})}
        if self.state_path is None:
            return
        path = Path(self.state_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._status, f)   # 로그인 여부와 시각만

    def status(self) -> list[dict]:
        out = [{"site": s.key, "name": display_name(s), "methods": list(s.login_urls), **({"inside": True, "cloud_ok": s.cloud_ok} if s.generic or s.inside_start else {}),
                **({"aliases": list(s.aliases)} if s.aliases else {}), **({"via": s.via} if s.via else {}),
                **({"backend": "chrome"} if s.backend == "chrome" else {}), **({"host": h} if (h := site_host(s)) else {}),
                "status": self._status.get(s.key, {}).get("status", "logged_out"),
                "checked_at": self._status.get(s.key, {}).get("checked_at"),
                **({"reason": self._status[s.key]["reason"]} if self._status.get(s.key, {}).get("reason") else {})}
               for s in self.sites.values()]
        if self.mailer is not None:   # 메일 연결은 메일 계정 상태로 답한다
            for o in out:
                if o["site"] == "naver_mail":
                    ok = any(a["provider"] == "naver" and a["status"] == "ready" for a in self.mailer.accounts())
                    o["status"] = "logged_in" if ok else "logged_out"
        return out

    # ---- 시작·조회·입력·취소 ----
    def start(self, site_key: str, method: str = "qr") -> dict:
        site = self._site(site_key)
        if method not in site.login_urls:
            raise LoginError("invalid_input")
        now = self.clock()
        with self._lock:
            self._starts = [t for t in self._starts if now - t < START_WINDOW_S]
            if len(self._starts) >= START_LIMIT:
                raise LoginError("rate_limited")
            self._starts.append(now)
            for k in [k for k, l in self._logins.items() if l.ended_at is not None and now - l.ended_at > KEEP_ENDED_S]:
                del self._logins[k]
            prev, prev_thread = self._active, self._thread
        if prev is not None and prev.state not in TERMINAL:   # 진행 중이던 로그인은 취소하고 브라우저가 닫히기를 기다린다
            prev._cancel = True
            prev._inputs.put(None)
        if prev_thread is not None:
            prev_thread.join(timeout=30)
        login = Login(id="l_" + secrets.token_urlsafe(9), site=site.key, method=method, deadline=now + OVERALL_TIMEOUT_S)
        if site.backend == "chrome" and not self.chrome_card_login:
            # 앱의 카드로 받는 로그인을 끈 경우: 맥 화면 앞에서 손으로 하는 방법을 알리고 끝낸다(고정 문장)
            with self._lock:
                self._logins[login.id] = login
                self._active, self._thread = login, None
            self._end(login, "failed", MANUAL_LOGIN_SAY.format(name=site.name, key=site.key), "unsupported_login")
            return login.public(self.clock())
        target = self._run_mail_setup if site.key == "naver_mail" else self._run_generic if site.generic else self._run
        t = threading.Thread(target=target, args=(site, login), name=f"site-login-{site.key}", daemon=True)
        with self._lock:
            self._logins[login.id] = login
            self._active, self._thread = login, t
        t.start()
        return login.public(self.clock())

    def _get(self, site_key: str, login_id: str) -> Login:
        self._site(site_key)
        login = self._logins.get(login_id)
        if login is None or login.site != site_key:
            raise LoginError("unknown_login")
        return login

    def get(self, site_key: str, login_id: str) -> dict:
        return self._get(site_key, login_id).public(self.clock())

    def input(self, site_key: str, login_id: str, step: int, values: dict) -> dict:
        login = self._get(site_key, login_id)
        with self._lock:
            if login.state not in ("need_input", "need_choice") or step != login.step:
                raise LoginError("stale_step")
            want = {f["name"] for f in login.fields} if login.state == "need_input" else {"choice"}
            if not isinstance(values, dict) or set(values) != want or \
                    any(not isinstance(v, str) or not v or len(v) > 256 for v in values.values()):
                raise LoginError("invalid_input")
            if login.state == "need_choice" and values["choice"] not in login.choices:
                raise LoginError("invalid_input")
            login.state, login.message, login.error = "working", "확인하고 있어요", None   # 같은 step의 입력은 한 번만 받는다
            login.fields, login.image = [], None
        login._inputs.put((step, dict(values)))
        return login.public(self.clock())

    def cancel(self, site_key: str, login_id: str) -> dict:
        login = self._get(site_key, login_id)
        if login.state not in TERMINAL:
            login._cancel = True
            login._inputs.put(None)
            for _ in range(100):   # 브라우저가 닫히기를 잠깐 기다린다
                if login.state in TERMINAL:
                    break
                time.sleep(0.05)
        return login.public(self.clock())

    # ---- 저장된 로그인 확인·삭제 ----
    def _busy(self) -> bool:
        return self._active is not None and self._active.state not in TERMINAL

    def check(self, site_key: str) -> list[dict]:
        """저장된 로그인이 아직 살아 있는지 실제로 열어 본다."""
        site = self._site(site_key)
        if site.key == "naver_mail":   # 설정 흐름에는 따로 확인할 로그인 상태가 없다
            return self.status()
        if self._busy():
            raise LoginError("busy")
        if site.backend != "chrome" and not self.store.get(self._key(site)):
            self._mark(site, "logged_out")
            return self.status()
        try:
            with self._open(site) as b:
                self.restore(b, site)
                b.request("navigate", {"url": site.check_url or next(iter(site.login_urls.values()))})
                if site.generic:   # 쿠키 이름을 모른다: 로그인돼 있어야 열리는 화면에 로그인 칸이 나오면 풀린 것
                    ok = self._in_restored(b, site)
                else:
                    url = b.request("observe")["url"]
                    names = set(b.request("cookie_names", {"url": site.cookie_url})["names"])
                    ok = urlsplit(url).hostname not in site.login_hosts and set(site.auth_cookies) <= names
                if ok:
                    self._keep(b, site)   # 사이트가 갱신한 값을 다시 보관한다
            if not ok and site.backend != "chrome":
                self.store.delete(self._key(site))   # 죽은 로그인 상태는 두지 않는다
            self._mark(site, "logged_in" if ok else "logged_out", None if ok else "expired")
        except LoginError:
            raise
        except Exception:
            pass   # 열어 보지 못했으면 마지막으로 아는 상태를 그대로 둔다
        return self.status()

    def logout(self, site_key: str) -> list[dict]:
        """보관한 로그인 상태를 지운다."""
        site = self._site(site_key)
        if site.key == "naver_mail":
            return self.status()
        active, thread = self._active, self._thread
        if active is not None and active.site == site_key and active.state not in TERMINAL:
            active._cancel = True
            active._inputs.put(None)
        if thread is not None:
            thread.join(timeout=30)
        if site.backend == "chrome":
            # 전용 크롬 길: 로그인은 그 프로필에 있다 — 프로필 폴더를 지운다(에이전트 전용 폴더다. 평소 쓰는 크롬과는 다른 곳)
            import shutil
            from ..browser import hostchrome
            shutil.rmtree(self.chrome_profile or hostchrome.PROFILE, ignore_errors=True)
            self._mark(site, "logged_out")
            return self.status()
        self.store.delete(self._key(site))
        self.sandbox.delete_user_data(self._uid(site))   # 예전 방식의 프로필 볼륨이 남아 있으면 같이 지운다
        self._mark(site, "logged_out")
        return self.status()

    # ---- 로그인 진행 (전용 스레드) ----
    def _end(self, login: Login, state: str, message: str, reason: str | None = None) -> None:
        # 약속(docs/API.md): failed의 message에는 사이트 화면의 글을 넣지 않는다 — 앱이 이 글을 그대로 보여 준다(2026-11-15부터).
        # 넣어도 되는 변수는 호스트·사이트 이름·로그인 회사 이름·오류 코드뿐이다. 바꾸려면 앱에 먼저 알린다
        with self._lock:
            login.state, login.message, login.reason = state, message, reason
            login.fields, login.image, login.qr, login.number, login.choices, login.text = [], None, None, None, [], None
            login.host = None
            login.ended_at = self.clock()

    def _snap(self, b, site: Site) -> _Page:
        o = b.request("observe")
        text = b.request("extract_text", {"ref": None}).get("text", "")
        names = set(b.request("cookie_names", {"url": site.cookie_url})["names"])
        return _Page(o.get("url") or "", Observation.parse(o["text"]), text, names)

    def _show(self, login: Login, kind: str, state: str, message: str, **kw) -> None:
        """사용자에게 보여 줄 화면을 바꾼다. 종류·내용이 바뀌었을 때만 step을 올린다."""
        with self._lock:
            if kind != login._kind:
                login.step += 1
                login._kind = kind
            login.state, login.message = state, message
            login.fields = kw.get("fields", [])
            login.image, login.qr, login.number = kw.get("image"), kw.get("qr"), kw.get("number")
            login.text, login.choices = kw.get("text"), kw.get("choices", [])
            login.error = kw.get("error")
            login.host = kw.get("host")
            login.handover = kw.get("handover")   # D106: 이 화면을 폰에서 직접 만질 수 있다(손 넘기기 정보)
            if "deadline" in kw:
                login.deadline = kw["deadline"]
        tag = kind.split(":", 1)[0]
        if tag in ("how", "host", "in", "act", "wait", "confirm", "stuck", "quiz") and kind != getattr(login, "_logged_kind", None):
            # 공통 로그인이 사용자에게 무엇을 보여 줬는지(종류·호스트·개수만 — 화면 글·단추 글·칸 값은 남기지 않는다). 실기기에서 어디서 멈췄는지 보려고
            login._logged_kind = kind
            sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic show {tag} state={state} step={login.step} host={kw.get('host') or '-'} "
                             f"fields={len(kw.get('fields', []))} choices={len(kw.get('choices', []))}{' wrong' if kw.get('error') else ''}\n")

    def _run(self, site: Site, login: Login) -> None:
        overall = self.clock() + OVERALL_TIMEOUT_S
        try:
            with self.sandbox.session(self._uid(site)) as b:
                b.request("navigate", {"url": site.login_urls[login.method]})
                submitted = False
                while True:
                    if login._cancel:
                        return self._end(login, "cancelled", "로그인을 취소했어요")
                    if self.clock() > overall:
                        return self._end(login, "expired", "시간이 지나 로그인을 끝냈어요. 다시 시작해 주세요")
                    p = self._snap(b, site)
                    self._debug(b, login, p)
                    if set(site.auth_cookies) <= p.cookies:
                        self._keep(b, site)   # 브라우저를 닫기 전에 로그인 상태를 암호화 저장소로 옮긴다
                        break
                    wait = self._classify(b, site, login, p, submitted, overall)
                    if wait == "end":
                        return
                    if wait == "poll":
                        if login._cancel or login._inputs.qsize():
                            continue
                        time.sleep(self.poll_s)
                        continue
                    # 사용자 입력을 기다린다
                    try:
                        item = login._inputs.get(timeout=max(0.1, overall - self.clock()))
                    except queue.Empty:
                        continue
                    if item is None:
                        continue
                    self._apply(b, login, p, item[1])
                    submitted = True
                    time.sleep(min(1.0, self.poll_s))
            # with를 나오며 브라우저가 닫힌다. 컨테이너에는 아무것도 남지 않는다
            self._mark(site, "logged_in")
            self._end(login, "logged_in", f"{site.name}에 로그인했어요")
        except Exception:
            # 브라우저 오류의 내용에는 화면 글이 섞일 수 있어 종류만 남긴다
            self._end(login, "failed", "브라우저에서 문제가 생겨 로그인을 끝내지 못했어요", "browser_error")

    # ---- 공통 로그인 (D70) ----
    def _open(self, site: Site, downloads: bool = False):
        """사이트의 브라우저 세션. 공통 로그인 사이트는 (켜져 있으면) 창 있는 모드로 연다. 전용 크롬 길의 사이트는 맥의 진짜 크롬(전용 프로필)으로."""
        if site.backend == "chrome":
            from ..browser import hostchrome
            return hostchrome.session(self.chrome_profile, extra_args=self.chrome_args)
        gate = {"ALLOW_DOWNLOADS": "1", "DOWNLOADS_GATE": "1"} if downloads else {}   # D126: 사이트마다 허락받은 뒤에만 표로 받는다
        if site.generic and self.headed:
            return self.sandbox.session(self._uid(site), env={"BROWSER_HEADED": "1", **gate})
        return self.sandbox.session(self._uid(site), **({"env": gate} if gate else {}))

    @staticmethod
    def _note(e: Exception, what: str) -> None:
        """공통 로그인에서 난 오류를 기록한다: 종류, 난 줄, 그리고 오류의 꼴(우리 서비스가 쓴 정해진 문장이나 브라우저 도구의 첫 줄 머리).
        화면 글이 섞이지 않게 꼴이 맞는 것만 남긴다 — "Locator.click: Timeout 5000ms exceeded" / "잘못된 인자: 화면의 입력 칸이 바뀌었다" 같은 것."""
        where = next((t.tb_lineno for t in _walk(e.__traceback__) if t.tb_frame.f_code.co_filename == __file__), 0)
        first = (str(e).splitlines() or [""])[0]
        m = re.match(r"^(잘못된 인자: [가-힣 ()·]{2,40}|[A-Za-z]+\.[a-z_]+: Timeout \d+ms exceeded|브라우저 서비스 [가-힣 ]{2,20}(\([\d.]+s\))?|[A-Za-z.]+: [A-Za-z ]{3,40})", first)
        kind = m.group(1) if m else "-"
        sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic {what} {type(e).__name__} line={where} kind={kind}\n")

    @staticmethod
    def _same_form(b, sig: tuple) -> bool:
        f = b.request("form_fields")
        return ((f.get("host") or ""), tuple(x["name"] for x in f.get("fields", []))) == sig

    def _confirmed(self, b, site: Site) -> bool:
        """로그인이 끝났는지 확인한다. 확인 주소를 아는 사이트는 그 화면을 열어서, 모르는 사이트(주소로 시작)는 지금 화면에서."""
        if site.check_url:
            b.request("navigate", {"url": site.check_url})
        return self._still_in(b, site)

    def _landed(self, b, site: Site) -> str | None:
        """로그인이 끝난 화면에 자리 잡았나 → 그 화면의 주소(아니면 None).

        소셜 로그인에서 돌아온 직후의 화면(돌아오는 주소)은 로그인 칸도 입구도 없지만 아직 로그인 표식을 받아 적는 중일 수 있다.
        그때 보관하면 표식이 빠진다. 주소가 LAND_QUIET_S 동안 그대로일 때까지 기다린 뒤 다시 확인한다.
        """
        if not self._confirmed(b, site):
            return None
        url = b.request("form_fields").get("url") or ""
        t0 = since = time.monotonic()
        while time.monotonic() - since < self.land_quiet_s and time.monotonic() - t0 < LAND_S:
            time.sleep(min(0.5, max(0.01, self.land_quiet_s / 4)))
            now = b.request("form_fields").get("url") or ""
            if now != url:
                url, since = now, time.monotonic()
        return url if self._still_in(b, site) else None

    def can_relogin(self, key: str) -> bool:
        """이 사이트는 풀려도 스스로 다시 로그인해 볼 수 있나(사용자가 소셜 로그인으로 들어왔고, 그 회사의 로그인 상태가 맥에 있고, 기능이 켜져 있다)."""
        site = self.sites.get(key)
        if site is None or not (self.auto_relogin and site.generic and site.backend != "chrome" and site.via in PROVIDER_HOSTS):
            return False
        prov = self.sites.get(site.via)
        return prov is not None and not prov.generic and self.store.get(self._key(prov)) is not None

    def relogin(self, b, site: Site) -> bool:
        """로그인이 풀린 사이트에, 사용자가 처음 고른 소셜 로그인으로 **스스로** 다시 들어간다(D90, 사용자 결정 2026-11-16) → 됐나.

        하는 일은 그 회사의 로그인 단추를 누르는 것뿐이다 — 맥에 보관해 둔 그 회사(네이버)의 로그인 상태를 싣고 누른다. 아이디·비밀번호는 넣지 않는다.
        그 회사 화면이 무엇이든 더 요구하면(계정 입력, 동의·계속 단추, 인증) **하지 않고 그만둔다** — 그때는 사용자가 직접 한다."""
        via = site.via
        prov = self.sites.get(via)
        if not (self.auto_relogin and site.generic and site.backend != "chrome" and via in PROVIDER_HOSTS and prov is not None and not prov.generic):
            return False
        why, t0 = "timeout", self.clock()
        try:
            if not self.restore(b, prov):
                why = "no_provider_state"
                return False
            b.request("navigate", {"url": next(iter(site.login_urls.values()))})
            opened = entered = False
            while self.clock() - t0 < RELOGIN_S:
                f = b.request("form_fields")
                host = f.get("host") or ""
                if host in PROVIDER_HOSTS[via]:
                    asks = bool(f.get("fields")) or bool([x for x in b.request("login_buttons").get("buttons", []) if x not in (WAIT_MORE, STOP)])
                    if asks:   # 그 회사가 무엇을 더 묻는다: 대신 답하지 않는다
                        why = "provider_asks"
                        return False
                elif opened and not f.get("fields"):
                    landed = self._landed(b, site)
                    if landed:
                        self._keep(b, site)
                        self._mark(site, "logged_in")
                        why = "ok"
                        return True
                elif not opened:
                    m = b.request("login_methods")
                    if via in (m.get("providers") or []):
                        b.request("login_open", {"provider": via})
                        opened = True
                    elif m.get("entry") and not entered:
                        b.request("login_open", {"entry": True})
                        entered = True
                    elif entered or not m.get("entry"):
                        why = "no_button"   # 그 방법의 단추를 찾지 못했다
                        return False
                time.sleep(min(1.0, self.poll_s))
            return False
        except Exception as e:
            self._note(e, "relogin")
            why = "error"
            return False
        finally:
            sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic relogin via={via} result={why} {self.clock() - t0:.0f}s host={urlsplit(next(iter(site.login_urls.values()))).hostname}\n")

    def _in_restored(self, b, site: Site) -> bool:
        """보관한 로그인 상태를 싣고 연 화면이 로그인된 화면인가. 로그아웃으로 보여도 한 번에 판단하지 않는다 —
        스크립트가 로그인 표식을 읽고 화면을 다시 그리기 전일 수 있다(틀리게 판단하면 살아 있는 로그인 상태를 지운다)."""
        # 전용 크롬 길은 더 오래 기다린다: 진짜 크롬은 뜨는 데 시간이 걸리고, 큰 화면(유튜브)은 처음에 "로그인" 단추를 그렸다가 계정을 읽은 뒤 바꾼다
        # (실기기 2026-11-16: 로그인돼 있는데 4초 안에 판단해 "풀렸다"고 했다)
        tries = READY_TRIES if site.backend != "chrome" else CHROME_READY_TRIES
        for i in range(tries):
            if self._still_in(b, site):
                return True
            if site.backend == "chrome" and i in (3, 6):
                # 그래도 로그아웃으로 보이면 화면을 다시 연다: 구글은 로그인 쿠키를 몇 분마다 바꿔 주는데, 오래된 것으로 연 첫 화면은 로그아웃으로 그리고
                # 뒤에서 새것을 받아 온다 — 다시 열면 로그인된 화면이 나온다(실기기 2026-11-16: 같은 프로필이 열 때마다 됐다 안 됐다 했다 — 추정)
                try:
                    b.request("navigate", {"url": site.check_url or next(iter(site.login_urls.values()))})
                except Exception:
                    pass
            if i < tries - 1:
                time.sleep(min(1.5, self.poll_s))
        return False

    def _carried(self, site: Site) -> bool | None:
        """방금 보관한 로그인 상태로 새 브라우저에서 다시 열어 본다 → 로그인돼 있나(열어 보지 못했으면 None).

        "로그인했어요"라고 말하기 전에 한다. 실기기: 로그인은 끝까지 됐는데 보관한 것이 모자라 다음에 열자 풀려 있었고, 사용자는 까닭을 모른 채 다시 로그인했다.
        """
        try:
            with self._open(site) as b:
                if not self.restore(b, site):
                    return False
                b.request("navigate", {"url": site.check_url or next(iter(site.login_urls.values()))})
                ok = self._in_restored(b, site)
                if ok:
                    self._keep(b, site)   # 사이트가 갱신한 값을 다시 보관한다
                return ok
        except Exception as e:
            self._note(e, "carry-check")
            return None

    @staticmethod
    def _still_in(b, site: Site) -> bool:
        """확인 주소를 연 화면이 로그인된 화면인가: 로그인 칸(아이디·비밀번호)도 소셜 로그인 단추도 "로그인" 입구도 없고, 로그인 호스트로 돌려보내지지 않았다."""
        g = b.request("form_fields")
        if any(x["name"] in ("id", "password") for x in g.get("fields", [])) or (g.get("host") or "") in site.login_hosts:
            return False
        try:   # 접속을 막는 안내만 뜬 화면은 로그인된 화면이 아니다(실기기 2026-11-16: 쿠팡의 "Access Denied" 화면을 로그인 입구가 없다는 이유로 로그인됨으로 봤다)
            text = " ".join((b.request("extract_text", {"ref": None}).get("text") or "").split())
            if len(text) < 600 and any(w in text for w in (*REFUSED_WORDS, "접속이 차단", "접속이 불가능", "Access Denied")):
                return False
            if any(w in text.lower() for w in SIGNED_IN_WORDS):
                return True   # "로그아웃"이 보이면 로그인된 화면이다(실기기 2026-11-16 쿠팡: 로그인된 머리에도 '로그인'으로 읽히는 링크가 있어 풀렸다고 봤다)
        except Exception:
            pass
        try:   # 로그인된 화면에는 "로그인" 입구가 없다(로그아웃된 첫 화면에는 있다). "마이"·"계정 설정" 같은 내 정보 입구는 로그인된 화면에도 있으니 세지 않는다
            m = b.request("login_methods")
            if m.get("provider_logins"):   # "카카오로 로그인"이라고 쓴 단추가 있는 화면은 로그인 화면이다(칸이 없어도). 합성: 단추뿐인 로그인 화면을 로그인된 화면으로 읽었다
                return False
            return not (m.get("entry") and m.get("entry_kind", "login") in ("login", "login_link"))
        except Exception:
            return True

    def _run_generic(self, site: Site, login: Login) -> None:
        """화면의 칸을 표준 속성으로 알아보고(브라우저의 form_fields), 사용자가 앱에서 넣은 값을 그 칸에 넣어 제출한다.

        낱말·쿠키 이름에 기대지 않는다: 같은 칸이 다시 나오면 틀린 입력으로 세고, 칸이 사라지면 사용자에게 끝났는지 묻는다.
        로그인 화면이 아니면 "로그인" 입구를 한 번 누른다. 방법이 여럿이면(아이디·비밀번호, 카카오·네이버·구글·애플) 사용자가 고른다.
        다른 호스트로 넘어가면 입력을 받기 전에 묻는다(사용자가 고른 소셜 로그인의 호스트는 묻지 않는다). https가 아닌 화면에는 입력을 받지 않는다.
        소셜 로그인의 폰 승인은 화면을 지켜보다 넘어가면 이어 간다. 값은 기록하지 않는다.
        """
        overall = self.clock() + OVERALL_TIMEOUT_S
        start = site.login_urls[login.method]
        home = urlsplit(start).hostname or ""
        ok_hosts = {home, *site.login_hosts}
        try:
            with self._open(site) as b:
                b.request("navigate", {"url": start})
                submitted, last, blank = False, None, 0
                picked: str | None = None      # 고른 방법: "password" 또는 소셜 로그인 이름
                entered = False                # "로그인" 입구를 눌렀는가
                carded = 0                     # 이름 없는 방법 카드를 고른 횟수(되풀이 막기)
                easy_tries = 0                 # 간편인증을 요청한 횟수(D79)
                easy_done = 0                  # "인증 완료"를 누른 횟수
                easy_wait_from = None          # "인증 완료"를 누른 뒤 간편인증 화면에 머문 때부터(그 사이는 묻지 않고 지켜본다)
                easy_last_look = False         # 기다림이 끝나 실패로 하기 전에 사이트 쪽을 한 번 봤나
                easy_opener = None             # 그때 원래 탭(사이트)의 주소
                easy_asked = False             # 간편인증 뒤 로그인됐는지 코드가 확인해 봤다(한 번만)
                waited_from: float | None = None   # 폰 승인을 지켜보기 시작한 때
                done_url = ""
                refills = 0
                rejected = False
                before_alert = ""
                quizzes = 0
                seen_quiz = False
                handed = 0   # 손 넘기기로 사람에게 넘긴 횟수(D106)
                while True:
                    if login._cancel:
                        return self._end(login, "cancelled", "로그인을 취소했어요")
                    if self.clock() > overall:
                        return self._end(login, "expired", "시간이 지나 로그인을 끝냈어요. 다시 시작해 주세요")
                    f = b.request("form_fields")
                    host = f.get("host") or ""
                    names = [x["name"] for x in f.get("fields", [])]
                    if picked == "easy":
                        # 인증을 요청한 뒤에도 간편인증 입력 화면이 그대로다 = 받지 않았다(이름·생년월일·번호가 맞지 않음 등): 화면의 안내를 보여 주고 다시 받는다
                        ez = b.request("easy_form")
                        if ez.get("easy"):
                            if easy_tries >= MAX_WRONG:
                                return self._end(login, "failed", "간편인증 요청이 여러 번 받아들여지지 않아 끝냈어요", "wrong_input")
                            said = " ".join(str(f.get("alert_text") or "").split())[:140]
                            ended = self._easy_auth(b, site, login, overall, ez, note=(
                                f"간편인증 화면이 이렇게 안내했어요: “{said}” — 다시 입력해 주세요" if said
                                else "간편인증 화면이 요청을 받지 않았어요(안내 글은 읽지 못했어요). 다시 입력해 주세요"))
                            if ended:
                                return
                            easy_tries += 1
                            login._submits += 1
                            continue
                        # 요청이 받아들여졌다: 폰의 인증 앱에서 인증한 뒤 "인증 완료"를 눌러야 넘어간다 — 인증은 사용자가 하고, 누르기는 사용자가 고르면 한다
                        fin = next((x for x in b.request("login_buttons").get("buttons", []) if EASY_DONE.fullmatch(x)), None)
                        if fin:
                            if easy_done >= EASY_DONE_MAX:
                                return self._end(login, "failed", "인증이 끝나지 않아 로그인을 마치지 못했어요. 다시 시도해 주세요", "timeout")
                            easy_wait_from = None
                            said = " ".join(str(f.get("alert_text") or "").split())[:140]
                            # 화면 글을 통째로 옮기지 않는다(실기기 2026-11-17: 간편인증 창의 안내문이 중간에 잘린 채 카드에 나왔다) — 사이트가 띄운 알림 글만, 없으면 할 일 한 문장
                            self._show(login, f"easy:done:{host}:{easy_done}", "need_choice", "폰의 인증 앱에서 인증해 주세요",
                                       text=(f"화면이 이렇게 안내했어요: “{said}”\n" if said and easy_done else
                                             "아직 인증이 확인되지 않았어요.\n" if easy_done else "")
                                       + f"폰의 인증 앱에서 승인하셨으면 '{fin}'를 눌러 주세요.", choices=[fin, STOP], deadline=overall, host=host)
                            got = self._ask(login, overall)
                            if got is None:
                                continue
                            if got.get("choice") != fin:
                                return self._end(login, "cancelled", "로그인을 취소했어요")
                            easy_done += 1
                            try:
                                b.request("login_press", {"text": fin})
                            except Exception as e:
                                self._note(e, "easy_done")
                            time.sleep(min(1.5, max(self.poll_s, 0.3)))
                            continue
                        if easy_done and not names and host and not _same_site(host, home):   # www. 차이는 같은 사이트(실기기 2026-11-18 브이월드: www.vworld.kr ≠ vworld.kr로 보고 사이트 화면을 3분 기다렸다)
                            # "인증 완료"를 눌렀는데 아직 간편인증 쪽 화면이고 단추도 없다(인증 기관이 확인하는 중 — 실기기 2026-11-17 브이월드:
                            # 여기서 "로그인이 끝났나요?"를 물었고, "됐어요"를 고르자 처음부터 다시 하다 엉뚱하게 끝났다) → 묻지 않고 지켜본다.
                            # 사이트로 돌아오면 아래에서 코드가 로그인됐는지 확인하고, 단추가 다시 나오면 위의 카드로 다시 묻는다
                            op_url = (b.request("login_opener") or {}).get("opener")
                            # 원래 탭이 이미 사이트의 로그인 아닌 화면에 와 있다(D120, 실기기 2026-11-18 브이월드: 기다리기 시작했을 때 이미 넘어가 있어
                            # '넘어갔나'를 견줄 기준이 그 화면이 됐고, 3분 뒤 실패로 끝냈다 — 실제로는 로그인돼 있었다)
                            back_home = bool(op_url) and _same_site(urlsplit(op_url).hostname or "", home) and not LOGIN_PATH.search(urlsplit(op_url).path or "")
                            if easy_wait_from is None:
                                easy_wait_from = self.clock()
                                easy_opener = op_url
                                op_h = (urlsplit(op_url).hostname or "") if op_url else ""
                                sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic easy-auth wait host={host} "
                                                 f"opener={'none' if not op_url else ('site' if _same_site(op_h, home) else op_h)} "
                                                 f"opener_login={'y' if op_url and LOGIN_PATH.search(urlsplit(op_url).path or '') else 'n'}\n")
                            if back_home or (easy_opener and op_url not in (None, easy_opener)):
                                # 간편인증 창은 '확인 중'인 채 남았는데 원래 탭(사이트)이 다른 화면으로 넘어갔다 = 인증 기관이 사이트로 돌려보냈다:
                                # 창을 닫고 사이트 쪽에서 로그인됐는지 코드가 확인한다(아래 host == home 갈래)
                                b.request("login_opener", {"close": True})
                                sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic easy-auth opener moved host={host} home={'y' if back_home else 'n'}\n")
                                continue
                            if self.clock() - easy_wait_from < APPROVAL_WAIT_S:
                                self._show(login, f"easy:wait:{host}:{easy_done}", "need_approval",
                                           "간편인증을 확인하고 있어요. 폰의 인증 앱에서 승인하면 저절로 이어져요", deadline=overall, host=host)
                                time.sleep(self.poll_s)
                                continue
                            if not easy_last_look:   # 끝내기 전에 한 번: 사이트 쪽에서 로그인됐는지 본다 — 원래 탭이 있으면 창을 닫고, 없으면 이 창에서 사이트 확인 화면을 연다
                                easy_last_look = True
                                if op_url:
                                    b.request("login_opener", {"close": True})
                                else:
                                    b.request("navigate", {"url": site.check_url or start})
                                sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic easy-auth last_look host={host} opener={'y' if op_url else 'n'}\n")
                                continue
                            return self._end(login, "failed", "간편인증 화면에서 넘어가지 않았어요. 폰의 인증 앱에서 승인했는지 확인하고 다시 시도해 주세요", "timeout")
                    if names and not f.get("https") and host not in self.insecure_hosts:
                        return self._end(login, "failed", "암호화되지 않은 화면(https 아님)이라 입력을 받지 않았어요", "not_https")
                    if f.get("signup") or len(names) > MAX_FIELDS:
                        return self._end(login, "failed", "로그인 화면이 아니거나 아직 다루지 못하는 화면이에요", "unsupported_login")
                    # 방법 고르기: 아직 아무것도 넣지 않았고 방법을 정하지 않았다
                    if picked is None and not submitted:
                        m = b.request("login_methods")
                        found = [x for x in m.get("providers", []) if x in PROVIDER_NAME]
                        providers = [x for x in found if x not in REFUSING_PROVIDERS]
                        skipped = [PROVIDER_NAME[x] for x in found if x in REFUSING_PROVIDERS]
                        if not names and m.get("entry") and not entered:
                            # 로그인 화면이 아니다(칸은 없고 "로그인" 입구가 있다): 입구부터 누른다. 첫 화면의 소셜 링크(채널·앱 받기 등)를 로그인 방법으로 잘못 묻지 않게
                            # (실측: 쇼핑몰 첫 화면에서 "카카오·구글"만 물었다 — 로그인 화면에는 아이디·비밀번호 칸도 있었다)
                            entered = True
                            b.request("login_open", {"entry": True})
                            time.sleep(min(1.0, self.poll_s))
                            continue
                        if not names and carded < 3 and (entered or not m.get("entry")):
                            # 칸도 소셜 단추도 없는 로그인 화면: 방법이 그림 카드로 나뉘어 있을 수 있다(실측 2026-11-16, 정부 통합로그인 —
                            # "간편 인증 / 민간ID / 아이디 로그인 …"이 모두 이름 없는 링크였다) → 카드의 이름을 읽어 사용자가 고르게 한다
                            cards = [c for c in b.request("login_cards").get("cards", []) if isinstance(c, str)][:8]
                            if len(cards) >= 2 or (cards and not providers and not skipped):
                                self._show(login, f"how:{host}:cards:{'|'.join(cards)}", "need_choice", f"{site.name} 로그인 방법을 골라 주세요",
                                           text=f"{site.name}에 어떤 방법으로 로그인할까요?", choices=[*cards, STOP], deadline=overall, host=host)
                                got = self._ask(login, overall)
                                if got is None:
                                    continue
                                choice = got.get("choice")
                                if choice == STOP:
                                    return self._end(login, "cancelled", "로그인을 취소했어요")
                                if choice in cards:
                                    carded += 1
                                    b.request("login_open", {"card": choice})   # 새 창으로 뜨는 방법이면 그 창에서 이어 간다
                                    time.sleep(min(1.5, self.poll_s))
                                    # 간편인증 입력 화면(인증 기관·이름·생년월일·휴대폰 번호·동의 → 폰에서 승인, D79): 사용자가 카드에 넣은 것을 그대로 넣는다
                                    ez = b.request("easy_form")
                                    for _ in range(8):   # 스크립트로 그리는 화면이라 칸이 조금 뒤에 나타난다(실측: 2~4초)
                                        if ez.get("easy") or b.request("form_fields").get("fields"):
                                            break
                                        time.sleep(0.5)
                                        ez = b.request("easy_form")
                                    if ez.get("easy"):
                                        ended = self._easy_auth(b, site, login, overall, ez)
                                        if ended:
                                            return
                                        picked, submitted, waited_from = "easy", True, None
                                        easy_tries += 1
                                        login._submits += 1
                                        ok_hosts.add(ez.get("host") or "")
                                    continue
                        if providers and not names:   # 화면이 그려지는 도중이었을 수 있다(실제 사이트: 단추는 보이는데 칸은 아직) → 칸을 한 번 더 읽는다
                            time.sleep(min(1.0, self.poll_s))
                            f = b.request("form_fields")
                            host = f.get("host") or host
                            names = [x["name"] for x in f.get("fields", [])]
                        try:   # QR 로그인 탭이 있는 화면인가(D85)
                            qr_tab = (b.request("qr_form") or {}).get("tab")
                        except Exception:
                            qr_tab = None
                        if providers or (qr_tab and names):
                            options = ([BY_PASSWORD] if names else []) + ([BY_QR] if qr_tab else []) + [f"{PROVIDER_NAME[x]}로 로그인" for x in providers]
                            self._show(login, f"how:{host}:{','.join(options)}", "need_choice", f"{site.name} 로그인 방법을 골라 주세요",
                                       text=f"{site.name}에 어떤 방법으로 로그인할까요?"
                                       + (f" ({'·'.join(skipped)} 로그인은 그 회사가 이 브라우저를 받지 않아서 뺐어요.)" if skipped else ""),
                                       choices=[*options, STOP], deadline=overall, host=host)
                            got = self._ask(login, overall)
                            if got is None:
                                continue
                            choice = got.get("choice")
                            if choice == STOP:
                                return self._end(login, "cancelled", "로그인을 취소했어요")
                            if choice == BY_PASSWORD:
                                picked = "password"
                                continue
                            if choice == BY_QR:
                                done_url = self._qr_login(b, site, login, overall)
                                if done_url is None:
                                    return
                                break
                            picked = next(x for x in providers if f"{PROVIDER_NAME[x]}로 로그인" == choice)
                            ok_hosts.update(PROVIDER_HOSTS[picked])   # 사용자가 고른 방법의 로그인 호스트는 다시 묻지 않는다
                            if picked in self.sites and self.sites[picked].key != site.key and not self.sites[picked].generic:
                                # 그 회사에 이미 로그인해 둔 상태가 있으면(지금은 네이버) 싣는다 — 그 회사 화면에서 다시 입력하지 않아도 된다.
                                # 그 뒤의 "동의하고 계속하기" 같은 단추는 여전히 사용자가 고른 것만 누른다. 보관할 때는 그 회사의 쿠키를 뺀다(_keep)
                                try:
                                    carried = self.restore(b, self.sites[picked])
                                except Exception:
                                    carried = False
                                sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic carry {picked} {'y' if carried else 'n'}\n")
                            b.request("login_open", {"provider": picked})
                            time.sleep(min(1.0, self.poll_s))
                            continue
                        if not names and skipped and not providers:   # 쓸 수 있는 방법이 받지 않는 회사의 로그인뿐이다
                            return self._end(login, "failed", f"이 사이트는 {'·'.join(skipped)} 로그인만 있는데, 그 회사가 이 브라우저의 로그인을 받지 않아요.", "blocked")
                        if names:
                            picked = "password"
                        elif m.get("entry") and not entered:   # 로그인 화면이 아니다: "로그인" 입구를 한 번 누른다
                            entered = True
                            b.request("login_open", {"entry": True})
                            time.sleep(min(1.0, self.poll_s))
                            continue
                    refusing = next((p_ for p_ in REFUSING_PROVIDERS if host in PROVIDER_HOSTS.get(p_, ())), None)
                    if names and refusing and not submitted and site.backend != "chrome":
                        # 로그인 화면이 이 브라우저의 로그인을 받지 않는 회사(구글)의 것이다: 사용자에게 계정을 입력받기 **전에** 멈춘다
                        # (실기기 2026-11-15: 그 회사 계정으로만 로그인하는 사이트에서 계정을 입력받고 나서야 막혔다고 알렸다)
                        sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic refused host={host} at=before_input\n")
                        return self._end(login, "failed", f"{site.name}는 {PROVIDER_NAME[refusing]} 계정으로 로그인하는데, {PROVIDER_NAME[refusing]}은 맥의 브라우저에서 하는 로그인을 받지 않아요. "
                                         "그래서 이 사이트는 지금 방식으로는 로그인할 수 없어요(다시 해도 같아요).", "blocked")
                    if names and host not in ok_hosts:   # 처음 것과 다른 호스트: 값을 받기 전에 한 번 묻는다
                        self._show(login, f"host:{host}", "need_choice", f"{host} 화면으로 넘어갔어요",
                                   text=f"{site.name} 로그인이 {host} 화면으로 넘어갔어요. 이 화면에 입력해도 될까요?",
                                   choices=[GO_ON, STOP], deadline=overall, host=host)
                        got = self._ask(login, overall)
                        if got is None:
                            continue
                        if got.get("choice") != GO_ON:
                            return self._end(login, "failed", f"{host} 화면에는 입력하지 않고 멈췄어요", "host_refused")
                        ok_hosts.add(host)
                        continue
                    if names:
                        waited_from = None
                        sig = (host, tuple(names))
                        wrong = rejected and sig == last   # 제출했고 사이트가 거절했다(오류 안내가 떴거나 비밀번호 칸을 비웠다)
                        rejected = False
                        if wrong:
                            login.wrong += 1
                            if login.wrong > MAX_WRONG:   # 계정 잠김을 피한다
                                return self._end(login, "failed", "입력이 여러 번 맞지 않아 로그인을 끝냈어요", "wrong_input")
                        image = None
                        if "captcha" in names and f.get("captcha_image"):
                            image = {"mime": "image/png", "b64": b.request("form_image")["png_b64"]}
                        fields = [{"name": x["name"], "label": x.get("label") or FIELD_LABEL.get(x["name"], x["name"]),
                                   "secret": x["name"] == "password"} for x in f["fields"]]
                        # 앱은 호스트를 따로 보여 주지 않는다 — 어느 화면에 넣는지는 이 글로만 안다. 소셜 로그인이면 어느 회사의 계정인지도 적는다
                        who = PROVIDER_NAME.get(picked or "") if picked in PROVIDER_HOSTS and host in PROVIDER_HOSTS[picked] else None
                        where = f"{who} 로그인 화면({host})" if who else f"{host} 화면"
                        what = f"{who} 계정 정보" if who else f"{site.name} 로그인 정보"
                        said = " ".join(str(f.get("alert_text") or "").split())[:140]   # 사이트가 띄운 안내 그대로(앱에만 보여 준다 — 기록하지 않는다)
                        if said and said == before_alert[:140]:
                            said = ""   # 제출 전부터 떠 있던 글(도움말)은 거절의 까닭이 아니다
                        if wrong and any(w in said for w in REQUEST_FAILED):
                            # 틀렸다는 안내가 아니라 "요청을 받지 못했다"는 안내다(실기기 2026-11-16: 이것을 틀린 입력으로 보고 다시 받았고,
                            # 두 번째 제출 뒤 그 사이트가 접속을 막았다). 다시 넣지 않고 멈춘다 — 다른 길로 돌아가지도 않는다
                            sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic refused host={host} at=submit\n")
                            return self._end(login, "failed", f"{where}이 로그인 요청을 받지 않았어요(화면의 안내: “{said}”). 입력이 틀린 것이 아니라 "
                                             "이 브라우저에서 보낸 요청을 받지 않은 것으로 보여요. 다시 넣지 않고 여기서 멈출게요.", "blocked")
                        msg = ((f"{where}이 이렇게 안내했어요: “{said}” — 다시 입력해 주세요" if said
                                else f"{where}이 입력을 받지 않았어요(안내 글은 읽지 못했어요). 다시 입력해 주세요") if wrong
                               else f"{where}이에요. {what}를 입력해 주세요")
                        if "captcha" in names:
                            # 그림 문제: 화면이 묻는 말을 함께 보여 준다(실기기 2026-11-17: 채용 사이트 → 네이버 로그인의 영수증 문제에서 물음 없이
                            # "정답을 입력해 주세요"만 떠 풀 수 없었다 — 네이버 전용 흐름에만 있던 물음 뽑기를 여기에도). 앱에만 보이고 기록하지 않는다
                            try:
                                q = captcha_question(b.request("extract_text", {"ref": None}).get("text") or "", site.name)
                            except Exception:
                                q = ""
                            msg = (f"{where}에서 그림 문제를 냈어요. 그림을 보고 답해 주세요" + (f": “{q}”" if q else
                                   " (물음 글을 읽지 못했어요 — 그림 아래 물음을 확인할 수 없으면 취소해 주세요)"))
                        self._show(login, f"in:{host}:{','.join(names)}:{login.wrong}:{login._submits}", "need_input", msg,
                                   fields=fields, image=image, error="wrong_input" if wrong else None, deadline=overall, host=host)
                        values = self._ask(login, overall)
                        if values is None:
                            continue
                        try:
                            b.request("form_fill", {"values": values})
                            # 제출하기 직전 화면의 안내 글(값을 넣으면 뜨는 도움말이 있다). 제출 뒤에 이것과 달라진 안내만 거절로 본다
                            before_alert = " ".join(str(b.request("form_fields").get("alert_text") or "").split())
                            b.request("form_submit")
                        except Exception as e:   # 받는 사이 화면의 칸이 바뀌었거나 넣지 못했다: 끝내지 않고 지금 화면을 다시 읽어 묻는다
                            refills += 1
                            self._note(e, "fill")
                            if refills > 2:
                                raise
                            last = None          # 틀린 입력으로 세지 않는다(제출되지 않았다)
                            time.sleep(min(1.0, self.poll_s))
                            continue
                        submitted, last, blank = True, sig, 0
                        login._submits += 1
                        while True:
                            how = self._after_submit(b, login, sig, overall, before_alert)
                            sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic after_submit {how} host={host}\n")
                            if how not in ("blocked", "stuck"):
                                break
                            if how == "blocked":
                                # 화면 위에 확인 문제(보안문자) 층이 떴다: 풀지 않는다 — 그 그림과 물음을 사용자에게 보여 주고, 사용자가 쓴 답을 넣는다(네이버 로그인의 그림 문제와 같다)
                                ch = b.request("challenge")
                                sh = ch.get("shape") or {}
                                # 어떤 종류의 확인 문제였는지(개수·호스트뿐) — 다음에 무엇을 만들어야 하는지 보려고
                                sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic quiz-shape host={host} present={bool(ch.get('present'))} "
                                                 f"answerable={bool(ch.get('answerable'))} box_inputs={sh.get('box_inputs')} box_pictures={sh.get('box_pictures')} "
                                                 f"frame_host={sh.get('frame_host') or '-'} frame_inputs={sh.get('frame_inputs')} frame_buttons={sh.get('frame_buttons')} "
                                                 f"frame_pictures={sh.get('frame_pictures')} frame_clickables={sh.get('frame_clickables')}\n")
                                if ch.get("present") and ch.get("answerable") and ch.get("b64") and quizzes < MAX_QUIZ:
                                    quizzes += 1
                                    ask = " ".join(str(ch.get("text") or "").split())[:160]
                                    self._show(login, f"quiz:{host}:{login._submits}:{quizzes}", "need_input",
                                               f"{host}가 확인 문제를 냈어요. 그림을 보고 답해 주세요 — {ask}",
                                               fields=[{"name": "captcha", "label": (ch.get("label") or "답")[:40], "secret": False}],
                                               image={"mime": ch.get("mime") or "image/png", "b64": ch["b64"]}, deadline=overall, host=host)
                                    vals = self._ask(login, overall)
                                    if vals is None:
                                        break
                                    try:
                                        sent = b.request("challenge_answer", {"text": vals.get("captcha", "")})
                                        sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic quiz-answer how={sent.get('how')}\n")
                                    except Exception as e:
                                        self._note(e, "quiz")
                                    continue   # 답을 넣었다: 화면이 넘어가는지 다시 지켜본다(틀리면 새 문제가 뜬다)
                                if ch.get("present") and not ch.get("answerable") and getattr(self, "handover_on", True) and handed < 2:
                                    # 글자로 답할 수 없는 확인 문제(그림을 눌러 고르는 퍼즐 등): 사람에게 넘겨 폰에서 직접 풀게 한다(D106 — 로그인 흐름에도)
                                    handed += 1
                                    if not self._hand(b, login, host, overall, handed, "사람 확인(퍼즐)"):
                                        return self._end(login, "cancelled", "로그인을 취소했어요")
                                    continue   # 풀었다: 화면이 넘어가는지 다시 지켜본다
                                if ch.get("present") and not ch.get("answerable") and ch.get("b64") and not seen_quiz:
                                    # 글자로 답할 칸이 없는 문제(그림을 눌러 고르는 종류 등): 대신 풀지 않고, 지금 앱으로는 누르기를 전할 길도 없다.
                                    # 그래도 무슨 문제인지는 사용자가 볼 수 있게 그림을 보여 준다(그림을 실을 수 있는 카드가 입력 카드뿐이라 그 카드를 쓴다)
                                    seen_quiz = True
                                    ask = " ".join(str(ch.get("text") or "").split())[:160]
                                    self._show(login, f"quiz:{host}:{login._submits}:view", "need_input",
                                               f"{host}의 확인 문제 화면이에요 — {ask} / 글자로 답할 칸을 찾지 못했어요(문제를 불러오지 못했거나, 화면을 직접 눌러야 하는 문제예요). "
                                               "여기서는 답을 전할 수 없어요. 확인하셨으면 아무 글자나 넣어 주세요(거기서 멈출게요).",
                                               fields=[{"name": "captcha", "label": "확인했어요 (아무 글자)", "secret": False}],
                                               image={"mime": ch.get("mime") or "image/png", "b64": ch["b64"]}, deadline=overall, host=host)
                                    self._ask(login, overall)
                                    return self._end(login, "failed", f"{host}의 확인 문제에 답할 칸을 찾지 못해 여기서 멈췄어요. 잠시 뒤에 다시 해 주세요(문제를 불러오지 못한 것일 수 있어요).", "blocked")
                                if ch.get("present") and not ch.get("answerable"):
                                    note = "확인 절차가 떠 있는데, 화면을 직접 눌러야 하는 종류라 여기서는 답할 수 없어요."
                                    seen = " ".join(str(ch.get("text") or "").split())[:EXCERPT_CHARS]
                                    self._show(login, f"stuck:{host}:{login._submits}:{blank}:q", "need_choice", note,
                                               text=f"{screen_gist(host, seen)}\n{note}", choices=[WAIT_MORE, STOP], deadline=overall, host=host)
                                    got = self._ask_watch(login, overall, lambda: self._same_form(b, sig))
                                    if got is None or got is CHANGED:
                                        break
                                    if got.get("choice") == STOP:
                                        return self._end(login, "failed", f"{host}의 확인 절차에서 멈췄어요.", "blocked")
                                    blank += 1
                                    continue
                            # 화면이 넘어가지 않았다: 틀렸다고 단정하지 않는다. 화면의 글을 보여 주고 사용자가 정한다(확인 절차·안내 층이 떠 있을 수 있다 — 대신 풀지 않는다)
                            seen = " ".join((b.request("extract_text", {"ref": None}).get("text") or "").split())[:EXCERPT_CHARS]
                            note = "확인 절차나 안내 창이 떠 있는 것 같아요." if how == "blocked" else "제출했는데 화면이 넘어가지 않았어요."
                            self._show(login, f"stuck:{host}:{login._submits}:{blank}", "need_choice", note,
                                       text=f"{screen_gist(host, seen)}\n{note}", choices=[RETYPE, WAIT_MORE, STOP], deadline=overall, host=host)
                            got = self._ask_watch(login, overall, lambda: self._same_form(b, sig))
                            if got is None or got is CHANGED:   # 취소·시간 초과이거나 화면이 스스로 넘어갔다
                                break
                            if got.get("choice") == STOP:
                                return self._end(login, "cancelled", "로그인을 취소했어요")
                            if got.get("choice") == WAIT_MORE:   # 더 지켜본다
                                blank += 1
                                continue
                            last = None   # 다시 입력: 칸을 다시 띄운다(틀린 횟수로 세지 않는다)
                            break
                        rejected = how == "rejected"
                        continue
                    # 로그인 칸이 없는 화면
                    social = picked in PROVIDER_HOSTS
                    away = social and host in PROVIDER_HOSTS[picked]   # 아직 그 회사의 화면에 있다
                    if not submitted and not social:   # 아직 아무것도 넣지 않았다: 스크립트가 화면을 그리는 중일 수 있어 조금 기다린다
                        blank += 1
                        if blank >= 2 and handed < 2 and getattr(self, "handover_on", True):
                            from .inside import challenge_on
                            if challenge_on(b):
                                # 로그인 화면 대신 사람 확인(Cloudflare 등)이 떴다(실기기 2026-11-18 claude.ai): 폰으로 넘겨 직접 풀게 한다(D106)
                                handed += 1
                                if not self._hand(b, login, host, overall, handed, "사람 확인"):
                                    return self._end(login, "cancelled", "로그인을 취소했어요")
                                blank = 0
                                continue
                        if blank in (2, 6):
                            # 로그인 화면 대신 "접속이 차단되었습니다" 같은 짧은 안내만 있는 화면(실측 2026-11-15: 공공 포털이 첫 화면에서 이 브라우저를 막았다):
                            # 로그인 칸을 못 찾았다고 하지 않고 막혔다고 알린다. 돌아가지 않는다. 그 사이트는 공개 길에서도 다시 열지 않는다
                            page_text = " ".join((b.request("extract_text", {"ref": None}).get("text") or "").split())
                            if len(page_text) < 600 and any(w in page_text for w in (*REFUSED_WORDS, "접속이 차단", "접속이 불가능", "Access Denied")):
                                sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic refused host={host} at=door\n")
                                try:
                                    from .public import note_refused
                                    note_refused(host)
                                except Exception:
                                    pass
                                return self._end(login, "failed", f"{host}가 이 브라우저의 접속을 막았어요(\"접속이 차단되었다\"는 안내가 떴어요). 여기서 멈출게요 — 다시 해도 같아요.", "blocked")
                        if blank > 5:
                            return self._end(login, "failed", "로그인 칸을 찾지 못했어요", "unsupported_login")
                        time.sleep(self.poll_s)
                        continue
                    if social and not away and _same_site(host, home):
                        # 소셜 로그인을 마치고 사이트로 돌아왔고 로그인 칸이 없다: 끝난 것으로 본다(로그인 입구가 다시 보이면 아니다)
                        landed = self._landed(b, site)
                        if landed:
                            done_url = landed
                            break
                        submitted, last, blank, picked, waited_from = False, None, 0, None, None
                        time.sleep(self.poll_s)
                        continue
                    if picked == "easy" and easy_done and _same_site(host, home) and not easy_asked:
                        # 간편인증: 사용자가 폰에서 승인하고 카드에서 "인증 완료"를 눌렀고, 사이트로 돌아와 로그인 칸이 없다 →
                        # 다시 묻지 않고 로그인된 화면인지 코드가 확인한다(실기기 2026-11-16: 여기서 "로그인이 끝났나요?"를 한 번 더 물었다).
                        # 확인이 안 되면 그때만 아래 카드로 묻는다
                        easy_asked = True
                        landed = self._landed(b, site)
                        if landed:
                            done_url = landed
                            break
                    whole = " ".join((b.request("extract_text", {"ref": None}).get("text") or "").split())
                    seen = whole[:EXCERPT_CHARS]
                    if any(w in seen for w in REFUSED_WORDS):
                        # 그 화면이 이 브라우저의 로그인을 받지 않겠다고 한다: 기다리지 않고 멈춘다(실기기: 이것을 "폰 승인 대기"로 보여 줬다). 돌아가지 않는다
                        sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic refused host={host}\n")
                        if site.backend == "chrome":
                            return self._end(login, "failed", f"{host}가 에이전트가 넣는 로그인을 받지 않았어요. " + MANUAL_LOGIN_SAY.format(name=site.name, key=site.key), "blocked")
                        return self._end(login, "failed", f"{host}가 이 브라우저에서의 로그인을 받지 않았어요. 여기서 멈출게요. 다른 로그인 방법이 있으면 그쪽으로 해 주세요.", "blocked")
                    if away:
                        # 그 회사 화면에 칸은 없고 단추가 있다(예: "이 계정으로 계속하기", 동의 화면): 대신 누르지 않는다 — 화면 글을 보여 주고 사용자가 고른 것만 누른다
                        acts = [x for x in b.request("login_buttons").get("buttons", []) if x not in (WAIT_MORE, STOP)]
                        if acts:
                            shown = (host, tuple(acts))
                            gist = screen_gist(host, whole, acts)
                            self._show(login, f"act:{host}:{'|'.join(acts)}:{login._submits}:{blank}", "need_choice",
                                       gist if any(w in whole for w in APPROVAL) else f"{host} 화면에서 골라 주세요",
                                       text=gist, choices=[*acts, WAIT_MORE, STOP], deadline=overall, host=host)

                            def same():
                                g = b.request("form_fields")
                                return not g.get("fields") and (g.get("host") or "") == shown[0] and \
                                    tuple(x for x in b.request("login_buttons").get("buttons", []) if x not in (WAIT_MORE, STOP)) == shown[1]
                            got = self._ask_watch(login, overall, same)
                            if got is None or got is CHANGED:   # 화면이 스스로 넘어갔다(폰에서 승인했다 등): 다시 읽는다
                                continue
                            choice = got.get("choice")
                            if choice == STOP:
                                return self._end(login, "cancelled", "로그인을 취소했어요")
                            if choice != WAIT_MORE:
                                b.request("login_press", {"text": choice})
                            blank += 1
                            time.sleep(min(1.0, self.poll_s))
                            continue
                    if away and (waited_from is None or self.clock() - waited_from < APPROVAL_WAIT_S):
                        # 그 회사 화면에 칸 없이 머물러 있다(폰 승인·2단계 인증 대기): 화면을 지켜보다 넘어가면 이어 간다
                        waited_from = waited_from if waited_from is not None else self.clock()
                        self._show(login, f"wait:{host}:{login._submits}", "need_approval",
                                   screen_gist(host, whole) if any(w in whole for w in APPROVAL) else f"{screen_gist(host, whole)} — 폰에서 승인하면 이어서 진행돼요",
                                   deadline=overall, host=host)
                        time.sleep(self.poll_s)
                        continue
                    failed_msg = not away and bool(LOGIN_FAILED.search(whole))
                    # 사이트가 실패를 알렸으면 묻기 전에 스스로 본다(D120, 실기기의 채용 사이트: 실패 안내가 떠 있는데 "로그인이 끝났나요?"를 물었고,
                    # 실제로는 로그인돼 있었다). 로그아웃이 보이고 로그인 칸·입구가 없으면 묻지 않고 확인으로 넘어간다
                    if failed_msg and any(w in whole.lower() for w in SIGNED_IN_WORDS) and self._still_in(b, site):
                        landed = self._landed(b, site)
                        if landed:
                            sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic confirm self_checked host={host}\n")
                            done_url = landed
                            break
                    # 끝났는지, 승인·인증을 기다리는 화면인지 코드는 모른다 → 화면 글을 보여 주고 사용자가 고른다.
                    # 사이트가 실패를 알렸으면 그렇게 말하고 다시 해 보기를 먼저 둔다
                    self._show(login, f"confirm:{host}:{login._submits}:{blank}", "need_choice",
                               "사이트가 로그인에 실패했다고 해요" if failed_msg else "제출했어요. 화면을 확인해 주세요",
                               text=failed_text(host, seen) if failed_msg else confirm_text(host, seen, whole),
                               choices=[RETRY, DONE, STOP] if failed_msg else [DONE, NOT_YET, STOP], deadline=overall, host=host)
                    got = self._ask(login, overall)
                    if got is None:
                        continue
                    choice = got.get("choice")
                    if choice == STOP:
                        return self._end(login, "cancelled", "로그인을 취소했어요")
                    if choice == RETRY:   # 처음부터 다시(로그인 화면을 다시 연다 — 틀린 횟수는 그대로)
                        sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic retry_after_fail host={host}\n")
                        b.request("navigate", {"url": start})
                        submitted, last, blank, picked, waited_from = False, None, 0, None, None
                        continue
                    if choice == NOT_YET:   # 폰 승인·인증을 마치고 다시 본다(그 사이 새 칸이 나올 수 있다)
                        blank += 1
                        waited_from = self.clock() if away else waited_from
                        time.sleep(self.poll_s)
                        continue
                    landed = self._landed(b, site)   # 사용자가 끝났다고 했다: 로그인돼 있어야 열리는 화면으로 확인한다
                    if landed:
                        done_url = landed
                        break
                    submitted, last, blank, picked, waited_from = False, None, 0, None, None   # 로그인 화면이 다시 나왔다: 처음부터(틀린 횟수는 그대로)
                    continue
                self._keep(b, site)   # 브라우저를 닫기 전에 로그인 상태만 암호화 저장소로 옮긴다
            ok_scheme = urlsplit(done_url).scheme == "https" or (urlsplit(done_url).hostname or "") in self.insecure_hosts
            if not site.check_url and ok_scheme and len(done_url) <= 300:
                # 다음에 확인할 화면: 로그인이 끝났을 때 떠 있던 화면(물음표 뒤는 뗀다)
                site = Site(site.key, site.name, site.login_urls, done_url.split("?")[0], site.login_hosts, (), site.cookie_url, generic=True,
                            cloud_ok=self.sites.get(site.key, site).cloud_ok)
                with self._lock:
                    cur = self.sites.get(site.key)   # 로그인하는 사이에 적힌 '부르는 이름'을 잃지 않는다
                    if cur is not None and cur.aliases != site.aliases:
                        site = dataclasses.replace(site, aliases=cur.aliases)
                    self.sites[site.key] = site
            # 말하기 전에 확인한다: 보관한 것만으로 새 브라우저에서 로그인돼 있나
            self._show(login, "working:carry", "working", "로그인됐어요. 다음에도 이어서 쓸 수 있는지 확인하고 있어요", deadline=overall)
            carried = self._carried(site)
            sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic carry={ {True: 'ok', False: 'lost', None: 'unknown'}[carried] } host={home}\n")
            if carried is False:
                self.store.delete(self._key(site))
                self._mark(site, "logged_out")
                return self._end(login, "failed", f"{site.name} 로그인은 끝까지 됐는데, 그 로그인 상태가 다음에 열 때 이어지지 않았어요. "
                                 "이 사이트는 아직 이어서 쓸 수 없어요(다시 로그인해도 같아요).", "unsupported_login")
            # 어느 방법으로 들어왔는지 적어 둔다. 소셜이면 풀렸을 때 그 단추로 다시(D90), 아이디·비밀번호면 앱이 폰 키체인(Face ID)으로 다시 채운다(D105)
            via = picked if picked in PROVIDER_HOSTS or picked == "password" else ""
            with self._lock:
                cur = self.sites.get(site.key, site)
                if cur.via != via:
                    site = dataclasses.replace(cur, via=via)
                    self.sites[site.key] = site
            self._remember(site)
            self._mark(site, "logged_in")
            self._end(login, "logged_in", f"{site.name}에 로그인했어요")
        except Exception as e:
            self._note(e, "failed")
            self._end(login, "failed", "브라우저에서 문제가 생겨 로그인을 끝내지 못했어요", "browser_error")

    @staticmethod
    def _diag(login: Login, why: str, p: _Page) -> None:
        """모르는 화면의 뼈대만 서버 기록에 남긴다(다음에 처리할 수 있게): 주소의 경로와 요소 종류·이름 앞부분. 화면 본문·입력값·쿠키는 남기지 않는다."""
        parts = urlsplit(p.url)
        wide = why.startswith("setup")
        roles = ("textbox", "button", "checkbox", "img", "heading") + (("link", "radio", "tab", "combobox", "strong") if wide else ())
        els = [f"{e.role}:{e.label.strip()[:28 if wide else 16]}" for e in p.obs.elements.values() if e.role in roles]
        sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} {login.site} {login.method} {why} "
                         f"{parts.hostname}{parts.path} elements={els[:70 if wide else 20]}\n")

    def _debug(self, b, login: Login, p: _Page) -> None:
        """개발용 기록(SITE_LOGIN_DEBUG=1일 때만): 화면이 바뀔 때마다 주소(경로·오류 표시)와 화면 캡처를 이 맥의 폴더에만 남긴다.

        캡처에는 입력한 아이디가 보인다(비밀번호 칸은 브라우저가 점으로 가린다). 폴더 권한 700, 하루 지난 것은 지운다. 원인을 찾으면 끈다.
        """
        if self.debug_dir is None:
            return
        parts = urlsplit(p.url)
        sig = (parts.path, tuple(f"{e.role}:{e.label}" for e in p.obs.elements.values()), len(p.text))
        if sig == login._debug_sig:
            return
        login._debug_sig = sig
        login._debug_n += 1
        q = {k: (v[0][:40] if k.lower() in ("mode", "error", "errorcode", "err", "svctype", "locale", "step") else "…")
             for k, v in parse_qs(parts.query).items()}
        marks = [n for n, ws in (("wrong", WRONG), ("captcha", CAPTCHA), ("approval", APPROVAL), ("expired", EXPIRED),
                                 ("blocked", BLOCKED)) if any(w in p.text for w in ws)]
        shot = ""
        try:
            d = Path(self.debug_dir) / login.id
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.debug_dir, 0o700)
            path = d / f"screen-{login._debug_n:02d}.jpg"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode(b.request("screenshot")["jpeg_b64"]))
            shot = str(path)
        except Exception:
            pass
        sys.stderr.write(f"[site-login-debug] {time.strftime('%Y-%m-%d %H:%M:%S')} {login.id} #{login._debug_n} submits={login._submits} "
                         f"{parts.hostname}{parts.path} query={q} marks={marks} "
                         f"fields={[e.label.strip()[:16] for e in p.boxes()]} cookies_auth={sorted(set(p.cookies) & {'NID_AUT', 'NID_SES'})} "
                         f"shot={shot}\n")

    def _qr_login(self, b, site: Site, login: Login, overall: float) -> str | None:
        """QR 로그인(D85): QR 탭을 누르고, 뜬 QR 그림을 사용자에게 보여 주고, 폰에서 승인해 사이트로 넘어갈 때까지 지켜본다.
        → 로그인이 끝난 화면의 주소. 끝내 버렸으면(취소·만료·막힘) None. 비밀번호는 어디에도 넣지 않는다. 그림은 메모리에만 두고 기록하지 않는다."""
        import hashlib
        try:
            b.request("qr_press", {"what": "tab"})
        except Exception as e:
            self._note(e, "qr")
            self._end(login, "failed", "QR 로그인 화면을 열지 못했어요", "unsupported_login")
            return None
        t0, refreshed, shown_once = self.clock(), 0, False
        while True:
            if login._cancel:
                self._end(login, "cancelled", "로그인을 취소했어요")
                return None
            if self.clock() > overall:
                self._end(login, "expired", "시간이 지나 로그인을 끝냈어요. 다시 시작해 주세요")
                return None
            q = b.request("qr_form")
            host = q.get("host") or ""
            if not q.get("shown"):
                f = b.request("form_fields")
                if shown_once and not f.get("fields"):
                    # QR이 사라지고 로그인 칸도 없다: 폰에서 승인해 넘어갔는지 로그인된 화면으로 확인한다
                    landed = self._landed(b, site)
                    if landed:
                        sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic qr done host={host}\n")
                        return landed
                whole = " ".join((b.request("extract_text", {"ref": None}).get("text") or "").split())
                if len(whole) < 600 and any(w in whole for w in (*REFUSED_WORDS, "접속이 차단", "Access Denied")):
                    sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic refused host={host} at=qr\n")
                    self._end(login, "failed", f"{host}가 이 브라우저의 접속을 막았어요. 여기서 멈출게요 — 다시 해도 같아요.", "blocked")
                    return None
                if not shown_once and self.clock() - t0 > QR_FIND_S and any(w in whole for w in (*REQUEST_FAILED, "다시 시도")):
                    # QR 탭은 열렸는데 그림 대신 "다시 시도" 같은 안내가 떴다: 사이트가 이 브라우저에는 QR을 내주지 않았다
                    # (실기기 2026-11-16, 쿠팡: 같은 화면이 맥의 보통 크롬에서는 QR을 그렸다). 다시 누르지 않고 멈춘다
                    sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic refused host={host} at=qr_issue\n")
                    self._end(login, "failed", f"{host}가 이 브라우저에는 QR 코드를 내주지 않았어요(화면에 QR 대신 다시 시도하라는 안내가 떴어요). "
                              "다시 누르지 않고 여기서 멈출게요.", "blocked")
                    return None
                if self.clock() - t0 > (QR_FIND_S if not shown_once else 20.0):
                    self._end(login, "failed", "QR 코드를 화면에서 찾지 못했어요" if not shown_once else "QR 화면이 사라졌는데 로그인된 것을 확인하지 못했어요. 다시 시작해 주세요",
                              "unsupported_login" if not shown_once else "timeout")
                    return None
                time.sleep(min(1.0, max(self.poll_s, 0.2)))
                continue
            shown_once, t0 = True, self.clock()
            if q.get("expired"):
                if refreshed >= QR_MAX_REFRESH or not q.get("refresh"):
                    self._end(login, "expired", "QR 코드 시간이 지났어요. 다시 시작해 주세요")
                    return None
                self._show(login, f"qr:expired:{refreshed}", "need_choice", "QR 코드 시간이 지났어요", text="QR 코드 시간이 지났어요. 새로 받을까요?",
                           choices=[QR_REFRESH, STOP], deadline=overall, host=host)
                got = self._ask(login, overall)
                if got is None:
                    continue
                if got.get("choice") != QR_REFRESH:
                    self._end(login, "cancelled", "로그인을 취소했어요")
                    return None
                refreshed += 1
                try:
                    b.request("qr_press", {"what": "refresh"})
                except Exception as e:
                    self._note(e, "qr_refresh")
                time.sleep(min(1.0, max(self.poll_s, 0.2)))
                continue
            try:
                png = b.request("qr_image")["png_b64"]
            except Exception as e:
                self._note(e, "qr_image")
                time.sleep(min(1.0, max(self.poll_s, 0.2)))
                continue
            number = q.get("number")
            kind = f"qr:{host}:{hashlib.sha256(png.encode()).hexdigest()[:12]}:{number}"
            if kind != login._kind:
                sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic show qr host={host} number={'y' if number else 'n'}\n")
            self._show(login, kind, "need_approval",
                       f"{site.name} 앱으로 이 QR 코드를 찍어 로그인해 주세요" + (f" — 앱에서 숫자 {number}을(를) 골라 주세요" if number else ""),
                       qr={"mime": "image/png", "b64": png}, number=number, deadline=overall, host=host)
            time.sleep(self.poll_s)

    def _easy_auth(self, b, site: Site, login: Login, overall: float, ez: dict, note: str = "") -> bool:
        """간편인증 입력 화면(D79): 인증 기관 고르기 → 이름·생년월일·휴대폰 번호 → 동의 확인 → "인증 요청"까지. 끝냈으면(실패·취소) True.

        - 값은 사용자가 앱의 카드에 넣은 것만, 그 화면의 칸에만 넣는다. 보관·기록하지 않고 모델에 주지 않는다.
        - 동의 항목은 화면에서 읽은 이름을 그대로 보여 주고 사용자가 "동의하고 인증 요청"을 골랐을 때만 표시한다(필수 항목만).
        - 주민등록번호 칸이 있거나 알아보지 못한 칸이 있는 화면에는 넣지 않는다. 폰에서의 승인은 사용자가 한다.
        """
        host = ez.get("host") or ""
        sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic easy-auth host={host} providers={len(ez.get('providers') or [])} "
                         f"fields={len(ez.get('fields') or [])} consents={len(ez.get('consents') or [])}\n")
        if not ez.get("https") and host not in self.insecure_hosts:
            self._end(login, "failed", "암호화되지 않은 화면(https 아님)이라 입력을 받지 않았어요", "not_https")
            return True
        names = sorted(x["name"] for x in ez.get("fields") or [])
        if ez.get("rrn") or ez.get("other") or names not in (["birth", "name", "phone"], ["birth", "phone"]):
            self._end(login, "failed", f"{site.name}의 간편인증 화면이 주민등록번호 같은 다른 정보를 요구해요. 그런 화면에는 대신 넣지 않아요. "
                      "다른 로그인 방법을 골라 주세요.", "unsupported_login")
            return True
        providers = [p for p in ez.get("providers") or [] if isinstance(p, str)][:20]
        before = getattr(login, "_easy", None) or {}   # 다시 입력받는 경우: 앞에서 고른 인증 앱과 동의는 다시 묻지 않는다(화면의 항목이 같을 때)
        provider = before.get("provider") if note and before.get("provider") in providers else None
        if providers and provider is None:
            self._show(login, f"easy:who:{host}:{login._submits}:{'|'.join(sorted(providers))}", "need_choice", "간편인증에 쓸 인증 앱을 골라 주세요",
                       text=f"간편인증 화면({host})이에요. 어느 인증 앱으로 인증할까요? (폰에 그 앱이 있어야 해요)", choices=[*providers, STOP], deadline=overall, host=host)
            got = self._ask(login, overall)
            if got is None or got.get("choice") == STOP or got.get("choice") not in providers:
                self._end(login, "cancelled", "로그인을 취소했어요")
                return True
            provider = got["choice"]
        labels = {"name": "이름", "birth": "생년월일 8자리 (예: 19900101)", "phone": "휴대폰 번호 (숫자만)"}
        prefix = [x for x in ez.get("prefix") or [] if isinstance(x, str)]
        required = [c["label"] for c in ez.get("consents") or [] if c.get("required")]
        tries = 0
        while True:
            tries += 1
            if tries > MAX_WRONG + 1:
                self._end(login, "failed", "입력이 여러 번 맞지 않아 로그인을 끝냈어요", "wrong_input")
                return True
            self._show(login, f"easy:in:{host}:{login._submits}:{tries}", "need_input",
                       note or f"간편인증 화면({host})이에요. 본인 확인에 쓸 정보를 입력해 주세요. 맥에 저장하지 않아요",
                       fields=[{"name": n, "label": labels[n], "secret": False} for n in ("name", "birth", "phone") if n in names],
                       error="wrong_input" if note else None, deadline=overall, host=host)
            values = self._ask(login, overall)
            if values is None:
                self._end(login, "cancelled", "로그인을 취소했어요")
                return True
            values = {k: " ".join(str(v).split()) for k, v in values.items() if k in names}
            birth = "".join(ch for ch in values.get("birth", "") if ch.isdigit())
            phone = "".join(ch for ch in values.get("phone", "") if ch.isdigit())
            if len(birth) != 8 or not ("1900" <= birth[:4] <= "2100" and "01" <= birth[4:6] <= "12" and "01" <= birth[6:] <= "31"):
                note = "생년월일은 숫자 8자리로 입력해 주세요 (예: 19900101)"
                continue
            if not 10 <= len(phone) <= 11 or (prefix and phone[:3] not in prefix):
                note = "휴대폰 번호를 숫자만으로 다시 입력해 주세요 (예: 01012345678)"
                continue
            if "name" in names and not values.get("name"):
                note = "이름을 입력해 주세요"
                continue
            values["birth"], values["phone"] = birth, phone
            break
        if required and not (note and before.get("agreed") == tuple(required)):
            listed = "\n".join(f"· {c}" for c in required)
            self._show(login, f"easy:agree:{host}:{login._submits}:{'|'.join(required)}", "need_choice", "인증을 요청하려면 동의가 필요해요",
                       text=f"{host}의 간편인증은 아래 항목에 동의해야 요청할 수 있어요.\n{listed}\n동의하고 {provider or '인증 앱'}(으)로 인증을 요청할까요?",
                       choices=[EASY_AGREE, STOP], deadline=overall, host=host)
            got = self._ask(login, overall)
            if got is None or got.get("choice") != EASY_AGREE:
                self._end(login, "cancelled", "동의하지 않아 인증을 요청하지 않았어요")
                return True
        login._easy = {"provider": provider, "agreed": tuple(required)}   # 고른 것만(넣은 값은 두지 않는다)
        self._show(login, f"easy:send:{host}:{login._submits}", "working", "인증을 요청하고 있어요", deadline=overall)
        try:
            b.request("easy_fill", {"provider": provider, "values": values, "consents": required})
            b.request("login_press", {"text": "인증 요청"})
        except Exception as e:
            self._note(e, "easy")
            self._end(login, "failed", "간편인증 화면에 넣지 못했어요(화면이 바뀌었을 수 있어요). 다시 시도해 주세요", "unsupported_login")
            return True
        finally:
            values.clear()
        time.sleep(min(1.5, max(self.poll_s, 0.3)))
        return False

    def _hand(self, b, login: Login, host: str, overall: float, n: int, what: str) -> bool:
        """그 브라우저를 폰에 넘겨 사람이 직접 풀게 한다(D106) → '다 했어요'면 True, 그만·시간 초과면 False. 맥은 그동안 누르지 않는다."""
        from .handover import STORE
        hid = STORE.open(b, f"{host}가 {what}을 냈어요. 이 확인은 사람이 직접 해야 해요.", host)
        sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic handover open host={host} what={what}\n")
        try:
            self._show(login, f"hand:{host}:{login._submits}:{n}", "need_choice", f"{host}가 {what}을 냈어요. 직접 풀어 주세요",
                       text=f"{host}가 {what}을 냈어요. '브라우저 열기'로 화면을 열어 직접 풀어 주세요. 끝나면 '다 했어요'를 눌러 주세요.",
                       choices=[HAND_DONE, STOP], handover=STORE.public(hid), deadline=overall, host=host)
            got = self._ask(login, overall)
        finally:
            STORE.close(hid)
        done = bool(got) and got.get("choice") == HAND_DONE
        if done:   # 확인이 몇 초 더 돈다(실기기 claude.ai) — 사람 확인 화면이 사라질 때까지 잠깐 기다린 뒤 이어 간다
            from .inside import wait_cleared
            self._show(login, f"handwait:{host}:{n}", "working", "확인이 끝나기를 기다리고 있어요", deadline=overall)
            wait_cleared(b)
        sys.stderr.write(f"[site-login] {time.strftime('%Y-%m-%d %H:%M:%S')} generic handover close host={host} done={'y' if done else 'n'}\n")
        return done

    def _ask(self, login: Login, overall: float):
        """사용자 입력·선택이 올 때까지 기다린다. 취소·시간 초과면 None."""
        while True:
            if login._cancel or self.clock() > overall:
                return None
            try:
                item = login._inputs.get(timeout=min(1.0, max(0.1, overall - self.clock())))
            except queue.Empty:
                continue
            if item is None:
                return None
            return item[1]

    def _after_submit(self, b, login: Login, sig: tuple, overall: float, before: str = "") -> str:
        """제출한 뒤 화면이 어떻게 됐는지 지켜본다 → moved(넘어감) | rejected(거절 안내·비밀번호 칸이 비워짐) | blocked(덮는 층·확인 절차) | stuck(그대로).

        같은 칸이 보인다는 것만으로 틀렸다고 하지 않는다 — 사이트가 확인하는 동안에도 칸은 그대로 있다.
        before: 제출하기 직전에 화면에 이미 떠 있던 안내 글. 거절은 **제출한 뒤에 새로 뜬(달라진) 안내**이거나 비밀번호 칸이 비워졌을 때만이다 —
        실기기: 입력 칸 아래에 원래 있는 도움말("TIP …")을 오류 안내로 읽어, 맞는 계정을 거절됐다고 했다.
        """
        end = min(overall, self.clock() + self.settle_s)
        hits = {"rejected": 0, "blocked": 0}
        while True:
            if login._cancel:
                return "moved"
            time.sleep(min(0.7, max(0.01, self.poll_s)))
            f = b.request("form_fields")
            if ((f.get("host") or ""), tuple(x["name"] for x in f.get("fields", []))) != sig:
                return "moved"
            new_alert = bool(f.get("alert")) and " ".join(str(f.get("alert_text") or "").split()) != before
            for k, on in (("rejected", new_alert or f.get("pw_empty")), ("blocked", f.get("dialog") or f.get("challenge"))):
                hits[k] = hits[k] + 1 if on else 0
            if hits["rejected"] >= 2:      # 두 번 이어서 보일 때만(화면이 바뀌는 도중의 한순간이 아니다)
                return "rejected"
            if hits["blocked"] >= 3:
                return "blocked"
            if self.clock() >= end:
                return "stuck"

    def _ask_watch(self, login: Login, overall: float, same):
        """사용자의 선택을 기다리되, 그 사이 화면이 바뀌면(same()이 거짓) CHANGED를 돌려준다. 취소·시간 초과면 None.

        폰에서 승인하면 화면이 스스로 넘어가는 로그인이 있다 — 물음을 띄워 둔 채 멈춰 있지 않게 한다.
        """
        while True:
            if login._cancel or self.clock() > overall:
                return None
            try:
                item = login._inputs.get(timeout=max(0.05, self.poll_s))
            except queue.Empty:
                try:
                    if not same():
                        return CHANGED
                except Exception:
                    return CHANGED
                continue
            if item is None:
                return None
            return item[1]

    def _run_mail_setup(self, site: Site, login: Login) -> None:
        """네이버 메일 연결(D54): 로그인된 브라우저로 정해진 길을 간다. 바꾸는 단계는 사용자 승인 뒤에만.

        주소 묻기 → IMAP/SMTP 사용 확인(꺼져 있으면 승인 받고 켬) → 보안설정 → 2단계 인증 관리 → 비밀번호 재확인(사용자 입력)
        → 승인 → 애플리케이션 비밀번호 생성 → 값을 읽어 메일 계정으로 연결(로그인 확인 뒤 보안 저장소에 보관).
        만든 비밀번호는 기록·화면 캡처·응답에 넣지 않는다.
        """
        base = self.sites.get("naver", NAVER)
        overall = self.clock() + OVERALL_TIMEOUT_S
        pause = lambda: time.sleep(min(2.5, self.poll_s * 2))
        stop = lambda: self._end(login, "cancelled" if login._cancel else "expired",
                                 "취소했어요" if login._cancel else "시간이 지나 끝냈어요. 다시 시작해 주세요")
        if self.mailer is None:
            return self._end(login, "failed", "메일 연동이 켜져 있지 않아요", "browser_error")
        try:
            with self.sandbox.session(self._uid(base)) as b:
                if not self.restore(b, base):
                    return self._end(login, "failed", "먼저 네이버 로그인을 해 주세요", "not_logged_in")
                # 0. 연결할 주소
                address, tries = None, 0
                while address is None:
                    self._show(login, f"address:{tries}", "need_input",
                               "연결할 네이버 메일 주소(또는 아이디)를 입력해 주세요" if not tries else "네이버 메일 주소 형식이 아니에요. 다시 입력해 주세요",
                               fields=[{"name": "id", "label": "네이버 메일 주소", "secret": False}],
                               error="wrong_input" if tries else None, deadline=overall)
                    v = self._ask(login, overall)
                    if v is None:
                        return stop()
                    a = v["id"].strip().lower()
                    a = a if "@" in a else a + "@naver.com"
                    tries += 1
                    if re.fullmatch(r"[a-z0-9._-]{3,40}@naver\.com", a):
                        address = a
                    elif tries >= 3:
                        return self._end(login, "failed", "주소를 확인하지 못했어요", "wrong_input")
                # 1. IMAP/SMTP 사용 여부(읽기). 꺼져 있을 때만 승인을 받아 켠다
                self._show(login, "working:imap", "working", "네이버 메일의 IMAP/SMTP 설정을 확인하고 있어요", deadline=overall)
                b.request("navigate", {"url": NAVER_MAIL_SETTINGS_URL})
                pause()
                p = self._snap(b, base)
                tab = p.find("link", "IMAP/SMTP 설정", exact=True)
                if tab is not None:
                    b.request("click", {"ref": tab.ref})
                    pause()
                    p = self._snap(b, base)
                self._debug(b, login, p)
                on = p.find("radio", "사용함", exact=True)
                if on is None:
                    self._diag(login, "setup_imap_page", p)
                    return self._end(login, "failed", "네이버 메일 설정 화면을 알아보지 못했어요", "unsupported_page")
                if on.value != "선택됨":
                    self._show(login, "choice:imap", "need_choice", "IMAP/SMTP 사용을 켤까요?",
                               text="네이버 메일의 IMAP/SMTP 사용이 꺼져 있어요. 메일을 읽고 보내려면 '사용함'으로 바꿔야 해요. 바꿀까요?",
                               choices=["바꾸기", "취소"], deadline=overall)
                    v = self._ask(login, overall)
                    if v is None or v.get("choice") != "바꾸기":
                        return self._end(login, "cancelled", "IMAP/SMTP 설정을 바꾸지 않고 끝냈어요")
                    b.request("click", {"ref": on.ref})
                    save = p.find("button", "저장", exact=True)
                    b.request("dialog_policy", {"accept": True})
                    b.request("click", {"ref": save.ref})
                    pause()
                    b.request("dialog_policy", {"accept": False})
                    p = self._snap(b, base)
                    on = p.find("radio", "사용함", exact=True)
                    if on is None or on.value != "선택됨":
                        self._diag(login, "setup_imap_save", p)
                        return self._end(login, "failed", "IMAP/SMTP 설정을 저장하지 못했어요", "unsupported_page")
                # 2. 보안설정 → 2단계 인증 관리 → 비밀번호 재확인
                self._show(login, "working:security", "working", "네이버 보안설정으로 가고 있어요", deadline=overall)
                b.request("navigate", {"url": site.login_urls[login.method]})
                pause()
                p = self._snap(b, base)
                self._debug(b, login, p)
                ref = button_after(p.obs.text, "2단계 인증", "관리")
                if ref is None:
                    self._diag(login, "setup_no_2step_button", p)
                    return self._end(login, "failed", "네이버 보안설정 화면에서 2단계 인증 관리를 찾지 못했어요(2단계 인증이 꺼져 있을 수 있어요)",
                                     "unsupported_page")
                b.request("click", {"ref": ref})
                pause()
                submitted = False
                while True:
                    p = self._snap(b, base)
                    self._debug(b, login, p)
                    boxes = p.boxes()
                    if not (len(boxes) == 1 and _field_name(boxes[0].label) == "password" and "재확인" in p.text):
                        break
                    if submitted:
                        login.wrong += 1
                        if login.wrong > MAX_WRONG:
                            return self._end(login, "failed", "비밀번호가 여러 번 맞지 않아 끝냈어요", "wrong_input")
                    self._show(login, f"recheck:{login.wrong}:{login._submits}", "need_input",
                               "입력한 비밀번호가 맞지 않아요. 다시 입력해 주세요" if submitted else
                               "네이버가 본인 확인을 위해 비밀번호를 다시 물어요(2단계 인증 관리 화면으로 들어가는 중이에요)",
                               fields=[{"name": "password", "label": boxes[0].label.strip(), "secret": True}],
                               error="wrong_input" if submitted else None, deadline=overall)
                    v = self._ask(login, overall)
                    if v is None:
                        return stop()
                    self._apply(b, login, p, v)
                    submitted = True
                    pause()
                # 3. 애플리케이션 비밀번호 생성 (승인 뒤)
                custom = p.find("radio", "직접 입력", exact=True)
                create = p.find("button", "생성하기", exact=True)
                name_ref = ref_after(p.obs.text, '"직접 입력"', "textbox")
                if custom is None or create is None or name_ref is None:
                    self._diag(login, "setup_reached", p)
                    return self._end(login, "failed", "2단계 인증 관리 화면을 알아보지 못했어요. 아무것도 바꾸지 않았어요", "unsupported_page")
                self._show(login, "choice:create", "need_choice", "애플리케이션 비밀번호를 만들까요?",
                           text=f"네이버에 '{APP_PASSWORD_NAME}'이라는 이름의 애플리케이션 비밀번호를 새로 만들까요? "
                                f"만든 비밀번호는 {address} 메일을 읽고 보내는 데만 쓰고, 맥의 보안 저장소에만 보관해요. "
                                "네이버 보안설정에서 언제든 지울 수 있어요.",
                           choices=["만들기", "취소"], deadline=overall)
                v = self._ask(login, overall)
                if v is None or v.get("choice") != "만들기":
                    return self._end(login, "cancelled", "애플리케이션 비밀번호를 만들지 않고 끝냈어요")
                self._show(login, "working:create", "working", "애플리케이션 비밀번호를 만들고 있어요", deadline=overall)
                before = {e.ref for e in p.obs.elements.values() if e.role == "textbox"}
                b.request("click", {"ref": custom.ref})
                b.request("secret_type", {"ref": name_ref, "text": APP_PASSWORD_NAME})
                b.request("dialog_policy", {"accept": True})
                b.request("click", {"ref": create.ref})
                pause()
                b.request("dialog_policy", {"accept": False})
                # 여기부터는 화면에 비밀번호가 보인다: 화면 기록(_debug)을 하지 않는다
                p = self._snap(b, base)
                secret = ""
                for e in p.obs.elements.values():
                    if e.role != "textbox":
                        continue
                    val = b.request("field_secret", {"ref": e.ref}).get("value", "").strip()
                    if val and val != APP_PASSWORD_NAME and len(val) >= 8 and " " not in val:
                        secret = val
                        break
                if not secret:
                    self._diag(login, "setup_after_create", p)
                    return self._end(login, "failed", f"비밀번호를 만들었을 수 있지만 읽지 못했어요. 네이버 보안설정의 사용 내역에 "
                                     f"'{APP_PASSWORD_NAME}'이 생겼으면 지워 주세요", "unsupported_page")
                # 4. 메일 계정으로 연결: 로그인(IMAP·SMTP)이 확인된 뒤에만 보관된다
                self._show(login, "working:connect", "working", "만든 비밀번호로 메일 서버에 연결해 보고 있어요", deadline=overall)
                try:
                    accounts = self.mailer.connect(address, secret, make_default=True)
                except Exception as e:
                    reason = getattr(e, "reason", "browser_error")
                    return self._end(login, "failed", f"메일 계정으로 등록하지 못했어요({reason}). 네이버 보안설정에서 "
                                     f"'{APP_PASSWORD_NAME}' 비밀번호를 지워 주세요", "blocked" if reason == "rate_limited" else "unsupported_page")
                finally:
                    secret = ""
                st = next((a["status"] for a in accounts if a["address"] == address), "auth_failed")
                if st != "ready":
                    return self._end(login, "failed", f"비밀번호는 만들었지만 메일 서버 로그인에 실패했어요({st}). 주소가 맞는지 확인하고, "
                                     f"네이버 보안설정에서 '{APP_PASSWORD_NAME}' 비밀번호를 지운 뒤 다시 해 주세요", "wrong_input")
            self._end(login, "logged_in", "네이버 메일을 연결했어요. 이제 메일을 읽고 보낼 수 있어요")
        except Exception:
            self._end(login, "failed", "브라우저에서 문제가 생겨 끝내지 못했어요", "browser_error")

    def _classify(self, b, site: Site, login: Login, p: _Page, submitted: bool, overall: float) -> str:
        """지금 화면이 무엇을 요구하는지 가려 login을 갱신한다. 돌려주는 값: "input"(입력 대기) | "poll"(지켜보기) | "end"."""
        text = p.text
        if any(w in text for w in BLOCKED):
            self._end(login, "failed", f"{site.name}가 이 로그인을 막았어요. 잠시 뒤에 다시 하거나 다른 방법을 써 주세요", "blocked")
            return "end"
        qr = p.find("img", "QR")
        if qr is not None:
            if any(w in text for w in EXPIRED):
                self._end(login, "expired", "QR 코드 시간이 지났어요. 다시 시작해 주세요")
                return "end"
            m = re.search(r"QR\s*Code\s*(\d{1,3})\b", text) or re.search(r"(?m)^\s*(\d{1,3})\s*$", text)
            number = m.group(1) if m else None
            left = re.search(r"(\d{1,2}):(\d{2})", text)
            deadline = min(overall, self.clock() + int(left.group(1)) * 60 + int(left.group(2))) if left else overall
            kind = f"qr:{number}"
            image = login.qr
            if kind != login._kind or image is None:
                png = b.request("element_screenshot", {"ref": qr.ref})["png_b64"]
                image = {"mime": "image/png", "b64": png}
            self._show(login, kind, "need_approval",
                       f"{site.name} 앱으로 QR 코드를 열고" + (f" 숫자 {number}을(를) 선택해 주세요" if number else " 승인해 주세요"),
                       qr=image, number=number, deadline=deadline)
            return "poll"
        boxes = p.boxes()
        if boxes:
            names = [_field_name(e.label) for e in boxes]
            if None in names or len(set(names)) != len(names):
                self._diag(login, "unsupported_fields", p)
                self._end(login, "failed", "앱에서 처리할 수 없는 화면이 나왔어요", "unsupported_page")
                return "end"
            # 제출했는데 같은 입력 화면이 다시 나왔다: 문구를 알아보지 못해도 틀린 입력으로 본다(같은 값을 되풀이해 넣지 않게)
            wrong = submitted and (any(w in text for w in WRONG) or names == login._submitted_names)
            login._submitted_names = None
            if wrong:
                login.wrong += 1
                if login.wrong > MAX_WRONG:
                    self._end(login, "failed", "입력이 여러 번 맞지 않아 로그인을 끝냈어요", "wrong_input")
                    return "end"
            image, question = None, ""
            if "captcha" in names:
                # 그림: 이름으로 찾고, 없으면 화면의 첫 그림(추가 확인 화면의 그림 이름은 그림 내용이라 매번 다르다)
                img = p.find("img", "자동입력", "자동 입력", "보안", "문자", "captcha", "Captcha") or \
                    next((e for e in p.obs.elements.values() if e.role == "img" and e.label.strip()), None)
                if img is not None:
                    image = {"mime": "image/png", "b64": b.request("element_screenshot", {"ref": img.ref})["png_b64"]}
                question = captcha_question(text, site.name)   # 화면이 묻는 말만(안내 줄·버튼 이름은 뺀다)
            fields = [{"name": n, "label": e.label.strip(), "secret": n == "password"} for n, e in zip(names, boxes)]
            kind = "input:" + ",".join(names) + f":{login.wrong}:{login._submits}"
            msg = f"{site.name} 로그인 정보를 입력해 주세요"
            if "captcha" in names:
                msg = f"그림을 보고 답해 주세요: {question}" if question else "그림을 보고 답을 입력해 주세요"
            if wrong:
                msg = "입력한 내용이 맞지 않아요. 다시 입력해 주세요" + (f" — {question}" if question else "")
            self._show(login, kind, "need_input", msg, fields=fields, image=image,
                       error="wrong_input" if wrong else None, deadline=overall)
            return "input"
        if any(w in text for w in APPROVAL):
            self._show(login, "approval", "need_approval", f"휴대폰의 {site.name} 앱 알림에서 로그인을 승인해 주세요", deadline=overall)
            return "poll"
        host = urlsplit(p.url).hostname or ""
        choices = [e.label.strip() for e in p.obs.elements.values()
                   if e.role in ("button", "link") and e.label.strip() in CHOICE_LABELS][:4]
        if host in site.login_hosts and len(choices) >= 1 and submitted:
            self._show(login, "choice:" + "|".join(choices), "need_choice", "화면의 물음에 답해 주세요",
                       text=" ".join(text.split())[:300], choices=choices, deadline=overall)
            return "input"
        if host not in site.login_hosts or not submitted:
            # 로그인 화면을 떠났거나 아직 넘어가는 중: 쿠키가 생기는지 조금 더 지켜본다
            self._show(login, "working", "working", "확인하고 있어요", deadline=overall)
            return "poll"
        self._diag(login, "unsupported_page", p)
        self._end(login, "failed", "앱에서 처리할 수 없는 화면이 나왔어요", "unsupported_page")
        return "end"

    def _apply(self, b, login: Login, p: _Page, values: dict) -> None:
        """사용자가 준 값을 화면에 넣고 제출한다. 값은 여기서 쓰고 버린다."""
        if "choice" in values:
            el = next(e for e in p.obs.elements.values()
                      if e.role in ("button", "link") and e.label.strip() == values["choice"])
            b.request("click", {"ref": el.ref})
            return
        login._submits += 1
        login._submitted_names = [_field_name(e.label) for e in p.boxes()]
        for e in p.boxes():
            name = _field_name(e.label)
            if name in values:
                b.request("secret_type", {"ref": e.ref, "text": values[name]})
        keep = p.find("checkbox", KEEP_LOGIN)
        if keep is not None and not login._kept:
            # 로그인 상태를 남기는 것이 목적이다. 관찰 결과로는 체크 여부를 알 수 없어 로그인마다 한 번만 누른다. 못 눌러도 계속한다
            login._kept = True
            try:
                b.request("click", {"ref": keep.ref})
            except Exception:
                pass
        btn = p.find("button", "로그인", exact=True) or p.find("button", "로그인", "확인", "다음")
        if btn is None:
            raise RuntimeError("no submit button")
        b.request("click", {"ref": btn.ref})
