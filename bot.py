from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message
from dotenv import load_dotenv

from ai import parse_natural_task, summarize_messages
from db import (
    add_task,
    complete_task,
    due_reminders,
    find_user_id_by_username,
    init_db,
    last_messages,
    list_open_tasks,
    mark_reminder_sent,
    save_message,
    upsert_user,
    weekly_stats,
)

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}


def is_group(message: Message) -> bool:
    return message.chat.type in GROUP_TYPES


def mention(username: str | None) -> str:
    return f"@{escape(username)}" if username else "не назначен"


def format_deadline(value: str) -> str:
    dt = datetime.fromisoformat(value)
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")


async def resolve_username_to_user_id(chat_id: int, username: str | None) -> int | None:
    if not username:
        return None
    return await find_user_id_by_username(chat_id, username.lstrip("@"))


@router.message(CommandStart())
async def start(message: Message) -> None:
    if is_group(message):
        await message.answer(
            "Командный планировщик подключён.\n"
            "Команды: /addtask, /tasks, /done, /stats, /summarize, /poll, /help"
        )
    else:
        await message.answer(
            "Добавьте меня в групповой чат. В группе я веду общий список задач, "
            "напоминаю о дедлайнах, считаю статистику, делаю резюме и голосования."
        )


@router.message(Command("help"))
async def help_cmd(message: Message) -> None:
    await message.answer(
        "Команды:\n"
        "/addtask Текст | @username | 2026-08-15 18:00\n"
        "/tasks — открытые задачи\n"
        "/done 12 — отметить задачу выполненной\n"
        "/stats — выполненные задачи за 7 дней\n"
        "/summarize — резюме последних 20 сообщений\n"
        "/poll Вопрос? Вариант 1, Вариант 2, Вариант 3\n\n"
        "Можно также писать естественно, например: «Добавь задачу @anna подготовить отчёт к пятнице 18:00»."
    )


@router.message(Command("addtask"))
async def add_task_cmd(message: Message, bot: Bot) -> None:
    if not is_group(message):
        await message.answer("Эта команда предназначена для группового чата.")
        return
    text = message.text or ""
    args = text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Формат: /addtask Текст | @username | 2026-08-15 18:00")
        return

    parts = [p.strip() for p in args[1].split("|")]
    if len(parts) != 3:
        await message.answer("Нужно 3 части через |: задача | @username | дата время")
        return

    title, assignee_raw, deadline_raw = parts
    assignee = assignee_raw.lstrip("@") if assignee_raw else None
    try:
        deadline = datetime.strptime(deadline_raw, "%Y-%m-%d %H:%M").astimezone()
    except ValueError:
        await message.answer("Дата должна быть в формате YYYY-MM-DD HH:MM, например 2026-08-15 18:00")
        return

    user_id = await resolve_username_to_user_id(message.chat.id, assignee)
    task_id = await add_task(
        message.chat.id,
        title,
        user_id,
        assignee,
        message.from_user.id,
        message.from_user.username,
        deadline,
    )
    await message.answer(
        f"Задача #{task_id} добавлена.\n"
        f"{escape(title)}\n"
        f"Ответственный: {mention(assignee)}\n"
        f"Дедлайн: {deadline.strftime('%d.%m.%Y %H:%M')}"
    )


@router.message(Command("tasks"))
async def tasks_cmd(message: Message) -> None:
    if not is_group(message):
        return
    tasks = await list_open_tasks(message.chat.id)
    if not tasks:
        await message.answer("Открытых задач нет.")
        return
    lines = ["Открытые задачи:"]
    for t in tasks:
        lines.append(
            f"#{t['id']} — {escape(t['title'])}\n"
            f"Ответственный: {mention(t['assignee_username'])}; дедлайн: {format_deadline(t['deadline'])}"
        )
    await message.answer("\n\n".join(lines))


@router.message(Command("done"))
async def done_cmd(message: Message) -> None:
    if not is_group(message):
        return
    match = re.search(r"/done(?:@\w+)?\s+(\d+)", message.text or "", flags=re.I)
    if not match:
        await message.answer("Формат: /done 12")
        return
    task_id = int(match.group(1))
    task = await complete_task(message.chat.id, task_id, message.from_user.id)
    if not task:
        await message.answer("Открытая задача с таким номером не найдена.")
        return
    await message.answer(f"Задача #{task_id} выполнена: {escape(task['title'])}")


@router.message(Command("stats"))
async def stats_cmd(message: Message) -> None:
    if not is_group(message):
        return
    rows = await weekly_stats(message.chat.id)
    if not rows:
        await message.answer("За последние 7 дней выполненных задач пока нет.")
        return
    lines = ["Статистика за последние 7 дней:"]
    for row in rows:
        lines.append(f"@{escape(row['username'])}: {row['completed']}")
    await message.answer("\n".join(lines))


