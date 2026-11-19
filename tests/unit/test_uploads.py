"""사진·PDF 붙여 묻기(D73): 올리기, 대화에 붙이기, Claude로만 가기. 실제 Claude가 읽는지는 scripts/bench_uploads.py."""

import base64
import json
import threading

import httpx
import pytest

from agent.api.dialog import UPLOAD_TURNS
from agent.api.server import make_server
from agent.api.uploads import IMAGE_MAX, UploadError, UploadStore, describe, sniff
from agent.providers.claude import ClaudeProvider

from test_dialog_context import CloudSeq, make, make_cloud

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
JPEG = b"\xff\xd8\xff\xe0" + b"synthetic-jpeg" * 4
PDF = b"%PDF-1.4\n% synthetic\n%%EOF\n"
H = {"X-App-Token": "t" * 32, "X-Api-Version": "4"}


def test_kind_comes_from_the_file_head_not_from_what_the_app_says():
    assert sniff(PNG) == ("image", "image/png") and sniff(JPEG) == ("image", "image/jpeg") and sniff(PDF) == ("pdf", "application/pdf")
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ("image", "image/webp") and sniff(b"GIF89a....") == ("image", "image/gif")
    for other in (b"\x00\x00\x00\x18ftypmp42", b"PK\x03\x04docx", b"<html>", b"MZ\x90\x00", b""):   # 영상·문서 묶음·웹 문서·실행 파일은 받지 않는다
        assert sniff(other) is None


def test_store_keeps_things_in_memory_for_a_while_and_drops_old_or_overflowing_ones():
    now = [0.0]
    s = UploadStore(clock=lambda: now[0], ttl_s=100, total_max=len(JPEG) * 2 + 10)
    a = s.put(JPEG, "../../etc/영수증 <1>.jpg")
    assert a["kind"] == "image" and a["mime"] == "image/jpeg" and a["bytes"] == len(JPEG) and a["id"].startswith("u_")
    assert a["name"] == "....etc영수증 1.jpg"                                # 경로·꺾쇠는 뗀다(보여 주는 이름일 뿐이다)
    with pytest.raises(UploadError) as e:
        s.put(b"not a picture", "x.jpg")
    assert e.value.code == "unsupported_media_type"
    with pytest.raises(UploadError) as e:
        s.put(b"\xff\xd8\xff" + b"0" * IMAGE_MAX, "big.jpg")
    assert e.value.code == "payload_too_large"
    now[0] = 60
    b = s.put(PDF, "계약서.pdf")
    assert [u.id for u in s.get([a["id"], "u_none", b["id"]])] == [a["id"], b["id"]]   # 없는 것은 빠진다. 쓰면 시간을 새로 센다
    assert describe(s.get([a["id"], b["id"]])) == "사진 1장, PDF '계약서.pdf'"
    now[0] = 200
    assert len(s) == 0                                                        # 시간이 지나면 지운다
    c, d = s.put(JPEG), s.put(JPEG)
    e_ = s.put(JPEG)                                                          # 넘치면 오래된 것부터 버린다
    assert [u.id for u in s.get([c["id"], d["id"], e_["id"]])] == [d["id"], e_["id"]]


def test_the_block_sent_to_claude_has_the_documented_shape():
    s = UploadStore()
    img, pdf = s.get([s.put(PNG)["id"]])[0].block(), s.get([s.put(PDF, "a.pdf")["id"]])[0].block()
    assert img == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.standard_b64encode(PNG).decode()}}
    assert pdf["type"] == "document" and pdf["source"]["media_type"] == "application/pdf" and pdf["source"]["type"] == "base64"
    # Provider: 사용자 말의 블록이 글 앞에 온다(글만 있는 말은 예전 그대로 문자열)
    from agent.providers.base import Message
    p = ClaudeProvider.__new__(ClaudeProvider)
    p._raw = {}
    _, out = p._convert([Message(role="system", content="s"), Message(role="user", content="앞말"), Message(role="assistant", content="네"),
                         Message(role="user", content="이거 뭐야", blocks=[img])])
    assert out[0] == {"role": "user", "content": "앞말"} and out[2]["content"] == [img, {"type": "text", "text": "이거 뭐야"}]


def blocks_of(cloud):
    return [m.blocks for m in cloud.seen[-1] if m.role == "user" and m.blocks]


