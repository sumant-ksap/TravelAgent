import json

import asyncpg

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_chat_id_created_at_idx ON messages (chat_id, created_at);

CREATE TABLE IF NOT EXISTS trip_state (
    chat_id BIGINT PRIMARY KEY,
    state JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS traveler_profile (
    chat_id BIGINT PRIMARY KEY,
    preferences JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class Memory:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "Memory":
        pool = await asyncpg.create_pool(dsn)
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def add_message(self, chat_id: int, role: str, content: str) -> None:
        await self._pool.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES ($1, $2, $3)",
            chat_id,
            role,
            content,
        )

    async def history(self, chat_id: int, limit: int) -> list[dict]:
        rows = await self._pool.fetch(
            """
            SELECT role, content FROM (
                SELECT role, content, created_at FROM messages
                WHERE chat_id = $1
                ORDER BY created_at DESC
                LIMIT $2
            ) AS recent
            ORDER BY created_at ASC
            """,
            chat_id,
            limit,
        )
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    async def search(self, chat_id: int, keyword: str, limit: int = 5) -> list[dict]:
        rows = await self._pool.fetch(
            """
            SELECT role, content, created_at FROM messages
            WHERE chat_id = $1 AND content ILIKE $2
            ORDER BY created_at DESC
            LIMIT $3
            """,
            chat_id,
            f"%{keyword}%",
            limit,
        )
        return [
            {"role": row["role"], "content": row["content"], "created_at": row["created_at"].isoformat()}
            for row in rows
        ]

    async def get_trip_state(self, chat_id: int) -> dict:
        row = await self._pool.fetchrow("SELECT state FROM trip_state WHERE chat_id = $1", chat_id)
        if row is None or row["state"] is None:
            return {}
        state = row["state"]
        return json.loads(state) if isinstance(state, str) else dict(state)

    async def save_trip_state(self, chat_id: int, state: dict) -> None:
        await self._pool.execute(
            """
            INSERT INTO trip_state (chat_id, state, updated_at)
            VALUES ($1, $2::jsonb, now())
            ON CONFLICT (chat_id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()
            """,
            chat_id,
            json.dumps(state, default=str),
        )

    async def reset_trip_state(self, chat_id: int) -> None:
        await self._pool.execute("DELETE FROM trip_state WHERE chat_id = $1", chat_id)

    async def get_preferences(self, chat_id: int) -> dict:
        row = await self._pool.fetchrow(
            "SELECT preferences FROM traveler_profile WHERE chat_id = $1", chat_id
        )
        if row is None or row["preferences"] is None:
            return {}
        prefs = row["preferences"]
        return json.loads(prefs) if isinstance(prefs, str) else dict(prefs)

    async def save_preferences(self, chat_id: int, preferences: dict) -> None:
        await self._pool.execute(
            """
            INSERT INTO traveler_profile (chat_id, preferences, updated_at)
            VALUES ($1, $2::jsonb, now())
            ON CONFLICT (chat_id) DO UPDATE SET preferences = EXCLUDED.preferences, updated_at = now()
            """,
            chat_id,
            json.dumps(preferences, default=str),
        )