@router.message(Command("summarize"))
async def summarize_cmd(message: Message) -> None:
    if not is_group(message):
        return
    messages = await last_messages(message.chat.id, 20)
    # Не включаем саму команду в резюме, если она уже успела сохраниться.
    messages = [m for m in messages if not m["text"].startswith("/summarize")]
    if len(messages) < 2:
        await message.answer(
            "Недостаточно сообщений для резюме. Проверьте, что в BotFather отключён Privacy Mode."
        )
        return
    try:
        summary = await summarize_messages(messages[-20:])
    except Exception as exc:
        logger.exception("Gemini summarize failed: %s", exc)
        await message.answer("Не удалось обратиться к AI. Проверьте GEMINI_API_KEY.")
        return
    await message.answer(summary)


@router.message(Command("poll"))
async def poll_cmd(message: Message, bot: Bot) -> None:
    if not is_group(message):
        return
    raw = re.sub(r"^/poll(?:@\w+)?\s*", "", message.text or "", flags=re.I).strip()
    if not raw:
        await message.answer("Пример: /poll Когда созвон? Понедельник, Среда, Пятница")
        return

    # Вопрос заканчивается первым ?; варианты после него разделены запятыми.
    if "?" not in raw:
        await message.answer("Добавьте знак ? после вопроса. Затем перечислите варианты через запятую.")
        return
    question, options_raw = raw.split("?", 1)
    question = question.strip() + "?"
    options = [x.strip() for x in options_raw.split(",") if x.strip()]
    if not 2 <= len(options) <= 12:
        await message.answer("Нужно от 2 до 12 вариантов ответа.")
        return

    await bot.send_poll(
        chat_id=message.chat.id,
        question=question[:300],
        options=options,
        is_anonymous=False,
        allows_multiple_answers=False,
    )


@router.message(F.text)
async def all_text_messages(message: Message, bot: Bot) -> None:
    if not is_group(message) or not message.from_user:
        return

    await upsert_user(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        username=message.from_user.username,
        display_name=message.from_user.full_name,
    )

    await save_message(
        chat_id=message.chat.id,
        telegram_message_id=message.message_id,
        user_id=message.from_user.id,
        username=message.from_user.username,
        display_name=message.from_user.full_name,
        text=message.text or "",
    )

    # Команды уже обрабатываются отдельными handlers.
    if (message.text or "").startswith("/"):
        return

    lower = (message.text or "").lower()
    trigger_words = ("задач", "дедлайн", "сделать", "подготов", "назнач", "заплан")
    if not any(word in lower for word in trigger_words):
        return

    try:
        parsed = await parse_natural_task(message.text or "")
    except Exception:
        logger.exception("Natural task parsing failed")
        return
    if not parsed:
        return

    try:
        deadline = datetime.fromisoformat(parsed["deadline"])
        if deadline.tzinfo is None:
            deadline = deadline.astimezone()
    except (TypeError, ValueError):
        return

    assignee = parsed.get("assignee_username")
    if assignee:
        assignee = str(assignee).lstrip("@")

    assignee_user_id = await resolve_username_to_user_id(message.chat.id, assignee)
    if assignee and message.from_user.username and assignee.lower() == message.from_user.username.lower():
        assignee_user_id = message.from_user.id

    task_id = await add_task(
        message.chat.id,
        str(parsed["title"]),
        assignee_user_id,
        assignee,
        message.from_user.id,
        message.from_user.username,
        deadline,
    )
    await message.reply(
        f"Понял как задачу и добавил #{task_id}.\n"
        f"{escape(str(parsed['title']))}\n"
        f"Ответственный: {mention(assignee)}\n"
        f"Дедлайн: {deadline.astimezone().strftime('%d.%m.%Y %H:%M')}"
    )


async def reminder_worker(bot: Bot) -> None:
    while True:
        try:
            tasks = await due_reminders()
            for task in tasks:
                text = (
                    f"Напоминание: завтра дедлайн задачи #{task['id']}\n"
                    f"{escape(task['title'])}\n"
                    f"Ответственный: {mention(task['assignee_username'])}\n"
                    f"Дедлайн: {format_deadline(task['deadline'])}"
                )
                try:
                    await bot.send_message(task["chat_id"], text)
                    await mark_reminder_sent(task["id"])
                except Exception:
                    logger.exception("Could not send reminder for task %s", task["id"])
        except Exception:
            logger.exception("Reminder worker failed")
        await asyncio.sleep(60)


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="addtask", description="Добавить задачу"),
            BotCommand(command="tasks", description="Показать открытые задачи"),
            BotCommand(command="done", description="Отметить задачу выполненной"),
            BotCommand(command="stats", description="Статистика за 7 дней"),
            BotCommand(command="summarize", description="Резюме последних 20 сообщений"),
            BotCommand(command="poll", description="Создать голосование"),
            BotCommand(command="help", description="Помощь"),
        ]
    )


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not configured")

    await init_db()
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await set_commands(bot)

    reminder_task = asyncio.create_task(reminder_worker(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
