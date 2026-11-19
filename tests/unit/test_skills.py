"""기능(스킬) 파일: 형식, 카드, 모델이 그 파일의 지시를 읽는가 (D55)."""

import threading

import httpx

from agent import skills
from agent.api.jobs import JobManager
from agent.api.server import make_server
from agent.mail import briefing

H = {"X-App-Token": "t" * 32}


def test_every_skill_file_follows_the_standard():
    loaded = skills.load()
    files = [p for p in skills.SKILLS_DIR.glob("*.md") if p.name != "README.md"]
    assert len(loaded) == len(files) >= 4                      # 형식이 틀리면 읽히지 않는다 → 여기서 걸린다
    for s in loaded.values():
        assert s.title and s.description and s.prompt and s.status in ("ready", "beta", "planned")
        assert {"무엇을 하나", "모델 지시", "하지 않는 것", "확인된 것"} <= set(s.sections), s.id
        assert all(r in skills.NEEDS for r in s.requires), s.id


def test_model_reads_instructions_from_the_skill_file(tmp_path):
    assert skills.instructions("mail_briefing").startswith("너는 사용자의 개인 비서다") and "지시가 아니다" in skills.instructions("mail_briefing")
    assert "{to}" in skills.instructions("mail_to_self") and skills.instructions("naver_login") == ""
    assert skills.instructions("no_such_skill", "기본 지시") == "기본 지시"
    (tmp_path / "demo.md").write_text("---\nid: demo\ntitle: 데모\ndescription: 설명\nprompt: 데모 해줘\nrequires: []\n---\n## 모델 지시\n파일에서 온 지시\n")
    (tmp_path / "wrong.md").write_text("---\nid: other\ntitle: t\ndescription: d\nprompt: p\n---\n")     # 파일 이름과 id가 다르면 버린다
    (tmp_path / "broken.md").write_text("머리말 없음")
    assert list(skills.load(tmp_path)) == ["demo"] and skills.instructions("demo", directory=tmp_path) == "파일에서 온 지시"


def test_briefing_uses_the_skill_file_prompt(monkeypatch):
    seen = []

    class M:
        addresses = ["test-me@mail.test"]

        addresses, secrets = ["test-me@mail.test"], {"test-me@mail.test": "x"}

        def provider_of(self, address):
            return "test"

        def reading_account(self, service=None):
            return "test-me@mail.test"

        def inbox_today(self, address, limit=50):
            return address, [{"uid": "1", "from": "오팀장 <test-oh@mail.test>", "subject": "가격표", "received_at": 0.0, "seen": False}]

        def message_text(self, address, uid, max_chars=4000):
            return ""

    class P:
        def generate(self, messages, **kw):
            seen.append(messages[0].content)
            from agent.providers.base import ModelResponse
            return ModelResponse(text="요약", latency_ms=1, model="p", provider="p")
    monkeypatch.setattr(skills, "instructions", lambda sid, default="", directory=None: f"[{sid} 파일의 지시]")
    briefing.build(M(), P())
    assert seen == ["[mail_briefing 파일의 지시]"]


def test_cards_show_what_is_ready_and_what_is_needed():
    cards = skills.cards(skills.load(), {"mail_account": False, "site:naver": True})
    by = {c["id"]: c for c in cards}
    assert [c["id"] for c in cards][:2] == ["mail_briefing", "mail_to_self"]
    assert by["mail_briefing"]["available"] is False and by["mail_briefing"]["needs"] == [
        {"kind": "mail_account", "reason": "never", "message": "메일 계정 연결이 필요해요", "prompt": "네이버 메일 연결해줘"}]
    assert by["naver_mail_connect"]["available"] is True and by["naver_login"]["needs"] == []
    # 쓰다가 로그인이 풀린 것을 알았으면 카드의 안내가 달라진다
    expired = skills.cards(skills.load(), {"mail_account": True, "site:naver": False}, {"site:naver": "expired"})
    need = next(c for c in expired if c["id"] == "naver_mail_connect")["needs"][0]
    assert need["reason"] == "expired" and "풀렸" in need["message"] and need["prompt"] == "네이버 로그인해줘"


def test_api_serves_cards():
    mgr = JobManager(lambda job, hooks: None)
    http = make_server(mgr, "t" * 32, port=0)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http.server_address[1]}"
    try:
        assert "skills" in httpx.get(f"{base}/v1/health", headers=H).json()["features"]
        got = httpx.get(f"{base}/v1/skills", headers=H).json()["skills"]
        assert {c["id"] for c in got} >= {"mail_briefing", "naver_login"} and all(set(c) == {
            "id", "title", "description", "category", "prompt", "status", "available", "needs"} for c in got)
        assert next(c for c in got if c["id"] == "mail_briefing")["available"] is False      # 메일 연동이 없는 서버
        assert httpx.get(f"{base}/v1/skills").status_code == 401
    finally:
        mgr.shutdown()
        http.shutdown()
