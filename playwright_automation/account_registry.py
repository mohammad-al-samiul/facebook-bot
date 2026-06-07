"""Account registry: JSON, per-account env files, and legacy cookies.txt."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright_automation.account_session import (
    DEFAULT_COOKIES_PATH,
    cookie_string_to_dicts,
    parse_account_block_from_cookies,
)

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = _ROOT / "accounts" / "accounts.json"
DEFAULT_ACCOUNTS_DIR = _ROOT / "accounts"


@dataclass(frozen=True, slots=True)
class AccountRecord:
    """Credentials and optional proxy for one Facebook account."""

    account_id: str
    password: str
    cookies_raw: str = ""
    proxy_url: str = ""

    @property
    def cookies(self) -> list[dict[str, Any]]:
        if not self.cookies_raw.strip():
            return []
        return cookie_string_to_dicts(self.cookies_raw)


def parse_proxy_url(raw: str | None) -> dict[str, str] | None:
    """Convert ``http://user:pass@host:port`` to Playwright proxy dict."""
    text = (raw or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    if not parsed.hostname:
        return None
    scheme = parsed.scheme or "http"
    port = parsed.port
    server = f"{scheme}://{parsed.hostname}"
    if port:
        server = f"{server}:{port}"
    out: dict[str, str] = {"server": server}
    if parsed.username:
        out["username"] = parsed.username
    if parsed.password:
        out["password"] = parsed.password
    return out


def resolve_proxy(
    cli_proxy: str | None = None,
    *,
    account_proxy: str = "",
) -> dict[str, str] | None:
    """CLI ``--proxy`` > ``PROXY_URL`` env > account registry proxy."""
    for candidate in (cli_proxy, os.environ.get("PROXY_URL"), account_proxy):
        proxy = parse_proxy_url(candidate)
        if proxy:
            return proxy
    return None


def _record_from_dict(data: dict[str, Any]) -> AccountRecord | None:
    account_id = str(data.get("id") or data.get("account_id") or "").strip()
    password = str(data.get("password") or "").strip()
    if not account_id or not password:
        return None
    cookies_raw = str(data.get("cookies") or data.get("cookie") or "").strip()
    proxy_url = str(data.get("proxy") or data.get("proxy_url") or "").strip()
    return AccountRecord(
        account_id=account_id,
        password=password,
        cookies_raw=cookies_raw,
        proxy_url=proxy_url,
    )


def _load_env_account(path: Path) -> AccountRecord | None:
    if not path.is_file():
        return None
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip().upper()] = val.strip()
    account_id = values.get("ACCOUNT_ID", "").strip()
    password = values.get("PASSWORD", "").strip()
    if not account_id or not password:
        return None
    return AccountRecord(
        account_id=account_id,
        password=password,
        cookies_raw=values.get("COOKIES", "").strip(),
        proxy_url=values.get("PROXY_URL", values.get("PROXY", "")).strip(),
    )


def load_registry(path: Path | None = None) -> list[AccountRecord]:
    """Load all accounts from JSON registry and ``accounts/*.env`` files."""
    registry_path = path or DEFAULT_REGISTRY_PATH
    records: dict[str, AccountRecord] = {}

    if registry_path.is_file():
        try:
            payload = json.loads(registry_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        items = payload if isinstance(payload, list) else payload.get("accounts", [])
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                rec = _record_from_dict(item)
                if rec:
                    records[rec.account_id] = rec

    if DEFAULT_ACCOUNTS_DIR.is_dir():
        for env_path in sorted(DEFAULT_ACCOUNTS_DIR.glob("*.env")):
            rec = _load_env_account(env_path)
            if rec:
                records[rec.account_id] = rec

    return list(records.values())


def load_account(
    account_id: str,
    *,
    registry_path: Path | None = None,
    cookies_path: Path | None = None,
    password_override: str = "",
    proxy_override: str = "",
) -> AccountRecord | None:
    """Resolve one account: registry/env > legacy cookies.txt."""
    aid = account_id.strip()
    if not aid:
        return None

    for rec in load_registry(registry_path):
        if rec.account_id == aid:
            return AccountRecord(
                account_id=rec.account_id,
                password=password_override or rec.password,
                cookies_raw=rec.cookies_raw,
                proxy_url=proxy_override or rec.proxy_url,
            )

    cookies_file = cookies_path or DEFAULT_COOKIES_PATH
    parsed = parse_account_block_from_cookies(cookies_file, aid)
    if parsed:
        pwd, cookie_dicts = parsed
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookie_dicts)
        return AccountRecord(
            account_id=aid,
            password=password_override or pwd,
            cookies_raw=cookie_str,
            proxy_url=proxy_override,
        )

    if password_override:
        return AccountRecord(
            account_id=aid,
            password=password_override,
            proxy_url=proxy_override,
        )
    return None


def list_account_ids(
    *,
    registry_path: Path | None = None,
    cookies_path: Path | None = None,
) -> list[str]:
    """All known account IDs from registry and cookies.txt."""
    ids: list[str] = []
    seen: set[str] = set()

    for rec in load_registry(registry_path):
        if rec.account_id not in seen:
            seen.add(rec.account_id)
            ids.append(rec.account_id)

    cookies_file = cookies_path or DEFAULT_COOKIES_PATH
    if cookies_file.is_file():
        lines = [
            ln.strip()
            for ln in cookies_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            if ln.strip()
        ]
        for i in range(0, len(lines), 3):
            if i + 2 >= len(lines):
                break
            uid = lines[i]
            if uid and uid not in seen:
                seen.add(uid)
                ids.append(uid)
    return ids


def dedupe_cookie_string(raw: str) -> str:
    """Remove duplicate cookie keys (some cookies.txt lines repeat the same block)."""
    seen: set[str] = set()
    parts: list[str] = []
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name = pair.split("=", 1)[0].strip()
        if name in seen:
            continue
        seen.add(name)
        parts.append(pair)
    return "; ".join(parts)


def _load_existing_proxies(output_path: Path) -> dict[str, str]:
    if not output_path.is_file():
        return {}
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    items = payload if isinstance(payload, list) else payload.get("accounts", [])
    out: dict[str, str] = {}
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            uid = str(item.get("id") or item.get("account_id") or "").strip()
            proxy = str(item.get("proxy") or item.get("proxy_url") or "").strip()
            if uid and proxy:
                out[uid] = proxy
    return out


def migrate_cookies_txt_to_json(
    cookies_path: Path,
    output_path: Path,
    *,
    proxy_by_id: dict[str, str] | None = None,
) -> int:
    """Convert legacy 3-line cookies.txt blocks into ``accounts.json``."""
    if not cookies_path.is_file():
        return 0
    lines = [
        ln.strip()
        for ln in cookies_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if ln.strip()
    ]
    accounts: list[dict[str, str]] = []
    proxies = {**_load_existing_proxies(output_path), **(proxy_by_id or {})}
    for i in range(0, len(lines), 3):
        if i + 2 >= len(lines):
            break
        uid, pwd, cookie_str = lines[i], lines[i + 1], lines[i + 2]
        entry: dict[str, str] = {
            "id": uid,
            "password": pwd,
            "cookies": dedupe_cookie_string(cookie_str),
            "proxy": proxies.get(uid, ""),
        }
        accounts.append(entry)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({"accounts": accounts}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return len(accounts)


def validate_account_id(account_id: str) -> bool:
    return bool(re.fullmatch(r"\d{5,20}", account_id.strip()))
