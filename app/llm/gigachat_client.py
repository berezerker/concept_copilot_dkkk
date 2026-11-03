"""Коннектор к GigaChat API (нативный формат function calling).

Авторизация:
  * GIGACHAT_CREDENTIALS — ключ авторизации (Base64 client_id:client_secret) → OAuth-токен
    на 30 минут, обновляется автоматически;
  * или GIGACHAT_ACCESS_TOKEN — готовый токен (например, выданный внутренним шлюзом).
Сертификаты: GIGACHAT_CA_BUNDLE — путь к сертификату НУЦ Минцифры (russian_trusted_root_ca.cer),
или GIGACHAT_VERIFY_SSL=false (только для локальных экспериментов).

Формат функций: в запросе "functions" и "function_call" ("auto" | {"name": ...}),
в ответе message.function_call {name, arguments(объект)} и functions_state_id,
результат возвращается сообщением с ролью "function".
"""
from __future__ import annotations

import json
import time
import uuid

import httpx

from ..config import env, env_bool
from .base import LLMClient, LLMError, LLMResponse, ToolCall


class GigaChatClient(LLMClient):
    name = "gigachat"
    parallel_tools = False

    def __init__(self, model: str | None = None):
        self.base_url = env("GIGACHAT_BASE_URL", "https://gigachat.devices.sberbank.ru/api/v1").rstrip("/")
        self.auth_url = env("GIGACHAT_AUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth")
        self.scope = env("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
        self.credentials = env("GIGACHAT_CREDENTIALS")
        self.static_token = env("GIGACHAT_ACCESS_TOKEN")
        self.model = model or env("GIGACHAT_MODEL", "GigaChat-2-Max")
        self.temperature = env("GIGACHAT_TEMPERATURE")
        self.max_tool_chars = int(env("GIGACHAT_MAX_TOOL_CHARS", "8000"))
        verify = env("GIGACHAT_CA_BUNDLE") or env_bool("GIGACHAT_VERIFY_SSL", True)
        self.http = httpx.Client(timeout=float(env("GIGACHAT_TIMEOUT", "180")), verify=verify)
        self._token, self._exp = None, 0
        if not (self.credentials or self.static_token):
            raise LLMError("Не задан GIGACHAT_CREDENTIALS или GIGACHAT_ACCESS_TOKEN")

    def describe(self):
        return f"GigaChat · {self.model}"

    def _get_token(self):
        if self.static_token:
            return self.static_token
        if self._token and time.time() < self._exp - 60:
            return self._token
        try:
            r = self.http.post(self.auth_url, data={"scope": self.scope}, headers={
                "Authorization": f"Basic {self.credentials}", "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        except httpx.HTTPError as e:
            raise LLMError(f"GigaChat OAuth, сеть: {e}") from e
        if r.status_code >= 400:
            raise LLMError(f"GigaChat OAuth {r.status_code}: {r.text[:300]}")
        d = r.json()
        self._token = d["access_token"]
        exp = d.get("expires_at", 0)
        self._exp = exp / 1000 if exp > 1e11 else (exp or time.time() + 1500)
        return self._token

    @staticmethod
    def _convert(messages):
        out, names = [], {}
        for m in messages:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                tc = m["tool_calls"][0]                       # GigaChat: один вызов на сообщение
                names[tc["id"]] = tc["function"]["name"]
                mm = {"role": "assistant", "content": m.get("content") or "",
                      "function_call": {"name": tc["function"]["name"],
                                        "arguments": json.loads(tc["function"]["arguments"] or "{}")}}
                if m.get("_functions_state_id"):
                    mm["functions_state_id"] = m["_functions_state_id"]
                out.append(mm)
            elif role == "tool":
                content = m.get("content") or "{}"
                try:
                    json.loads(content)
                except Exception:
                    content = json.dumps({"result": content}, ensure_ascii=False)
                out.append({"role": "function", "name": m.get("name") or names.get(m.get("tool_call_id"), ""),
                            "content": content})
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return out

    def chat(self, messages, tools, tool_choice="auto"):
        body = {"model": self.model, "messages": self._convert(messages)}
        if tools:
            body["functions"] = tools
            body["function_call"] = {"name": tool_choice["name"]} if isinstance(tool_choice, dict) else tool_choice
        if self.temperature is not None:
            body["temperature"] = float(self.temperature)
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._get_token()}", "Content-Type": "application/json",
                       "Accept": "application/json"}
            try:
                r = self.http.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            except httpx.HTTPError as e:
                raise LLMError(f"GigaChat, сеть: {e}") from e
            if r.status_code == 401 and attempt == 1 and not self.static_token:
                self._token = None                            # токен истёк — получаем новый
                continue
            break
        if r.status_code >= 400:
            raise LLMError(f"GigaChat {r.status_code}: {r.text[:500]}")
        data = r.json()
        ch = data["choices"][0]
        msg = ch["message"]
        calls = []
        fc = msg.get("function_call")
        if fc:
            args = fc.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            calls.append(ToolCall(id="gc_" + uuid.uuid4().hex[:10], name=fc.get("name"), arguments=args))
        return LLMResponse(content=msg.get("content") or "", tool_calls=calls,
                           extra={"functions_state_id": msg.get("functions_state_id")},
                           usage=data.get("usage", {}))
