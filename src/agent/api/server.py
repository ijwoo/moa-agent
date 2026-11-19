"""앱이 호출하는 HTTP API (Phase 8 축소판, D40). 명세: docs/API.md

- 127.0.0.1에만 바인딩한다. 외부(tailnet) 노출은 `tailscale serve`가 HTTPS로 감싸서 한다.
- 모든 요청은 X-App-Token 헤더가 필요하다. 토큰은 .env의 AGENT_APP_TOKEN (커밋하지 않는다).
- 요청·응답 본문은 로그에 남기지 않는다.
"""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..providers.base import ProviderError
from .dialog import MAX_CONTACTS, MAX_TEXT_CHARS, TONES
from .jobs import (MAX_ANSWER_CHARS, MAX_CONTEXT_CHARS, MAX_REASON_CHARS, MAX_TASK_CHARS, Busy, Conflict, Gone, HashMismatch, JobManager, NotFound)

MAX_BODY = 64 * 1024
TOKEN_HEADER = "X-App-Token"
API_VERSION_HEADER = "X-Api-Version"
_CLIENT_MSG_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
AUTH_FAIL_LIMIT, AUTH_FAIL_WINDOW_S = 10, 60.0
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EMAIL_FULL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_CONV_ID = re.compile(r"^c_[A-Za-z0-9_-]{8,40}$")
_TASK_PATH = re.compile(r"^/v1/tasks/([A-Za-z0-9_-]{8,40})(/approval|/cancel|/answer|/notification|/browser)?$")
_ATTACH_PATH = re.compile(r"^/v1/tasks/([A-Za-z0-9_-]{8,40})/attachments/([A-Za-z0-9_-]{1,80})$")
API_VERSION = 2


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def _contacts(v) -> dict[str, str] | None:
    if v is None:
        return None
    if not isinstance(v, dict) or len(v) > MAX_CONTACTS:
        raise ApiError(400, "invalid_request", f"contacts는 {{이름: 메일 주소}} 객체({MAX_CONTACTS}개 이하)")
    out = {}
    for name, addr in v.items():
        if not (isinstance(name, str) and 0 < len(name.strip()) <= 50 and isinstance(addr, str)
                and len(addr) <= 254 and _EMAIL_FULL.match(addr.strip())):
            raise ApiError(400, "invalid_request", "contacts의 이름(1~50자)과 메일 주소 형식을 확인")
        out[name.strip()] = addr.strip()
    return out


def _no_mail(v) -> list[str] | None:
    """연락처에 있지만 메일 주소가 없는 사람의 이름(D78). 선택 키라 꼴이 다르면 없는 것으로 본다."""
    if not isinstance(v, list):
        return None
    return list(dict.fromkeys(n.strip() for n in v[:MAX_CONTACTS] if isinstance(n, str) and 0 < len(n.strip()) <= 50))


_SITE_PATH = re.compile(r"^/v1/sites/([a-z0-9_-]{1,32})/(login|logout|check)(?:/(l_[A-Za-z0-9_-]{6,40})(/input|/cancel)?)?$")


