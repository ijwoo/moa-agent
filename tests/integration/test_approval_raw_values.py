"""r-004 회귀 테스트: 승인 카드의 입력칸 값이 실제 저장 내용과 같은가 (길이·줄바꿈 포함).

실제 경로: AgentLoop → 컨테이너 안 Chromium → 테스트 메일 사이트. 모델 대신 정해 둔 도구 호출을 쓰고,
ref는 매번 최신 관찰 결과에서 이름으로 찾는다. RUN_INTEGRATION=1 + colima 실행 + 이미지 필요.
"""

import os

import pytest

from agent.browser import testenv
from agent.browser.client import BrowserSession
import time

from agent.api.jobs import JobManager
from agent.core.approval import draft_sha256
from agent.core.loop import AgentLoop
from agent.providers.base import ModelProvider, ModelResponse, ToolCall
from agent.tools import Policy
from agent.tools.browser import agent_registry
from agent.tools.observation import Observation

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="RUN_INTEGRATION=1 필요"),
]

POLICY = Policy(allowed_hosts=frozenset(testenv.SITE_HOSTS), mode="read_draft")
SITES = {"메일": "http://mail.test/"}
# 합성 본문: 200자를 넘고 빈 줄을 포함한 여러 줄
# 합성 본문: 400자 이상, 빈 줄·들여쓰기·연속 공백 포함 (요약이 지우는 것들)
BODY = ("안녕하세요.\n\n지난 회의에서 요청드린 매출 자료 관련해 확인 부탁드립니다.\n"
        + "".join(f"{i}. 세부 항목 확인 요청 문장입니다 (합성).  항목 설명을 덧붙입니다.\n" for i in range(1, 13))
        + "\n    - 들여쓴 줄\n\n감사합니다.")
assert len(BODY) >= 400 and BODY.count("\n") >= 15


class ByLabel(ModelProvider):
    """정해 둔 호출을 순서대로 낸다. ref 자리에 (역할, 이름)을 주면 마지막 관찰 결과에서 찾아 바꾼다."""

    name = "by-label"

    def __init__(self, calls):
        self.calls = list(calls)

    def generate(self, messages, *, tools=None, json_schema=None, temperature=0.0, max_tokens=None):
        name, args = self.calls.pop(0) if self.calls else ("finish", {"summary": "끝"})
        args = dict(args)
        if isinstance(args.get("ref"), tuple):
            role, label = args["ref"]
            obs = next(Observation.parse(m.content) for m in reversed(messages)
                       if m.role == "tool" and "URL:" in m.content)
            args["ref"] = next(e.ref for e in obs.elements.values() if e.role == role and label in e.label)
        return ModelResponse(text="", tool_calls=[ToolCall(name=name, arguments=args)], latency_ms=1,
                             model="s", provider="s")


@pytest.fixture(scope="module")
def browser():
    testenv.start_site()
    with BrowserSession(network=testenv.NETWORK) as b:
        yield b
    testenv.stop_site()


def test_approval_card_matches_saved_draft_exactly(browser):
    testenv.reset_site()
    calls = [
        ("navigate", {"url": "http://mail.test/compose"}),
        ("type", {"ref": ("textbox", "받는 사람"), "text": "test-a@mail.test"}),
        ("type", {"ref": ("textbox", "제목"), "text": "매출 자료 확인 요청"}),
        ("type", {"ref": ("textbox", "본문"), "text": BODY}),
        ("observe", {}),
        ("click", {"ref": ("button", "임시 저장")}),
        ("finish", {"summary": "임시 저장함"}),
    ]
    seen = []
    loop = AgentLoop(ByLabel(calls), browser, agent_registry(), POLICY, SITES,
                     lambda req: seen.append(req) or True)
    run = loop.run("초안 저장 (합성)")
    assert run.status == "completed", [(s.tool, s.result_status, s.note) for s in run.steps]
    assert len(seen) == 1
    card = seen[0].form_values
    drafts = testenv.site_state()["drafts"]
    assert len(drafts) == 1
    saved = drafts[0]["body"].replace("\r\n", "\n")  # 폼 전송은 textarea 줄바꿈을 CRLF로 바꾼다 (HTML 표준)
    assert saved == BODY
    assert card["본문"] == saved
    assert len(card["본문"]) > 201 and card["본문"].count("\n") == BODY.count("\n") > 0
    assert card["받는 사람"] == drafts[0]["to"] and card["제목"] == drafts[0]["subject"]


