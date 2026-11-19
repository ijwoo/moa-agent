"""공통 로그인의 브라우저 쪽 동작(D70): 로그인 칸을 표준 속성으로 알아보고, 넣고, 제출한다. 합성 로그인 사이트(login.test)만 쓴다.

RUN_INTEGRATION=1 + colima 실행 + 이미지 빌드 필요.
"""

import os

import pytest

from agent.browser import testenv
from agent.browser.client import BrowserError, BrowserSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="RUN_INTEGRATION=1 필요"),
]
ID, PW = "tester@example.com", "synthetic-pass-1"


@pytest.fixture(scope="module", autouse=True)
def site():
    testenv.start_site()
    yield


@pytest.fixture
def b():
    s = BrowserSession(network=testenv.NETWORK)
    yield s
    s.close()


def names(f):
    return [(x["name"], x["kind"], x["label"]) for x in f["fields"]]


def test_plain_form_picks_the_login_group_not_the_search_box_or_the_guest_form(b):
    b.request("navigate", {"url": "http://login.test/a/login"})
    f = b.request("form_fields")
    assert names(f) == [("id", "text", "이메일"), ("password", "password", "비밀번호")]
    assert f["host"] == "login.test" and f["https"] is False and f["submit"] is True and f["signup"] is False
    b.request("form_fill", {"values": {"id": ID, "password": "wrong-pass"}})
    assert b.request("form_submit")["how"] == "button"           # 글이 맞는 제출 단추가 있으면 그 단추를 누른다
    f = b.request("form_fields")                                   # 틀리면 같은 칸이 다시 나온다
    assert [x["name"] for x in f["fields"]] == ["id", "password"]
    b.request("form_fill", {"values": {"id": ID, "password": PW}})
    out = b.request("form_submit")
    assert out["url"].endswith("/home") and b.request("form_fields")["fields"] == []
    assert "lsid" in b.request("cookie_names")["names"]
    assert PW not in b.request("observe")["text"] and PW not in b.request("extract_text")["text"]


def test_two_page_login_then_a_one_time_code(b):
    b.request("navigate", {"url": "http://login.test/b/login"})
    assert names(b.request("form_fields")) == [("id", "text", "아이디 또는 이메일")]
    b.request("form_fill", {"values": {"id": ID}})
    b.request("form_submit")
    assert names(b.request("form_fields")) == [("password", "password", "비밀번호")]
    b.request("form_fill", {"values": {"password": PW}})
    b.request("form_submit")
    assert names(b.request("form_fields")) == [("code", "code", "인증번호")]
    b.request("form_fill", {"values": {"code": "246810"}})
    assert b.request("form_submit")["url"].endswith("/home")


def test_fields_outside_a_form_and_a_link_as_the_button(b):
    b.request("navigate", {"url": "http://login.test/c/login"})
    f = b.request("form_fields")
    assert names(f) == [("id", "text", "아이디"), ("password", "password", "비밀번호")] and f["submit"] is True
    b.request("form_fill", {"values": {"id": ID, "password": PW}})
    out = b.request("form_submit")
    assert out["how"] == "button" and out["url"].endswith("/home")   # 양식 밖의 칸: 단추를 누른다


def test_captcha_field_comes_with_its_picture(b):
    b.request("navigate", {"url": "http://login.test/d/login"})
    f = b.request("form_fields")
    assert [x["name"] for x in f["fields"]] == ["id", "password", "captcha"] and f["captcha_image"] is True
    assert b.request("form_image")["bytes"] > 100
    b.request("form_fill", {"values": {"id": ID, "password": PW, "captcha": "7k2p"}})
    assert b.request("form_submit")["url"].endswith("/home")


def test_login_on_another_host_reports_that_host(b):
    b.request("navigate", {"url": "http://login.test/e/login"})
    f = b.request("form_fields")
    assert f["host"] == "auth.test" and [x["name"] for x in f["fields"]] == ["id", "password"]
    b.request("form_fill", {"values": {"id": ID, "password": PW}})
    out = b.request("form_submit")
    assert out["url"] == "http://login.test/home"


def test_signup_page_is_flagged_and_pages_without_login_fields_give_nothing(b):
    b.request("navigate", {"url": "http://login.test/signup"})
    assert b.request("form_fields")["signup"] is True
    b.request("navigate", {"url": "http://login.test/"})            # 검색칸만 있는 화면
    f = b.request("form_fields")
    assert f["fields"] == [] and f["submit"] is False
    with pytest.raises(BrowserError):
        b.request("form_submit")


