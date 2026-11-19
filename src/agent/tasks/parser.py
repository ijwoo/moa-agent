"""한국어 명령 → TaskRequest 변환."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import ValidationError

from ..providers.base import Message, ModelProvider, ModelResponse
from .schema import TaskRequest, apply_approval_policy

SYSTEM_PROMPT = """\
너는 사용자의 한국어 명령을 구조화된 작업 요청 JSON으로 바꾸는 변환기다.
명령을 실행하지 말고, 지정된 JSON 스키마에 맞는 객체 하나만 출력한다.

intent 값:
- check_email: 메일이 왔는지, 메일 내용이 무엇인지 확인
- draft_email: 메일 초안만 작성 (보내지 않음)
- send_email: 메일을 실제로 보냄
- delete_email: 메일을 삭제
- web_search: 웹에서 검색
- web_extract: 특정 웹페이지·사이트에 들어가 정보를 확인하거나 가져옴
- submit_form: 웹에서 양식 제출, 예약, 신청
- purchase: 결제나 구매
- recall_memory: 지난 회의, 결정, 할 일 등 과거 기록을 조회
- create_todo: 할 일을 새로 등록
- unknown: 위 어느 것에도 해당하지 않음

규칙:
- target: 작업 대상(사람, 사이트, 자료 등)을 명령에 나온 말 그대로 짧게. 없으면 null.
- time_ref: 명령에 나온 시간 표현을 원문 그대로. 없으면 null. 날짜를 계산하지 않는다.
- details: 무엇을 해야 하는지 한 문장으로 요약.
- needs_approval: 메일 발송·삭제, 양식 제출·예약·신청, 결제·구매면 true. 조회·검색·초안·할 일 등록은 false.
- needs_clarification: 대상이나 내용이 빠져 무엇을 해야 할지 정할 수 없으면 true.
"""


@dataclass
class ParseResult:
    task: TaskRequest | None        # 정책 적용 후 결과 (실패 시 None)
    raw_task: TaskRequest | None    # 정책 적용 전 모델 출력 (평가용)
    response: ModelResponse | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.task is not None


def build_messages(command: str) -> list[Message]:
    return [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content=command),
    ]


def parse_command(provider: ModelProvider, command: str) -> ParseResult:
    response = provider.generate(
        build_messages(command),
        json_schema=TaskRequest.model_json_schema(),
        temperature=0.0,
    )
    try:
        data = json.loads(response.text)
    except json.JSONDecodeError as e:
        return ParseResult(None, None, response, f"JSON 파싱 실패: {e}")
    try:
        raw_task = TaskRequest.model_validate(data)
    except ValidationError as e:
        return ParseResult(None, None, response, f"스키마 검증 실패: {e.error_count()}개 오류")
    return ParseResult(apply_approval_policy(raw_task), raw_task, response)