def test_hash_approval_through_job_manager(browser):
    """D42: 승인 카드(approval.fields) · sha256 · approved_drafts · 실제 저장 내용이 모두 같은 원문인가."""
    testenv.reset_site()

    def runner(job, hooks):
        calls = [
            ("navigate", {"url": "http://mail.test/compose"}),
            ("type", {"ref": ("textbox", "받는 사람"), "text": "test-a@mail.test"}),
            ("type", {"ref": ("textbox", "제목"), "text": "매출 자료 확인 요청"}),
            ("type", {"ref": ("textbox", "본문"), "text": BODY}),
            ("observe", {}),
            ("click", {"ref": ("button", "임시 저장")}),
            ("finish", {"summary": "임시 저장함"}),
        ]
        return AgentLoop(ByLabel(calls), browser, agent_registry(), POLICY, SITES, hooks.approver,
                         on_step=hooks.on_step, cancel_check=hooks.cancel_check).run(job.task, task_id=job.id)

    mgr = JobManager(runner, approval_timeout_s=60)
    try:
        job = mgr.submit("초안 저장 (합성)")
        end = time.time() + 60
        while job.status != "awaiting_approval" and time.time() < end:
            time.sleep(0.1)
        ap = dict(job.approval)
        card = ap["fields"]
        assert card["본문"] == BODY and len(card["본문"]) >= 400            # 승인 카드 = 입력한 원문
        assert ap["sha256"] == draft_sha256("draft", ap["action"], ap["url"], card)
        mgr.decide(job.id, ap["approval_id"], "approve", sha256=ap["sha256"])
        while job.status not in ("completed", "failed") and time.time() < end:
            time.sleep(0.1)
        assert job.status == "completed", job.result
        approved = job.result["approved_drafts"]
        assert len(approved) == 1 and approved[0]["sha256"] == ap["sha256"] and approved[0]["fields"] == card
        saved = testenv.site_state()["drafts"]
        assert len(saved) == 1
        saved_fields = {"받는 사람": saved[0]["to"], "제목": saved[0]["subject"],
                        "본문": saved[0]["body"].replace("\r\n", "\n")}   # 폼 전송의 CRLF만 맞춘다
        assert saved_fields == card == approved[0]["fields"]
    finally:
        mgr.shutdown()


def test_unnamed_fields_are_named_from_the_page_and_no_value_is_lost_on_the_approval_card():
    """실기기: 이름표 없는 칸이 승인 카드에 이름 없이, 또는 값이 이름 자리에 나왔다. 실제 브라우저로: 앞의 글·칸의 종류로 이름을 붙이고 겹치지 않게 한다."""
    from agent.core.approval import form_fields, read_form_values
    from agent.sandbox.manager import SandboxManager
    testenv.start_site()
    sb = SandboxManager(network=testenv.NETWORK, max_concurrent=1, persist_profile=False)
    with sb.session("fields") as b:
        b.request("navigate", {"url": "http://login.test/s/form"})
        obs = Observation.parse(b.request("observe")["text"])
        boxes = [e.ref for e in obs.elements.values() if e.role == "textbox"]
        assert len(boxes) == 5
        for ref, text in zip(boxes, ("합성 할 일", "2026-11-16", "", "", "합성 담당")):
            if text:
                b.request("type", {"ref": ref, "text": text})
        obs = Observation.parse(b.request("observe")["text"])
        assert form_fields(obs, read_form_values(b, obs)) == {"제목": "합성 할 일", "날짜": "2026-11-16", "입력 칸": "", "입력 칸 (2)": "", "담당자": "합성 담당"}
