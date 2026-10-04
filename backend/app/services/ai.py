"""AI assistant for chats: reply suggestions and conversation summaries.

Provider-agnostic: YandexGPT through Yandex AI Studio's OpenAI-compatible API (data stays in Russia),
any OpenAI-style endpoint, or Anthropic's Messages API. Chosen by LLM_PROVIDER in the environment.
The model only drafts: a person reads and sends every message.
"""
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("uvicorn.error.ai")
YANDEX_URL = "https://llm.api.cloud.yandex.net/v1"
PROVIDER_NAMES = {"yandex": "YandexGPT", "openai": "OpenAI", "anthropic": "Claude (Anthropic)", "openai_compatible": "LLM"}


class AIError(ValueError):
    pass


def provider() -> str:
    return (settings.llm_provider or "").strip().lower()


def configured() -> bool:
    name = provider()
    if name not in PROVIDER_NAMES or not settings.llm_api_key:
        return False
    if name == "yandex":
        return bool(settings.yandex_folder_id or settings.llm_model)
    if name in {"openai", "openai_compatible"}:
        return bool(settings.llm_model) and (name == "openai" or bool(settings.llm_base_url))
    return True


def provider_name() -> str | None:
    return PROVIDER_NAMES.get(provider()) if configured() else None


def normalize(messages: list[dict]) -> list[dict]:
    """Alternate user/assistant turns, starting and ending with the user (required by some providers)."""
    result: list[dict] = []
    for message in messages:
        text = (message.get("content") or "").strip()
        if not text:
            continue
        if result and result[-1]["role"] == message["role"]:
            result[-1]["content"] += "\n" + text
        else:
            result.append({"role": message["role"], "content": text})
    if not result or result[0]["role"] != "user":
        result.insert(0, {"role": "user", "content": "(начало диалога)"})
    if result[-1]["role"] != "user":
        result.append({"role": "user", "content": "(клиент пока не ответил)"})
    return result


async def complete(system: str, messages: list[dict], *, max_tokens: int = 500, temperature: float = 0.3) -> str:
    if not configured():
        raise AIError("ИИ-помощник не подключён: администратору нужно указать LLM_PROVIDER и ключ в настройках сервера")
    name, turns = provider(), normalize(messages)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            if name == "anthropic":
                response = await client.post("https://api.anthropic.com/v1/messages", headers={
                    "x-api-key": settings.llm_api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": settings.llm_model or "claude-sonnet-5-5", "max_tokens": max_tokens, "temperature": temperature,
                          "system": system, "messages": turns})
            else:
                headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
                if name == "yandex":
                    base = (settings.llm_base_url or YANDEX_URL).rstrip("/")
                    model = settings.llm_model or f"gpt://{settings.yandex_folder_id}/yandexgpt/latest"
                    if settings.yandex_folder_id:
                        headers["OpenAI-Project"] = settings.yandex_folder_id
                else:
                    base = (settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")
                    model = settings.llm_model
                response = await client.post(f"{base}/chat/completions", headers=headers, json={
                    "model": model, "max_tokens": max_tokens, "temperature": temperature,
                    "messages": [{"role": "system", "content": system}, *turns]})
    except httpx.HTTPError:
        raise AIError("Сервис ИИ недоступен, попробуйте ещё раз") from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        logger.warning("llm error status=%s body=%s", response.status_code, str(data)[:500])
        raise AIError("Сервис ИИ отклонил запрос" + (" — проверьте ключ" if response.status_code in {401, 403} else ""))
    try:
        text = data["content"][0]["text"] if name == "anthropic" else data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AIError("Сервис ИИ вернул пустой ответ") from None
    return (text or "").strip()


def transcript(messages, client_name: str) -> list[dict]:
    """Chat history as model turns: the client speaks as `user`, the company as `assistant`."""
    return [{"role": "user" if m.direction == "in" else "assistant", "content": m.text or ""}
            for m in messages if m.direction in {"in", "out"} and m.status != "failed"]


def suggest_prompt(project_name: str, channel: str, kb: dict, deal_line: str | None, limit: int) -> str:
    knowledge = (kb.get("knowledge") or "").strip() or "(база знаний не заполнена — не называй цен, сроков и условий)"
    return (f"Ты менеджер по продажам компании «{project_name}» и отвечаешь клиенту в {channel}.\n"
            f"Напиши следующий ответ клиенту от имени компании: по-русски, коротко (1–4 предложения, не больше {limit} символов), "
            "по-человечески, без канцелярита и без приветствия, если диалог уже идёт.\n"
            "Факты (цены, сроки, условия, адреса) бери ТОЛЬКО из базы знаний ниже. Если ответа там нет — не выдумывай, "
            "а предложи уточнить у специалиста или созвониться. Никогда не обещай скидок.\n"
            f"Цель диалога: {kb.get('goal') or 'выяснить потребность и договориться о звонке или встрече'}.\n"
            f"Тон: {kb.get('tone') or 'дружелюбный и уверенный, на «вы»'}.\n"
            + (f"Данные сделки: {deal_line}.\n" if deal_line else "")
            + f"\nБаза знаний:\n{knowledge[:8000]}\n\nВерни только текст сообщения, без кавычек и пояснений.")


SUMMARY_PROMPT = ("Ты помощник менеджера по продажам. Кратко перескажи переписку с клиентом для CRM: "
                  "3–6 пунктов — что хочет клиент; параметры (размеры, бюджет, сроки, адрес), если названы; "
                  "о чём договорились; возражения; следующий шаг. Только факты из переписки, по-русски, без вступления.")
