from __future__ import annotations

import asyncio
import json
import os
import re
from html import escape
from datetime import datetime
from typing import Any

from google import genai


_GENAI_CLIENT: genai.Client | None = None


def clean_telegram_text(text: str) -> str:
    """Remove common Markdown artifacts and return HTML-safe plain text for Telegram."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    # Markdown headings: ### Title -> Title
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    # Markdown bullets: -, *, + -> Unicode bullet.
    text = re.sub(r"(?m)^\s*[-*+]\s+", "• ", text)
    # Remove bold/italic/strike/code markers while keeping their content.
    text = re.sub(r"(\*\*|__|~~|`)", "", text)
    text = re.sub(r"(?<!\*)\*(?!\*)", "", text)
    text = re.sub(r"(?<!_)_(?!_)", "", text)
    # Remove fenced-code markers if the model emitted them unexpectedly.
    text = re.sub(r"(?m)^\s*```(?:[A-Za-z0-9_+-]+)?\s*$", "", text)
    # Keep Telegram HTML parse mode safe: Gemini output must not create HTML tags.
    text = escape(text, quote=False)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or "Не удалось сформировать резюме."

def _client() -> genai.Client:
    global _GENAI_CLIENT

    if _GENAI_CLIENT is not None:
        return _GENAI_CLIENT

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    _GENAI_CLIENT = genai.Client(api_key=api_key)
    return _GENAI_CLIENT


def _model() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-3.5-flash")


async def summarize_messages(messages: list[dict[str, Any]]) -> str:
    transcript = "\n".join(
        f"{m.get('display_name') or m.get('username') or 'Участник'}: {m['text']}"
        for m in messages
    )
    prompt = f"""
Ты помощник командного планировщика. Сожми обсуждение в краткое, полезное резюме на русском языке.
Верни обычный текст без Markdown-разметки, без звёздочек и решёток.
Структура: Итог; Решения; Задачи; Открытые вопросы.
Если какого-то пункта нет, пропусти его.

Обсуждение:
{transcript}
"""
    response = await asyncio.to_thread(
        _client().models.generate_content,
        model=_model(),
        contents=prompt,
    )
    return clean_telegram_text(response.text or "Не удалось сформировать резюме.")


async def parse_natural_task(text: str) -> dict[str, Any] | None:
    now = datetime.now().astimezone()
    prompt = f"""
Из сообщения пользователя извлеки задачу для командного планировщика.
Текущее локальное время: {now.isoformat()}.
Если это НЕ просьба добавить/создать/запланировать задачу, верни ровно null.
Если это задача, верни только валидный JSON без Markdown:
{{
  "title": "краткое название задачи",
  "assignee_username": "username без @ или null",
  "deadline": "ISO 8601 datetime с часовым поясом"
}}
Если дедлайн не указан, верни null.
Если ответственный не указан, assignee_username=null.

Сообщение: {text}
"""
    response = await asyncio.to_thread(
        _client().models.generate_content,
        model=_model(),
        contents=prompt,
    )
    raw = (response.text or "").strip()
    if raw == "null":
        return None
    raw = raw.removeprefix("```json").removesuffix("```").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("title") or not data.get("deadline"):
        return None
    return data
