"""내려받은 표(D125): 공개 사이트가 내려 주는 CSV·엑셀(xlsx)을 표로 읽고, 정해 둔 거르기·정렬·합계만 한다.

모델은 코드를 쓰지 않는다 — table_query의 조건(열·비교·값, 정렬, 개수, 합계)만 고른다. 파일은 믿을 수 없는 데이터다:
읽기만 하고(수식·매크로를 실행하지 않는다 — xlsx는 셀에 저장된 값만 읽는다) 크기·줄 수에 한도를 둔다.
표준 라이브러리만 쓴다(컨테이너 이미지에 더 깔지 않는다).
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from xml.etree import ElementTree as ET

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 50_000
MAX_COLS = 60
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class TableError(Exception):
    pass


def _decode(data: bytes) -> str:
    # 한국 공공 사이트의 CSV는 UTF-8(BOM)이거나 CP949(EUC-KR)가 많다
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _csv(data: bytes) -> list[list[str]]:
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in row)]


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _xlsx(data: bytes) -> list[list[str]]:
    """첫 시트의 저장된 값만(수식은 계산하지 않는다 — 파일에 저장된 결과값을 읽는다)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise TableError("엑셀 파일을 열지 못했다(옛 xls 형식이면 아직 읽지 못한다)") from e
    if sum(i.file_size for i in z.infolist()) > MAX_BYTES * 5:   # 압축 폭탄
        raise TableError("파일이 너무 크다")
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % NS["m"])))
    sheets = sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
    if not sheets:
        raise TableError("엑셀 파일에 시트가 없다")
    root = ET.fromstring(z.read(sheets[0]))
    rows: list[list[str]] = []
    for r in root.iter("{%s}row" % NS["m"]):
        cells: dict[int, str] = {}
        for c in r.findall("m:c", NS):
            v = c.find("m:v", NS)
            t = c.get("t")
            if t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter("{%s}t" % NS["m"]))
            elif v is None:
                continue
            elif t == "s":
                i = int(v.text or 0)
                val = shared[i] if i < len(shared) else ""
            else:
                val = v.text or ""
            cells[_col_index(c.get("r") or "A1")] = val
        if cells:
            width = min(max(cells) + 1, MAX_COLS)
            rows.append([cells.get(i, "") for i in range(width)])
        if len(rows) > MAX_ROWS:
            break
    return rows


def parse(name: str, data: bytes) -> tuple[list[str], list[list[str]]]:
    """(열 이름, 줄들). 첫 줄 가운데 비어 있지 않은 칸이 많은 줄을 머리글로 본다(위에 제목 줄이 붙은 공공 자료가 많다)."""
    if len(data) > MAX_BYTES:
        raise TableError(f"파일이 너무 크다({len(data) // 1024 // 1024}MB — 한도 {MAX_BYTES // 1024 // 1024}MB)")
    low = (name or "").lower()
    if low.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        rows = _xlsx(data)
    elif low.endswith(".xls"):
        raise TableError("옛 엑셀(xls) 형식은 아직 읽지 못한다 — CSV로 받을 수 있으면 CSV로")
    else:
        rows = _csv(data)
    rows = [[str(c).strip() for c in r[:MAX_COLS]] for r in rows[:MAX_ROWS + 10]]
    if not rows:
        raise TableError("표가 비어 있다")
    widest = max(len([c for c in r if c]) for r in rows[:20])
    head_i = next(i for i, r in enumerate(rows[:20]) if len([c for c in r if c]) >= max(1, widest * 0.6))
    head = rows[head_i]
    nxt = rows[head_i + 1] if head_i + 1 < len(rows) else []
    filled = [c for c in head if c]
    if nxt and _is_header(nxt) and (len(set(filled)) < len(filled) or any(not c for c in head)):
        # 머리글이 두 줄(위: 기간·묶음, 아래: 계·남자·여자 — KOSIS 내려받기): 위 칸이 비면 왼쪽 것을 이어 쓰고 아래와 합친다
        top, last = [], ""
        for c in head:
            last = c or last
            top.append(last)
        width = max(len(top), len(nxt))
        top, low = top + [""] * (width - len(top)), nxt + [""] * (width - len(nxt))
        head = [t if t == b or not b else (b if not t else f"{t} {b}") for t, b in zip(top, low)]
        head_i += 1
    cols, seen = [], {}
    for i, h in enumerate(head):
        h = h or f"열{i + 1}"
        seen[h] = seen.get(h, 0) + 1
        cols.append(h if seen[h] == 1 else f"{h} ({seen[h]})")
    body = [r + [""] * (len(cols) - len(r)) for r in rows[head_i + 1:] if any(c for c in r)]
    # 머리글 아래에 남은 머리글 같은 줄(여러 줄 머리글의 나머지 — 실기기 2026-11-18 따릉이 xlsx): 뒤에 숫자 줄이 이어지는 표에서만 앞의 세 줄까지 뺀다
    if len(body) > 3 and not _is_header(body[3]):
        while body and _is_header(body[0]) and len(body) > 3 and not _is_header(body[3]):
            body = body[1:]
    return cols, [r[:len(cols)] for r in body[:MAX_ROWS]]


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(round(x, 4))


