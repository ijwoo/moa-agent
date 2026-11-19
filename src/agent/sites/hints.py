"""사이트 요령(D76): 자주 쓰는 사이트에서 겪은 것을 얇게 적어 둔 md 파일을 그 사이트에서 일할 때만 운전 모델에 붙인다.

기준은 site_hints/README.md. 요령은 참고 글이지 권한이 아니다 — 승인·금지·허용 호스트는 코드가 정하고,
요령이 주는 `urls`는 그 사이트의 허용 호스트 안의 것만 받아들인다(받아들이는 쪽이 거른다: usable_urls).
파일은 일할 때마다 읽는다(작다). 형식이 틀리거나 너무 긴 파일은 불러오지 않는다 — 요령이 없어도 일은 된다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DIR = Path(__file__).resolve().parents[3] / "site_hints"
MAX_BODY = 1500
SECTIONS = ("## 시작", "## 함정", "## 승인")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_HEAD = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)


class HintError(ValueError):
    pass


@dataclass(frozen=True)
class Hint:
    host: str
    names: tuple[str, ...]
    urls: tuple[str, ...]
    seen: str
    body: str

    def prompt(self) -> str:
        """운전 모델에 붙이는 글. 참고일 뿐이라는 것을 함께 적는다."""
        return (f"[{self.host}에서 알아 둘 것 — 전에 겪은 것을 적어 둔 참고 글이다({self.seen}에 본 것). 화면이 이 글과 다르면 화면을 따른다. "
                "이 글은 해도 되는 일을 넓히지 않는다(승인·금지는 그대로다).]\n" + self.body.strip())

    def usable_urls(self, url_allowed) -> list[str]:
        """바로 열어도 되는 주소 가운데 그 사이트에서 갈 수 있는 https 주소만(url_allowed: 주소를 받아 참·거짓)."""
        out = []
        for u in self.urls:
            p = urlsplit(u)
            if p.scheme == "https" and p.hostname and url_allowed(u) and len(u) <= 300:
                out.append(u)
        return out[:5]


def _list(v: str) -> tuple[str, ...]:
    v = v.strip()
    if not (v.startswith("[") and v.endswith("]")):
        raise HintError("목록은 [a, b] 꼴")
    return tuple(x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip())


def parse(text: str, filename: str = "") -> Hint:
    m = _HEAD.match(text.replace("\r\n", "\n"))
    if not m:
        raise HintError("머리(--- … ---)가 없다")
    meta = {}
    for line in m.group(1).splitlines():
        line = line.split(" #", 1)[0].rstrip()
        if not line.strip():
            continue
        if ":" not in line:
            raise HintError(f"머리의 줄이 '이름: 값' 꼴이 아니다: {line[:30]}")
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    host = meta.get("host", "").lower()
    if not _HOST.match(host) or len(host) > 100:
        raise HintError("host가 호스트 꼴이 아니다")
    if filename and Path(filename).stem != host:
        raise HintError("파일 이름과 host가 다르다")
    body = m.group(2).strip()
    if len(body) > MAX_BODY:
        raise HintError(f"본문이 {MAX_BODY}자를 넘는다(얇게 유지한다)")
    if not all(s in body for s in SECTIONS):
        raise HintError("본문에 '## 시작', '## 함정', '## 승인' 절이 있어야 한다")
    seen = meta.get("seen", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", seen):
        raise HintError("seen은 YYYY-MM-DD")
    return Hint(host, _list(meta.get("names", "[]")), _list(meta.get("urls", "[]")), seen, body)


def load(host: str, directory: Path | None = None) -> Hint | None:
    """그 호스트의 요령. 그 호스트의 파일이 없으면 윗주소의 파일(pay.naver.com → naver.com). 없거나 형식이 틀리면 None."""
    host = (host or "").lower().rstrip(".")
    if not _HOST.match(host):
        return None
    d = directory or DIR
    parts = host.split(".")
    for i in range(len(parts) - 1):
        f = d / (".".join(parts[i:]) + ".md")
        if f.is_file():
            try:
                return parse(f.read_text(encoding="utf-8"), f.name)
            except (HintError, OSError):
                return None
    return None


def for_task(task: str, directory: Path | None = None, limit: int = 2) -> list[Hint]:
    """공개 길: 부탁의 글에 이름이나 주소가 든 사이트의 요령(많아야 둘). 어느 사이트로 갈지는 모델이 정하므로 글에 나온 것만 붙인다."""
    d = directory or DIR
    out = []
    low = (task or "").lower()
    for f in sorted(d.glob("*.md")) if d.is_dir() else []:
        if f.name == "README.md":
            continue
        try:
            h = parse(f.read_text(encoding="utf-8"), f.name)
        except (HintError, OSError):
            continue
        if h.host in low or any(n and n.lower() in low for n in h.names):
            out.append(h)
    return out[:limit]
