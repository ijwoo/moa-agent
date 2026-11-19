"""결과 화면 캡처(사람이 보는 용도): 페이지가 창보다 길면 창 높이씩 나눠 여러 장(최대 3장) — 한 장이 잘리지 않게
(실기기 2026-11-17, 사용자: "화면이 잘린다 싶으면 여러 장으로 보내 줘도 돼"). 한 장마다 용량 한도를 넘으면 그 장은 뺀다."""

from __future__ import annotations

import base64


def page_shots(b, max_bytes: int, n: int = 3) -> list[bytes]:
    try:
        got = b.request("screenshot", {"slices": n})
        raw = got.get("shots") or [got["jpeg_b64"]]
    except Exception:
        return []
    out = [base64.b64decode(x) for x in raw]
    return [d for d in out if len(d) <= max_bytes]


def captioned(datas: list[bytes], caption: str) -> list[tuple[bytes, str]]:
    """여러 장이면 "(1/3)"을 붙인다."""
    if len(datas) <= 1:
        return [(d, caption) for d in datas]
    return [(d, f"{caption} ({i + 1}/{len(datas)})") for i, d in enumerate(datas)]
