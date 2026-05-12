"""Seed all 15 Facebook accounts from cookies.txt into MongoDB."""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


def _parse_cookies_line(line: str) -> list[dict[str, Any]]:
    """Parse 'name=value;name2=value2' into Playwright cookie dicts."""
    cookies = []
    for pair in line.strip().split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
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
        print(f"{db_name}.{coll_name} already has {coll.count_documents({})} documents. Replacing with cookies.txt accounts...")
        coll.delete_many({})
        print("Existing documents deleted.")

    cookies_path = Path(__file__).resolve().parent.parent / "cookies.txt"
    content = cookies_path.read_text(encoding="utf-8")
    lines = [ln.rstrip("\n") for ln in content.splitlines()]

    now = datetime.now(timezone.utc)
    inserted = 0
    i = 0
    while i < len(lines):
        # Each block: account_id, blank, password, blank, cookie_line
        account_line = lines[i].strip()
        if not account_line or not account_line.isdigit():
            i += 1
            continue
        account_id = account_line
        if i + 1 >= len(lines):
            break
        # password at i+2? Let's follow: line1=id, line2=blank, line3=password, line4=blank, line5=cookies
        password_line = lines[i + 2].strip() if i + 2 < len(lines) else ""
        cookie_line = lines[i + 4].strip() if i + 4 < len(lines) else ""
        cookies = _parse_cookies_line(cookie_line) if cookie_line else []
        doc = {
            "account_id": account_id,
            "enabled": True,
            "email": account_id,  # use numeric ID as login username
            "password": password_line,
            "headless": False,
            "cookies": cookies,
            "timezone_id": "Asia/Dhaka",
            "proxy": None,
            "warmup_enabled": True,
            "warmup_complete": False,
            "warmup_duration_days": 7,
            "warmup_started_at": now,
            "created_at": now,
        }
        coll.insert_one(doc)
        print(f"Inserted {account_id} with {len(cookies)} cookies")
        inserted += 1
        i += 6  # move to next block (assuming blank line after each block)

    print(f"\nDone. Inserted {inserted} accounts.")


if __name__ == "__main__":
    main()
