"""기능(스킬) 파일: 에이전트가 할 수 있는 일 하나 = skills/<id>.md 하나. 기준은 skills/README.md.

- 앱의 "할 수 있는 일" 카드는 이 파일들의 머리말로 만든다(GET /v1/skills).
- 모델은 그 일을 할 때 그 파일의 `## 모델 지시` 절만 읽는다. 공용 지시문에 줄을 더하지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
_ID = re.compile(r"^[a-z0-9_]{2,40}$")
# 준비 조건이 안 되어 있을 때 카드가 권하는 말
# 로그인이 풀려서(쓰다가 알게 됨) 다시 필요한 때의 안내
EXPIRED = {"site:naver": "네이버 로그인이 풀렸어요. 다시 로그인해 주세요"}
NEEDS = {
    "mail_account": ("메일 계정 연결이 필요해요", "네이버 메일 연결해줘"),
    "site:naver": ("네이버 로그인이 필요해요", "네이버 로그인해줘"),
}


@dataclass(frozen=True)
class Skill:
    id: str
    title: str
    description: str
    prompt: str
    category: str = ""
    requires: tuple[str, ...] = ()
    status: str = "ready"
    order: int = 100
    sections: dict = field(default_factory=dict)

    @property
    def model_instructions(self) -> str:
        """`## 모델 지시` 절의 글. "(없음"으로 시작하면 모델에게 주는 지시가 없는 기능이다."""
        text = self.sections.get("모델 지시", "").strip()
        return "" if text.startswith("(없음") else text


def parse(text: str) -> Skill:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError("머리말(--- … ---)이 없다")
    meta: dict = {}
    for line in m.group(1).splitlines():
        if not line.strip() or ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.split("  #", 1)[0].strip()
        meta[k.strip()] = [x.strip() for x in v[1:-1].split(",") if x.strip()] if v.startswith("[") and v.endswith("]") else v
    sections, name = {}, None
    for line in m.group(2).splitlines():
        h = re.match(r"^##\s+(.+?)\s*$", line)
        if h:
            name = h.group(1)
            sections[name] = ""
        elif name is not None:
            sections[name] += line + "\n"
    for k in ("id", "title", "description", "prompt"):
        if not isinstance(meta.get(k), str) or not meta[k]:
            raise ValueError(f"머리말에 {k}가 없다")
    if not _ID.match(meta["id"]):
        raise ValueError("id 형식 오류")
    req = meta.get("requires", [])
    return Skill(meta["id"], meta["title"], meta["description"], meta["prompt"], str(meta.get("category", "")),
                 tuple(req if isinstance(req, list) else [req]), str(meta.get("status", "ready")),
                 int(meta.get("order", 100)) if str(meta.get("order", "100")).isdigit() else 100,
                 {k: v.strip() for k, v in sections.items()})


def load(directory: Path | None = None) -> dict[str, Skill]:
    """폴더의 기능 파일을 읽는다. 형식이 틀린 파일은 건너뛴다(README.md 제외). 파일 이름과 id가 달라도 건너뛴다."""
    out: dict[str, Skill] = {}
    d = Path(directory or SKILLS_DIR)
    for path in sorted(d.glob("*.md")) if d.is_dir() else []:
        if path.name.lower() == "readme.md":
            continue
        try:
            s = parse(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if s.id == path.stem:
            out[s.id] = s
    return out


def instructions(skill_id: str, default: str = "", directory: Path | None = None) -> str:
    """그 기능의 모델 지시(파일이 없거나 비어 있으면 default)."""
    s = load(directory).get(skill_id)
    return (s.model_instructions if s else "") or default


def cards(skills: dict[str, Skill], ready: dict[str, bool], why: dict[str, str | None] | None = None) -> list[dict]:
    """앱에 내려 줄 카드. ready: 준비 조건별로 지금 되어 있는지. why: 안 되어 있는 까닭("expired"면 풀린 것). planned인 기능은 내보내지 않는다."""
    why = why or {}
    out = []
    for s in sorted(skills.values(), key=lambda x: (x.order, x.id)):
        if s.status == "planned":
            continue
        needs = [{"kind": r, "reason": "expired" if why.get(r) == "expired" else "never",
                  "message": EXPIRED[r] if why.get(r) == "expired" and r in EXPIRED else NEEDS.get(r, ("준비가 필요해요", ""))[0],
                  "prompt": NEEDS.get(r, ("", ""))[1]}
                 for r in s.requires if not ready.get(r, False)]
        out.append({"id": s.id, "title": s.title, "description": s.description, "category": s.category, "prompt": s.prompt,
                    "status": s.status, "available": not needs, "needs": needs})
    return out
