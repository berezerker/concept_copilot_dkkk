"""Коннектор к OpenAI Chat Completions и любым совместимым API
(Azure/OpenAI-прокси, vLLM, Ollama, LM Studio, OpenAI-совместимый режим GigaChat)."""
from __future__ import annotations

import json
import uuid

import httpx

from ..config import env, env_bool
from .base import LLMClient, LLMError, LLMResponse, ToolCall


class OpenAIClient(LLMClient):
    name = "openai"

    def __init__(self, model: str | None = None):
        self.base_url = env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.api_key = env("OPENAI_API_KEY")
        self.model = model or env("OPENAI_MODEL", "gpt-5.4")
        self.temperature = env("OPENAI_TEMPERATURE")
        self.max_tool_chars = int(env("OPENAI_MAX_TOOL_CHARS", "16000"))
        verify = env("OPENAI_CA_BUNDLE") or env_bool("OPENAI_VERIFY_SSL", True)
        self.http = httpx.Client(timeout=float(env("OPENAI_TIMEOUT", "180")), verify=verify)
        if not self.api_key and "api.openai.com" in self.base_url:
            raise LLMError("Не задан OPENAI_API_KEY")

    def describe(self):
        return f"OpenAI · {self.model}"

    @staticmethod
    def _clean(messages):
        out = []
        for m in messages:
            mm = {k: v for k, v in m.items() if not k.startswith("_")}
            if mm.get("role") == "tool":
                mm.pop("name", None)
            out.append(mm)
        return out

    def chat(self, messages, tools, tool_choice="auto"):
        body = {"model": self.model, "messages": self._clean(messages)}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
            body["tool_choice"] = ({"type": "function", "function": {"name": tool_choice["name"]}}
                                   if isinstance(tool_choice, dict) else tool_choice)
        if self.temperature is not None:
            body["temperature"] = float(self.temperature)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            r = self.http.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise LLMError(f"Сеть: {e}") from e
        if r.status_code >= 400:
            raise LLMError(f"OpenAI {r.status_code}: {r.text[:500]}")
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            calls.append(ToolCall(id=tc.get("id") or uuid.uuid4().hex[:12], name=fn.get("name"), arguments=args))
        return LLMResponse(content=msg.get("content") or "", tool_calls=calls, usage=data.get("usage", {}))
