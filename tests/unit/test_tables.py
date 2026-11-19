"""D125: 내려받은 표 읽기·거르기 — CP949 CSV, 제목 줄이 붙은 공공 자료, xlsx 저장값, 숫자 비교·정렬·합계."""

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "browser_service"))
import tables  # noqa: E402

CSV = ("서울특별시 송파구 아파트 전월세 실거래가\n\n"
       "단지명,전용면적(㎡),전월세구분,보증금(만원),월세(만원),계약일\n"
       "잠실엘스,84.8,전세,\"110,000\",0,20261001\n"
       "리센츠,84.99,월세,\"50,000\",120,20261003\n"
       "트리지움,84.83,전세,\"98,000\",0,20260915\n"
       "파크리오,59.9,전세,\"80,000\",0,20261010\n").encode("cp949")


def test_cp949_csv_with_a_title_row_reads_the_real_header():
    cols, rows = tables.parse("data.csv", CSV)
    assert cols[0] == "단지명" and cols[3] == "보증금(만원)" and len(rows) == 4


def test_filter_sort_and_aggregate_like_the_user_would_ask():
    cols, rows = tables.parse("data.csv", CSV)
    q = {"where": [{"col": "전월세구분", "op": "=", "value": "전세"}, {"col": "전용면적(㎡)", "op": ">=", "value": "84"}],
         "sort_by": "보증금(만원)", "desc": True, "limit": 5, "columns": ["단지명", "보증금(만원)"]}
    got = tables.query(cols, rows, q)
    assert got["matched"] == 2 and got["rows"][0] == ["잠실엘스", "110,000"]   # 월세 낀 줄·59㎡는 빠진다
    assert tables.query(cols, rows, {**q, "desc": False})["rows"][0][0] == "트리지움"
    avg = tables.query(cols, rows, {"where": q["where"], "agg": "avg", "agg_col": "보증금(만원)"})
    assert avg["value"] == 104000 and avg["matched"] == 2


def test_unknown_column_is_a_clear_error():
    cols, rows = tables.parse("data.csv", CSV)
    try:
        tables.query(cols, rows, {"sort_by": "가격"})
    except tables.TableError as e:
        assert "열 '가격'" in str(e) and "단지명" in str(e)
    else:
        raise AssertionError


def _xlsx(rows):
    strings = sorted({c for r in rows for c in r if not c.replace(".", "").isdigit()})
    sx = "".join(f"<si><t>{s}</t></si>" for s in strings)
    def cell(i, j, v):
        ref = chr(65 + j) + str(i + 1)
        if v.replace(".", "").isdigit():
            return f'<c r="{ref}"><v>{v}</v></c>'
        return f'<c r="{ref}" t="s"><v>{strings.index(v)}</v></c>'
    sheet = "".join(f'<row r="{i+1}">' + "".join(cell(i, j, v) for j, v in enumerate(r)) + "</row>" for i, r in enumerate(rows))
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>{sx}</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f"<worksheet {ns}><sheetData>{sheet}</sheetData></worksheet>")
    return buf.getvalue()


def test_xlsx_saved_values_are_read():
    data = _xlsx([["시도", "2024"], ["경기도", "71285"], ["서울특별시", "39456"]])
    cols, rows = tables.parse("t.xlsx", data)
    assert cols == ["시도", "2024"] and rows[0] == ["경기도", "71285"]
    assert tables.query(cols, rows, {"sort_by": "2024", "limit": 1})["rows"][0][0] == "경기도"


def test_old_xls_and_huge_files_are_refused():
    for name, data in (("a.xls", b"\xd0\xcf\x11\xe0junk"), ("a.csv", b"x" * (tables.MAX_BYTES + 1))):
        try:
            tables.parse(name, data)
        except tables.TableError:
            pass
        else:
            raise AssertionError(name)


def test_two_row_headers_like_kosis_are_merged():
    data = ("시군구별,2025.10,2025.10,2025.10,2025.11,2025.11,2025.11\n"
            "시군구별,계 (명),남자 (명),여자 (명),계 (명),남자 (명),여자 (명)\n"
            "전국,21994,11349,10645,20702,10530,10172\n경기도,6699,3410,3289,6139,3139,3000\n").encode("utf-8")
    cols, rows = tables.parse("k.csv", data)
    assert cols[:3] == ["시군구별", "2025.10 계 (명)", "2025.10 남자 (명)"] and rows[1][0] == "경기도"
    got = tables.query(cols, rows, {"sort_by": "2025.11 계 (명)", "limit": 1, "columns": ["시군구별", "2025.11 계 (명)"]})
    assert got["rows"][0] == ["전국", "20702"]


def test_a_total_column_adds_split_columns_row_by_row():
    data = "대여소,LCD,QR\n가,30,10\n나,0,35\n다,20,20\n".encode("utf-8")
    cols, rows = tables.parse("b.csv", data)
    got = tables.query(cols, rows, {"total_of": ["LCD", "QR"], "sort_by": "합계", "limit": 2, "columns": ["대여소", "합계"]})
    assert got["rows"] == [["가", "40"], ["다", "40"]] or got["rows"] == [["다", "40"], ["가", "40"]]
    assert tables.query(cols, rows, {"sort_by": "QR", "limit": 1})["rows"][0][0] == "나"   # QR만 보면 답이 달라진다


def test_leftover_header_rows_under_the_header_are_dropped_but_text_tables_are_kept():
    data = ("대여소번호,대여소명,거치대\n,,설치형태\n,,LCD\n1,가,10\n2,나,5\n3,다,7\n4,라,9\n").encode("utf-8")
    cols, rows = tables.parse("x.csv", data)
    assert rows[0][1] == "가" and len(rows) == 4
    text = "이름,설명\n가,하나\n나,둘\n다,셋\n라,넷\n".encode("utf-8")
    assert len(tables.parse("t.csv", text)[1]) == 4
