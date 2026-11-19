"""기억(회의) 데이터 구조. iOS 앱의 저장 구조(회의·발화·요약·결정·미해결 질문·할 일)와 맞춘다.

모든 결정·할 일·질문은 근거 발화 번호(sources, 예: "m1-u3")를 갖는다 — 답의 근거를 추적하기 위해서다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ItemKind = Literal["decision", "todo", "question"]


class Utterance(BaseModel):
    id: str                      # "<회의 id>-u<번호>" (1부터)
    speaker: str | None = None
    text: str
    start_s: float | None = None
    end_s: float | None = None


class Item(BaseModel):
    """결정·할 일·미해결 질문."""

    kind: ItemKind
    text: str
    sources: list[str] = Field(default_factory=list)   # 근거 발화 id
    assignee: str | None = None      # 할 일: 담당자
    due_text: str | None = None      # 할 일: 기한 문구 원문 ("11월 7일까지")
    done: bool = False


class Meeting(BaseModel):
    id: str
    title: str
    date: str                        # YYYY-MM-DD
    utterances: list[Utterance]
    topic: str = ""
    summary: str = ""
    items: list[Item] = Field(default_factory=list)


# ---- 모델 구조화 출력용 (추출) ----

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractedDecision(_Strict):
    text: str = Field(description="결정된 내용 한 문장")
    sources: list[int] = Field(description="근거 발화 번호 목록 (예: [3])")


class ExtractedTodo(_Strict):
    text: str = Field(description="할 일 한 문장")
    assignee: str | None = Field(description="담당자 (발화자 이름 그대로). 없으면 null")
    due_text: str | None = Field(description="기한 표현 원문. 없으면 null")
    sources: list[int] = Field(description="근거 발화 번호 목록")


class ExtractedQuestion(_Strict):
    text: str = Field(description="아직 정해지지 않았거나 답을 기다리는 사항 한 문장")
    sources: list[int] = Field(description="근거 발화 번호 목록")


class ExtractedMeeting(_Strict):
    topic: str = Field(description="회의 주제 한 구절")
    summary: str = Field(description="회의 요약 2~3문장")
    decisions: list[ExtractedDecision]
    todos: list[ExtractedTodo]
    open_questions: list[ExtractedQuestion]


class Hit(BaseModel):
    """검색 결과 한 건."""

    kind: Literal["utterance", "decision", "todo", "question", "summary"]
    text: str
    meeting_id: str
    meeting_title: str
    date: str
    sources: list[str]
    score: float
    speaker: str | None = None
    assignee: str | None = None
