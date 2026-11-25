"""모델이 낸 도구 호출 검증: 도구 존재 → 인자 스키마 → ref 존재 → URL 정책 → 승인 필요 여부.

모델 출력은 신뢰할 수 없는 입력이다. 여기서 ok/needs_approval이 아닌 호출은 실행하지 않는다.
"""

from __future__ import annotations

import re

import os
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ValidationError

from ..providers.base import ToolCall
from .observation import Observation
from .spec import ToolRegistry

CheckStatus = Literal[
    "ok",              # 실행 가능
    "needs_approval",  # 유효하지만 사용자 승인 후에만 실행
    "unknown_tool",    # 등록되지 않은 도구
    "invalid_args",    # 인자 스키마 위반
    "unknown_ref",     # 현재 관찰 결과에 없는 요소 ref
    "policy_denied",   # 허용되지 않은 URL 등 정책 위반
]

# 이 단어가 들어간 요소를 클릭하면 승인 필요 (보수적 휴리스틱, Phase 4에서 보강)
DEFAULT_SENSITIVE_KEYWORDS = (
    "결제", "구매", "주문", "보내기", "발송", "전송", "삭제", "제출", "신청", "예약",
)


# 테스트 하네스 전용 경로 — 에이전트가 브라우저로 상태를 엿보거나 초기화하지 못하게 막는다.
DEFAULT_DENIED_PATH_PREFIXES = ("/_test",)


