"""환경 변수 기반 설정. MODEL_PROVIDER는 SANDBOX_BACKEND·AGENT_MODE와 독립이다."""

from __future__ import annotations

import os

from .providers.base import ModelProvider


def get_provider(name: str | None = None) -> ModelProvider:
    name = name or os.environ.get("MODEL_PROVIDER", "qwen")
    if name == "qwen":
        from .providers.qwen import QwenProvider

        return QwenProvider(
            model=os.environ.get("QWEN_MODEL", "qwen3:8b"),
            host=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
            timeout=float(os.environ.get("MODEL_TIMEOUT", "120")),
            think=os.environ.get("QWEN_THINK", "0") == "1",
            tool_mode=os.environ.get("QWEN_TOOL_MODE", "json"),  # type: ignore[arg-type]
        )
    if name == "claude":
        from .providers.claude import ClaudeProvider

        return ClaudeProvider(model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"))
    raise ValueError(f"지원하지 않는 MODEL_PROVIDER: {name!r} ('qwen' 또는 'claude')")
