from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

DB_PATH = Path(__file__).with_name("planner.db")


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                assignee_user_id INTEGER,
                assignee_username TEXT,
                creator_user_id INTEGER NOT NULL,
                creator_username TEXT,
                deadline TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                completed_at TEXT,
                reminder_sent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_chat_status
                ON tasks(chat_id, status);
            CREATE INDEX IF NOT EXISTS idx_tasks_deadline
                ON tasks(deadline, reminder_sent, status);

            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                display_name TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(chat_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_users_chat_username
                ON users(chat_id, username);

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT,
                display_name TEXT,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(chat_id, telegram_message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_chat_id
                ON messages(chat_id, id DESC);
            """
        )
        await db.commit()


async def add_task(
    chat_id: int,
    title: str,
    assignee_user_id: int | None,
    assignee_username: str | None,
    creator_user_id: int,
    creator_username: str | None,
    deadline: datetime,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO tasks(
                chat_id, title, assignee_user_id, assignee_username,
                creator_user_id, creator_username, deadline, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                title,
                assignee_user_id,
                assignee_username,
                creator_user_id,
                creator_username,
                deadline.astimezone(timezone.utc).isoformat(),
                now,
            ),
        )
        await db.commit()
        return int(cur.lastrowid)


async def list_open_tasks(chat_id: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM tasks
            WHERE chat_id = ? AND status = 'open'
            ORDER BY deadline ASC
            """,
            (chat_id,),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def complete_task(chat_id: int, task_id: int, actor_user_id: int) -> dict[str, Any] | None:
    completed_at = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM tasks WHERE id = ? AND chat_id = ? AND status = 'open'",
            (task_id, chat_id),
        )
        row = await cur.fetchone()
        if not row:
            return None

        await db.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ? AND chat_id = ?",
            (completed_at, task_id, chat_id),
        )
        await db.commit()
        result = dict(row)
        result["completed_by"] = actor_user_id
        return result


async def weekly_stats(chat_id: int) -> list[dict[str, Any]]:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                assignee_user_id,
                COALESCE(assignee_username, 'без_username') AS username,
                COUNT(*) AS completed
            FROM tasks
            WHERE chat_id = ?
              AND status = 'done'
              AND completed_at >= ?
            GROUP BY assignee_user_id, assignee_username
            ORDER BY completed DESC, username ASC
            """,
            (chat_id, since),
        )
        return [dict(row) for row in await cur.fetchall()]


async def upsert_user(
    chat_id: int,
    user_id: int,
    username: str | None,
    display_name: str | None,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users(chat_id, user_id, username, display_name, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name,
                updated_at = excluded.updated_at
            """,
            (chat_id, user_id, username, display_name, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def find_user_id_by_username(chat_id: int, username: str) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT user_id FROM users
            WHERE chat_id = ? AND lower(username) = lower(?)
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (chat_id, username.lstrip("@")),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else None


async def save_message(
    chat_id: int,
    telegram_message_id: int,
    user_id: int | None,
    username: str | None,
    display_name: str | None,
    text: str,
) -> None:
    if not text.strip():
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO messages(
                chat_id, telegram_message_id, user_id, username,
                display_name, text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                telegram_message_id,
                user_id,
                username,
                display_name,
                text.strip(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()


async def last_messages(chat_id: int, limit: int = 20) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM (
                SELECT * FROM messages
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
            ) ORDER BY id ASC
            """,
            (chat_id, limit),
        )
        return [dict(row) for row in await cur.fetchall()]


async def due_reminders() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    start = now + timedelta(hours=23)
    end = now + timedelta(hours=25)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM tasks
            WHERE status = 'open'
              AND reminder_sent = 0
              AND deadline BETWEEN ? AND ?
            ORDER BY deadline ASC
            """,
            (start.isoformat(), end.isoformat()),
        )
        return [dict(row) for row in await cur.fetchall()]


async def mark_reminder_sent(task_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET reminder_sent = 1 WHERE id = ?", (task_id,))
        await db.commit()
