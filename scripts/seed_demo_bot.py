#!/usr/bin/env python3
"""Insert one demo ``bots`` row if the collection is empty (local testing)."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
from pymongo.errors import ServerSelectionTimeoutError


def _parse_cookies_line(line: str) -> list[dict[str, Any]]:
    """Parse 'name=value;name2=value2' cookie string into Playwright cookie dicts."""
    cookies = []
    for pair in line.strip().split(";"):
        pair = pair.strip()
        if not pair:
            continue
        if "=" in pair:
            name, value = pair.split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".facebook.com",
                "path": "/",
            })
    return cookies


def main() -> None:
    uri = os.environ.get("MONGO_URI", "mongodb://127.0.0.1:27017")
    db_name = os.environ.get("MONGO_DB", "fb-bot")
    coll_name = os.environ.get("MONGO_BOTS_COLLECTION", "bots")

    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
    except ServerSelectionTimeoutError:
        print("MongoDB is not running. Start it first: docker compose up -d", file=sys.stderr)
        sys.exit(3)

    coll = client[db_name][coll_name]
    if coll.count_documents({}) > 0:
        print(f"{db_name}.{coll_name} already has documents; skip seed.")
        return

    # Read cookies from cookies.txt if available
    cookies: list[dict[str, Any]] = []
    cookies_path = Path(__file__).resolve().parent.parent / "cookies.txt"
    if cookies_path.is_file():
        lines = cookies_path.read_text(encoding="utf-8").splitlines()
        # Second non-empty line after account_id and password (index 4 if pattern holds)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not stripped.isdigit():
                # Found first cookie line
                cookies = _parse_cookies_line(stripped)
                break

    now = datetime.now(timezone.utc)
    coll.insert_one(
        {
            "account_id": "demo_bot_001",
            "enabled": True,
            "email": "CHANGE_ME@example.com",  # Replace for real use
            "password": "CHANGE_ME",
            "headless": False,
            "cookies": cookies,
            "timezone_id": "Asia/Dhaka",
            "proxy": None,
            "warmup_enabled": True,
            "warmup_complete": False,
            "warmup_duration_days": 7,
            "warmup_started_at": now,
            "created_at": now,
        },
    )
    print(f"Inserted demo bot into {db_name}.{coll_name} with {len(cookies)} cookies. Update email/password before real use.")


if __name__ == "__main__":
    main()
