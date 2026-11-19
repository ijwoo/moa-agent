"""로그인한 사이트 안에서 하기(D72): 허락 질문, 대화 연결, 막는 동작. 브라우저·모델 없이. 끝에서 끝까지는 scripts/bench_inside.py."""

import json
import types

import pytest

from agent.api.runner import ALLOW_NO, ALLOW_ONCE, ALLOW_SITE, AgentRunner
from agent.core.loop import RunResult
from agent.providers.base import ToolCall
from agent.sites.inside import SITE_DENIED, SITE_SENSITIVE
from agent.sites.login import LoginError, SiteLoginManager, generic_site, load_generic_sites
from agent.sites.store import MemoryBlobs
from agent.tools import Policy, default_registry, validate_call
from agent.tools.observation import Observation
from test_dialog_context import done, make_cloud, sent


def manager(tmp_path, cloud_ok=False):
    site = generic_site("shop-example", "shop.example", "https://shop.example/", "https://shop.example/my", cloud_ok=cloud_ok)
    return SiteLoginManager(object(), MemoryBlobs(), {"shop-example": site}, sites_path=tmp_path / "sites_generic.json")


def run_site_do(monkeypatch, sites, answer):
    """_site_do를 돌린다: 질문에는 answer로 답하고, 실제 브라우저 대신 무엇을 받았는지 적는 가짜를 쓴다."""
    seen = {"asked": [], "ran": []}

    def fake_inside(manager, site, driver, task, **kw):
        seen["ran"].append((site.key, site.cloud_ok, task))
        return RunResult("completed", "했다", report={"done": ["했다"], "blocked": [], "by": "claude"}), []
    monkeypatch.setattr("agent.sites.inside.inside_site", fake_inside)
    monkeypatch.setattr("agent.providers.claude.ClaudeProvider", lambda **kw: object())
    runner = AgentRunner.__new__(AgentRunner)
    runner.sites, runner.browse_model, runner.capture = sites, "claude-haiku-5-5", False

    def ask(q, choices=None):
        seen["asked"].append((q, choices))
        return answer
    job = types.SimpleNamespace(task="워크스페이스 바꿔줘", _site="shop-example")
    hooks = types.SimpleNamespace(ask=ask if answer != "old-app" else None, approver=lambda r: True, on_step=None, cancel_check=lambda: False)
    return runner._site_do(job, hooks), seen


def test_the_first_time_asks_whether_claude_may_see_this_site_and_remembers_only_a_yes_for_the_site(monkeypatch, tmp_path):
    sites = manager(tmp_path)
    run, seen = run_site_do(monkeypatch, sites, ALLOW_SITE)
    q, choices = seen["asked"][0]
    assert "shop.example" in q and "Claude" in q and "비밀번호는 가지 않아요" in q and choices == [ALLOW_SITE, ALLOW_ONCE, ALLOW_NO]
    assert seen["ran"] == [("shop-example", True, "워크스페이스 바꿔줘")] and sites.sites["shop-example"].cloud_ok
    assert load_generic_sites(tmp_path / "sites_generic.json")["shop-example"].cloud_ok          # 서버가 다시 떠도 남는다
    run, seen = run_site_do(monkeypatch, sites, ALLOW_NO)
    assert seen["asked"] == [] and len(seen["ran"]) == 1                                            # 허락해 둔 사이트는 다시 묻지 않는다


def test_once_runs_without_remembering_and_no_or_silence_does_nothing(monkeypatch, tmp_path):
    sites = manager(tmp_path)
    run, seen = run_site_do(monkeypatch, sites, ALLOW_ONCE)
    assert len(seen["ran"]) == 1 and not sites.sites["shop-example"].cloud_ok and not (tmp_path / "sites_generic.json").exists()
    for answer, word in ((ALLOW_NO, "허락하지 않으셔서"), (None, "허락을 받지 못해"), ("old-app", "허락을 받지 못해"), ("아무 말", "허락을 받지 못해")):
        run, seen = run_site_do(monkeypatch, sites, answer)
        assert seen["ran"] == [] and run.status == "completed" and word in run.report["done"][0] and run.report["by"] == "mac"
    naver = SiteLoginManager(object(), MemoryBlobs())
    assert [s for s in sites.status() if s["site"] == "shop-example"][0]["inside"] is True
    # 네이버도 그 안에서 할 수 있다(D75, 사용자 결정 2026-11-16). 쇼핑·검색 결과·로그인 화면은 그 안에서도 열지 않는다
    from agent.sites.login import NAVER
    from agent.tools.validate import Policy
    assert [s for s in naver.status() if s["site"] == "naver"][0]["inside"] is True and NAVER.inside_start == "https://pay.naver.com/"
    pol = Policy(allowed_hosts=frozenset({"pay.naver.com", *NAVER.hosts}), denied_hosts=NAVER.no_hosts)
    assert pol.host_allowed("pay.naver.com") and pol.host_allowed("mail.naver.com") and pol.host_allowed("www.naver.com")
    for h in ("shopping.naver.com", "search.shopping.naver.com", "search.naver.com", "smartstore.naver.com", "nid.naver.com"):
        assert not pol.host_allowed(h), h
    with pytest.raises(LoginError):
        naver.allow_cloud("naver_mail")                                                             # 그 안에서 하기가 없는 것(메일 연결 흐름)은 안 된다


