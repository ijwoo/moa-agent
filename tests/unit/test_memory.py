import json

from agent.memory.extract import extract_meeting, make_utterances
from agent.memory.qa import MEMORY_HEADER, answer_question, memory_context
from agent.memory.schema import Item, Meeting
from agent.memory.store import MemoryStore, bigrams, format_hits
from agent.providers.base import ModelProvider, ModelResponse


class OneShot(ModelProvider):
    name = "one"

    def __init__(self, text):
        self.text, self.seen = text, None

    def generate(self, messages, *, tools=None, json_schema=None, temperature=0.0, max_tokens=None):
        self.seen = (messages, json_schema)
        return ModelResponse(text=self.text, latency_ms=1, model="m", provider="p")


ROWS = [{"speaker": "김팀장", "text": "기계식 키보드 K200으로 한 대 구매하기로 합시다."},
        {"speaker": "나", "text": "제가 K200을 장바구니에 담아 두겠습니다."},
        {"speaker": "김팀장", "text": "웹캠 C5는 보류하겠습니다."}]


def store_with_meeting():
    s = MemoryStore(":memory:")
    us = make_utterances("m2", ROWS)
    s.add_meeting(Meeting(id="m2", title="사무용품 구매 회의", date="2026-11-05", utterances=us, summary="키보드 구매 결정",
                          items=[Item(kind="decision", text="기계식 키보드 K200 한 대 구매", sources=["m2-u1"]),
                                 Item(kind="todo", text="K200 장바구니에 담기", sources=["m2-u2"], assignee="나")]))
    us2 = make_utterances("m3", [{"speaker": "김팀장", "text": "다음 주간 회의는 11월 17일 오후 3시 소회의실 A입니다."}])
    s.add_meeting(Meeting(id="m3", title="주간 회의", date="2026-11-07", utterances=us2))
    return s


def test_bigrams_ignore_spaces_and_punct():
    assert bigrams("회의 에서!") == ["회의", "의에", "에서"]
    assert bigrams("") == [] and bigrams("가") == ["가"]


def test_search_matches_despite_particles_and_boosts_items():
    s = store_with_meeting()
    hits = s.search("사무용품 회의에서 뭘 사기로 했지? 키보드", k=3)
    assert hits[0].meeting_id == "m2" and any(h.kind in ("decision", "todo") for h in hits)
    assert s.search("주간회의는 언제야", k=1)[0].sources == ["m3-u1"]
    assert s.search("zzzz qqqq") == []
    assert all(h.kind == "todo" for h in s.search("키보드", kinds=("todo",)))


def test_add_meeting_replaces_and_get_utterances():
    s = store_with_meeting()
    s.add_meeting(Meeting(id="m2", title="사무용품 구매 회의", date="2026-11-05", utterances=make_utterances("m2", ROWS[:1])))
    assert [u.id for u in s.get_utterances(["m2-u1", "m2-u2"])] == ["m2-u1"]
    assert s.items() == [] and len(s.meetings()) == 2


def test_task_results_and_format(tmp_path):
    s = MemoryStore(tmp_path / "u" / "memory.db")
    s.add_task_result("메일 보내줘", "completed", "보냈습니다")
    assert s.task_results()[0][1:] == ("메일 보내줘", "completed", "보냈습니다")
    assert oct((tmp_path / "u" / "memory.db").stat().st_mode)[-3:] == "600"
    line = format_hits(store_with_meeting().search("K200 구매", k=1))
    assert line.startswith("[m2-u1] 2026-11-05 사무용품 구매 회의 · 결정")


def test_extract_drops_invalid_sources():
    out = {"topic": "구매", "summary": "키보드 구매", "decisions": [{"text": "K200 구매", "sources": [1, 99]}],
           "todos": [{"text": "장바구니 담기", "assignee": "나", "due_text": None, "sources": [2]},
                     {"text": "지어낸 일", "assignee": None, "due_text": None, "sources": [42]}],
           "open_questions": []}
    p = OneShot(json.dumps(out, ensure_ascii=False))
    m, st = extract_meeting(p, "m2", "사무용품 구매 회의", "2026-11-05", make_utterances("m2", ROWS))
    assert [(i.kind, i.sources) for i in m.items] == [("decision", ["m2-u1"]), ("todo", ["m2-u2"])]
    assert st.dropped_no_source == 1 and st.invalid_source_refs == 2
    assert "[2] 나: 제가 K200" in p.seen[0][1].content


def test_extract_bad_json_keeps_raw_meeting():
    m, st = extract_meeting(OneShot("nope"), "m2", "t", "2026-11-05", make_utterances("m2", ROWS))
    assert st.error and m.items == [] and len(m.utterances) == 3


def test_answer_validates_citations():
    s = store_with_meeting()
    p = OneShot(json.dumps({"found": True, "answer": "K200", "citations": ["m2-u1", "m9-u9"]}))
    a = answer_question(p, s, "뭘 사기로 했지? 키보드")
    assert a.found and a.citations == ["m2-u1"] and a.invalid_citations == ["m9-u9"]
    assert MEMORY_HEADER in p.seen[0][1].content
    p2 = OneShot(json.dumps({"found": True, "answer": "지어냄", "citations": ["m9-u9"]}))
    assert answer_question(p2, s, "키보드").found is False  # 근거 없는 답은 받아들이지 않는다
    assert answer_question(p, MemoryStore(":memory:"), "아무거나").found is False


def test_memory_context():
    s = store_with_meeting()
    ctx = memory_context(s, "사무용품 회의에서 사기로 한 상품을 장바구니에 담아줘")
    assert ctx.startswith(MEMORY_HEADER) and "K200" in ctx
    assert memory_context(MemoryStore(":memory:"), "x") is None


def test_summary_line_meeting_id_is_a_valid_citation():
    s = store_with_meeting()
    p = OneShot(json.dumps({"found": True, "answer": "키보드 구매", "citations": ["m2"]}))
    a = answer_question(p, s, "키보드 구매 결정 요약")
    assert a.found and a.citations == ["m2"] and a.invalid_citations == []
