"""도구 명세와 등록부. 모델은 여기 등록된 도구만 고를 수 있다."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from ..providers.base import ToolDefinition


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]   # 인자 스키마이자 검증기 (extra="forbid" 권장)
    timeout_s: float = 30.0       # 실행 단계(Phase 3)에서 사용

    def definition(self) -> ToolDefinition:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return ToolDefinition(name=self.name, description=self.description, parameters=schema)


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec]):
        names = [s.name for s in specs]
        if len(names) != len(set(names)):
            raise ValueError(f"도구 이름 중복: {names}")
        self._specs = {s.name: s for s in specs}

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def definitions(self) -> list[ToolDefinition]:
        return [s.definition() for s in self._specs.values()]
