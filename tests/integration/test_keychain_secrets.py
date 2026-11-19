"""맥 키체인 보관소. RUN_INTEGRATION=1 + macOS에서만. 시험용 서비스 이름과 가상 주소만 쓰고 끝나면 지운다."""

import os
import subprocess
import sys

import pytest

from agent.mail.secrets import KeychainSecrets

pytestmark = [
    pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="RUN_INTEGRATION=1 필요"),
    pytest.mark.skipif(sys.platform != "darwin", reason="macOS 키체인"),
]
ADDR = "test-keychain@mail.test"


def test_set_get_delete_roundtrip_without_password_on_command_line(monkeypatch):
    store = KeychainSecrets(service="local-ai-agent.mail.selftest")
    seen = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        seen.append(list(cmd))
        return real(cmd, *a, **kw)
    monkeypatch.setattr(subprocess, "run", spy)
    try:
        assert store.get(ADDR) is None
        store.set(ADDR, "Ab1!selftest#Pass")
        assert store.get(ADDR) == "Ab1!selftest#Pass"
        store.set(ADDR, "second-Value-0002")              # 덮어쓰기
        assert store.get(ADDR) == "second-Value-0002"
        assert not any("selftest#Pass" in " ".join(c) or "second-Value" in " ".join(c) for c in seen)   # 인자에 값이 없다
        with pytest.raises(ValueError):
            store.set(ADDR, 'bad"quote-password')
    finally:
        store.delete(ADDR)
    assert store.get(ADDR) is None


def test_blob_store_roundtrip_splits_large_values_and_cleans_up():
    """로그인 상태 보관(D52): 키체인 한 항목은 4천 자쯤에서 잘리므로 압축해 나눠 넣는다."""
    import json
    import os as _os
    from agent.sites.store import KeychainBlobs
    store = KeychainBlobs(service="local-ai-agent.site.selftest")
    big = json.dumps([{"name": f"c{i}", "value": _os.urandom(60).hex(), "domain": ".login.test"} for i in range(120)])
    try:
        assert store.get("app.selftest") is None
        store.set("app.selftest", big)
        assert store.get("app.selftest") == big
        assert store._read("app.selftest#1") is not None          # 여러 조각으로 나뉘었다
        store.set("app.selftest", '[{"name": "small"}]')          # 작은 값으로 덮어쓰면 남은 조각이 없어야 한다
        assert store.get("app.selftest") == '[{"name": "small"}]' and store._read("app.selftest#1") is None
    finally:
        store.delete("app.selftest")
    assert store.get("app.selftest") is None and store._read("app.selftest#0") is None
