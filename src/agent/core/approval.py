"""민감 작업 승인: 무엇을 승인받았는지 고정하고, 실행 직전에 같은지 다시 확인한다.

승인 대상(지문)에 들어가는 것: 도구, 인자(ref 제외), 페이지 URL, 누를 요소의 이름,
그리고 **페이지의 모든 입력칸 값** (예: 받는 사람·제목·본문). ref는 관찰할 때마다 다시 매겨지므로 넣지 않는다.
승인 후 실행 직전에 페이지를 다시 관찰해 지문을 새로 만들고, 다르면 실행하지 않는다.

입력칸 값은 관찰 결과(모델용 요약: 200자에서 자르고 줄바꿈을 합침)가 아니라 브라우저에서 읽은 **원문**이다(r-004).
요약 값으로 승인하면 사용자는 본문 앞 200자만 보고 승인하게 되고, 지문 비교도 201자 뒤의 변경을 놓친다.
원문을 하나라도 읽지 못하면 승인을 요청하지 않는다(FormUnreadable).

초안 고정 + 해시(D42): 승인 요청 시점의 초안(종류·누를 버튼 이름·URL·입력칸 원문)을 고정하고
draft_sha256()으로 해시를 만든다. 앱은 이 해시를 함께 보내 승인하고, 실행 직전에 같은 방식으로 다시 계산한 값이
같을 때만 실행한다. 계산식은 앱도 재현할 수 있도록 docs/API.md에 적는다.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from ..tools.observation import Observation
from ..tools.validate import CallCheck


FORM_ROLES = ("textbox", "combobox", "spinbutton")


def draft_sha256(kind: str, action: str, url: str | None, fields: dict[str, str]) -> str:
    """고정한 초안의 해시. 정규형: 키 정렬, 구분자 (",", ":"), ensure_ascii=False 의 JSON을 UTF-8로 sha256.

    {"action": 누를 버튼 이름, "fields": {입력칸 이름: 원문}, "kind": "draft"|…, "url": 페이지 URL}
    값은 원문 그대로(줄바꿈·공백 정규화 없음).
    """
    payload = json.dumps({"action": action, "fields": dict(fields), "kind": kind, "url": url},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class FormUnreadable(Exception):
    """승인 화면에 보여 줄 입력칸 원문을 읽지 못했다. 이때는 승인을 요청하지도, 실행하지도 않는다."""


def read_form_values(browser: Any, observation: Observation) -> dict[str, str]:
    """관찰 결과의 입력칸들에 대해 브라우저에서 원문 값을 읽는다. {ref: 원문}.

    브라우저의 ref 대응표는 마지막 observe 기준이므로 observation은 마지막 관찰 결과여야 한다.
    """
    refs = [ref for ref, e in observation.elements.items() if e.role in FORM_ROLES]
    if not refs:
        return FormValues()
    try:
        got = browser.request("field_values", {"refs": refs})
        values = got.get("values")
    except Exception as e:  # BrowserError 등
        raise FormUnreadable(f"입력칸 원문 읽기 실패: {e}") from e
    if not isinstance(values, dict) or any(not isinstance(values.get(r), str) for r in refs):
        raise FormUnreadable("일부 입력칸의 원문을 읽지 못했다")
    out = FormValues({r: values[r] for r in refs})
    names = got.get("names")
    out.names = {r: n for r, n in names.items() if r in out and isinstance(n, str) and n.strip()} if isinstance(names, dict) else {}
    return out


class FormValues(dict):
    """{ref: 원문}. names: 이름표 없는 칸을 사람이 알아볼 이름 {ref: 이름}(브라우저가 준 것 — 없을 수 있다)."""

    names: dict[str, str] = {}


def form_fields(observation: Observation, raw_values: dict[str, str]) -> dict[str, str]:
    """승인 화면에 보여 주고 해시에 넣는 {칸 이름: 원문}. 빈 칸도 넣는다(승인자가 비어 있는 것을 볼 수 있어야 한다).

    칸 이름은 겹치지 않게 한다 — 이름이 같은 칸이 둘이면 뒤의 값이 앞의 값을 덮어 승인 내용에서 사라진다.
    이름표 없는 칸: 관찰 결과는 그 칸의 값을 이름 자리에 쓴다(실기기: 날짜 칸이 "2026-11-16: 2026-11-16"으로 나왔고,
    빈 칸은 이름 없이 "(비어 있음)"이었다) → 브라우저가 준 이름(앞의 글·칸의 종류), 그것도 없으면 "입력 칸".
    """
    names = getattr(raw_values, "names", None) or {}
    form: dict[str, str] = {}
    for e in observation.elements.values():
        if e.role not in FORM_ROLES:
            continue
        if e.ref not in raw_values:
            raise FormUnreadable(f"입력칸 '{e.label}'의 원문이 없다")
        value = raw_values[e.ref]
        label = e.label.strip()
        flat = " ".join(value.split())
        same = flat.startswith(label[:-1]) if label.endswith("…") and len(label) > 100 else label == flat   # 관찰 결과가 값을 이름 자리에 쓴 것(긴 값은 잘려 있다)
        if not label or same:
            label = (names.get(e.ref) or "").strip() or "입력 칸"
        key, n = label, 2
        while key in form:
            key, n = f"{label} ({n})", n + 1
        form[key] = value
    return form


@dataclass(frozen=True)
class ApprovalRequest:
    tool: str
    args: dict[str, Any]
    url: str | None
    element_label: str
    form_values: dict[str, str] = field(default_factory=dict)
    screenshot_path: str | None = None  # 승인 화면에 첨부할 스크린샷 (지문에는 넣지 않는다)
    kind: str = "action"                 # "draft"(초안 저장) 등. validate_call의 approval_detail에서
    draft_id: str = field(default_factory=lambda: "d_" + secrets.token_urlsafe(9))  # 고정한 초안의 id (해시에는 넣지 않는다)

    @property
    def sha256(self) -> str:
        """앱에 보여 주고 승인받는 초안 해시 (draft_sha256 정규형)."""
        return draft_sha256(self.kind, self.element_label, self.url, self.form_values)

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            [self.tool, self.args, self.url, self.element_label, sorted(self.form_values.items())],
            ensure_ascii=False, sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def describe(self) -> str:
        lines = [f"'{self.element_label}' {self.tool} — {self.url}"]
        for k, v in self.form_values.items():
            lines.append(f"  {k}: {v if v else '(비어 있음)'}")
        return "\n".join(lines)


def build_request(check: CallCheck, observation: Observation, raw_values: dict[str, str]) -> ApprovalRequest:
    """raw_values: read_form_values()가 읽은 {ref: 원문}. 관찰 결과의 요약 값은 쓰지 않는다."""
    assert check.args is not None
    args = {k: v for k, v in check.args.model_dump().items() if k != "ref"}
    ref = getattr(check.args, "ref", None)
    el = observation.elements.get(ref) if ref else None
    # 빈 칸도 포함한다: 승인자가 '시간이 비어 있음'을 볼 수 있어야 한다 (Phase 4 v2 측정에서 발견)
    form = form_fields(observation, raw_values)
    return ApprovalRequest(
        tool=check.call.name, args=args, url=observation.url,
        element_label=(check.approval_detail or {}).get("label") or (el.label if el else ""), form_values=form,
        kind=(check.approval_detail or {}).get("kind", "action"),
    )