def test_buttons_that_change_things_need_approval_and_account_killers_are_refused_even_then():
    obs = Observation.parse("\n".join(["URL: https://shop.example/my", '[e1] link "서연"', '[e2] button "추가"', '[e3] button "회원 탈퇴"',
                                         '[e4] link "로그아웃"', '[e5] button "비밀번호 변경"', '[e6] button "Save"', '[e7] link "다음 페이지"']))
    policy = Policy(allowed_hosts=frozenset({"shop.example"}), sensitive_keywords=SITE_SENSITIVE, denied_keywords=SITE_DENIED,
                    navigate_urls=frozenset({"https://shop.example/my"}))
    reg = default_registry()
    status = lambda ref: validate_call(ToolCall(name="click", arguments={"ref": ref}), reg, policy, obs).status
    assert [status(r) for r in ("e1", "e7")] == ["ok", "ok"]                                        # 보기를 바꾸는 것은 바로
    assert [status(r) for r in ("e2", "e4", "e6")] == ["needs_approval"] * 3                        # 바꾸는 단추·로그아웃은 승인
    assert [status(r) for r in ("e3", "e5")] == ["policy_denied"] * 2                               # 탈퇴·비밀번호 변경은 승인이 있어도 안 한다
    go = lambda url: validate_call(ToolCall(name="navigate", arguments={"url": url}), reg, policy, obs).status
    assert go("https://shop.example/my") == "ok" and go("https://shop.example/guess") != "ok" and go("https://other.example/") != "ok"


class InsideSites:
    """대화 테스트용 사이트 관리 자리: 공통 로그인 사이트 하나."""

    def __init__(self, logged):
        self.logged = logged

    def status(self):
        return [{"site": "naver", "name": "네이버", "status": "logged_out"},
                {"site": "shop-example", "name": "shop.example", "status": "logged_in" if self.logged else "logged_out", "inside": True, "cloud_ok": False}]


SITE_DO = {"action": "site_do", "target": "shop-example", "question": "워크스페이스를 서연으로 바꾼다", "replies": ["바로 해 볼게요!"]}


def test_chat_makes_an_inside_task_or_asks_for_login_first_and_resumes_after(monkeypatch):
    dlg, mgr, sites, model, ran = make_cloud(SITE_DO, SITE_DO, {"action": "site_do", "target": "other", "question": "x"})
    dlg.sites, dlg.site_do = InsideSites(False), True
    try:
        r = dlg.handle("shop.example에서 워크스페이스 서연으로 바꿔줘", site_login=True)             # 로그인이 안 되어 있다 → 로그인부터
        assert r["action"] == {"type": "site_login", "site": "shop-example", "resume": True} and "로그인이 필요해요" in r["replies"][0]["text"]
        out = sent(dlg.cloud)
        assert "site_do — 사용자가 **로그인해 둔 사이트 안에서**" in out and "shop-example(shop.example, 로그인 안 됨)" in out
        cid = r["conversation_id"]
        dlg.sites.logged = True
        r = dlg.handle("했어", cid, site_login=True)                                                 # 로그인 뒤: 멈췄던 부탁을 잇는다(모델을 다시 부르지 않는다)
        task = r["action"]["task"]
        assert r["action"]["type"] == "task" and task["skill"] == "site_do" and task["title"] == "shop.example에서 하기"
        job = mgr.get(task["id"])
        assert job._site == "shop-example" and job.task == "워크스페이스를 서연으로 바꾼다"
        done(mgr, task["id"])
        r = dlg.handle("거기서 워크스페이스 바꿔줘", cid, site_login=True)                            # 로그인돼 있으면 바로 작업
        assert r["action"]["type"] == "task" and r["replies"][0]["text"] == "바로 해 볼게요!"
        done(mgr, r["action"]["task"]["id"])
        r = dlg.handle("저기서도 해줘", cid, site_login=True)                                        # 목록에 없는 사이트를 고르면 열지 않는다
        assert r["action"]["type"] == "ask"
        dlg.site_do = False
        assert "site_do — " not in " ".join(m.content for m in dlg.cloud.calls[-1]) if hasattr(dlg.cloud, "calls") else True
    finally:
        mgr.shutdown()


