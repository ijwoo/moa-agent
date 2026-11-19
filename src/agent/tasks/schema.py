"""한국어 명령을 변환한 구조화 작업 요청(TaskRequest)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Intent = Literal[
    "check_email",      # 메일 도착·내용 확인
    "draft_email",      # 메일 초안 작성 (발송 안 함)
    "send_email",       # 메일 발송
    "delete_email",     # 메일 삭제
    "web_search",       # 웹 검색
    "web_extract",      # 특정 페이지에서 정보 확인·추출
    "submit_form",      # 웹 양식 제출·예약·신청
    "purchase",         # 결제·구매
    "recall_memory",    # 과거 회의·결정·할 일 기억 조회
    "create_todo",      # 할 일 등록
    "unknown",          # 위에 해당하지 않거나 이해할 수 없음
]

# 모델 출력과 무관하게 항상 승인이 필요한 의도 (발송·삭제·제출·결제)
SENSITIVE_INTENTS: frozenset[str] = frozenset(
    {"send_email", "delete_email", "submit_form", "purchase"}
)


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent = Field(description="명령의 의도")
    target: str | None = Field(
        description="작업 대상 (사람, 사이트, 자료, 메일 등). 없으면 null"
    )
    time_ref: str | None = Field(
        description="명령에 나온 시간 표현 원문 (예: '지난 회의', '내일 오전 10시'). 없으면 null"
    )
    details: str = Field(description="작업 내용 한 문장 요약")
    needs_approval: bool = Field(
        description="발송·삭제·제출·결제처럼 되돌리기 어려운 작업이면 true"
    )
    needs_clarification: bool = Field(
        description="필수 정보가 빠져 바로 실행할 수 없으면 true"
    )


def apply_approval_policy(task: TaskRequest) -> TaskRequest:
    """모델 출력은 신뢰하지 않는다: 민감 의도면 needs_approval을 강제로 True로 만든다.

    모델이 true라고 한 경우는 그대로 둔다(더 보수적인 쪽은 허용).
    """
    if task.intent in SENSITIVE_INTENTS and not task.needs_approval:
        return task.model_copy(update={"needs_approval": True})
    return task
