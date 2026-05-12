"""MongoDB helpers for bot accounts and structured action/error logs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase


def get_database(client: AsyncIOMotorClient, db_name: str) -> AsyncIOMotorDatabase:
    return client[db_name]


async def fetch_enabled_bots(db: AsyncIOMotorDatabase, *, collection: str = "bots") -> list[dict[str, Any]]:
    """Return enabled bot documents (``enabled`` not false) that have ``account_id`` or ``bot_id``."""
    coll: AsyncIOMotorCollection = db[collection]
    cur = coll.find({"enabled": {"$ne": False}})
    docs = await cur.to_list(length=None)
    return [d for d in docs if d.get("account_id") or d.get("bot_id")]


async def fetch_bots_by_account_ids(
    db: AsyncIOMotorDatabase,
    account_ids: list[str],
    *,
    collection: str = "bots",
) -> list[dict[str, Any]]:
    coll = db[collection]
    cur = coll.find(
        {"$or": [{"account_id": {"$in": account_ids}}, {"bot_id": {"$in": account_ids}}]},
    )
    return await cur.to_list(length=None)


async def log_bot_event(
    db: AsyncIOMotorDatabase,
    *,
    account_id: str,
    level: str,
    message: str,
    collection: str = "bot_logs",
    meta: dict[str, Any] | None = None,
) -> None:
    """Persist one log line for dashboards (blocked bots, errors, successes)."""
    doc: dict[str, Any] = {
        "account_id": account_id,
        "level": level,
        "message": message,
        "created_at": datetime.now(timezone.utc),
    }
    if meta:
        doc["meta"] = meta
    await db[collection].insert_one(doc)


async def count_actions_since(
    db: AsyncIOMotorDatabase,
    *,
    account_id: str,
    action: str,
    since: datetime,
    collection: str = "bot_logs",
) -> int:
    """Count log entries tagged with ``meta.action`` for warmup / rate limits."""
    return await db[collection].count_documents(
        {
            "account_id": account_id,
            "created_at": {"$gte": since},
            "meta.action": action,
        },
    )
