"""Claude API(공식 Anthropic SDK)를 호출하는 Provider. 비교 평가용(D34).

정규화: 다른 Provider와 같이 ModelResponse(text, tool_calls, finish_reason, usage, latency)로 돌려준다.
- 도구 호출: Messages API의 tools / tool_use / tool_result. 한 번에 하나만(disable_parallel_tool_use).
- 구조화 출력: output_config.format(json_schema).
- thinking: think=False면 가장 낮은 설정(Sonnet 5.5는 between_tools, Haiku 5.5는 disabled), True면 adaptive(기본값).
- temperature는 보내지 않는다(이 모델들은 기본값이 아닌 샘플링 값을 거부한다) — 호출자가 준 값은 무시하고 기록만 한다.
- 거절(stop_reason=refusal)은 다른 모델로 넘기지 않고 finish_reason="error"로 돌려준다(측정 대상이 바뀌면 안 된다).
API 키는 환경 변수 또는 저장소의 .env에서만 읽는다. 키를 출력·기록하지 않는다.
합성 데이터만 보낸다(D7, D34) — 실제 회의 데이터를 다루는 경로(memory import-txt)는 로컬 Qwen으로 고정되어 있다.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import anthropic

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

_SCHEMA_DROP = frozenset({"pattern", "minLength", "maxLength", "title", "default", "minimum", "maximum",
                          "minItems", "maxItems", "format"})
MAX_TOKENS = 16000


def load_env_file(path: Path | None = None) -> bool:
    """저장소의 .env에서 KEY=VALUE를 읽어 아직 없는 환경 변수만 채운다. 값은 어디에도 출력하지 않는다."""
    path = path or Path(__file__).resolve().parents[3] / ".env"
    if not path.is_file():
        return False
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v
    return True


def has_api_key() -> bool:
    load_env_file()
    return len(os.environ.get("ANTHROPIC_API_KEY", "")) >= 20


def strict_schema(schema: Any) -> Any:
    """구조화 출력용: 지원되지 않을 수 있는 제약 키워드를 빼고, 모든 객체에 additionalProperties=false를 넣는다."""
    if isinstance(schema, dict):
        out = {k: strict_schema(v) for k, v in schema.items() if k not in _SCHEMA_DROP or not isinstance(v, (str, int, float, bool))}
        if out.get("type") == "object" or "properties" in out:
            out["additionalProperties"] = False
            out.setdefault("required", list(out.get("properties", {})))
        return out
    if isinstance(schema, list):
        return [strict_schema(v) for v in schema]
    return schema


class ClaudeProvider(ModelProvider):
    name = "claude"

    def __init__(self, model: str = "claude-sonnet-5-5", think: bool = False, effort: str = "medium",
                 timeout: float = 120.0, client: Any = None, cache_system: bool = False, max_retries: int = 2):
        # cache_system: 캐시 지점을 시스템 지시문 끝에 둔다(지시문은 같고 뒤의 대화만 바뀌는 호출 — 대화 판단).
        #   끄면 요청 끝에 둔다(같은 대화를 이어 가는 호출 — 에이전트 루프)
        self.model, self.think, self.effort, self.cache_system = model, think, effort, cache_system
        if client is None:
            load_env_file()
            client = anthropic.Anthropic(timeout=timeout, max_retries=max_retries)
        self._client = client
        self._raw: dict[str, Any] = {}   # tool_use id → 그 응답의 원래 content (다음 요청에 그대로 되돌려 보낸다)
        self.totals = {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    # ---- 요청 만들기 ----
    def _thinking(self) -> dict | None:
        if self.think:
            return None                      # 생략 = adaptive
        if self.model.startswith("claude-sonnet-5-5"):
            return {"type": "between_tools"}  # 이 모델에서 thinking을 끄는 유일한 방법
        if self.model.startswith("claude-opus-5-5"):
            return None                      # 이 모델은 thinking을 끌 수 없다(끄면 400). 깊이는 effort로만 고른다
        return {"type": "disabled"}

    def _convert(self, messages: list[Message]) -> tuple[str, list[dict]]:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        out: list[dict] = []
        pending_id: str | None = None
        n = 0
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "assistant" and m.tool_calls:
                call = m.tool_calls[0]
                n += 1
                pending_id = call.id or f"toolu_local_{n:04d}"
                raw = self._raw.get(call.id) if call.id else None
                content = raw if raw is not None else [
                    {"type": "tool_use", "id": pending_id, "name": call.name, "input": call.arguments}]
                out.append({"role": "assistant", "content": content})
            elif m.role == "tool":
                if pending_id is None:  # 짝이 없는 도구 결과는 일반 텍스트로
                    out.append({"role": "user", "content": f"[{m.tool_name or '도구'} 결과]\n{m.content}"})
                else:
                    # 도구 결과에 그림이 붙을 수 있다(작업실의 view_image — D96): 블록을 먼저, 글을 뒤에
                    body = [*m.blocks, {"type": "text", "text": m.content or "(빈 결과)"}] if m.blocks else (m.content or "(빈 결과)")
                    out.append({"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": pending_id, "content": body}]})
                    pending_id = None
            else:
                if pending_id is not None:  # tool_use 뒤에 결과가 없으면 API가 거부한다 → 빈 결과를 채운다
                    out.append({"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": pending_id, "content": "(결과 없음)"}]})
                    pending_id = None
                if m.role == "user" and m.blocks:   # 사용자가 붙인 사진·PDF: 블록을 먼저, 글을 뒤에(D73)
                    out.append({"role": "user", "content": [*m.blocks, {"type": "text", "text": m.content or "(글 없이 보냈다)"}]})
                elif m.content:
                    out.append({"role": m.role, "content": m.content})
        return system, out

    def generate(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,   # 무시됨 (모듈 설명 참고)
        max_tokens: int | None = None,
    ) -> ModelResponse:
        if tools and json_schema is not None:
            raise ValueError("tools와 json_schema는 함께 쓸 수 없다")
        system, msgs = self._convert(messages)
        output_config: dict[str, Any] = {"effort": self.effort}
        kwargs: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens or MAX_TOKENS, "messages": msgs,
        }
        if self.cache_system and system:
            kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        else:
            kwargs["cache_control"] = {"type": "ephemeral"}
            if system:
                kwargs["system"] = system
        thinking = self._thinking()
        if thinking is not None:
            kwargs["thinking"] = thinking
        if tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                               for t in tools]
            kwargs["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": strict_schema(json_schema)}
        kwargs["output_config"] = output_config

        started = time.perf_counter()
        try:
            resp = self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise ProviderError("Claude 인증 실패: .env의 ANTHROPIC_API_KEY를 확인") from e
        except anthropic.RateLimitError as e:
            raise ProviderError("Claude 요청 한도 초과(429)") from e
        except anthropic.BadRequestError as e:
            raise ProviderError(f"Claude 요청 오류(400): {e.message[:300]}") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Claude API 오류({e.status_code}): {e.message[:200]}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"Claude 연결 실패: {type(e).__name__}") from e
        latency_ms = (time.perf_counter() - started) * 1000

        u = resp.usage
        self.totals["calls"] += 1
        self.totals["input"] += u.input_tokens or 0
        self.totals["output"] += u.output_tokens or 0
        self.totals["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        self.totals["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        calls = [ToolCall(name=b.name, arguments=dict(b.input) if isinstance(b.input, dict) else {"_raw": b.input},
                          id=b.id) for b in resp.content if b.type == "tool_use"]
        if calls:
            self._raw[calls[0].id] = resp.content
        finish: FinishReason
        if resp.stop_reason == "refusal":
            finish, text, calls = "error", "", []
        elif calls:
            finish = "tool_calls"
        elif resp.stop_reason == "max_tokens":
            finish = "length"
        elif resp.stop_reason == "end_turn":
            finish = "stop"
        else:
            finish = "unknown"
        return ModelResponse(
            text=text, tool_calls=calls, finish_reason=finish,
            usage=Usage(input_tokens=(u.input_tokens or 0) + self.totals_last_cache(u), output_tokens=u.output_tokens),
            latency_ms=latency_ms, model=resp.model, provider=self.name,
        )

    def search(self, system: str, question: str, *, max_uses: int = 2, max_tokens: int = 1500,
               tool: str = "web_search_20250305") -> tuple[str, list[tuple[str, str]], int, float]:
        """웹 검색 도구(Anthropic 서버에서 도는 도구)를 붙여 한 물음에 답하게 한다. (답 글, [(제목, 주소)], 검색 횟수, 걸린 ms).

        [(제목, 주소)]는 답에 붙은 인용이다. 인용이 하나도 없으면 비어 있고, 그때 쓸 수 있게 검색이 돌려준 페이지들을
        self.last_results에 둔다(답이 그 페이지들을 실제로 썼다는 보장은 없다).

        공개 정보 조회 전용(D60): 보내는 것은 system과 question뿐이다. 구조화 출력은 쓰지 않는다(인용과 함께 쓸 수 없다).
        도구는 기본 버전을 쓴다 — 측정(2026-11-13): 새 버전(20260209, 결과를 코드로 거르는 방식)은 같은 물음에 2~3배 느리고
        답에 인용(출처)이 붙지 않았다. 답은 마지막 검색 뒤에 쓴 글만 쓴다(검색 전에 한 말은 버린다).
        """
        kwargs: dict[str, Any] = {
            "model": self.model, "max_tokens": max_tokens,
            "system": system,   # 캐시 지점을 두지 않는다: 지시문은 짧고, 두면 검색 결과까지 캐시에 쓰여 더 비싸다(측정)
            "messages": [{"role": "user", "content": question}],
            "tools": [{"type": tool, "name": "web_search", "max_uses": max_uses}],
            "output_config": {"effort": self.effort},
        }
        thinking = self._thinking()
        if thinking is not None:
            kwargs["thinking"] = thinking
        started = time.perf_counter()
        parts: list[str] = []
        links: list[tuple[str, str]] = []
        found: list[tuple[str, str]] = []
        searches = 0
        for _ in range(3):   # pause_turn(서버 쪽 반복 한도)이면 같은 대화를 그대로 다시 보내 이어 간다 — 최대 두 번
            try:
                resp = self._client.messages.create(**kwargs)
            except anthropic.AuthenticationError as e:
                raise ProviderError("Claude 인증 실패: .env의 ANTHROPIC_API_KEY를 확인") from e
            except anthropic.RateLimitError as e:
                raise ProviderError("Claude 요청 한도 초과(429)") from e
            except anthropic.BadRequestError as e:
                raise ProviderError(f"Claude 요청 오류(400): {e.message[:300]}") from e
            except anthropic.APIStatusError as e:
                raise ProviderError(f"Claude API 오류({e.status_code}): {e.message[:200]}") from e
            except anthropic.APIConnectionError as e:
                raise ProviderError(f"Claude 연결 실패: {type(e).__name__}") from e
            u = resp.usage
            self.totals["calls"] += 1
            self.totals["input"] += u.input_tokens or 0
            self.totals["output"] += u.output_tokens or 0
            self.totals["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            self.totals["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
            searches += getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
            for b in resp.content:
                if b.type != "text" and b.type != "thinking":   # 도구 호출·결과: 그 앞의 글은 중간 말이다
                    parts, links = [], []
                    if b.type == "web_search_tool_result" and isinstance(getattr(b, "content", None), list):
                        for r in b.content:
                            url = getattr(r, "url", None)
                            if url and url not in [x[1] for x in found]:
                                found.append((getattr(r, "title", "") or "", url))
                elif b.type == "text":
                    parts.append(b.text)
                    for c in getattr(b, "citations", None) or []:
                        url = getattr(c, "url", None)
                        if url and url not in [x[1] for x in links]:
                            links.append((getattr(c, "title", "") or "", url))
            if resp.stop_reason != "pause_turn":
                break
            kwargs["messages"] = [{"role": "user", "content": question}, {"role": "assistant", "content": resp.content}]
        self.totals["searches"] = self.totals.get("searches", 0) + searches
        self.last_results = found
        text = "".join(parts).strip()
        if resp.stop_reason == "refusal" or not text:
            raise ProviderError("웹 검색 답을 받지 못했다")
        return text, links, searches, (time.perf_counter() - started) * 1000

    @staticmethod
    def totals_last_cache(u: Any) -> int:
        return (getattr(u, "cache_read_input_tokens", 0) or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)


# 표준 요금 ($/1M 토큰, 2026-11 기준 — 실제 청구는 콘솔에서 확인)
PRICES = {"claude-sonnet-5-5": (2.00, 10.00, 0.10), "claude-opus-5-5": (4.00, 20.00, 0.20), "claude-haiku-5-5": (0.10, 0.50, 0.01)}


SEARCH_USD = 0.01   # 웹 검색 1회($10 / 1,000회) — 검색 결과 글은 입력 토큰으로 따로 센다


def cost_usd(model: str, totals: dict) -> float:
    pin, pout, pread = PRICES[model]
    return (totals["input"] * pin + totals["cache_write"] * pin * 1.25 + totals["cache_read"] * pread
            + totals["output"] * pout) / 1e6 + totals.get("searches", 0) * SEARCH_USD
