from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
import uuid
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import asyncpg

from ..core.models import Booking, Conversation, LlmMessage, Proposal, User
from ..logger import log
from .memory import CLAIM_TTL_MS
from .migrations import MIGRATIONS
from .repos import StoredMessage


def _ms() -> int:
    return int(time.time() * 1000)


# Columns are always listed, never "*": after a migration, a pooler (Neon) can keep serving a plan prepared for the old
# table shape, which fails with "cached statement plan is invalid".
_USER_COLS = "created_at, last_seen_at"
_CONV_COLS = "id, user_id, created_at, outage_armed, active_at"
_MESSAGE_COLS = "id, message, created_at"
_PROPOSAL_COLS = "token, user_id, conversation_id, status, payload, expires_at, created_at, shown"
_BOOKING_COLS = "id, user_id, reference, proposal_token, payload, created_at"

_SEAT_LOCK = 0x7472_6970  # advisory lock key: one slot check at a time
# Strictly increasing per user ($1 = now, $2 = user id), so actions in the same millisecond still have an order.
_NEXT_ACTIVE = "GREATEST($1::bigint, COALESCE((SELECT max(active_at) FROM conversations WHERE user_id=$2), 0) + 1)"


def _proposal(r: asyncpg.Record) -> Proposal:
    return Proposal(r["token"], r["user_id"], r["conversation_id"], r["status"], r["payload"], int(r["expires_at"]), int(r["created_at"]), r["shown"])


def _conv(r: asyncpg.Record) -> Conversation:
    return Conversation(r["id"], r["user_id"], int(r["created_at"]), r["outage_armed"], int(r["active_at"]))


async def _init_connection(conn: asyncpg.Connection) -> None:
    # Store/return jsonb as Python objects (asyncpg's default is raw text).
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def _skip_reset(_conn: asyncpg.Connection) -> None:
    """
    Replaces asyncpg's reset query, which costs an extra round trip after every pool operation. Nothing here changes
    session state (no SET, LISTEN or session-level locks), and asyncpg still rolls back an open transaction.
    """


def _connection_args(url: str) -> tuple[str, Any]:
    """Strip libpq-only query params (asyncpg rejects some) and decide on TLS the same way the Node server did."""
    u = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(u.query) if k not in ("sslmode", "channel_binding")]
    dsn = urlunparse(u._replace(query=urlencode(query)))
    if re.search(r"localhost|127\.0\.0\.1", url):
        return dsn, None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # managed Postgres (Neon) presents its own chain
    return dsn, ctx


class PostgresRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, url: str, attempts: int = 6) -> PostgresRepo:
        """Connects with retry and backoff: Neon auto-suspends, so the first query can be slow or fail."""
        dsn, tls = _connection_args(url)
        i = 0
        while True:
            i += 1
            try:
                # Reconnecting costs several round trips, so idle connections are kept until just before Neon's
                # 5 minute auto-suspend.
                pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, timeout=15, ssl=tls, init=_init_connection,
                    reset=_skip_reset, max_inactive_connection_lifetime=240, command_timeout=20)
                repo = cls(pool)
                await repo.migrate()
                return repo
            except Exception as e:
                if i >= attempts:
                    raise
                wait = 0.5 * 2**i
                log("warn", "postgres not ready, retrying", attempt=i, waitMs=int(wait * 1000), err=str(e))
                await asyncio.sleep(wait)

    async def migrate(self) -> None:
        await self.pool.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY)")
        done = {r["name"] for r in await self.pool.fetch("SELECT name FROM schema_migrations")}
        for name, sql in MIGRATIONS:
            if name in done:
                continue
            async with self.pool.acquire() as conn, conn.transaction():
                for stmt in (s.strip() for s in sql.split(";")):
                    if stmt:
                        await conn.execute(stmt)
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", name)

    async def close(self) -> None:
        await self.pool.close()

    async def ping(self) -> None:
        await self.pool.execute("SELECT 1")

    async def ensure_user(self, user_id: str) -> User:
        select = f"SELECT {_USER_COLS} FROM users WHERE id=$1"
        r = await self.pool.fetchrow(select, user_id)
        if r is None:
            await self.pool.execute("INSERT INTO users (id, created_at) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING", user_id, _ms())
            r = await self.pool.fetchrow(select, user_id)
        return User(user_id, int(r["created_at"]), int(r["last_seen_at"]))

    async def create_conversation(self, user_id: str) -> Conversation:
        await self.ensure_user(user_id)
        now = _ms()
        r = await self.pool.fetchrow(
            f"""INSERT INTO conversations (id, user_id, created_at, outage_armed, active_at)
                VALUES ($3, $2, $1, false, {_NEXT_ACTIVE}) RETURNING {_CONV_COLS}""",
            now, user_id, str(uuid.uuid4()),
        )
        return _conv(r)

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        r = await self.pool.fetchrow(f"SELECT {_CONV_COLS} FROM conversations WHERE id=$1", conversation_id)
        return _conv(r) if r else None

    async def latest_conversation(self, user_id: str) -> Conversation | None:
        r = await self.pool.fetchrow(f"SELECT {_CONV_COLS} FROM conversations WHERE user_id=$1 ORDER BY active_at DESC, id DESC LIMIT 1", user_id)
        return _conv(r) if r else None

    async def list_conversations(self, user_id: str) -> list[Conversation]:
        rows = await self.pool.fetch(f"SELECT {_CONV_COLS} FROM conversations WHERE user_id=$1 ORDER BY created_at, active_at", user_id)
        return [_conv(r) for r in rows]

    async def touch_conversation(self, conversation_id: str) -> None:
        await self.pool.execute(
            """UPDATE conversations AS c
               SET active_at = GREATEST($1::bigint, (SELECT max(active_at) FROM conversations WHERE user_id = c.user_id) + 1)
               WHERE c.id = $2""",
            _ms(), conversation_id,
        )

    async def delete_conversation(self, conversation_id: str) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM messages WHERE conversation_id=$1", conversation_id)
            await conn.execute("DELETE FROM proposals WHERE conversation_id=$1", conversation_id)
            await conn.execute("DELETE FROM conversations WHERE id=$1", conversation_id)

    async def claim_seat(self, user_id: str, max_users: int, idle_ms: int, now_ms: int) -> bool:
        cutoff = now_ms - idle_ms
        # Common case, one round trip: refreshing a slot the user already holds doesn't change the count, so no lock.
        if await self.pool.fetchval(
            "UPDATE users SET last_seen_at=$3 WHERE id=$1 AND last_seen_at > $2 RETURNING id", user_id, cutoff, now_ms,
        ):
            return True
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _SEAT_LOCK)
            seen = await conn.fetchval("SELECT last_seen_at FROM users WHERE id=$1", user_id)
            if seen is None or seen <= cutoff:
                if await conn.fetchval("SELECT count(*) FROM users WHERE last_seen_at > $1", cutoff) >= max_users:
                    return False
            await conn.execute(
                """INSERT INTO users (id, created_at, last_seen_at) VALUES ($1, $2, $3)
                   ON CONFLICT (id) DO UPDATE SET last_seen_at = $3""",
                user_id, _ms(), now_ms,
            )
            return True

    async def set_outage(self, conversation_id: str, armed: bool) -> None:
        await self.pool.execute("UPDATE conversations SET outage_armed=$2 WHERE id=$1", conversation_id, armed)

    async def append_messages(self, conversation_id: str, msgs: list[LlmMessage]) -> None:
        # One pipelined, atomic batch: a tool call is never stored without its result.
        await self.pool.executemany(
            "INSERT INTO messages (conversation_id, message, created_at) VALUES ($1,$2,$3)",
            [(conversation_id, m, _ms()) for m in msgs],
        )

    async def get_messages(self, conversation_id: str) -> list[StoredMessage]:
        rows = await self.pool.fetch(f"SELECT {_MESSAGE_COLS} FROM messages WHERE conversation_id=$1 ORDER BY id", conversation_id)
        return [StoredMessage(int(r["id"]), conversation_id, r["message"], int(r["created_at"])) for r in rows]

    async def save_proposal(self, p: Proposal) -> None:
        await self.pool.execute(
            """INSERT INTO proposals (token,user_id,conversation_id,status,payload,expires_at,created_at,shown) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
               ON CONFLICT (token) DO UPDATE SET status=$4, payload=$5, expires_at=$6""",
            p.token, p.user_id, p.conversation_id, p.status, p.payload, p.expires_at, p.created_at, p.shown,
        )

    async def get_proposal(self, token: str) -> Proposal | None:
        r = await self.pool.fetchrow(f"SELECT {_PROPOSAL_COLS} FROM proposals WHERE token=$1", token)
        return _proposal(r) if r else None

    async def latest_proposed(self, user_id: str, conversation_id: str) -> Proposal | None:
        r = await self.pool.fetchrow(
            f"SELECT {_PROPOSAL_COLS} FROM proposals WHERE user_id=$1 AND conversation_id=$2 AND status='proposed' ORDER BY created_at DESC LIMIT 1",
            user_id, conversation_id,
        )
        return _proposal(r) if r else None

    async def transition_proposal(self, token: str, from_status: str, to_status: str) -> bool:
        rows = await self.pool.fetch("UPDATE proposals SET status=$3 WHERE token=$1 AND status=$2 RETURNING token", token, from_status, to_status)
        return len(rows) == 1

    async def mark_proposal_shown(self, token: str) -> None:
        await self.pool.execute("UPDATE proposals SET shown=true WHERE token=$1", token)

    async def add_booking(self, b: Booking) -> None:
        await self.pool.execute(
            "INSERT INTO bookings (id,user_id,reference,proposal_token,payload,created_at) VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (id) DO NOTHING",
            b.id, b.user_id, b.reference, b.proposal_token, b.payload, b.created_at,
        )

    async def list_bookings(self, user_id: str) -> list[Booking]:
        rows = await self.pool.fetch(f"SELECT {_BOOKING_COLS} FROM bookings WHERE user_id=$1 ORDER BY created_at, id", user_id)
        return [Booking(r["id"], r["user_id"], r["reference"], r["proposal_token"], r["payload"], int(r["created_at"])) for r in rows]

    async def claim_event(self, channel: str, event_id: str) -> Literal["claimed", "duplicate"]:
        now = _ms()
        try:
            await self.pool.execute("INSERT INTO processed_events (channel,event_id,state,created_at) VALUES ($1,$2,'claimed',$3)", channel, event_id, now)
            return "claimed"
        except asyncpg.UniqueViolationError:
            pass
        # A claim that was never completed (crash) can be taken over after the TTL.
        # Optimistic update on the old timestamp so two takers cannot both win.
        row = await self.pool.fetchrow("SELECT state, created_at FROM processed_events WHERE channel=$1 AND event_id=$2", channel, event_id)
        if not row or row["state"] != "claimed" or int(row["created_at"]) >= now - CLAIM_TTL_MS:
            return "duplicate"
        took = await self.pool.fetch(
            "UPDATE processed_events SET created_at=$3 WHERE channel=$1 AND event_id=$2 AND created_at=$4 RETURNING event_id",
            channel, event_id, now, row["created_at"],
        )
        return "claimed" if len(took) == 1 else "duplicate"

    async def complete_event(self, channel: str, event_id: str, result: Any = None) -> None:
        await self.pool.execute("UPDATE processed_events SET state='done', result=$3 WHERE channel=$1 AND event_id=$2", channel, event_id, result)

    async def release_event(self, channel: str, event_id: str) -> None:
        await self.pool.execute("DELETE FROM processed_events WHERE channel=$1 AND event_id=$2", channel, event_id)

    async def get_event_result(self, channel: str, event_id: str) -> Any | None:
        r = await self.pool.fetchrow("SELECT result FROM processed_events WHERE channel=$1 AND event_id=$2 AND state='done'", channel, event_id)
        return r["result"] if r else None
