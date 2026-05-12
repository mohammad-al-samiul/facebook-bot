#!/usr/bin/env python3
"""
Fleet entrypoint: load bot accounts + fixed proxies from MongoDB, run many Playwright bots
with **asyncio** (and optional **multiprocessing** process pool), randomised schedules, warmup,
and structured logging (files + MongoDB for dashboards).

Environment (typical)
---------------------
``MONGO_URI`` — required unless ``--mongo-uri`` is set (e.g. ``mongodb://127.0.0.1:27017``).

Optional tuning
---------------
``INITIAL_STAGGER_MAX_SEC`` — spread first wake-ups (default 3600).

``ACTIVE_HOUR_START`` / ``ACTIVE_HOUR_END`` — local quiet hours for slot jitter (default 8–23).

``ACTION_GAP_MIN_SEC`` / ``ACTION_GAP_MAX_SEC`` — post-action delay when not in warmup.

``WARMUP_GAP_MIN_SEC`` / ``WARMUP_GAP_MAX_SEC`` — gentler spacing during warmup.

MongoDB schema (collection ``bots`` by default)
-----------------------------------------------
Each document should include a **stable** ``account_id``, **fixed** ``proxy`` (same IP day to
day), and a **unique** profile directory is derived as ``<profiles_dir>/<account_id>/``.

Example document::

    {
      "account_id": "acct_001",
      "enabled": true,
      "email": "user@example.com",
      "password": "…",
      "headless": true,
      "timezone_id": "America/New_York",
      "proxy": {"server": "http://1.2.3.4:8000", "username": "u", "password": "p"},
      "warmup_enabled": true,
      "warmup_complete": false,
      "warmup_duration_days": 7,
      "warmup_started_at": {"$date": "2026-01-01T00:00:00Z"},
      "created_at": {"$date": "2026-01-01T00:00:00Z"}
    }

Logs are written to ``bot_logs`` (default) with fields ``account_id``, ``level`` (``info``,
``error``, ``blocked``), ``message``, ``created_at``, optional ``meta`` (e.g. ``action``).
Per-bot file logs live under ``<logs_dir>/<account_id>.log``.

Scale note: keep ``headless=true`` for large fleets; assign **one fixed proxy per account**;
use a long warmup with mostly scrolling and very few likes.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import multiprocessing as mp
import os
import signal
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import OperationFailure, PyMongoError, ServerSelectionTimeoutError

from playwright_automation.fleet_worker import run_fleet_async

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env", override=False)


def _mongo_ping(mongo_uri: str) -> None:
    """
    Sanity check: at least one MongoDB node reachable.

    When the Atlas Primary flaps, ``ping`` / ``list_database_names`` can block
    for 30s+ (retryReads keeps retrying). We don't actually need the Primary
    for our use case — reading from a Secondary is enough to fetch the bot
    list. So this function just opens a client, waits for the topology to
    populate, and returns as soon as any healthy node is seen.
    """
    import time as _time

    try:
        with MongoClient(
            mongo_uri,
            serverSelectionTimeoutMS=8_000,
            connectTimeoutMS=10_000,
            socketTimeoutMS=15_000,
        ) as client:
            deadline = _time.monotonic() + 12.0
            while _time.monotonic() < deadline:
                desc = client.topology_description
                alive = [
                    s for s in desc.server_descriptions().values()
                    if "Unknown" not in str(s.server_type)
                ]
                if alive:
                    return
                _time.sleep(0.4)
            raise ConnectionError(
                f"MongoDB cluster unreachable: no shard became alive within 12 seconds ({mongo_uri})."
            )
    except ServerSelectionTimeoutError as exc:
        raise ConnectionError(
            f"MongoDB is not reachable ({mongo_uri}). If local, run `docker compose up -d`; "
            f"If Atlas, check URI, IP whitelist (Network Access), and internet connection."
        ) from exc
    except PyMongoError as exc:
        raise ConnectionError(
            f"MongoDB connection error: {exc}. Check Atlas cluster status / network connectivity."
        ) from exc


def _resolve_bot_row_id(doc: dict[str, Any]) -> str | None:
    """Prefer ``account_id``, fall back to ``bot_id`` (same string used as fleet worker id)."""
    for key in ("account_id", "bot_id"):
        v = doc.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def _load_account_ids(mongo_uri: str, db_name: str, collection: str) -> list[str]:
    _mongo_ping(mongo_uri)
    try:
        # Bot list is read-only — even if Primary is slow, a Secondary can serve it.
        with MongoClient(
            mongo_uri,
            serverSelectionTimeoutMS=30_000,
            connectTimeoutMS=15_000,
            socketTimeoutMS=20_000,
            readPreference="secondaryPreferred",
        ) as client:
            cur = client[db_name][collection].find(
                {"enabled": {"$ne": False}},
                {"account_id": 1, "bot_id": 1},
            )
            out: list[str] = []
            for d in cur:
                wid = _resolve_bot_row_id(d)
                if wid:
                    out.append(wid)
            return out
    except ServerSelectionTimeoutError as exc:
        raise ConnectionError(
            f"MongoDB primary unavailable while loading bots ({mongo_uri}). "
            f"The Atlas Primary node may be down — retry in a few minutes, "
            f"or use the cookies.txt-based standalone runners:\n"
            f"  python scripts/run_single_account.py          # single account\n"
            f"  python scripts/login_all_from_cookies.py      # all accounts, login only"
        ) from exc
    except OperationFailure as exc:
        raise ConnectionError(
            f"Failed to fetch bots from MongoDB ({exc.code}): {exc.details}. "
            f"Verify that the DB user has read permission."
        ) from exc


def _diagnose_bots_empty(mongo_uri: str, db_name: str, collection: str) -> str:
    """Short stats when no runnable ids (helps wrong DB/collection/schema)."""
    try:
        with MongoClient(
            mongo_uri,
            serverSelectionTimeoutMS=20_000,
            readPreference="secondaryPreferred",
        ) as client:
            coll = client[db_name][collection]
            total = coll.count_documents({})
            enabled_ok = coll.count_documents({"enabled": {"$ne": False}})
            with_any_id = coll.count_documents(
                {"$or": [{"account_id": {"$exists": True}}, {"bot_id": {"$exists": True}}]},
            )
            return (
                f"db={db_name!r} collection={collection!r}: total_docs={total}, "
                f"enabled_ne_false={enabled_ok}, rows_with_account_id_or_bot_id_field={with_any_id}. "
                f"Every bot document must have ``account_id`` or ``bot_id`` (string), "
                f"and will be ignored if ``enabled`` is false. If empty, run: python scripts/seed_demo_bot.py"
            )
    except Exception as exc:
        return f"(diagnostics failed: {exc})"


def _chunked(ids: list[str], n: int) -> list[list[str]]:
    if n <= 1:
        return [ids]
    return [ids[i::n] for i in range(n)]


def _configure_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = logging.FileHandler(logs_dir / "fleet_main.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers.clear()
    root.addHandler(sh)
    root.addHandler(fh)
    # Low-level pymongo / motor / asyncio debug spam hides the real errors — keep them quiet.
    for noisy in ("pymongo", "pymongo.topology", "pymongo.connection",
                  "pymongo.serverSelection", "pymongo.command",
                  "motor", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run MongoDB-driven Playwright bot fleet.")
    p.add_argument("--mongo-uri", default=os.environ.get("MONGO_URI"), help="Mongo connection URI")
    p.add_argument(
        "--db",
        default=os.environ.get("MONGO_DB", "fb-bot"),
        help="Database name (match it with the URI path, e.g. fb-bot)",
    )
    p.add_argument(
        "--bots-collection",
        default=os.environ.get("MONGO_BOTS_COLLECTION", "bots"),
        help="Collection holding bot credentials + proxy",
    )
    p.add_argument(
        "--profiles-dir",
        default=os.environ.get("PROFILES_DIR", "./profiles"),
        help="Root directory for per-account Chromium user-data dirs",
    )
    p.add_argument(
        "--logs-dir",
        default=os.environ.get("LOGS_DIR", "./logs"),
        help="Directory for per-bot log files and fleet_main.log",
    )
    p.add_argument(
        "--max-concurrent",
        type=int,
        default=int(os.environ.get("MAX_CONCURRENT_BROWSERS", "3")),
        help="Max simultaneous browser contexts fleet-wide in this process",
    )
    p.add_argument(
        "--processes",
        type=int,
        default=int(os.environ.get("FLEET_PROCESSES", "1")),
        help="If >1, split accounts across N OS processes (each runs its own asyncio loop)",
    )
    p.add_argument(
        "--account-id",
        action="append",
        dest="account_ids",
        default=None,
        help="Restrict to specific account_id (repeatable). Default: all enabled bots.",
    )
    return p.parse_args(argv)


async def _run_with_stop_signal(coro, stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except (NotImplementedError, ValueError, RuntimeError):
            pass
    try:
        await coro
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, ValueError, RuntimeError):
                pass


async def _async_main(args: argparse.Namespace, *, account_ids: list[str] | None, stop: asyncio.Event) -> None:
    assert args.mongo_uri
    await run_fleet_async(
        args.mongo_uri,
        args.db,
        profiles_root=Path(args.profiles_dir),
        logs_root=Path(args.logs_dir),
        max_concurrent_browsers=args.max_concurrent,
        stop=stop,
        account_ids=account_ids,
        bots_collection=args.bots_collection,
    )


def _process_entry(cfg: dict[str, Any], chunk: list[str]) -> None:
    async def _runner() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _stop() -> None:
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except (NotImplementedError, ValueError, RuntimeError):
                pass
        try:
            await run_fleet_async(
                cfg["mongo_uri"],
                cfg["db"],
                profiles_root=Path(cfg["profiles_dir"]),
                logs_root=Path(cfg["logs_dir"]),
                max_concurrent_browsers=int(cfg["max_concurrent"]),
                stop=stop,
                account_ids=chunk,
                bots_collection=cfg["bots_collection"],
            )
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, ValueError, RuntimeError):
                    pass

    asyncio.run(_runner())


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if not args.mongo_uri:
        print("Missing Mongo URI: set MONGO_URI or pass --mongo-uri", file=sys.stderr)
        sys.exit(2)

    logs_root = Path(args.logs_dir)
    _configure_logging(logs_root)
    log = logging.getLogger("main")

    log.info("Fleet startup: mongo=%s db=%s collection=%s",
             args.mongo_uri.split("@")[-1], args.db, args.bots_collection)
    log.info("Checking MongoDB cluster reachability...")

    try:
        if args.account_ids:
            ids = list(dict.fromkeys(args.account_ids))
            _mongo_ping(args.mongo_uri)
        else:
            ids = _load_account_ids(args.mongo_uri, args.db, args.bots_collection)
        log.info("Fetched %d bot(s) from MongoDB: %s", len(ids), ids[:5] if ids else [])
    except ConnectionError as exc:
        log.error("%s", exc)
        print(str(exc), file=sys.stderr)
        sys.exit(3)
    except PyMongoError as exc:
        log.exception("Unexpected error during MongoDB operation")
        print(f"MongoDB error: {exc}", file=sys.stderr)
        sys.exit(3)
    if not ids:
        log.error(
            "No bot ids to run. %s",
            _diagnose_bots_empty(args.mongo_uri, args.db, args.bots_collection),
        )
        sys.exit(1)

    if args.processes > 1:
        mp.set_start_method("spawn", force=True)
        chunks = [c for c in _chunked(ids, args.processes) if c]
        cfg = {
            "mongo_uri": args.mongo_uri,
            "db": args.db,
            "profiles_dir": args.profiles_dir,
            "logs_dir": args.logs_dir,
            "bots_collection": args.bots_collection,
            "max_concurrent": args.max_concurrent,
        }
        log.info("Starting %s worker processes for %s accounts", len(chunks), len(ids))
        procs = [mp.Process(target=_process_entry, args=(cfg, ch), name=f"fleet-{i}") for i, ch in enumerate(chunks)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
        return

    stop = asyncio.Event()

    async def _body() -> None:
        await _async_main(
            args,
            account_ids=args.account_ids if args.account_ids else None,
            stop=stop,
        )

    async def _wrapped() -> None:
        await _run_with_stop_signal(_body(), stop)

    asyncio.run(_wrapped())


if __name__ == "__main__":
    main()