def test_an_approved_button_press_is_not_listed_as_a_saved_draft():
    """승인한 단추 누르기(kind action)는 초안이 아니다 — 앱이 '저장된 초안'으로 보여 주지 않게 따로 준다."""
    import time
    from agent.api.jobs import JobManager
    from agent.core.approval import ApprovalRequest

    def runner(job, hooks):
        ok = hooks.approver(ApprovalRequest("click", {"ref": "e2"}, "https://shop.example/my", "추가", {"새 할 일": "합성 할 일"}))
        return RunResult("completed" if ok else "approval_denied", "했다", report={"done": ["했다"], "blocked": []})
    mgr = JobManager(runner)
    try:
        j = mgr.submit("추가해줘", None, skill="site_do", extra={"_site": "shop-example", "bad": 1})
        assert j._site == "shop-example" and not hasattr(j, "bad")                 # 작업이 돌기 전에 붙는다. 밑줄로 시작하지 않는 이름은 받지 않는다
        for _ in range(200):
            if j.status == "awaiting_approval":
                break
            time.sleep(0.01)
        ap = j.public()["approval"]
        assert ap["kind"] == "action" and ap["action"] == "추가" and ap["fields"] == {"새 할 일": "합성 할 일"}
        assert "'추가'을(를) 눌러도 될지 승인을 기다리고 있어요" in [n["text"] for n in j.public()["narration"]]
        mgr.decide(j.id, ap["approval_id"], "approve", None, ap["sha256"])
        for _ in range(200):
            if j.status == "completed":
                break
            time.sleep(0.01)
        r = j.public()["result"]
        assert r["approved_drafts"] == [] and r["approved_actions"] == ["추가"]
    finally:
        mgr.shutdown()


def test_the_report_of_an_inside_task_lists_only_the_front_address_of_the_site_not_the_logged_in_pages():
    """로그인한 화면의 주소(경로에 작업·문서 번호가 들 수 있다)는 보고의 sources에 싣지 않는다 — 앱은 sources를 누를 수 있는 링크로 그린다."""
    import time
    from types import SimpleNamespace
    from agent.api.jobs import JobManager

    def runner(job, hooks):
        for url in ("https://shop.example/w/41/board", "https://shop.example/w/41/tasks?filter=mine", "https://shop.example/w/41/board"):
            hooks.on_step(SimpleNamespace(tool="click", args={"ref": "e1"}, result_status="ok", check_status=None, url=url))
        return RunResult("completed", "읽었다", report={"done": ["읽었다"], "blocked": []})
    mgr = JobManager(runner)
    try:
        j = mgr.submit("오늘 할 일 있어?", None, skill="site_do", extra={"_site": "shop-example"})
        for _ in range(200):
            if j.status == "completed":
                break
            time.sleep(0.01)
        assert j.public()["result"]["report"]["sources"] == ["https://shop.example/"]
    finally:
        mgr.shutdown()


def test_the_result_text_rules_ask_for_a_conclusion_first_and_short_lines_in_the_chat_tone():
    """실기기 2026-11-15: 결과 글이 한 덩어리 문단에 "~했다"체라 읽기 어렵다고 했다 → 결론 한 줄 + 짧은 줄, 해요체(정중 말투면 합니다체)."""
    from agent.sites.public import answer_style
    friendly, formal = answer_style(None), answer_style("formal")
    assert "첫 줄에 결론 한 문장" in friendly and '"- "로 시작하는 짧은 줄' in friendly and "해요체로 쓴다" in friendly
    assert "합니다체로 쓴다" in formal and "해요체" not in formal
    assert "예약은 하지 않았습니다" in friendly                       # 묻지 않은 "하지 않은 일"을 늘어놓지 않는다는 예


def test_a_declined_approval_is_noted_as_declined_not_as_a_failure():
    """실기기: 삭제 승인을 거부했는데 다음 말에서 "삭제는 끝내지 못했어요. 다시 해볼까요?"라고 했다 — 대화 기록에 '끝내지 못했다'로 적혀 있었다."""
    import inspect
    from agent.api import dialog
    src = inspect.getsource(dialog.DialogManager._sync)
    assert 'outcome == "approval_denied"' in src and "사용자가 승인하지 않아 하지 않았다" in src and "다시 하자고 권하지 않는다" in src


def test_search_text_that_is_a_refusal_is_recognised_in_more_wordings():
    """실기기 2026-11-16: 검색 담당 모델의 "죄송하지만 저는 브라우저를 직접 열어서 화면을 보거나 버튼을 읽을 수 없어요 …"가 또 답으로 나갔다(앞서 넣은 낱말에 안 걸렸다)."""
    from agent.sites.public import after_giving_up
    said = "죄송하지만 저는 브라우저를 직접 열어서 화면을 보거나 버튼을 읽을 수 없어요. 제가 할 수 있는 건 웹 검색으로 공개된 정보를 찾아보는 것뿐이라, 지금 로그인된 화면의 버튼과 메뉴 목록은 확인해 드릴 수 없습니다"
    assert after_giving_up("화면을 열지 못했어요.", False, lambda: said) == ("화면을 열지 못했어요.", "site")