def test_fill_refuses_when_the_fields_changed_and_never_echoes_values(b):
    b.request("navigate", {"url": "http://login.test/b/login"})
    with pytest.raises(BrowserError) as e:
        b.request("form_fill", {"values": {"id": ID, "password": PW}})   # 화면에는 아이디 칸뿐이다
    assert PW not in str(e.value) and ID not in str(e.value)
    with pytest.raises(BrowserError):
        b.request("form_fill", {"values": {"id": ""}})


def test_a_page_that_freezes_on_first_load_is_reopened_once(b):
    """실제 사이트에서 본 것: 처음 받을 때만 페이지의 스크립트가 주 스레드를 놓지 않아 읽기가 끝없이 기다렸다. 한 번 새로 열면 된다."""
    import time
    b.request("navigate", {"url": "http://login.test/g/login"})
    time.sleep(0.4)                                                 # 받은 직후에는 답하다가 곧 멈춘다(실제 사이트도 그랬다)
    f = b.request("form_fields")                                    # 멈춘 것을 알아채고 한 번 새로 연 뒤 읽는다
    assert [x["name"] for x in f["fields"]] == ["id", "password"]
    o = b.request("observe")
    assert "revived" not in o and "비밀번호" in o["text"]            # 이미 살아 있으면 다시 열지 않는다
    b.request("navigate", {"url": "http://login.test/g/login?again"})   # 읽는 도중에 멈추는 경우(받자마자 읽기 시작)도 같다
    assert "로그인" in b.request("extract_text")["text"]
    b.request("navigate", {"url": "http://login.test/g/dead"})
    time.sleep(0.4)
    with pytest.raises(BrowserError) as e:
        b.request("observe")                                        # 다시 열어도 멈추면 그렇게 알린다(끝없이 기다리지 않는다)
    assert "응답하지 않는다" in str(e.value)


def test_login_methods_are_found_and_a_social_button_or_the_entry_link_can_be_opened(b):
    b.request("navigate", {"url": "http://login.test/"})
    m = b.request("login_methods")
    assert m["entry"] is True and m["providers"] == []              # 첫 화면: "로그인" 입구만(회원가입 링크는 입구가 아니다)
    assert b.request("login_open", {"entry": True})["url"].endswith("/h/login")
    m = b.request("login_methods")
    assert m["providers"] == ["kakao", "google"] and m["entry"] is False   # "Google Play에서 앱 받기"는 로그인 단추가 아니다
    assert [x["name"] for x in b.request("form_fields")["fields"]] == ["id", "password"]
    out = b.request("login_open", {"provider": "kakao"})            # 같은 탭에서 다른 호스트로 넘어가는 로그인
    assert out["how"] == "same_tab" and b.request("form_fields")["host"] == "auth.test"
    with pytest.raises(BrowserError):
        b.request("login_open", {"provider": "naver"})              # 화면에 없는 방법


def test_popup_login_continues_in_the_popup_and_returns_to_the_first_tab(b):
    b.request("navigate", {"url": "http://login.test/h/login"})
    out = b.request("login_open", {"provider": "google"})
    assert out["how"] == "popup"
    f = b.request("form_fields")
    assert f["host"] == "auth.test" and [x["name"] for x in f["fields"]] == ["id", "password"]
    b.request("form_fill", {"values": {"id": ID, "password": PW}})
    b.request("form_submit")                                        # 팝업이 원래 탭을 넘기고 스스로 닫는다
    import time
    time.sleep(0.8)
    f = b.request("form_fields")
    assert f["host"] == "login.test" and f["fields"] == [] and f["url"].endswith("/home")
    assert "lsid" in b.request("cookie_names")["names"]


def test_a_my_page_icon_is_the_way_in_when_there_is_no_login_text_and_a_guest_block_is_not_a_signup_page(b):
    """실제 사이트에서 본 것: 글자로 된 "로그인"이 없고 "마이"가 입구인 곳 / 회원 로그인과 비회원 조회(비밀번호 + 확인)가 한 화면인 곳."""
    b.request("navigate", {"url": "http://login.test/j"})
    m = b.request("login_methods")
    assert m["entry"] is True and m["entry_kind"] == "my"
    assert b.request("login_open", {"entry": True})["url"].endswith("/a/login")
    assert [x["name"] for x in b.request("form_fields")["fields"]] == ["id", "password"]
    b.request("navigate", {"url": "http://login.test/k/login"})
    f = b.request("form_fields")
    assert [x["name"] for x in f["fields"]] == ["id", "password"] and f["signup"] is False
    b.request("form_fill", {"values": {"id": ID, "password": PW}})
    assert b.request("form_submit")["url"].endswith("/home")
    b.request("navigate", {"url": "http://login.test/signup"})      # 비밀번호 + 확인이 붙어 있는 진짜 가입 화면은 그대로 가입 화면
    assert b.request("form_fields")["signup"] is True


