"""
Async MongoDB access (Motor) for bot configs, Playwright session storage, and action logs.

Use one :class:`AsyncBotDatabase` instance per process and share it across thousands of
concurrent tasks; Motor pools connections internally.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase


def _bot_id_filter(bot_id: str) -> dict[str, Any]:
    """Match either ``account_id`` (fleet convention) or ``bot_id``."""
    return {"$or": [{"account_id": bot_id}, {"bot_id": bot_id}]}


def _normalize_origins(local_storage: Any) -> list[dict[str, Any]]:
    """
    Build Playwright ``origins`` entries.

    Accepts:

    - ``None`` / empty → ``[]``
    - A list of Playwright-style ``{"origin": "...", "localStorage": [...]}`` dicts
    - A mapping ``{origin_url: {key: value, ...}}`` (shorthand)
    """
    if not local_storage:
        return []
    if isinstance(local_storage, list):
        return list(local_storage)
    if isinstance(local_storage, Mapping):
        out: list[dict[str, Any]] = []
        for origin, pairs in local_storage.items():
            if isinstance(pairs, Mapping):
                ls = [{"name": str(k), "value": str(v)} for k, v in pairs.items()]
                out.append({"origin": str(origin), "localStorage": ls})
        return out
    return []


async def get_bot_config(
    db: AsyncIOMotorDatabase,
    bot_id: str,
    *,
    collection: str = "bots",
) -> dict[str, Any] | None:
    """Load credentials, proxy, and other fields for one bot."""
    return await db[collection].find_one(_bot_id_filter(bot_id))


async def save_session(
    db: AsyncIOMotorDatabase,
    bot_id: str,
    cookies: list[dict[str, Any]] | None,
    local_storage: list[dict[str, Any]] | Mapping[str, Mapping[str, Any]] | None,
    *,
    collection: str = "bot_sessions",
) -> None:
    """
    Persist cookies and ``localStorage`` for later Playwright hydration.

    ``cookies`` should be Playwright's serializable cookie list; ``local_storage`` is either
    Playwright ``origins``-style list or a shorthand ``{origin: {key: value}}`` map.
    """
    origins = _normalize_origins(local_storage)
    storage_state = {"cookies": list(cookies or []), "origins": origins}
    now = datetime.now(timezone.utc)
    await db[collection].update_one(
        {"bot_id": bot_id},
        {
            "$set": {
                "bot_id": bot_id,
                "cookies": storage_state["cookies"],
                "origins": storage_state["origins"],
                "storage_state": storage_state,
                "updated_at": now,
            }
        },
        upsert=True,
    )


async def load_session(
    db: AsyncIOMotorDatabase,
    bot_id: str,
    *,
    collection: str = "bot_sessions",
) -> dict[str, Any] | None:
    """
    Return a dict suitable for Playwright ``storage_state`` (``BrowserContext`` / launch).

    Shape: ``{"cookies": [...], "origins": [...]}``. Returns ``None`` if no session exists.
    """
    doc = await db[collection].find_one({"bot_id": bot_id})
    if not doc:
        return None
    if isinstance(doc.get("storage_state"), dict):
        ss = doc["storage_state"]
        return {
            "cookies": list(ss.get("cookies") or doc.get("cookies") or []),
            "origins": list(ss.get("origins") or doc.get("origins") or []),
        }
    return {"cookies": list(doc.get("cookies") or []), "origins": list(doc.get("origins") or [])}


async def log_action(
    db: AsyncIOMotorDatabase,
    bot_id: str,
    action_type: str,
    details: Any,
    *,
    collection: str = "bot_logs",
) -> None:
    """
    Append an audit row (safe to call at high QPS; Motor batches internally).

    ``details`` should be JSON-serializable (``dict``, ``list``, ``str``, numbers, …).
    """
    payload: dict[str, Any] = {
        "bot_id": bot_id,
        "action_type": action_type,
        "created_at": datetime.now(timezone.utc),
    }
    if isinstance(details, Mapping):
        payload["details"] = dict(details)
    elif details is None:
        payload["details"] = {}
    else:
        payload["details"] = {"value": details}
    await db[collection].insert_one(payload)


class AsyncBotDatabase:
    """
    Motor-backed store for bot rows, Playwright ``storage_state`` fragments, and audit logs.

    Typical usage::

        store = AsyncBotDatabase.from_uri(os.environ["MONGO_URI"], "bot_fleet")
        try:
            cfg = await store.get_bot_config("acct_001")
            state = await store.load_session("acct_001")
        finally:
            await store.close()
    """

    __slots__ = ("_db", "_client", "_bots_c", "_sessions_c", "_logs_c")

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        *,
        client: AsyncIOMotorClient | None = None,
        bots_collection: str = "bots",
        sessions_collection: str = "bot_sessions",
        logs_collection: str = "bot_logs",
    ) -> None:
        self._db = db
        self._client = client
        self._bots_c = bots_collection
        self._sessions_c = sessions_collection
        self._logs_c = logs_collection

    @classmethod
    def from_uri(
        cls,
        mongo_uri: str,
        db_name: str,
        *,
        bots_collection: str = "bots",
        sessions_collection: str = "bot_sessions",
        logs_collection: str = "bot_logs",
        **client_kwargs: Any,
    ) -> "AsyncBotDatabase":
        """Create a new Motor client and database wrapper (call :meth:`close` when done)."""
        client = AsyncIOMotorClient(mongo_uri, **client_kwargs)
        return cls(
            client[db_name],
            client=client,
            bots_collection=bots_collection,
            sessions_collection=sessions_collection,
            logs_collection=logs_collection,
        )

    @property
    def database(self) -> AsyncIOMotorDatabase:
        return self._db

    async def close(self) -> None:
        """Close the underlying Motor client (if this instance owns it)."""
        if self._client is not None:
            self._client.close()
            self._client = None

    async def get_bot_config(self, bot_id: str) -> dict[str, Any] | None:
        return await get_bot_config(self._db, bot_id, collection=self._bots_c)

    async def save_session(
        self,
        bot_id: str,
        cookies: list[dict[str, Any]] | None,
        local_storage: list[dict[str, Any]] | Mapping[str, Mapping[str, Any]] | None,
    ) -> None:
        await save_session(
            self._db,
            bot_id,
            cookies,
            local_storage,
            collection=self._sessions_c,
        )

    async def load_session(self, bot_id: str) -> dict[str, Any] | None:
        return await load_session(self._db, bot_id, collection=self._sessions_c)

    async def log_action(self, bot_id: str, action_type: str, details: Any) -> None:
        await log_action(self._db, bot_id, action_type, details, collection=self._logs_c)
