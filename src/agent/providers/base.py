"""모델 제공자 공통 인터페이스와 정규화 타입.

모델별 차이(JSON 모드, thinking, 도구 호출 표기, 오류 형식)는 각 Provider 안에서만 처리하고,
에이전트 쪽에는 항상 ModelResponse 형태로 돌려준다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "length", "tool_calls", "error", "unknown"]


class ToolCall(BaseModel):
    """정규화된 도구 호출. arguments는 아직 검증되지 않은 모델 출력이다."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None  # Provider가 준 호출 id (Claude의 tool_use id). 없으면 Provider가 만들어 쓴다


class ToolDefinition(BaseModel):
    """Provider에 넘기는 공통 도구 정의 (이름·설명·인자 JSON 스키마)."""

    name: str
    description: str
    parameters: dict[str, Any]


class Message(BaseModel):
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] | None = None  # role=assistant일 때 모델이 낸 도구 호출
    tool_name: str | None = None              # role=tool일 때 어떤 도구의 결과인지
    # role=user일 때 글 앞에 붙는 내용 블록(사진·PDF — Claude Messages API의 꼴). 글만 읽는 Provider는 쓰지 않는다(D73)
    # role=tool일 때는 도구 결과에 붙는 그림(작업실의 view_image — D96)
    blocks: list[dict[str, Any]] | None = None


class Usage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelResponse(BaseModel):
    text: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: FinishReason = "unknown"
    usage: Usage = Field(default_factory=Usage)
    latency_ms: float
    model: str
    provider: str


class ProviderError(Exception):
    """연결 실패, HTTP 오류, 응답 형식 오류 등 Provider 수준의 실패."""


class ModelProvider(ABC):
    name: str

    @abstractmethod
    def generate(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        """메시지를 보내고 정규화된 응답을 받는다.

        json_schema를 주면 Provider가 지원하는 방식으로 구조화 출력을 요청한다.
        tools를 주면 도구 호출을 요청하고, 결과는 항상 ModelResponse.tool_calls로 정규화한다.
        tools와 json_schema는 함께 쓸 수 없다.
        반환된 text·tool_calls가 스키마를 만족한다는 보장은 없다 — 호출자가 반드시 검증한다.
        """