def test_the_login_button_is_pressed_when_there_is_one_and_the_sites_own_error_text_is_read(b):
    """실기기: 같은 계정이 단추로는 통과했고 Enter로는 사이트가 거절했다 — 단추의 누르기에 붙은 처리를 거쳐야 받는 로그인 화면이 있다."""
    b.request("navigate", {"url": "http://login.test/l/login"})
    b.request("form_fill", {"values": {"id": ID, "password": "wrong-pass"}})
    assert b.request("form_submit")["how"] == "button"
    f = b.request("form_fields")
    assert f["alert"] is True and f["alert_text"] == "계정 정보가 일치하지 않습니다. (합성 안내)" and f["pw_empty"] is True
    b.request("form_fill", {"values": {"id": ID, "password": PW}})
    assert b.request("form_submit")["url"].endswith("/home")       # 단추로 내야만 받는 로그인이 통과한다


def test_a_challenge_layer_over_the_login_form_can_be_shown_and_answered_by_the_person(b):
    """실기기: 계정을 제출하자 "안전한 서비스 이용을 위해 아래 문제에 답해 주세요"라는 층이 떴다. 풀지 않는다 — 그림과 물음을 사람에게 보여 주고 답을 받아 넣는다."""
    b.request("navigate", {"url": "http://login.test/o/login"})
    assert b.request("challenge")["present"] is False
    b.request("form_fill", {"values": {"id": ID, "password": PW}})
    b.request("form_submit")
    f = b.request("form_fields")
    assert f["dialog"] is True and [x["name"] for x in f["fields"]] == ["id", "password"]      # 로그인 칸은 그대로, 그 위에 층이 떴다
    c = b.request("challenge")
    assert c["present"] and c["answerable"] and "아래 문제에 답해 주세요" in c["text"] and c["label"] == "정답 입력" and c["bytes"] > 500
    out = b.request("challenge_answer", {"text": "7k2p"})
    assert out["how"] == "button" and out["url"].endswith("/home")
    with pytest.raises(BrowserError):
        b.request("challenge_answer", {"text": "x"})                                             # 문제가 없으면 넣지 않는다


def test_a_challenge_inside_a_frame_from_another_host_is_read_and_answered_and_a_tap_only_one_is_reported_with_its_picture(b):
    """실기기: 확인 문제 상자의 글이 "…답해 주세요. 닫기"뿐이었다 — 문제는 상자 안의 다른 주소의 틀에 있었다. 그 틀 안에서 답 칸을 찾는다."""
    b.request("navigate", {"url": "http://login.test/p/login"})
    b.request("form_fill", {"values": {"id": ID, "password": PW}})
    b.request("form_submit")
    c = b.request("challenge")
    assert c["present"] and c["answerable"] and c["label"] == "정답 입력" and c["bytes"] > 500
    assert "아래 문제에 답해 주세요" in c["text"] and "그림에 보이는 글자를 입력해 주세요" in c["text"]      # 상자의 글 + 틀 안의 글
    assert c["shape"]["frame_host"] == "auth.test" and c["shape"]["frame_inputs"] == 1 and c["shape"]["box_inputs"] == 0
    assert b.request("challenge_answer", {"text": "7K2P"})["how"] == "button"
    import time
    time.sleep(1.0)                                                                                          # 틀이 답을 바깥 화면에 알리고, 바깥 화면이 로그인을 낸다
    assert b.request("form_fields")["url"].endswith("/home")
    b.request("navigate", {"url": "http://login.test/p2/login"})                                            # 답 칸이 없는 종류(눌러 고르기)
    b.request("form_fill", {"values": {"id": ID, "password": PW}})
    b.request("form_submit")
    c = b.request("challenge")
    assert c["present"] and c["answerable"] is False and c["bytes"] > 500                                    # 답할 수는 없어도 그림은 준다
    assert c["shape"]["frame_inputs"] == 0 and c["shape"]["frame_pictures"] == 4 and "모두 고르세요" in c["text"]
    with pytest.raises(BrowserError):
        b.request("challenge_answer", {"text": "x"})