def _is_header(row: list[str]) -> bool:
    cells = [c for c in row if c]
    return bool(cells) and sum(1 for c in cells if _num(c) is None) >= 0.6 * len(cells)


def _num(v: str) -> float | None:
    s = re.sub(r"[,\s원㎡%]", "", str(v))
    try:
        return float(s)
    except ValueError:
        return None


def _match(cell: str, op: str, value: str) -> bool:
    if op == "contains":
        return value in cell
    a, b = _num(cell), _num(value)
    if a is not None and b is not None:
        return {"=": a == b, "!=": a != b, ">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
    return {"=": cell == value, "!=": cell != value, ">": cell > value, ">=": cell >= value, "<": cell < value, "<=": cell <= value}[op]


def query(cols: list[str], rows: list[list[str]], args: dict) -> dict:
    def ci(name: str | None) -> int:
        if name not in cols:
            raise TableError(f"열 '{name}'이(가) 없다. 열: {', '.join(cols[:30])}")
        return cols.index(name)
    total = [c for c in (args.get("total_of") or []) if c][:6]
    if total:   # 여러 열을 줄마다 더한 '합계' 열(예: 거치대 LCD + QR — 실기기 2026-11-18 따릉이)
        idx = [ci(c) for c in total]
        cols = [*cols, "합계"]
        rows = [[*r, _fmt(sum(_num(r[i]) or 0 for i in idx))] for r in rows]
    picked = rows
    for w in args.get("where") or []:
        i = ci(w.get("col"))
        picked = [r for r in picked if _match(r[i], w.get("op", "="), str(w.get("value", "")))]
    agg = args.get("agg") or ""
    if agg:
        if agg == "count":
            return {"agg": "count", "agg_col": "", "matched": len(picked), "value": len(picked)}
        i = ci(args.get("agg_col"))
        nums = [n for n in (_num(r[i]) for r in picked) if n is not None]
        if not nums:
            return {"agg": agg, "agg_col": cols[i], "matched": len(picked), "value": None}
        val = {"sum": sum(nums), "avg": sum(nums) / len(nums), "max": max(nums), "min": min(nums)}[agg]
        return {"agg": agg, "agg_col": cols[i], "matched": len(picked), "value": round(val, 4)}
    if args.get("sort_by"):
        i = ci(args["sort_by"])
        nums = all(_num(r[i]) is not None for r in picked if r[i])
        key = (lambda r: (_num(r[i]) is None, _num(r[i]) or 0)) if nums else (lambda r: r[i])
        picked = sorted(picked, key=key, reverse=bool(args.get("desc", True)))
        if nums and args.get("desc", True):   # 빈 칸은 맨 뒤로
            picked = [r for r in picked if _num(r[i]) is not None] + [r for r in picked if _num(r[i]) is None]
    show = [ci(c) for c in (args.get("columns") or [])] or list(range(len(cols)))
    limit = max(1, min(int(args.get("limit") or 10), 50))
    return {"matched": len(picked), "columns": [cols[i] for i in show], "rows": [[r[i] for i in show] for r in picked[:limit]]}
