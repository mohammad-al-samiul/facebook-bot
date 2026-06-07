#!/usr/bin/env python3
"""Generate docker-compose.override.yml with N bot services from accounts/accounts.json."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY = _ROOT / "accounts" / "accounts.json"


def _service_block(index: int, account_id: str) -> str:
    name = f"bot{index}"
    return f"""  {name}:
    build: .
    image: bot-agent:latest
    restart: unless-stopped
    mem_limit: 1g
    cpus: "0.5"
    environment:
      ACCOUNT_ID: "{account_id}"
      PASSWORD: "${{{name.upper()}_PASSWORD}}"
      PROXY_URL: "${{{name.upper()}_PROXY_URL:-}}"
      FLEET_MODE: "1"
      FLEET_OLLAMA_MIN_INTERVAL_SEC: "8.0"
      OLLAMA_HOST: "${{OLLAMA_HOST:-host.docker.internal:11434}}"
    volumes:
      - ./profiles/{account_id}:/app/profiles/{account_id}
      - ./accounts/accounts.json:/app/accounts/accounts.json:ro
    extra_hosts:
      - "host.docker.internal:host-gateway"
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--output", default=str(_ROOT / "docker-compose.override.yml"))
    args = p.parse_args()

    registry = Path(args.registry).expanduser().resolve()
    if not registry.is_file():
        print(f"Registry not found: {registry}")
        raise SystemExit(1)

    payload = json.loads(registry.read_text(encoding="utf-8"))
    items = payload if isinstance(payload, list) else payload.get("accounts", [])
    ids = [str(x.get("id") or x.get("account_id", "")).strip() for x in items if isinstance(x, dict)]
    ids = [i for i in ids if i][: args.count]

    lines = ["services:"]
    for i, aid in enumerate(ids, start=1):
        lines.append(_service_block(i, aid).rstrip())

    out = Path(args.output).expanduser().resolve()
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(ids)} service(s) → {out}")


if __name__ == "__main__":
    main()
