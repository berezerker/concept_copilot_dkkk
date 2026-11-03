"""Реестр коннекторов LLM."""
from ..config import env
from .base import LLMClient, LLMError


def available_providers():
    """Какие провайдеры настроены (для выпадающего списка в интерфейсе)."""
    out = [{"id": "mock", "title": "Офлайн-сценарий (без LLM)", "ready": True, "model": "offline-scripted"}]
    out.append({"id": "openai", "title": "OpenAI / совместимый API",
                "ready": bool(env("OPENAI_API_KEY") or (env("OPENAI_BASE_URL") and "api.openai.com" not in env("OPENAI_BASE_URL", ""))),
                "model": env("OPENAI_MODEL", "gpt-5.4")})
    out.append({"id": "gigachat", "title": "GigaChat",
                "ready": bool(env("GIGACHAT_CREDENTIALS") or env("GIGACHAT_ACCESS_TOKEN")),
                "model": env("GIGACHAT_MODEL", "GigaChat-2-Max")})
    return out


def make_client(provider: str, model: str | None = None) -> LLMClient:
    provider = (provider or "mock").lower()
    if provider == "openai":
        from .openai_client import OpenAIClient
        return OpenAIClient(model)
    if provider == "gigachat":
        from .gigachat_client import GigaChatClient
        return GigaChatClient(model)
    if provider == "mock":
        from .mock_client import MockClient
        return MockClient()
    raise LLMError(f"Неизвестный провайдер: {provider}")