@dataclass(frozen=True)
class Policy:
    allowed_hosts: frozenset[str]
    sensitive_keywords: tuple[str, ...] = DEFAULT_SENSITIVE_KEYWORDS
    denied_path_prefixes: tuple[str, ...] = DEFAULT_DENIED_PATH_PREFIXES
    # 설정하면 navigate는 이 URL들로만 가능 (나머지는 페이지 링크를 click). URL 짐작 방지.
    navigate_urls: frozenset[str] | None = None
    # "read_draft"(Phase 8 축소판, D40): 읽기와 초안 저장만. 민감 동작은 승인 대상이 아니라 아예 금지하고,
    # 버튼은 허용 목록에 있는 것만 누를 수 있다. 초안 저장 버튼은 승인 후에만.
    mode: Literal["full", "read_draft"] = "full"
    # 이 낱말이 든 요소는 승인을 받아도 누르지 않는다(로그인한 사이트 안에서 하기 D72: 탈퇴·비밀번호 변경 같은 것). 기본은 없음
    denied_keywords: tuple[str, ...] = ()
    # 단추일 때만 승인을 묻는 낱말: 메뉴·링크의 이름으로도 흔한 말("구독" 메뉴, "차단 목록" 링크)이라 링크에서는 보기를 바꾸는 것으로 본다
    # (실기기 2026-11-16: 유튜브의 "구독" 메뉴로 가려는데 승인을 물었다 — 구독하기 단추가 아니라 구독 목록으로 가는 링크였다)
    button_keywords: tuple[str, ...] = ()
    # 검색창이 아닌 칸에서 Enter를 누르는 것(press_key): True면 사용자 승인을 묻고, False면 하지 않는다.
    # Enter는 양식을 내거나 무엇을 더하는 일일 수 있다(빠른 추가·태그) — 승인을 물을 수 있는 길(로그인한 사이트 안에서 하기)에서만 켠다
    enter_approval: bool = False
    # 허용 호스트 안에 있어도 가지 않는 곳(예: 네이버 안에서 쇼핑·검색·메일 — 사이트 안에서 하기의 범위 밖)
    denied_hosts: tuple[str, ...] = ()
    # 가지 않는 호스트 안에서도 열어도 되는 주소의 앞부분(예: 네이버 쇼핑의 '내 쇼핑' 화면 — 검색·상품 화면은 그대로 막는다)
    denied_except: tuple[str, ...] = ()
    # 이 화면에서 칸에 무엇을 넣은 뒤에 누르는 "수정"·"확인"·"완료" 같은 단추와 이름 없는 단추는 사용자 승인을 묻는다(로그인한 사이트 안에서 하기).
    # 실기기 2026-11-16: 리뷰 수정 창의 저장 단추가 정해 둔 낱말에 없어 승인 없이 눌렸다
    input_approval: bool = False
    # (D120) 화면을 여는 메뉴·탭·링크와 문제 보기(라디오·체크)는 이름에 '제출'·'신청'·'제거' 같은 말이 들어 있어도 묻지 않는다 —
    # 이름이 동작 그 자체("제출", "제출하기")일 때와, 링크라도 돈·삭제·발송처럼 되돌리기 어려운 말(STRICT_WORDS)이 든 때만 묻는다.
    # 실기기 2026-11-18: '과제제출'·'평가제출'·'수업신청 내역' 메뉴와 퀴즈 보기 "제거소화…"에서 승인 카드가 떴다
    nav_relax: bool = False
    # (D120) 사용자가 하지 말라고 한 동작: 이 낱말이 든 바꾸는 동작은 승인을 묻지 않고 아예 하지 않는다. read_only면 바꾸는 동작은 모두.
    # 실기기 2026-11-18: "제출은 하지 말고"라고 했는데 '제출하기'를 승인 카드로 물었고 승인됐다
    forbid_words: tuple[str, ...] = ()
    read_only: bool = False
    draft_keywords: tuple[str, ...] = ("임시 저장", "임시저장", "초안 저장")
    safe_buttons: tuple[str, ...] = ("검색",)

    @classmethod
    def from_env(cls) -> Policy:
        raw = os.environ.get("ALLOWED_HOSTS", "")
        return cls(allowed_hosts=frozenset(h.strip().lower() for h in raw.split(",") if h.strip()))

    def url_allowed(self, url: str) -> bool:
        """그 주소로 가도 되나: 허용 호스트이고, 가지 않는 호스트가 아니거나 그 안의 열어도 되는 주소다."""
        try:
            p = urlsplit(url)
        except ValueError:
            return False
        host = (p.hostname or "").lower().rstrip(".")
        if not host:
            return False
        if self.host_allowed(host):
            return True
        if not any(host == h or host.endswith("." + h) for h in self.allowed_hosts):
            return False
        return p.scheme == "https" and any(url.startswith(x) for x in self.denied_except)

    def host_allowed(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        if any(host == h or host.endswith("." + h) for h in self.denied_hosts):
            return False
        return any(host == h or host.endswith("." + h) for h in self.allowed_hosts)


def is_search_box(element) -> bool:
    """검색창인가: 역할이 searchbox이거나, 글 칸의 이름에 '검색'·search가 든다(실기기 2026-11-16: 한 쇼핑몰의 상품 검색 칸이
    textbox로 읽혀 Enter에 승인 카드가 떴다 — 검색은 아무것도 바꾸지 않는다)."""
    if element.role == "searchbox":
        return True
    return element.role in ("textbox", "combobox") and bool(re.search(r"검색|search", element.label or "", re.IGNORECASE))


# 누를 수는 있어도 보통 '내용'인 역할. 이름이 동작 이름 그 자체일 때만 단추처럼 다룬다
CONTENT_ROLES = frozenset({"cell", "gridcell", "row", "columnheader", "rowheader", "heading", "paragraph", "listitem", "article", "img", "text"})
NAV_ROLES = frozenset({"link", "tab", "menuitem", "treeitem"})
CHOICE_ROLES = frozenset({"radio", "checkbox", "option", "menuitemradio", "menuitemcheckbox"})
# 링크·메뉴라도 이 말이 들면 묻는다(돈·삭제·발송·계정 — 되돌리기 어렵다)
STRICT_WORDS = ("결제", "구매", "주문", "삭제", "제거", "지우기", "탈퇴", "해지", "송금", "발송", "전송", "보내기", "로그아웃",
                "Pay", "Buy", "Purchase", "Delete", "delete", "Remove", "remove", "Send", "send", "Log out", "Logout", "Sign out")


def _action_name(name: str, w: str) -> bool:
    """이름이 그 동작 그 자체인가("제출", "제출하기", "최종 제출") — "과제제출"처럼 앞에 다른 말이 붙은 이름(화면을 여는 단추·메뉴일 수 있다)은 아니다."""
    return name in (w, w + "하기", w + " 하기", "최종 " + w, "최종" + w, w + " 완료", w + "완료")


def forbidden(policy: "Policy", label: str) -> str | None:
    """사용자가 하지 말라고 한 동작인가(D120) → 거절 이유(아니면 None). 승인을 물을 동작에만 쓴다.
    이름이 동작 그 자체이거나 돈·삭제·발송 같은 말이 든 것만 막는다. "과제제출"처럼 화면을 여는 이름일 수 있는 것은 막지 않고 승인 카드로 묻는다
    (실기기 2026-11-18: "제출하지 마"로 '과제제출'(작성 화면을 여는 단추)까지 막아, 오전에는 카드로 넘어가던 화면에서 멈췄다)."""
    name = (label or "").strip()
    strict = any(w in name for w in STRICT_WORDS)
    if policy.read_only:
        if strict or any(_action_name(name, k) for k in (*policy.sensitive_keywords, *policy.button_keywords)):
            return f"사용자가 보기만 하라고 했다 — '{label}'처럼 무엇을 바꾸는 동작은 하지 않는다. 하지 않았다고 보고한다"
        return None
    hit = [w for w in policy.forbid_words if _action_name(name, w) or (w in name and strict)]
    if hit:
        return f"사용자가 '{hit[0]}'은(는) 하지 말라고 했다 — '{label}'을(를) 누르지 않는다. 하지 않았다고 보고한다"
    return None



@dataclass
class CallCheck:
    status: CheckStatus
    call: ToolCall
    args: BaseModel | None = None
    reason: str = ""                 # 모델에 돌려줄 오류·사유 (한국어)
    approval_detail: dict = field(default_factory=dict)

    @property
    def executable(self) -> bool:
        return self.status == "ok"

    @property
    def valid(self) -> bool:
        """형식·정책상 유효한 호출 (승인 대기 포함)."""
        return self.status in ("ok", "needs_approval")


def check_url(url: str, policy: Policy) -> str | None:
    """문제가 있으면 사유를, 없으면 None을 돌려준다."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "URL 형식이 잘못됐다"
    if parts.scheme not in ("http", "https"):
        return f"허용되지 않은 URL 스킴: {parts.scheme or '(없음)'}"
    if not parts.hostname:
        return "URL에 호스트가 없다"
    if not policy.url_allowed(url):
        why = getattr(policy, "deny_reason", None)
        return (why(parts.hostname) if why else None) or f"허용 목록에 없는 호스트: {parts.hostname}"
    # 퍼센트 인코딩(%5F 등)으로 우회하지 못하게 디코딩한 뒤 검사한다 (서버 구현에 기대지 않음).
    path = "/" + unquote(parts.path).lstrip("/")
    for prefix in policy.denied_path_prefixes:
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            return f"접근이 금지된 경로: {prefix}"
    return None


def validate_call(
    call: ToolCall,
    registry: ToolRegistry,
    policy: Policy,
    observation: Observation | None = None,
) -> CallCheck:
    spec = registry.get(call.name)
    if spec is None:
        return CallCheck(
            "unknown_tool", call,
            reason=f"'{call.name}'은(는) 없는 도구다. 사용 가능: {', '.join(registry.names())}",
        )
    try:
        args = spec.args_model.model_validate(call.arguments)
    except ValidationError as e:
        msgs = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '(전체)'}: {err['msg']}" for err in e.errors()
        )
        return CallCheck("invalid_args", call, reason=f"{call.name} 인자 오류: {msgs}")

    ref = getattr(args, "ref", None)
    element = None
    if ref is not None:
        if observation is None:
            return CallCheck("unknown_ref", call, args, reason="관찰 결과가 없어 ref를 쓸 수 없다. 먼저 observe를 호출한다")
        element = observation.elements.get(ref)
        if element is None:
            return CallCheck(
                "unknown_ref", call, args,
                reason=f"현재 페이지에 ref '{ref}'가 없다. 관찰 결과에 있는 ref만 쓴다",
            )

    # 사용자는 화면의 ref를 알 수 없다. 조작법을 사용자에게 떠넘기는 질문은 거절하고 스스로 찾게 한다 (Phase 7 실측)
    if call.name == "ask_user" and "ref" in getattr(args, "question", "").lower():
        return CallCheck("policy_denied", call, args,
                         reason="ref나 화면 조작 방법은 사용자에게 묻지 않는다. 관찰 결과의 요소 목록에서 직접 찾아 click/type 한다")

    if call.name == "select_option" and element is not None and element.role not in ("combobox", "listbox"):
        return CallCheck("invalid_args", call, args,
                         reason=f"'{element.label}'은(는) 선택 상자가 아니다({element.role})")

    url = getattr(args, "url", None)
    if call.name == "navigate" and url is not None:
        problem = check_url(url, policy)
        if problem:
            return CallCheck("policy_denied", call, args, reason=problem)
        if policy.navigate_urls is not None and url not in policy.navigate_urls:
            allowed = ", ".join(sorted(policy.navigate_urls))
            return CallCheck("policy_denied", call, args,
                             reason=f"navigate는 사이트 첫 페이지({allowed})로만 쓴다. 다른 페이지는 링크를 click 한다")

    # 입력 후 Enter 제출: 검색창이 아니면 양식 제출이다 (클릭 없이 제출하는 승인 우회, Phase 4 실측).
    # 승인 요청으로 돌리지 않고 거절한다: 제출은 이름이 보이는 버튼 클릭으로만 하게 해 승인 화면이 명확해지고,
    # 모델은 사유를 받고 작업을 이어 갈 수 있다 (D21 갱신).
    if call.name == "type" and getattr(args, "submit", False) and element is not None and not is_search_box(element):
        return CallCheck(
            "policy_denied", call, args,
            reason=(f"'{element.label}'은(는) 검색창이 아니라서 submit=true(Enter 제출)를 쓸 수 없다. "
                    "submit=false로 입력하고, 모든 칸을 채운 뒤 제출 버튼을 click 한다"),
        )

    # press_key의 Enter: 검색창이면 그냥, 아니면 승인(켜져 있을 때) — 그 칸에 든 값을 승인 화면에 보여 준다. 다른 키(Escape·Tab·화살표)는 바꾸는 일이 아니다
    if call.name == "press_key" and getattr(args, "key", "") == "Enter" and element is not None and not is_search_box(element):
        name = element.label or "입력 칸"
        if not policy.enter_approval:
            return CallCheck("policy_denied", call, args,
                             reason=f"'{name}'에서 Enter를 누르는 것은 이 작업에서 하지 않는다(제출·추가가 될 수 있다). 다른 방법이 없으면 그렇게 보고한다")
        if policy.read_only:
            return CallCheck("policy_denied", call, args, reason=forbidden(policy, f"Enter 누르기 ({name})"))
        return CallCheck("needs_approval", call, args, reason=f"'{name}'에서 Enter를 누르려면 사용자 승인이 필요하다(입력을 확정·제출하는 키다)",
                         approval_detail={"tool": call.name, "ref": ref, "label": f"Enter 누르기 ({name})", "kind": "action"})

    if policy.mode == "read_draft" and call.name == "click" and element is not None:
        label = element.label
        if any(k in label for k in policy.draft_keywords):
            return CallCheck(
                "needs_approval", call, args,
                reason=f"초안 저장('{label}')은 사용자 승인이 필요하다",
                approval_detail={"tool": call.name, "ref": ref, "label": label, "kind": "draft"},
            )
        if any(k in label for k in policy.sensitive_keywords):
            return CallCheck("policy_denied", call, args,
                             reason=(f"'{label}'은(는) 이 서비스에서 할 수 없는 동작이다(발송·삭제·결제·신청 금지). "
                                     "읽기와 초안 '임시 저장'까지만 한다"))
        if element.role != "link" and label.strip() not in policy.safe_buttons:
            return CallCheck("policy_denied", call, args,
                             reason=f"읽기·초안 전용이라 '{label}' {element.role}은(는) 누를 수 없다. 링크와 검색, 초안 임시 저장만 가능하다")
        return CallCheck("ok", call, args)

    if call.name == "click" and element is not None and any(k in element.label for k in policy.denied_keywords):
        return CallCheck("policy_denied", call, args,
                         reason=f"'{element.label}'은(는) 이 작업에서 하지 않는 동작이다(계정을 없애거나 비밀번호를 바꾸는 일). 누르지 말고, 하지 않았다고 보고한다")

    if call.name == "click" and element is not None:
        hit = [k for k in policy.sensitive_keywords if k in element.label]
        if element.role == "button":
            hit += [k for k in policy.button_keywords if k in element.label]
        # 단추·링크·메뉴가 아닌 것(표의 칸, 머리글, 문단, 목록 줄)은 내용이다: 그 글에 낱말이 들어 있다고 승인을 묻지 않는다
        # (합성 측정: 할 일 제목 "명함 주문"의 칸을 누르는데 '주문' 때문에 승인을 물었다 — 항목을 열려고 누른 것이다).
        # 다만 글이 동작 이름 그 자체면("삭제", "주문하기") 묻는다 — 칸 전체가 단추 노릇을 하는 옛 화면이 있다
        if hit and element.role in CONTENT_ROLES:
            name = element.label.strip()
            if not any(name in (k, k + "하기", k + " 하기") for k in hit):
                hit = []
        if hit and policy.nav_relax and element.role in NAV_ROLES | CHOICE_ROLES:   # D120: 화면을 여는 메뉴·문제 보기
            name = element.label.strip()
            exact = any(name in (k, k + "하기", k + " 하기") for k in hit)
            strict = any(w in name for w in STRICT_WORDS)
            if not exact and (element.role in CHOICE_ROLES or not strict):
                hit = []
        if hit:
            why = forbidden(policy, element.label)
            if why:
                return CallCheck("policy_denied", call, args, reason=why)
            return CallCheck(
                "needs_approval", call, args,
                reason=f"민감한 동작('{element.label}')이라 사용자 승인이 필요하다",
                approval_detail={"tool": call.name, "ref": ref, "label": element.label},
            )

    return CallCheck("ok", call, args)
