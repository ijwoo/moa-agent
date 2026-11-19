import types

from agent.api.failures import FailureLog


def test_only_unfinished_jobs_are_kept_and_no_content(tmp_path):
    log = FailureLog(tmp_path / "f.jsonl")
    job = types.SimpleNamespace(skill="web_browse", steps=[{}, {}], task="비밀스러운 부탁 글")
    run = types.SimpleNamespace(elapsed_s=12.3, site=None, reason=None, browse={"blocked": ["x.test"], "gave_up": True, "hits": {"x.test": 3}})
    assert not log.record(job, "completed", {"outcome": "completed", "summary": "답"}, run)
    assert log.record(job, "failed", {"outcome": "site_blocked", "summary": "x.test가 막았어요 — 내용"}, run)
    text = (tmp_path / "f.jsonl").read_text()
    assert "비밀스러운" not in text and "내용" not in text and '"gave_up": true' in text and "hits" not in text
    assert oct((tmp_path / "f.jsonl").stat().st_mode)[-3:] == "600"
    s = log.summary()
    assert s["total"] == 1 and s["by_kind"][0][0] == "web_browse:site_blocked"
