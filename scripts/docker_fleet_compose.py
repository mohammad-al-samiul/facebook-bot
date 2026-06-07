#!/usr/bin/env python3
"""
Build docker-compose.fleet.yml — one container per account from accounts.json / cookies.txt.

Run automatically from start_fleet_docker.bat (one-click fleet start).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from playwright_automation.account_registry import (  # noqa: E402
    DEFAULT_COOKIES_PATH,
    DEFAULT_REGISTRY_PATH,
    AccountRecord,
    load_registry,
    migrate_cookies_txt_to_json,
)

FLEET_COMPOSE = _ROOT / "docker-compose.fleet.yml"


def _safe_service_name(account_id: str) -> str:
    return f"bot_{account_id}"


def _yaml_quote(value: str) -> str:
    return json.dumps(value)


def _service_block(rec: AccountRecord) -> str:
    name = _safe_service_name(rec.account_id)
    proxy = rec.proxy_url.strip()
    proxy_env = f"      PROXY_URL: {_yaml_quote(proxy)}" if proxy else '      PROXY_URL: ""'
    return f"""  {name}:
    image: bot-agent:fleet
    container_name: {name}
    restart: unless-stopped
    mem_limit: 768m
    cpus: "0.4"
    environment:
      ACCOUNT_ID: {_yaml_quote(rec.account_id)}
{proxy_env}
      FLEET_MODE: "1"
      FLEET_AGENT_MODE: "structured"
      FLEET_STAGGER_MIN: "${{FLEET_STAGGER_MIN:-15}}"
      FLEET_STAGGER_MAX: "${{FLEET_STAGGER_MAX:-90}}"
      FLEET_OLLAMA_MIN_INTERVAL_SEC: "8.0"
      OLLAMA_HOST: "${{OLLAMA_HOST:-host.docker.internal:11434}}"
    volumes:
      - ./profiles/{rec.account_id}:/app/profiles/{rec.account_id}
      - ./accounts/accounts.json:/app/accounts/accounts.json:ro
      - ./cookies.txt:/app/cookies.txt:ro
    extra_hosts:
      - "host.docker.internal:host-gateway"
"""


def _ensure_registry(registry_path: Path, cookies_path: Path) -> list[AccountRecord]:
    if not registry_path.is_file() and cookies_path.is_file():
        migrate_cookies_txt_to_json(cookies_path, registry_path)
        print(f"Created {registry_path} from {cookies_path}")

    records = load_registry(registry_path)
    if not records and cookies_path.is_file():
        migrate_cookies_txt_to_json(cookies_path, registry_path)
        records = load_registry(registry_path)

    valid = [r for r in records if r.account_id and r.password]
    return valid


def generate_fleet_compose(
    *,
    registry_path: Path,
    cookies_path: Path,
    output_path: Path,
    max_bots: int = 0,
) -> int:
    records = _ensure_registry(registry_path, cookies_path)
    if not records:
        raise SystemExit(
            "No accounts found. Add accounts/accounts.json or cookies.txt (id, password, cookies per account)."
        )

    if max_bots > 0:
        records = records[:max_bots]

    header = """# Auto-generated — one Docker container per Facebook account.
# Sessions persist in ./profiles/<account_id>/ (login once, reuse next time).
# Start all:  scripts/start_fleet_docker.bat
# Stop all:   scripts/stop_fleet_docker.bat
# Status:     python scripts/fleet_launcher.py --status

services:
  # Shared image — built once by start_fleet_docker.bat
"""
    blocks = [_service_block(rec) for rec in records]
    output_path.write_text(header + "\n".join(blocks) + "\n", encoding="utf-8")
    return len(records)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--registry", default=str(DEFAULT_REGISTRY_PATH))
    p.add_argument("--cookies-file", default=str(DEFAULT_COOKIES_PATH))
    p.add_argument("--output", default=str(FLEET_COMPOSE))
    p.add_argument("--max-bots", type=int, default=0, help="0 = all accounts")
    args = p.parse_args()

    count = generate_fleet_compose(
        registry_path=Path(args.registry).expanduser().resolve(),
        cookies_path=Path(args.cookies_file).expanduser().resolve(),
        output_path=Path(args.output).expanduser().resolve(),
        max_bots=args.max_bots,
    )
    print(f"Generated {count} bot container(s) -> {args.output}")


if __name__ == "__main__":
    main()