def make_handler(manager: JobManager, token: str, dialog=None, mailer=None, sites=None, push=None):
    fails: list[float] = []
    fail_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "local-ai-agent/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 본문·헤더 없이 시각·메서드·경로·상태·걸린 시간만
            took = f" {time.monotonic() - self._t0:.1f}s" if getattr(self, "_t0", None) else ""
            sys.stderr.write(f"[api] {time.strftime('%Y-%m-%d %H:%M:%S')} {self.command} {self.path.split('?')[0]} "
                             f"{args[1] if len(args) > 1 else ''}{took}\n")

        def _send(self, status: int, body):
            raw = isinstance(body, tuple)   # (mime, 바이트): 캡처 그림
            data = body[1] if raw else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", body[0] if raw else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            try:
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):   # 앱이 응답을 받기 전에 끊었다(화면을 닫음 등) — 서버 오류가 아니다. 기록에 긴 Traceback을 남기지 않는다
                self.close_connection = True

        def _auth(self):
            now = time.monotonic()
            with fail_lock:
                fails[:] = [t for t in fails if now - t < AUTH_FAIL_WINDOW_S]
                if len(fails) >= AUTH_FAIL_LIMIT:
                    raise ApiError(429, "too_many_auth_failures", "인증 실패가 많다. 잠시 후 다시 시도")
            given = self.headers.get(TOKEN_HEADER) or ""
            if not given or not hmac.compare_digest(given.encode(), token.encode()):
                with fail_lock:
                    fails.append(now)
                raise ApiError(401, "unauthorized", f"{TOKEN_HEADER} 헤더가 없거나 틀리다")

        def _v2(self) -> bool:
            # 3단계 질문 방식(awaiting_input)은 앱이 이 헤더로 켤 때만. 배포된 v1 앱은 모르는 상태를 실패로 처리한다
            v = (self.headers.get(API_VERSION_HEADER) or "").strip()   # 2 이상이면 v2 (나중 버전은 앞 버전을 포함한다)
            return v.isdigit() and int(v) >= 2

        def _ver(self) -> int:
            v = (self.headers.get(API_VERSION_HEADER) or "").strip()
            return int(v) if v.isdigit() else 1

        def _sites(self, path: str, method: str):
            """실제 사이트 로그인(D52). 입력값(아이디·비밀번호·인증 문자)은 브라우저 칸에 넣고 버린다: 응답·오류·기록에 없다."""
            if sites is None:
                raise ApiError(404, "not_found", "사이트 로그인이 켜져 있지 않다")
            from ..sites import LoginError
            if path == "/v1/sites" and method == "GET":
                return 200, {"sites": sites.status()}
            m_i = re.fullmatch(r"/v1/sites/([a-z0-9_-]{1,32})/icon", path)
            if m_i and method == "GET":   # 맥의 에이전트 브라우저가 그 사이트에서 받아 둔 아이콘(D110, 64px PNG). 없으면 404 — 앱은 폰에서 직접 받는다
                icons = getattr(sites, "icons", None)
                png = icons.get(m_i.group(1)) if icons is not None and m_i.group(1) in sites.sites else None
                if png is None:
                    raise ApiError(404, "icon_not_found", "받아 둔 아이콘이 없다")
                return 200, ("image/png", png)
            m = _SITE_PATH.match(path)
            if not m or method != "POST" and not (method == "GET" and m.group(3) and not m.group(4)):
                raise ApiError(404, "not_found", "없는 경로")
            site, what, lid, sub = m.groups()
            try:
                if what in ("logout", "check"):
                    if lid:
                        raise ApiError(404, "not_found", "없는 경로")
                    if what == "logout":   # 로그아웃 = 동의 철회: 그 사이트 화면의 캡처와, 대화가 들고 있던 읽은 결과도 지운다
                        manager.drop_attachments(site)
                        if dialog is not None:
                            dialog.drop_results(site)
                    return 200, {"sites": sites.logout(site) if what == "logout" else sites.check(site)}
                if lid is None:
                    b = self._body()
                    method_ = b.get("method", "qr")
                    if not isinstance(method_, str):
                        raise ApiError(400, "invalid_request", "method는 문자열")
                    return 200, {"login": sites.start(site, method_)}
                if sub is None:
                    return 200, {"login": sites.get(site, lid)}
                if sub == "/cancel":
                    return 200, {"login": sites.cancel(site, lid)}
                b = self._body()
                if not isinstance(b.get("step"), int) or isinstance(b.get("step"), bool) or not isinstance(b.get("values"), dict):
                    raise ApiError(400, "invalid_request", "step(정수)과 values(객체)가 필요")
                return 200, {"login": sites.input(site, lid, b["step"], b["values"])}
            except LoginError as e:
                status, msg = {"unknown_site": (404, "지원하지 않는 사이트"), "unknown_login": (404, "없는 로그인(끝난 지 오래됐거나 서버가 다시 떴다)"),
                               "stale_step": (409, "이미 지나간 단계다. 상태를 다시 조회한다"), "busy": (409, "로그인이 진행 중이다"),
                               "rate_limited": (429, "로그인 시작이 많다. 10분쯤 뒤에 다시 시도"),
                               "invalid_input": (400, "입력 형식 오류")}.get(e.code, (400, "요청 오류"))
                raise ApiError(status, "invalid_request" if e.code == "invalid_input" else e.code, msg) from None

        def _send_on(self) -> bool:
            # 본인 주소 발송(send_pending, 승인 kind "send")은 앱이 3 이상을 보낼 때만. 모르는 상태를 실패로 보는 이전 빌드 보호
            v = (self.headers.get(API_VERSION_HEADER) or "").strip()
            return mailer is not None and v.isdigit() and int(v) >= 3

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "invalid_request", "Content-Length가 잘못됐다") from None
            if n > MAX_BODY:
                raise ApiError(413, "payload_too_large", f"본문은 {MAX_BODY}바이트 이하")
            self._body_read = True
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, UnicodeDecodeError):
                raise ApiError(400, "invalid_json", "본문이 JSON이 아니다") from None
            if not isinstance(data, dict):
                raise ApiError(400, "invalid_request", "본문은 JSON 객체여야 한다")
            return data

        def _raw(self, limit: int) -> bytes:
            """JSON이 아닌 본문(붙이는 사진·PDF)을 그대로 읽는다. 길이를 모르거나 한도를 넘으면 읽지 않는다."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "invalid_request", "Content-Length가 잘못됐다") from None
            if n <= 0:
                raise ApiError(400, "invalid_request", "본문이 비어 있다")
            if n > limit:
                self.close_connection = True   # 큰 본문을 읽지 않고 끊는다
                self._body_read = True
                raise ApiError(413, "payload_too_large", f"파일은 {limit // (1024 * 1024)}MB 이하")
            self._body_read = True
            data = self.rfile.read(n)
            if len(data) != n:
                raise ApiError(400, "invalid_request", "본문이 끊겼다")
            return data

        def _route(self, method: str):
            path = self.path.split("?")[0].rstrip("/") or "/"
            query = parse_qs(urlsplit(self.path).query)
            self._auth()
            if path == "/v1/health" and method == "GET":
                # 앱은 features에 있는 기능만 켠다. 발송(send_self)·푸시(push)는 구현되면 추가한다
                features = ["hash_approval", "task_questions", "task_report", "narration"]
                if dialog is not None:
                    features[0:0] = ["messages", "client_message_id", "tone"]   # 같은 id 재시도는 처음 응답 (D47), 말투 (D48)
                    features.append("reply_to")   # 요청의 reply_to {task_id}: 사용자가 답장으로 가리킨 작업 (D59)
                    features.append("reply_by")   # 응답의 replies[].by: 그 말풍선을 쓴 곳 claude | local | mac (D60)
                    if getattr(dialog, "cloud", None) is not None:
                        features += ["dialog_cloud", "client_time"]   # 대화 판단을 Claude가 한다 / 요청의 client_time을 쓴다 (D59)
                        features.append("reaction")   # 요청에 client_features ["reaction"]이 있으면 응답에 선택 키 reaction(이모지 하나) (D64)
                        features.append("voice_call")   # 에이전트에게 거는 전화: client_features ["voice_call"] → 통화 말투·replies[].speak, 끝난 작업 report.speak (D116)
                        if getattr(dialog, "voice", None) is not None:
                            features.append("voice_tts")   # 통화 목소리(D117): replies[].voice_id, POST /v1/voice, GET /v1/voice/{id}/{n}
                        if getattr(dialog, "shop_drive", False):
                            features.append("shop_drive")   # 살 것 찾기를 작업으로: 작업 상태의 browser(폰이 할 일)와 POST /v1/tasks/{id}/browser (D68)
                        if getattr(dialog, "uploads", None) is not None:
                            features.append("uploads")   # 사진·PDF를 붙여 물어볼 수 있다: POST /v1/uploads → 요청의 uploads [id] (D73)
                        if getattr(dialog, "site_do", False):
                            features.append("site_do")   # 로그인해 둔 사이트 안에서 하는 일: 작업(skill site_do) — 질문 카드(허락)·승인 카드(kind action)를 쓴다 (D72)
                        if getattr(dialog, "shop_api", False):
                            features.append("shop_api")   # 살 것 찾기를 공식 쇼핑 검색 API로: 작업(skill shop_api)의 report.items 상품 카드. 화면을 열지 않는다 (D71)
                        if getattr(dialog, "shop_view", True):
                            features.append("shop_view")   # client_features ["shop"] → action shop / 요청의 view(앱이 화면에서 읽은 상품 칸) (D66)
                        features.append("weather_action")   # 요청에 client_features ["weather"]가 있으면 날씨 물음에 action weather (D62)
                        if getattr(dialog, "send_context", True):
                            features.append("dialog_context")   # 앱이 붙인 회의 자료의 내용이 Claude로 간다(없으면 붙어 있다는 사실만)
                        if getattr(dialog, "web", None) is not None:
                            features.append("web_lookup")   # 공개 정보를 웹 검색으로 찾아 답한다 (D60)
                            if getattr(dialog, "web_browse", False):
                                features.append("web_browse")   # 정해진 기능에 없는 부탁은 맥의 브라우저가 공개 사이트에서 직접 해 본다 (D67)
                            if getattr(dialog, "web_find", False):
                                # "찾아 달라"는 말은 맥의 브라우저가 공개 페이지를 직접 열어 읽는 작업이 된다 / 그 결과의 report.links (D61)
                                features += ["web_find", "report_links"]
                                rn = getattr(manager, "_runner", None)
                                if getattr(rn, "find_reader", "") == "claude" and getattr(rn, "find_claude", None) is not None:
                                    features.append("web_find_reads_cloud")   # 맥의 브라우저가 연 공개 페이지의 글을 Claude가 읽는다(기본은 아니다)
                                if getattr(getattr(getattr(manager, "_runner", None), "finder", None), "name", "") == "naver":
                                    features.append("web_find_naver")   # 후보 페이지를 네이버 검색 API로 찾는다(없으면 Claude의 웹 검색)
                if sites is not None:
                    features.append("site_login")   # 실제 사이트 로그인(D52): GET /v1/sites, POST /v1/sites/{site}/login …
                if mailer is not None:
                    features += ["mail_connect", "send_self", "mail_read"]   # 앱에서 메일 계정 연결, 본인 주소 발송 (D50·D51). 발송은 X-Api-Version: 3으로 켠다
                    if getattr(mailer, "allow_others", False):
                        features.append("send_others")   # 연락처·사용자가 말한 주소로도 보낸다(D77). 승인 카드에 external: true
                features.append("skills")   # 할 수 있는 일 카드: GET /v1/skills (D55)
                if push is not None:
                    features += ["push", "push_text"]   # 푸시 알림(D44) / 알림에 띄울 글을 GET /v1/tasks/{id}/notification으로 준다(D63)
                if sites is not None:
                    features.append("site_read")   # 로그인해 둔 사이트의 페이지 읽기 작업. 풀렸으면 outcome needs_login
                    if dialog is not None:
                        features.append("site_login_resume")   # 로그인이 없어 멈춘 부탁의 안내에는 action.resume: true (D59)
                if dialog is not None and getattr(dialog, "routines", None) is not None:
                    features.append("routines")   # 예약해 둔 일(D80): /v1/routines, 대화 응답의 routines
                if dialog is not None and getattr(dialog, "space", None) is not None:
                    features.append("space")      # 내 공간(D87): /v1/space
                    features.append("space_arrange")   # 손으로 고치기: POST /v1/space/arrange
                    features.append("space_widget")    # 홈 화면 위젯용: GET /v1/space/widget
                features.append("handover")   # 손 넘기기(D106): question.handover → /v1/handover/{id}/frame·input
                features.append("steer")          # 작업 중 끼어들기·함께 돌리기(D132): action steer, steers, wait_reason
                if getattr(dialog, "memory", None) is not None:
                    features.append("memory")     # 오래 가는 기억(D129): /v1/memory
                features.append("activity")   # 활동 탭·전역 검색(D103): GET /v1/activity, GET /v1/search?q=
                if getattr(getattr(manager, "_runner", None), "capture", False):
                    features.append("report_attachments")   # 보고의 화면 캡처: result.report.attachments
                return 200, {"ok": True, "scope": "read_draft", "api_version": API_VERSION, "features": features}
            if path == "/v1/voice" or path.startswith("/v1/voice/"):
                # 통화 목소리(D117): 사용자의 목소리 샘플로 맥이 만든 음성 조각. 글·목소리는 맥 밖으로 나가지 않는다
                from ..voice.engine import VoiceError
                voice = getattr(dialog, "voice", None) if dialog is not None else None
                if voice is None:
                    raise ApiError(404, "not_found", "통화 목소리가 켜져 있지 않다")
                try:
                    if path == "/v1/voice/warm" and method == "POST":   # 통화를 걸 때 부른다: 일꾼을 미리 띄운다(처음 올리기 약 7초)
                        voice.warm()
                        return 200, {"ok": True, "ready": voice.ready}
                    if path == "/v1/voice" and method == "POST":
                        text = self._body().get("text")
                        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                            raise ApiError(400, "invalid_request", "text는 1~2000자 문자열")
                        from .dialog import speakable
                        return 200, {"id": voice.start(speakable(text))}
                    m_v = re.fullmatch(r"/v1/voice/(v[A-Za-z0-9_-]{6,20})(?:/(\d{1,3}))?", path)
                    if m_v and m_v.group(2) is not None and method == "GET":
                        seg = voice.segment(m_v.group(1), int(m_v.group(2)))
                        return (204, ("audio/wav", b"")) if seg is None else (200, ("audio/wav", seg))
                    if m_v and m_v.group(2) is None and method == "DELETE":
                        voice.cancel(m_v.group(1))
                        return 200, {"ok": True}
                except VoiceError as e:
                    if e.code == "not_found":
                        raise ApiError(404, "not_found", "그 음성이 없다(10분 지나 지웠거나 없는 id)") from None
                    if e.code == "not_ready":
                        raise ApiError(409, "not_ready", "아직 만드는 중 — 같은 주소로 다시") from None
                    raise ApiError(503, "voice_unavailable", "음성을 만들지 못했다 — 폰 목소리로 읽기") from None
                raise ApiError(404, "not_found", "없는 경로")
            if path == "/v1/memory" or path.startswith("/v1/memory/"):
                # 사용자에 대한 오래 가는 기억(D129): 요약본 보기·고치기·되돌리기, 옛 대화 찾기, 모두 잊기
                mem = getattr(dialog, "memory", None) if dialog is not None else None
                if mem is None:
                    raise ApiError(404, "not_found", "기억 기능이 켜져 있지 않다")
                sub = path[len("/v1/memory"):]
                if sub == "" and method == "GET":
                    return 200, {"profile": mem.profile(), **mem.stats()}
                if sub == "/profile" and method == "PUT":
                    b = self._body()
                    if not isinstance(b.get("profile"), str) or len(b["profile"]) > 6000:
                        raise ApiError(400, "invalid_request", "profile은 6000자 이하 문자열")
                    return 200, {"profile": mem.set_profile(b["profile"], by="user")}
                if sub == "/undo" and method == "POST":
                    try:
                        return 200, {"profile": mem.undo_profile()}
                    except LookupError:
                        raise ApiError(409, "nothing_to_undo", "되돌릴 전 요약본이 없다") from None
                if sub == "/search" and method == "GET":
                    q = (parse_qs(urlsplit(self.path).query).get("q") or [""])[0]
                    if not q.strip() or len(q) > 100:
                        raise ApiError(400, "invalid_request", "q는 1~100자")
                    return 200, {"hits": mem.search(q, limit=20)}
                if sub == "" and method == "DELETE":
                    mem.forget_all()
                    return 200, {"ok": True}
                raise ApiError(404, "not_found", "없는 경로")
            if path == "/v1/space" or path.startswith("/v1/space/"):
                # 내 공간(D87): 말로 짜는 카드 화면. 구성 보기·말로 고치기·되돌리기·비우기·맥이 채우는 카드 새로 가져오기
                space = getattr(dialog, "space", None) if dialog is not None else None
                if space is None:
                    raise ApiError(404, "not_found", "내 공간 기능이 켜져 있지 않다")
                from .space import SpaceError
                sub = path[len("/v1/space"):]
                try:
                    if sub == "" and method == "GET":
                        return 200, {"space": space.public()}
                    if sub == "/widget" and method == "GET":   # 홈 화면 위젯용 짧은 모습(모델을 부르지 않는다 — 바로 답한다)
                        return 200, {"widget": space.widget()}
                    if sub == "/edit" and method == "POST":
                        b = self._body()
                        if not isinstance(b.get("text"), str) or not b["text"].strip() or len(b["text"]) > 500:
                            raise ApiError(400, "invalid_request", "text는 1~500자 문자열")
                        ac = b.get("app_cards")   # 선택: 앱이 채울 수 있는 폰 카드 종류(꼴이 다르면 없는 것으로 본다)
                        ac = [x for x in ac if isinstance(x, str)][:20] if isinstance(ac, list) else None
                        return 200, space.edit(b["text"], ac)
                    if sub == "/arrange" and method == "POST":
                        b = self._body()
                        if not isinstance(b.get("cards"), list):
                            raise ApiError(400, "invalid_request", "cards는 [{id, size}] 배열")
                        return 200, space.arrange(b["cards"])
                    if sub == "/undo" and method == "POST":
                        return 200, space.undo()
                    if sub == "" and method == "DELETE":
                        return 200, space.reset()
                    if sub == "/refresh" and method == "POST":
                        b = self._body()
                        cid = b.get("card")
                        if cid is not None and not (isinstance(cid, str) and re.fullmatch(r"c_[A-Za-z0-9_-]{4,20}", cid)):
                            raise ApiError(400, "invalid_request", "card는 카드 id")
                        return 200, space.refresh(cid, force=b.get("force") is not False)
                except SpaceError as e:
                    if e.code == "not_found":
                        raise ApiError(404, "not_found", "그런 카드가 없거나 되돌릴 것이 없다") from None
                    if e.code == "model_error":
                        raise ApiError(502, "model_error", "모델 호출 실패. 잠시 후 다시 시도") from None
                    if e.code == "off":
                        raise ApiError(404, "not_found", "내 공간 기능이 켜져 있지 않다") from None
                    raise ApiError(400, "invalid_request", "입력 형식 오류") from None
                raise ApiError(405, "method_not_allowed", "이 경로에서 지원하지 않는 메서드")
            if path == "/v1/routines" or path.startswith("/v1/routines/"):
                # 예약해 둔 일(D80): 목록·만들기·바꾸기·지우기·실행 기록. 만드는 것은 사용자(앱의 화면이나 대화)뿐이다
                store = getattr(dialog, "routines", None) if dialog is not None else None
                if store is None:
                    raise ApiError(404, "not_found", "예약 기능이 켜져 있지 않다")
                from .routines import RoutineError, parse_when
                listing = lambda: {"routines": [r.public() for r in store.list()]}
                rid = path[len("/v1/routines/"):] if path.startswith("/v1/routines/") else ""
                try:
                    if not rid and method == "GET":
                        return 200, listing()
                    if rid == "runs" and method == "GET":
                        sched = getattr(dialog, "scheduler", None)
                        q = parse_qs(urlsplit(self.path).query)
                        of = (q.get("routine_id") or [None])[0]
                        try:
                            lim = max(1, min(100, int((q.get("limit") or ["20"])[0])))
                        except ValueError:
                            lim = 20
                        return 200, {"runs": sched.recent(lim, of) if sched is not None else []}
                    if re.fullmatch(r"runs/rr_[A-Za-z0-9_-]{6,20}", rid) and method == "GET":
                        sched = getattr(dialog, "scheduler", None)
                        rec = sched.find_run(rid[5:]) if sched is not None else None
                        if rec is None:
                            raise ApiError(404, "not_found", "그런 실행 기록이 없다")
                        return 200, {"run": rec}
                    if not rid and method == "POST":
                        b = self._body()
                        if not isinstance(b.get("prompt"), str) or not isinstance(b.get("when"), str):
                            raise ApiError(400, "invalid_request", "prompt(할 말)와 when(때: \"daily 07:30\" 꼴)이 필요하다")
                        r = store.add(b.get("title") if isinstance(b.get("title"), str) else "", b["prompt"], parse_when(b["when"]),
                                      notes=b.get("notes") if isinstance(b.get("notes"), str) else "")
                        return 201, {"routine": r.public(), **listing()}
                    if re.fullmatch(r"rt_[A-Za-z0-9_-]{6,20}", rid) and method == "POST":
                        b = self._body()
                        if any(k in b and not isinstance(b[k], t) for k, t in (("enabled", bool), ("title", str), ("prompt", str), ("when", str), ("notes", str))):
                            raise ApiError(400, "invalid_request", "enabled(참·거짓), title·prompt·when·notes(문자열)")
                        r = store.update(rid, enabled=b.get("enabled"), title=b.get("title"), prompt=b.get("prompt"), notes=b.get("notes"),
                                         when=parse_when(b["when"]) if "when" in b else None)
                        return 200, {"routine": r.public(), **listing()}
                    if re.fullmatch(r"rt_[A-Za-z0-9_-]{6,20}/run", rid) and method == "POST":
                        sched = getattr(dialog, "scheduler", None)
                        if sched is None:
                            raise ApiError(404, "not_found", "예약 기능이 켜져 있지 않다")
                        sched.run_now(rid[:-4])   # 지금 한 번 돌린다(뒤에서). 결과는 실행 기록·알림·이은 카드로 온다
                        return 202, {"started": True}
                    if re.fullmatch(r"rt_[A-Za-z0-9_-]{6,20}", rid) and method == "DELETE":
                        store.delete(rid)
                        if getattr(dialog, "scheduler", None) is not None:
                            dialog.scheduler.forget(rid)   # 그 예약의 실행 기록도 지운다
                        return 200, listing()
                except RoutineError as e:
                    if e.code == "not_found":
                        raise ApiError(404, "not_found", "그런 예약이 없다") from None
                    if e.code == "too_many":
                        raise ApiError(409, "too_many", "예약은 20개까지") from None
                    raise ApiError(400, "invalid_request", {"past": "이미 지난 때다", "invalid_when": "when의 꼴: daily HH:MM[,HH:MM] | weekly mon,wed HH:MM | once YYYY-MM-DD HH:MM",
                                                           }.get(e.code, "prompt가 비었다")) from None
                raise ApiError(405, "method_not_allowed", "이 경로에서 지원하지 않는 메서드")
            m_h = re.fullmatch(r"/v1/handover/(h_[A-Za-z0-9_-]{8,24})/(frame|input)", path)
            if m_h:
                # 손 넘기기(D106): 사람이 폰에서 에이전트의 브라우저 화면을 보고 직접 누르고 쓴다. 열린 동안만(질문 카드가 떠 있는 동안)
                from ..sites.handover import STORE
                hid, what = m_h.group(1), m_h.group(2)
                try:
                    if what == "frame" and method == "GET":
                        f = STORE.frame(hid)
                        if f is None:
                            raise ApiError(404, "not_found", "닫혔거나 없는 손 넘기기")
                        return 200, {"jpeg_b64": f["jpeg_b64"], "width": f["width"], "height": f["height"]}
                    if what == "input" and method == "POST":
                        b = self._body()
                        args = {k: b[k] for k in ("kind", "x", "y", "x2", "y2", "dx", "dy", "text", "key") if k in b}
                        if not STORE.input(hid, args):
                            raise ApiError(404, "not_found", "닫혔거나 없는 손 넘기기")
                        return 200, {"ok": True}
                except ApiError:
                    raise
                except Exception as e:
                    msg = str(e)
                    raise ApiError(400 if "잘못된 인자" in msg else 502, "invalid_request" if "잘못된 인자" in msg else "browser_error",
                                   msg.split("잘못된 인자:")[-1].strip()[:120] if "잘못된 인자" in msg else "브라우저에 닿지 못했다") from None
                raise ApiError(405, "method_not_allowed", "이 경로에서 지원하지 않는 메서드")
            if path in ("/v1/activity", "/v1/search") and method == "GET":
                # 활동 탭(D103): 기다리는 작업 + 끝난 작업 기록 + 예약 실행 기록. 검색: 맥에 있는 것만(회의는 폰이 찾는다)
                from .activity import ActivityLog, activity, search
                log = getattr(manager, "activity", None) or ActivityLog()
                sched = getattr(dialog, "scheduler", None) if dialog is not None else None
                store = getattr(dialog, "routines", None) if dialog is not None else None
                runs = sched.recent(200) if sched is not None else []
                try:
                    lim = max(1, min(100, int((query.get("limit") or ["50"])[0])))
                except ValueError:
                    lim = 50
                if path == "/v1/activity":
                    return 200, activity(log, manager.list(limit=50), runs, lim)
                q = (query.get("q") or [""])[0].strip()
                if not q or len(q) > 100:
                    raise ApiError(400, "invalid_request", "q는 1~100자")
                return 200, {"q": q, "items": search(q, log, runs, [r.public() for r in store.list()] if store is not None else [], min(lim, 30))}
            if path == "/v1/skills" and method == "GET":
                from .. import skills as sk
                ready = {"mail_account": mailer is not None and mailer.reading_account() is not None,
                         "site:naver": sites is not None and any(x["site"] == "naver" and x["status"] == "logged_in"
                                                                 for x in sites.status())}
                why = {"site:naver": next((x.get("reason") for x in sites.status() if x["site"] == "naver"), None)} if sites else {}
                have = sk.load()
                if getattr(dialog, "web", None) is None:
                    have.pop("web_lookup", None)   # 공개 정보 웹 검색은 켰을 때만 카드로 내보낸다 (D60)
                if not getattr(dialog, "web_find", False):
                    have.pop("web_find", None)     # 공개 정보 직접 찾기도 (D61)
                if not getattr(dialog, "web_browse", False):
                    have.pop("web_browse", None)   # 일반 길도 (D67)
                return 200, {"skills": sk.cards(have, ready, why)}
            if path == "/v1/dev/handover" and method == "POST" and os.environ.get("DEV_HANDOVER") == "1":
                # 손 넘기기 시험(D106, 개발용): 합성 '사람 확인' 화면을 폰에 넘기는 작업을 만든다 — 끄면(기본) 이 길은 없다
                job = manager.submit("손 넘기기 시험", None, title="손 넘기기 시험", questions=True, skill="dev_handover")
                return 202, job.public()
            if path == "/v1/tasks" and method == "POST":
                b = self._body()
                task, ctx = b.get("task"), b.get("context")
                if not isinstance(task, str) or not task.strip() or len(task) > MAX_TASK_CHARS:
                    raise ApiError(400, "invalid_request", f"task는 1~{MAX_TASK_CHARS}자 문자열")
                if ctx is not None and (not isinstance(ctx, str) or len(ctx) > MAX_CONTEXT_CHARS):
                    raise ApiError(400, "invalid_request", f"context는 {MAX_CONTEXT_CHARS}자 이하 문자열")
                if not isinstance(b.get("plan", False), bool):
                    raise ApiError(400, "invalid_request", "plan은 true/false")
                try:
                    job = manager.submit(task.strip(), ctx or None, bool(b.get("plan", False)), questions=self._v2(),
                                         send=self._send_on())
                except Busy:
                    raise ApiError(429, "busy", "대기 중인 작업이 많다. 잠시 후 다시 시도") from None
                return 202, job.public()
            if path == "/v1/messages" and method == "POST":
                if dialog is None:
                    raise ApiError(404, "not_found", "대화 담당이 켜져 있지 않다")
                b = self._body()
                text, cid, ctx = b.get("text"), b.get("conversation_id"), b.get("context")
                # 붙인 사진·PDF의 id(D73). 붙인 것이 있으면 글은 비어 있어도 된다. 꼴이 다른 원소는 버린다
                ups = b.get("uploads")
                ups = [x for x in ups[:8] if isinstance(x, str) and re.fullmatch(r"u_[A-Za-z0-9_-]{8,40}", x)] if isinstance(ups, list) else []
                if ups and getattr(dialog, "uploads", None) is None:
                    ups = []
                if ups and text is None:
                    text = ""
                if not isinstance(text, str) or (not text.strip() and not ups) or len(text) > MAX_TEXT_CHARS:
                    raise ApiError(400, "invalid_request", f"text는 1~{MAX_TEXT_CHARS}자 문자열")
                if cid is not None and (not isinstance(cid, str) or not _CONV_ID.match(cid)):
                    raise ApiError(400, "invalid_request", "conversation_id 형식 오류")
                if ctx is not None and (not isinstance(ctx, str) or len(ctx) > MAX_CONTEXT_CHARS):
                    raise ApiError(400, "invalid_request", f"context는 {MAX_CONTEXT_CHARS}자 이하 문자열")
                contacts = _contacts(b.get("contacts"))
                no_mail = _no_mail(b.get("contacts_no_email"))
                tone = b.get("tone")
                if tone is not None and tone not in TONES:
                    raise ApiError(400, "invalid_request", "tone은 friendly 또는 formal")
                mid = b.get("client_message_id")
                if mid is not None and (not isinstance(mid, str) or not _CLIENT_MSG_ID.match(mid)):
                    raise ApiError(400, "invalid_request", "client_message_id 형식 오류(영문·숫자·-·_ 8~64자)")
                # 선택 키(D59): 꼴이 다르면 오류를 내지 않고 없는 것으로 본다
                rt = b.get("reply_to")
                # 답장으로 가리킨 말풍선(D108): 그 글과 누가 한 말인지 — 작업이 아닌 말에 답장해도 무엇을 가리키는지 알게
                rq = rt.get("text") if isinstance(rt, dict) and isinstance(rt.get("text"), str) else None
                rq = " ".join(rq.split())[:500] if rq and rq.strip() else None
                rr = rt.get("role") if isinstance(rt, dict) and rt.get("role") in ("user", "assistant") else None
                ri = rt.get("item_id") if isinstance(rt, dict) else None   # 답장으로 가리킨 상품 카드(D68)
                ri = ri if isinstance(ri, str) and re.fullmatch(r"i[0-9]{1,2}", ri) else None
                rt = rt.get("task_id") if isinstance(rt, dict) else None
                rt = rt if isinstance(rt, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", rt) else None
                ct = b.get("client_time")
                ct = ct if isinstance(ct, str) and len(ct) <= 40 else None
                # 앱이 할 수 있는 일(client_features)과 지난 응답 뒤에 한 일(events). 꼴이 다른 원소는 버린다
                cf = b.get("client_features")
                cf = frozenset(x for x in cf[:20] if isinstance(x, str) and len(x) <= 40) if isinstance(cf, list) else frozenset()
                ev = b.get("events")
                ev = [{"type": e["type"], "text": " ".join(e["text"].split())[:300]} for e in (ev if isinstance(ev, list) else [])
                      if isinstance(e, dict) and isinstance(e.get("type"), str) and isinstance(e.get("text"), str) and e["text"].strip()][:3]
                vw = b.get("view") if isinstance(b.get("view"), dict) and len(json.dumps(b.get("view"))) <= 20000 else None
                try:
                    return 200, dialog.handle(text.strip(), cid, ctx or None, contacts, questions=self._v2(),
                                              client_message_id=mid, tone=tone, send=self._send_on(),
                                              site_login=sites is not None and self._ver() >= 4, reply_to=rt, client_time=ct, reply_quote=(rr, rq) if rq else None,
                                              abilities=cf, events=ev, view=vw, reply_item=ri, uploads=ups, no_mail=no_mail)
                except Busy:
                    raise ApiError(429, "busy", "대기 중인 작업이 많다. 잠시 후 다시 시도") from None
                except ProviderError:
                    raise ApiError(502, "model_error", "모델 호출 실패. 잠시 후 다시 시도") from None
            if path == "/v1/uploads" and method == "POST":
                # 대화에 붙일 사진·PDF를 올린다(D73). 본문은 파일 그대로. 맥 메모리에만 두고 30분 뒤 지운다. 종류는 파일 머리로 정한다
                store = getattr(dialog, "uploads", None) if dialog is not None else None
                if store is None:
                    raise ApiError(404, "not_found", "붙이기 기능이 켜져 있지 않다")
                from urllib.parse import unquote
                from .uploads import UPLOAD_MAX, UploadError
                data = self._raw(UPLOAD_MAX)
                name = unquote(self.headers.get("X-Filename") or "")[:200]
                try:
                    meta = store.put(data, name)
                except UploadError as e:
                    status, msg = {"unsupported_media_type": (415, "사진(JPEG·PNG·GIF·WebP)과 PDF만 붙일 수 있다"),
                                   "payload_too_large": (413, "사진은 7MB, PDF는 10MB 이하"),
                                   "empty": (400, "본문이 비어 있다")}[e.code]
                    raise ApiError(status, e.code if e.code != "empty" else "invalid_request", msg) from None
                sys.stderr.write(f"[upload] {time.strftime('%Y-%m-%d %H:%M:%S')} kind={meta['kind']} bytes={meta['bytes']} held={len(store)}\n")
                return 201, {"upload": meta}
            if path == "/v1/push/devices" or path.startswith("/v1/push/devices/"):
                return self._push(path, method)
            if path == "/v1/tasks" and method == "GET":
                cid = (query.get("conversation_id") or [None])[0]
                if cid is not None and not _CONV_ID.match(cid):
                    raise ApiError(400, "invalid_request", "conversation_id 형식 오류")
                st = (query.get("status") or [None])[0]   # 기다리는 작업 찾기(앱이 열 때 — 맥이 직접 만든 대화의 승인 카드, 2026-11-17)
                tasks = manager.list(conversation_id=cid)
                if st is not None:
                    tasks = [t for t in tasks if t.get("status") in set(st.split(","))]
                return 200, {"tasks": tasks}
            if path.startswith("/v1/mail/"):
                return self._mail(path, method)
            if path == "/v1/sites" or path.startswith("/v1/sites/"):
                return self._sites(path, method)
            a = _ATTACH_PATH.match(path)
            if a and method == "GET":
                try:
                    return 200, manager.attachment(a.group(1), a.group(2))
                except NotFound:
                    raise ApiError(404, "task_not_found", "없는 작업 ID (서버를 재시작하면 작업 기록이 사라진다)") from None
                except Gone:
                    raise ApiError(404, "attachment_gone", "캡처가 지워졌거나 보관 기간이 지났다") from None
            m = _TASK_PATH.match(path)
            if not m:
                raise ApiError(404, "not_found", "없는 경로")
            job_id, sub = m.group(1), m.group(2)
            try:
                if sub is None and method == "GET":
                    return 200, manager.get(job_id).public()
                if sub == "/notification" and method == "GET":
                    # 알림에 띄울 실제 글: 앱의 알림 확장이 푸시의 type·task_id로 받아 간다(D63). Apple을 거치지 않는다
                    from .push import TEXT, notice
                    kind = (query.get("type") or [""])[0]
                    if kind not in TEXT:
                        raise ApiError(400, "invalid_request", "type이 없거나 모르는 값")
                    out = notice(manager.get(job_id), kind)
                    if out is None:
                        raise ApiError(404, "notification_gone", "그 알림에 띄울 글이 지금은 없다")
                    return 200, out
                if sub == "/browser" and method == "POST":
                    # 폰이 쇼핑 화면에서 한 일의 결과(D68). 지금 맡긴 일이 아니면 409(앱은 조용히 넘긴다). 끝난 작업에 늦게 온 결과도 409
                    b = self._body()
                    cmd = b.get("id")
                    if not isinstance(cmd, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cmd):
                        raise ApiError(400, "invalid_request", "id가 없거나 꼴이 다르다")
                    return 200, manager.browser_result(job_id, cmd, b).public()
                if sub == "/cancel" and method == "POST":
                    return 202, manager.cancel(job_id).public()
                if sub == "/answer" and method == "POST":
                    b = self._body()
                    ans = b.get("answer")
                    if not isinstance(b.get("question_id"), str) or not isinstance(ans, str) or not ans.strip() \
                            or len(ans) > MAX_ANSWER_CHARS:
                        raise ApiError(400, "invalid_request", f"question_id(문자열)와 answer(1~{MAX_ANSWER_CHARS}자)가 필요")
                    return 200, manager.answer(job_id, b["question_id"], ans).public()
                if sub == "/approval" and method == "POST":
                    b = self._body()
                    decision, reason = b.get("decision"), b.get("reason")
                    if decision not in ("approve", "reject") or not isinstance(b.get("approval_id"), str):
                        raise ApiError(400, "invalid_request", 'approval_id(문자열)와 decision("approve"|"reject")이 필요')
                    if reason is not None and (not isinstance(reason, str) or len(reason) > MAX_REASON_CHARS):
                        raise ApiError(400, "invalid_request", f"reason은 {MAX_REASON_CHARS}자 이하 문자열")
                    sha = b.get("sha256")
                    if sha is not None and (not isinstance(sha, str) or not _SHA256.match(sha)):
                        raise ApiError(400, "invalid_request", "sha256은 소문자 16진수 64자")
                    return 200, manager.decide(job_id, b["approval_id"], decision, reason, sha).public()
            except NotFound:
                raise ApiError(404, "task_not_found", "없는 작업 ID (서버를 재시작하면 작업 기록이 사라진다)") from None
            except HashMismatch as e:
                raise ApiError(409, "hash_mismatch", str(e)) from None
            except Conflict as e:
                raise ApiError(409, "conflict", str(e)) from None
            raise ApiError(405, "method_not_allowed", "이 경로에서 지원하지 않는 메서드")

        def _push(self, path: str, method: str):
            """푸시 기기 등록·해제 (D44). 푸시가 설정돼 있지 않으면 없는 경로다."""
            from .push import TOKEN
            if push is None:
                raise ApiError(404, "not_found", "없는 경로")
            if path == "/v1/push/devices" and method == "POST":
                b = self._body()
                tok, env, bid = b.get("token"), b.get("environment"), b.get("bundle_id")
                def reject(why: str, message: str):   # 왜 거절했는지 종류만 기록한다(값은 적지 않는다) — 앱은 등록 실패를 화면에 보이지 않는다
                    sys.stderr.write(f"[push] {time.strftime('%Y-%m-%d %H:%M:%S')} register rejected reason={why}\n")
                    raise ApiError(400, "invalid_request", message)
                if not isinstance(tok, str) or not TOKEN.match(tok):
                    reject("token_format", "token은 16진수 64~200자")
                if env not in ("production", "sandbox"):
                    reject("environment_value", "environment는 production 또는 sandbox")
                if env == "sandbox":   # 그 Apple 호스트 접속은 승인되지 않았다 — 받아 두고 안 보내지 않는다
                    reject("sandbox", "sandbox 환경은 지원하지 않는다")
                if bid is not None and bid != push.client.topic:
                    reject("bundle_id", "bundle_id가 서버 설정과 다르다")
                push.store.register(tok, env)
                sys.stderr.write(f"[push] {time.strftime('%Y-%m-%d %H:%M:%S')} registered environment={env} device={tok[:6].lower()}\n")
                return 200, {"registered": True}
            if path.startswith("/v1/push/devices/") and method == "DELETE":
                tok = path.rsplit("/", 1)[1]
                if not TOKEN.match(tok):
                    raise ApiError(400, "invalid_request", "token은 16진수 64~200자")
                push.store.unregister(tok)
                return 200, {"registered": False}
            raise ApiError(404, "not_found", "없는 경로")

        def _mail(self, path: str, method: str):
            """메일 계정 상태·연결. 비밀번호는 받기만 한다(쓰기 전용): 응답·오류·기록 어디에도 넣지 않는다."""
            if mailer is None:
                raise ApiError(404, "not_found", "메일 연동이 켜져 있지 않다")
            from ..mail import MailError
            if path == "/v1/mail/accounts" and method == "GET":
                return 200, {"accounts": mailer.accounts()}
            if path in ("/v1/mail/today", "/v1/mail/briefing"):
                # 오늘 받은 메일(D54). today: 코드가 가져온 목록만. briefing: 목록 + 로컬 모델의 요약. 읽음 표시는 바꾸지 않는다
                try:
                    if path.endswith("/today") and method == "GET":
                        from ..mail import briefing
                        _, rows = mailer.inbox_today()
                        return 200, {"count": len(rows), "items": [
                            {"time": briefing._hm(r["received_at"]), "from": briefing.sender_name(r["from"]),
                             "subject": r["subject"], "seen": r["seen"]} for r in rows]}
                    if path.endswith("/briefing") and method == "POST" and dialog is not None:
                        from ..mail import briefing
                        b = briefing.build(mailer, dialog.provider)
                        return 200, {k: b[k] for k in ("count", "items", "summary", "services", "failed")}
                except MailError as e:
                    raise ApiError(409, e.reason, "메일을 읽지 못했다") from None
                except ProviderError:
                    raise ApiError(502, "model_error", "모델 호출 실패. 잠시 후 다시 시도") from None
                raise ApiError(405, "method_not_allowed", "이 경로에서 지원하지 않는 메서드")
            if path == "/v1/mail/accounts/check" and method == "POST":
                return 200, {"accounts": mailer.check()}
            if path in ("/v1/mail/accounts/connect", "/v1/mail/accounts/disconnect") and method == "POST":
                b = self._body()
                addr = b.get("address")
                if not isinstance(addr, str) or len(addr) > 254 or not _EMAIL_FULL.match(addr.strip()):
                    raise ApiError(400, "invalid_request", "address 형식 오류")
                try:
                    if path.endswith("/disconnect"):
                        return 200, {"accounts": mailer.disconnect(addr)}
                    pw, prov = b.get("app_password"), b.get("provider")
                    if not isinstance(pw, str) or not (1 <= len(pw) <= 256):
                        raise ApiError(400, "invalid_request", "app_password가 필요하다")
                    if prov is not None and prov != mailer.provider_of(addr.strip().lower()):
                        if mailer.provider_of(addr.strip().lower()) not in mailer.backends:
                            raise ApiError(403, "not_allowed", "지원하지 않는 메일 서비스다")
                        raise ApiError(400, "provider_mismatch", "고른 메일 서비스와 주소가 맞지 않는다")
                    if not isinstance(b.get("default", False), bool):
                        raise ApiError(400, "invalid_request", "default는 true/false")
                    return 200, {"accounts": mailer.connect(addr, "".join(pw.split()), bool(b.get("default", False)))}
                except MailError as e:
                    if e.reason == "not_allowed":
                        raise ApiError(403, "not_allowed", "지원하지 않는 메일 서비스이거나 연결되지 않은 주소다") from None
                    if e.reason == "too_many_accounts":
                        raise ApiError(403, "too_many_accounts", "연결할 수 있는 계정 수를 넘었다. 쓰지 않는 계정을 해제") from None
                    if e.reason == "rate_limited":
                        raise ApiError(429, "rate_limited", "시도가 많다. 10분쯤 뒤에 다시 시도") from None
                    raise ApiError(502, "mail_error", "메일 계정 확인 실패") from None
            raise ApiError(405, "method_not_allowed", "이 경로에서 지원하지 않는 메서드")

        def _drain(self) -> None:
            """읽지 않은 요청 본문을 버린다. 연결을 이어 쓰는(HTTP/1.1) 앱이 본문을 보냈는데 그 경로가 본문을 읽지 않으면,
            남은 글자가 다음 요청의 머리에 붙어 그 요청이 깨진다(실기기: 취소 뒤의 로그인 시작이 "{}POST …" 501로 튕겼다)."""
            if getattr(self, "_body_read", False):
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n == 0:
                return
            if n < 0 or n > MAX_BODY:
                self.close_connection = True   # 얼마나 남았는지 모르거나 너무 크다: 이 연결은 더 쓰지 않는다
                return
            try:
                self.rfile.read(n)
            except OSError:
                self.close_connection = True

        def _handle(self, method: str):
            self._t0 = time.monotonic()
            self._body_read = False
            try:
                status, body = self._route(method)
            except ApiError as e:
                status, body = e.status, {"error": {"code": e.code, "message": e.message}}
            except Exception as e:
                status, body = 500, {"error": {"code": "internal_error", "message": type(e).__name__}}
            self._drain()
            self._send(status, body)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

    return Handler


def make_server(manager: JobManager, token: str, port: int = 8765, dialog=None, mailer=None, sites=None, push=None) -> ThreadingHTTPServer:
    if len(token) < 24:
        raise ValueError("앱 토큰이 너무 짧다 (24자 이상)")
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(manager, token, dialog, mailer, sites, push))  # 루프백 전용, 변경 불가


def ensure_token(env_path) -> str:
    """AGENT_APP_TOKEN을 환경 변수나 .env에서 읽고, 없으면 만들어 .env에 추가한다. 값은 출력하지 않는다."""
    import secrets
    from ..providers.claude import load_env_file
    load_env_file(env_path)
    tok = os.environ.get("AGENT_APP_TOKEN", "")
    if len(tok) >= 24:
        return tok
    tok = secrets.token_urlsafe(32)
    fd = os.open(env_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(f"\nAGENT_APP_TOKEN={tok}\n")
    os.chmod(env_path, 0o600)
    os.environ["AGENT_APP_TOKEN"] = tok
    return tok
