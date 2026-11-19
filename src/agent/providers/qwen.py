"""Ollama HTTP API(/api/chat)로 Qwen을 호출하는 Provider.

도구 호출 방식 두 가지 (Phase 2에서 측정으로 비교):
- native: Ollama `tools` 파라미터. 모델 템플릿의 도구 호출 형식을 Ollama가 파싱해 tool_calls로 준다.
- json:   도구 목록을 프롬프트에 쓰고, `format`에 {"tool", "arguments"} anyOf 스키마를 걸어
          도구 하나를 반드시 고르게 한다. 결과는 tool_calls로 변환해 돌려준다.
어느 방식이든 호출자는 ModelResponse.tool_calls만 본다.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Literal

import httpx

from .base import (
    FinishReason,
    Message,
    ModelProvider,
    ModelResponse,
    ProviderError,
    ToolCall,
    ToolDefinition,
    Usage,
)

ToolMode = Literal["native", "json"]

_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

JSON_TOOL_INSTRUCTION = """\

## 도구 호출 형식
아래 도구 중 정확히 하나를 골라 {"tool": "<도구 이름>", "arguments": {...}} JSON 객체 하나만 출력한다.
사용 가능한 도구:
"""


def _normalize_finish(done_reason: str | None, has_tool_calls: bool) -> FinishReason:
    if has_tool_calls:
        return "tool_calls"
    if done_reason in ("stop", "length"):
        return done_reason  # type: ignore[return-value]
    return "unknown"


def _parse_tool_calls(raw: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in raw or []:
        fn = (item or {}).get("function") or {}
        name = fn.get("name")
        if not isinstance(name, str):
            continue
        args = fn.get("arguments") or {}
        if isinstance(args, str):  # 일부 모델·버전은 문자열 JSON으로 준다
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {"_raw": args}
        calls.append(ToolCall(name=name, arguments=args))
    return calls


# Ollama 0.24의 JSON 스키마→문법 변환이 처리하지 못하는 키워드 (실측):
#   pattern의 \d → "unknown escape", maxLength 2048·5000 → "number of repetitions exceeds sane defaults"
# 이 제약은 도구 검증(validate_call)에서 그대로 강제하므로 format 스키마에서만 뺀다.
_GRAMMAR_UNSAFE_KEYS = frozenset({"pattern", "maxLength", "minLength"})


def grammar_safe(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: grammar_safe(v) for k, v in schema.items() if k not in _GRAMMAR_UNSAFE_KEYS}
    if isinstance(schema, list):
        return [grammar_safe(v) for v in schema]
    return schema


def json_tool_schema(tools: list[ToolDefinition]) -> dict[str, Any]:
    return {
        "anyOf": [
            {
                "type": "object",
                "properties": {"tool": {"const": t.name}, "arguments": grammar_safe(t.parameters)},
                "required": ["tool", "arguments"],
                "additionalProperties": False,
            }
            for t in tools
        ]
    }


def _json_tool_listing(tools: list[ToolDefinition]) -> str:
    lines = []
    for t in tools:
        params = json.dumps(t.parameters.get("properties", {}), ensure_ascii=False)
        lines.append(f"- {t.name}: {t.description}\n  인자: {params}")
    return "\n".join(lines)


def _to_json_mode_messages(messages: list[Message], tools: list[ToolDefinition]) -> list[dict]:
    """도구 호출·결과 메시지를 json 모드용 일반 텍스트 메시지로 바꾼다."""
    out: list[dict] = []
    listing = JSON_TOOL_INSTRUCTION + _json_tool_listing(tools)
    has_system = any(m.role == "system" for m in messages)
    if not has_system:
        out.append({"role": "system", "content": listing.lstrip()})
    for m in messages:
        if m.role == "system":
            out.append({"role": "system", "content": m.content + "\n" + listing})
        elif m.role == "assistant" and m.tool_calls:
            call = m.tool_calls[0]
            out.append({
                "role": "assistant",
                "content": json.dumps({"tool": call.name, "arguments": call.arguments}, ensure_ascii=False),
            })
        elif m.role == "tool":
            out.append({"role": "user", "content": f"[{m.tool_name or '도구'} 결과]\n{m.content}"})
        else:
            out.append({"role": m.role, "content": m.content})
    return out


def _to_native_messages(messages: list[Message]) -> list[dict]:
    out = []
    for m in messages:
        d: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            d["tool_calls"] = [
                {"function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls
            ]
        if m.tool_name:
            d["tool_name"] = m.tool_name
        out.append(d)
    return out


def _parse_json_tool_output(text: str) -> list[ToolCall]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict) or not isinstance(data.get("tool"), str):
        return []
    args = data.get("arguments")
    return [ToolCall(name=data["tool"], arguments=args if isinstance(args, dict) else {"_raw": args})]


class QwenProvider(ModelProvider):
    name = "qwen-ollama"

    def __init__(
        self,
        model: str = "qwen3:8b",
        host: str = "http://127.0.0.1:11434",
        timeout: float = 120.0,
        think: bool = False,
        tool_mode: ToolMode = "native",
        num_ctx: int | None = None,
        default_max_tokens: int | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if tool_mode not in ("native", "json"):
            raise ValueError(f"알 수 없는 tool_mode: {tool_mode!r}")
        self.model = model
        self.think = think
        self.tool_mode = tool_mode
        self.num_ctx = num_ctx  # None이면 Ollama 기본값(0.24: 4096)
        # 호출 측이 max_tokens를 안 주면 쓰는 상한. thinking이 끝없이 길어져 타임아웃 나는 것을 막는다 (Phase 4 실측).
        self.default_max_tokens = default_max_tokens
        self._client = httpx.Client(base_url=host, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def generate(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        if tools and json_schema is not None:
            raise ValueError("tools와 json_schema는 함께 쓸 수 없다")
        options: dict[str, Any] = {"temperature": temperature}
        if max_tokens is None:
            max_tokens = self.default_max_tokens
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "think": self.think,
            "options": options,
        }
        json_tools = bool(tools) and self.tool_mode == "json"
        if json_tools:
            payload["messages"] = _to_json_mode_messages(messages, tools)  # type: ignore[arg-type]
            payload["format"] = json_tool_schema(tools)  # type: ignore[arg-type]
        else:
            payload["messages"] = _to_native_messages(messages)
            if tools:
                payload["tools"] = [
                    {"type": "function", "function": t.model_dump()} for t in tools
                ]
            if json_schema is not None:
                payload["format"] = json_schema

        started = time.perf_counter()
        try:
            resp = self._client.post("/api/chat", json=payload)
        except httpx.HTTPError as e:
            raise ProviderError(f"Ollama 연결 실패: {e}") from e
        latency_ms = (time.perf_counter() - started) * 1000

        if resp.status_code != 200:
            try:
                detail = resp.json().get("error", resp.text)
            except ValueError:
                detail = resp.text
            raise ProviderError(f"Ollama HTTP {resp.status_code}: {detail}")

        try:
            data = resp.json()
        except ValueError as e:
            raise ProviderError("Ollama 응답이 JSON이 아님") from e

        message = data.get("message")
        if not isinstance(message, dict):
            raise ProviderError("Ollama 응답에 message가 없음")

        text = message.get("content") or ""
        # think=False여도 모델·버전에 따라 <think> 블록이 본문에 섞일 수 있어 제거한다.
        text = _THINK_BLOCK.sub("", text).strip()
        if json_tools:
            tool_calls = _parse_json_tool_output(text)
        else:
            tool_calls = _parse_tool_calls(message.get("tool_calls"))

        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            finish_reason=_normalize_finish(data.get("done_reason"), bool(tool_calls)),
            usage=Usage(
                input_tokens=data.get("prompt_eval_count"),
                output_tokens=data.get("eval_count"),
            ),
            latency_ms=latency_ms,
            model=data.get("model", self.model),
            provider=self.name,
        )
