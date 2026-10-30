from pathlib import Path

import aiosqlite

from config import settings

DB_PATH = str(Path(settings.DATA_DIR) / "checker.db")


async def init_db():
    Path(settings.DATA_DIR).mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS checkers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                phone TEXT NOT NULL,
                session_string TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(owner_id, phone)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                phone TEXT NOT NULL,
                verdict TEXT NOT NULL,
                details TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def add_checker(owner_id: int, phone: str, session_string: str) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO checkers (owner_id, phone, session_string)
                VALUES (?, ?, ?)
                ON CONFLICT(owner_id, phone) DO UPDATE SET session_string = excluded.session_string
            """, (owner_id, phone, session_string))
            await db.commit()
        return True
    except Exception:
        return False


async def get_checkers(owner_id: int) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT phone, session_string FROM checkers WHERE owner_id = ? ORDER BY id",
            (owner_id,)
        )
        return await cur.fetchall()


async def remove_checker(owner_id: int, phone: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM checkers WHERE owner_id = ? AND phone = ?", (owner_id, phone)
        )
        await db.commit()
        return cur.rowcount > 0


async def save_check(owner_id: int, phone: str, verdict: str, details: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO checks (owner_id, phone, verdict, details) VALUES (?, ?, ?, ?)",
            (owner_id, phone, verdict, details)
        )
        await db.commit()


async def get_history(owner_id: int, limit: int = 15) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT phone, verdict, details, created_at FROM checks "
            "WHERE owner_id = ? ORDER BY id DESC LIMIT ?",
            (owner_id, limit)
        )
        return await cur.fetchall()
