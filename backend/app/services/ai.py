"""AI assistant for chats: reply suggestions and conversation summaries.

Provider-agnostic: YandexGPT through Yandex AI Studio's OpenAI-compatible API (data stays in Russia),
any OpenAI-style endpoint, or Anthropic's Messages API. Chosen by LLM_PROVIDER in the environment.
The model only drafts: a person reads and sends every message.
"""
import logging
import asyncio
import time

import httpx

from app.core.config import settings

logger = logging.getLogger("uvicorn.error.ai")
YANDEX_URL = "https://llm.api.cloud.yandex.net/v1"
PROVIDER_NAMES = {"yandex": "YandexGPT", "openai": "OpenAI", "anthropic": "Claude (Anthropic)", "openai_compatible": "LLM"}


class AIError(ValueError):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status, self.code = status, code


def model_for(feature="chat"):
    return (getattr(settings, f"llm_model_{feature}", "") or settings.llm_model or
            (f"gpt://{settings.yandex_folder_id}/yandexgpt/latest" if provider() == "yandex" and settings.yandex_folder_id else
             "claude-sonnet-5-5" if provider() == "anthropic" else ""))


def provider() -> str:
    return (settings.llm_provider or "").strip().lower()


def configured(feature="chat") -> bool:
    name = provider()
    if name not in PROVIDER_NAMES or not settings.llm_api_key:
        return False
    if name == "yandex":
        return bool(model_for(feature))
    if name in {"openai", "openai_compatible"}:
        return bool(model_for(feature)) and (name == "openai" or bool(settings.llm_base_url))
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


def trim_input(system, messages):
    limit = max(1, settings.llm_max_input_chars)
    # Keep instructions and the most recent turns; no mutation of caller-owned history.
    system = system[:limit // 2] if messages and len(system) >= limit else system[:limit]
    remaining = limit - len(system)
    turns = []
    for message in reversed(messages):
        content = str(message.get("content") or "")[-remaining:] if remaining else ""
        if content:
            turns.append({"role": message["role"], "content": content})
            remaining -= len(content)
    return system, list(reversed(turns))


def failure(status):
    if status == 401:
        return "ИИ-сервис отклонил API-ключ: проверьте ключ подключения"
    if status == 403:
        return "У API-ключа нет доступа к выбранной модели"
    if status == 429:
        return "ИИ-сервис исчерпал лимит запросов или доступный баланс"
    if status >= 500:
        return "Провайдер ИИ временно недоступен, попробуйте позже"
    return "ИИ-сервис отклонил запрос: проверьте модель и параметры подключения"


def output_budget(model, feature, requested):
    # Gemini includes hidden thinking in its completion budget. A small visible
    # reply can therefore need considerably more tokens than its text alone.
    if "gemini" in model.lower():
        return max(requested, {"chat": 1024, "summary": 2048, "calls": 4096, "campaigns": 1024}.get(feature, 1024))
    return requested


async def complete(system: str, messages: list[dict], *, max_tokens=500, temperature=0.3,
                   feature="chat", workspace_id=None, api_key=None, base_url=None, model=None,
                   provider_override=None, endpoint=None, normalize_turns=True, validator=None) -> str:
    from app.services import ai_usage
    name = provider_override or provider()
    key, chosen = api_key or settings.llm_api_key, model or model_for(feature)
    base = (base_url or settings.llm_base_url or (YANDEX_URL if name == "yandex" else "https://api.openai.com/v1")).rstrip("/")
    if name not in PROVIDER_NAMES or not key or not chosen:
        raise AIError("ИИ не подключён: укажите провайдера, модель и API-ключ")
    if name == "openai_compatible" and not (base_url or settings.llm_base_url or endpoint):
        raise AIError("ИИ не подключён: укажите адрес API")
    max_tokens = output_budget(chosen, feature, max_tokens)
    turns = normalize(messages) if normalize_turns else messages
    system, turns = trim_input(system, turns)
    input_tokens = ai_usage.estimate(system + "".join(m["content"] for m in turns))
    for attempt in range(2):
        usage_id = await ai_usage.reserve(feature, name, chosen, workspace_id, input_tokens + max_tokens)
        started = time.monotonic()
        error = None; data = {}; result = ""; tin = input_tokens; tout = 0; retry = False
        try:
            async with httpx.AsyncClient(timeout=max(1, settings.llm_timeout)) as client:
                if name == "anthropic":
                    response = await client.post(endpoint or "https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                        json={"model": chosen, "max_tokens": max_tokens, "temperature": temperature, "system": system, "messages": turns})
                else:
                    headers = {"Authorization": f"Bearer {key}"}
                    if name == "yandex" and settings.yandex_folder_id:
                        headers["OpenAI-Project"] = settings.yandex_folder_id
                    payload = {"model": chosen, "messages": [{"role": "system", "content": system}, *turns],
                               "max_completion_tokens" if feature == "campaigns" else "max_tokens": max_tokens}
                    if feature != "campaigns":
                        payload["temperature"] = temperature
                    response = await client.post(endpoint or f"{base}/chat/completions", headers=headers, json=payload)
            if response.status_code >= 400:
                retry = response.status_code == 429 or response.status_code >= 500
                raise AIError(failure(response.status_code), response.status_code)
            data = response.json()
            usage = data.get("usage") or {}
            tin = int(usage.get("prompt_tokens", usage.get("input_tokens", input_tokens)))
            result = (data["content"][0]["text"] if name == "anthropic" else data["choices"][0]["message"]["content"])
            tout = int(usage.get("completion_tokens", usage.get("output_tokens", ai_usage.estimate(result or ""))))
            stop = data.get("stop_reason") if name == "anthropic" else data["choices"][0].get("finish_reason")
            if stop in {"length", "max_tokens", "MAX_TOKENS"}:
                retry = True
                max_tokens = min(max_tokens * 2, 8192)
                raise AIError("ИИ оборвал ответ из-за лимита генерации. Повторите запрос или выберите другую модель", code="truncated")
            if not isinstance(result, str) or not result.strip():
                raise AIError("ИИ вернул пустой ответ")
            if validator:
                validator(result)
        except httpx.TimeoutException:
            retry = True; error = AIError("ИИ не ответил вовремя, попробуйте ещё раз")
        except httpx.HTTPError:
            error = AIError("Провайдер ИИ недоступен, проверьте подключение")
        except asyncio.CancelledError:
            error = AIError("Запрос ИИ прерван")
            raise
        except AIError as exc:
            error = exc
        except (ValueError, KeyError, IndexError, TypeError):
            error = AIError("ИИ вернул некорректный ответ")
        finally:
            await ai_usage.finish(usage_id, tokens_in=tin, tokens_out=tout,
                                  duration_ms=int((time.monotonic() - started) * 1000), error=str(error) if error else None)
        if error is None:
            return result.strip()
        if retry and attempt == 0:
            await asyncio.sleep(1)
            continue
        raise error


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
