"""사이트 요령(D76): 형식, 불러오기, 그리고 요령이 권한을 넓히지 못한다는 것. 기준은 site_hints/README.md."""

import re
from pathlib import Path

import pytest

from agent.sites import hints
from agent.sites.hints import HintError, parse
from agent.tools.validate import Policy

GOOD = """---
host: shop.example.com
names: [합성 상점, shop]
urls: [https://shop.example.com/orders, https://evil.example.org/x, http://shop.example.com/plain, https://pay.shop.example.com/]
seen: 2026-11-16
---
## 시작
- 첫 화면에서 시작한다.
## 함정
- 주문 목록은 더 보기를 눌러야 다 보인다.
## 승인
- 저장은 승인을 묻는다.
"""


def test_a_hint_file_is_parsed_and_malformed_ones_are_rejected():
    h = parse(GOOD, "shop.example.com.md")
    assert h.host == "shop.example.com" and h.names == ("합성 상점", "shop") and h.seen == "2026-11-16" and "더 보기" in h.body
    assert "참고 글" in h.prompt() and "화면을 따른다" in h.prompt() and "넓히지 않는다" in h.prompt()
    for bad, why in ((GOOD.replace("host: shop.example.com", "host: other.example.com"), "파일 이름"),
                     (GOOD.replace("## 승인", "## 끝"), "절이 있어야"),
                     (GOOD.replace("seen: 2026-11-16", "seen: 어제"), "seen"),
                     (GOOD + "가" * 1600, "넘는다"),
                     ("그냥 글", "머리"),
                     (GOOD.replace("names: [합성 상점, shop]", "names: 합성 상점"), "목록")):
        with pytest.raises(HintError, match=why):
            parse(bad, "shop.example.com.md")


def test_a_hint_cannot_widen_what_may_be_opened():
    """요령의 urls는 그 사이트의 허용 호스트 안의 https 주소만 받아들인다. 가지 않기로 한 호스트는 요령에 적혀 있어도 버린다."""
    h = parse(GOOD, "shop.example.com.md")
    pol = Policy(allowed_hosts=frozenset({"shop.example.com"}), denied_hosts=("pay.shop.example.com",))
    assert h.usable_urls(pol.url_allowed) == ["https://shop.example.com/orders"]     # 다른 사이트·http·가지 않는 호스트는 빠진다
    assert h.usable_urls(lambda host: False) == []


def test_loading_falls_back_to_the_parent_host_and_ignores_broken_files(tmp_path):
    (tmp_path / "example.com.md").write_text(GOOD.replace("shop.example.com", "example.com").replace("names: [합성 상점, shop]", "names: []"))
    (tmp_path / "broken.example.net.md").write_text("머리 없음")
    assert hints.load("pay.shop.example.com", tmp_path).host == "example.com"        # 그 호스트의 파일이 없으면 윗주소의 것
    assert hints.load("example.com", tmp_path).host == "example.com"
    assert hints.load("broken.example.net", tmp_path) is None and hints.load("other.org", tmp_path) is None
    assert hints.load("../etc/passwd", tmp_path) is None and hints.load("com", tmp_path) is None   # 호스트 꼴이 아니면 읽지 않는다
    assert [h.host for h in hints.for_task("합성 상점에서 주문 찾아줘", tmp_path)] == []                # 이름은 그 파일의 names에 있어야 한다
    (tmp_path / "shop.example.com.md").write_text(GOOD)
    assert [h.host for h in hints.for_task("합성 상점에서 주문 찾아줘", tmp_path)] == ["shop.example.com"]
    assert hints.for_task("날씨 알려줘", tmp_path) == []


def test_every_hint_file_in_the_repo_is_well_formed_and_carries_no_personal_data_or_permission_talk():
    files = [f for f in sorted(hints.DIR.glob("*.md")) if f.name != "README.md"]
    assert files, "요령 파일이 하나도 없다"
    for f in files:
        h = parse(f.read_text(encoding="utf-8"), f.name)                              # 형식이 맞아야 불러온다
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}", text), f"{f.name}: 메일 주소"
        assert not re.search(r"\d{2,3}-\d{3,4}-\d{4}", text), f"{f.name}: 전화번호"
        assert not re.search(r"승인 없이|승인을 건너|승인하지 않아도|묻지 말고 누른", text), f"{f.name}: 요령은 승인을 바꾸지 못한다"
        assert not re.search(r"비밀번호는 |password\s*[:=]", text, re.I) or "넣지 말" in text, f"{f.name}: 계정 정보"
        assert all(u.startswith("https://") for u in h.urls), f"{f.name}: urls는 https"
