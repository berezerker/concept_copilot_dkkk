"""Общий интерфейс коннекторов LLM.

Внутренний формат сообщений — как в OpenAI Chat Completions:
  {"role": "system"|"user"|"assistant"|"tool", "content": str,
   "tool_calls": [{"id", "type": "function", "function": {"name", "arguments": json-строка}}],
   "tool_call_id": str, "name": str}
Служебные поля с префиксом "_" (например "_functions_state_id" для GigaChat) коннекторы,
которым они не нужны, отбрасывают.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list = field(default_factory=list)   # list[ToolCall]
    extra: dict = field(default_factory=dict)        # служебное (functions_state_id и т.п.)
    usage: dict = field(default_factory=dict)


class LLMError(RuntimeError):
    pass


class LLMClient:
    name = "base"
    max_tool_chars = 12000          # сколько символов результата инструмента отдаём модели
    parallel_tools = True

    def chat(self, messages: list, tools: list, tool_choice: str | dict = "auto") -> LLMResponse:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name