def test_an_attached_photo_goes_to_claude_with_that_message_and_the_next_few_and_only_its_kind_is_written_down():
    dlg, mgr, sites, model, ran = make_cloud(*[{"replies": ["영수증이에요.", "- 합계 12,000원"]}] + [{"replies": ["네!"]}] * (UPLOAD_TURNS + 2))
    dlg.uploads = UploadStore()
    try:
        up = dlg.uploads.put(JPEG, "영수증.jpg")
        r = dlg.handle("이거 뭐야", uploads=[up["id"]])
        cid = r["conversation_id"]
        assert r["replies"] == [{"text": "영수증이에요.", "by": "claude"}, {"text": "- 합계 12,000원", "by": "claude"}] and r["action"] == {"type": "none"}
        seen = dlg.cloud.seen[-1]
        assert blocks_of(dlg.cloud) == [[dlg.uploads.get([up["id"]])[0].block()]] and seen[-1].content.endswith("[붙인 것: 사진 1장]")
        assert "# 붙인 것 (사진·PDF)" in seen[0].content and "[붙인 것] 사진 1장" in seen[1].content
        conv = dlg._convs[cid] if hasattr(dlg, "_convs") else None
        text = json.dumps([m.content for m in seen], ensure_ascii=False)
        assert "synthetic-jpeg" not in text and base64.standard_b64encode(JPEG).decode()[:20] not in text   # 실물은 글에 섞이지 않는다(블록에만)
        for i in range(UPLOAD_TURNS - 1):                                      # 이어지는 말에서도 보인다
            dlg.handle("합계가 얼마야", cid)
            assert len(blocks_of(dlg.cloud)) == 1, i
        dlg.handle("하나만 더", cid)                                            # 몇 마디 뒤에는 다시 붙여야 한다(매번 다시 보내지 않는다)
        assert blocks_of(dlg.cloud) == [] and "# 붙인 것" not in dlg.cloud.seen[-1][0].content
        assert conv is None or (conv.uploads == [] and all("synthetic-jpeg" not in t for _, t in conv.turns))
    finally:
        mgr.shutdown()


def test_a_photo_sent_without_words_and_what_happens_when_it_cannot_be_seen():
    dlg, mgr, sites, model, ran = make_cloud({"replies": ["사진이 보여요. 뭘 해 드릴까요?"]}, None)
    dlg.uploads = UploadStore()
    try:
        r = dlg.handle("", uploads=[dlg.uploads.put(PNG)["id"]])                # 말 없이 붙이기만
        assert r["replies"][0]["by"] == "claude" and dlg.cloud.seen[-1][-1].content == "[붙인 것: 사진 1장]"
        r = dlg.handle("이건?", r["conversation_id"], uploads=["u_gone_gone_gone"])   # 지워진(모르는) id
        assert r["replies"] == [{"text": "붙이신 것을 받지 못했어요(시간이 지나 지워졌을 수 있어요). 다시 붙여 주세요.", "by": "mac"}]
        n = len(model.seen)
        r = dlg.handle("이건 뭐야", uploads=[dlg.uploads.put(PDF, "a.pdf")["id"]])  # Claude를 못 불렀다: 로컬 모델로 넘기지 않는다(글만 읽는다)
        assert r["replies"][0]["by"] == "mac" and "볼 수 없어요" in r["replies"][0]["text"] and len(model.seen) == n
    finally:
        mgr.shutdown()
    dlg, mgr, sites, model, ran = make()                                        # 대화 판단이 로컬일 때: 붙인 것을 받지 않는다
    dlg.uploads = UploadStore()
    try:
        r = dlg.handle("이거 뭐야", uploads=[dlg.uploads.put(PNG)["id"]])
        assert r["replies"] == [{"text": "사진·파일을 보는 기능이 지금 꺼져 있어요.", "by": "mac"}]
    finally:
        mgr.shutdown()


def test_upload_endpoint_takes_the_file_as_the_body_and_messages_refer_to_it_by_id():
    dlg, mgr, sites, model, ran = make_cloud({"replies": ["PDF예요."]}, {"replies": ["네!"]})
    http = make_server(mgr, "t" * 32, port=0, dialog=dlg, sites=sites)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http.server_address[1]}"
    try:
        assert "uploads" not in httpx.get(f"{base}/v1/health", headers=H).json()["features"]
        assert httpx.post(f"{base}/v1/uploads", headers=H, content=PDF).status_code == 404          # 꺼져 있으면 없다
        dlg.uploads = UploadStore()
        assert "uploads" in httpx.get(f"{base}/v1/health", headers=H).json()["features"]
        assert httpx.post(f"{base}/v1/uploads", content=PDF).status_code == 401                      # 토큰 없이는 안 된다
        r = httpx.post(f"{base}/v1/uploads", headers={**H, "Content-Type": "image/jpeg", "X-Filename": "%EA%B3%84%EC%95%BD%EC%84%9C.pdf"}, content=PDF)
        up = r.json()["upload"]
        assert r.status_code == 201 and up["kind"] == "pdf" and up["mime"] == "application/pdf" and up["name"] == "계약서.pdf" and up["bytes"] == len(PDF)
        assert httpx.post(f"{base}/v1/uploads", headers=H, content=b"\x00\x00\x00\x18ftypmp42 video").json()["error"]["code"] == "unsupported_media_type"
        assert httpx.post(f"{base}/v1/uploads", headers=H, content=b"").status_code == 400
        big = httpx.post(f"{base}/v1/uploads", headers=H, content=b"\xff\xd8\xff" + b"0" * (11 * 1024 * 1024))
        assert big.status_code == 413 and big.json()["error"]["code"] == "payload_too_large"
        r = httpx.post(f"{base}/v1/messages", headers=H, json={"text": "", "uploads": [up["id"], 7, "../x"]}).json()   # 글 없이 붙인 것만. 꼴이 다른 원소는 버린다
        assert r["replies"] == [{"text": "PDF예요.", "by": "claude"}] and blocks_of(dlg.cloud)[0][0]["type"] == "document"
        assert httpx.post(f"{base}/v1/messages", headers=H, json={"text": ""}).status_code == 400    # 붙인 것도 글도 없으면 예전처럼 오류
    finally:
        http.shutdown()
        mgr.shutdown()
