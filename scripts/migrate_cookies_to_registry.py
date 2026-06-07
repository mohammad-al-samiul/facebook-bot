#!/usr/bin/env python3
"""Migrate legacy cookies.txt (3 lines per account) to accounts/accounts.json."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from playwright_automation.account_registry import (  # noqa: E402
    DEFAULT_COOKIES_PATH,
    DEFAULT_REGISTRY_PATH,
    migrate_cookies_txt_to_json,
)


def _regenerate_docker_compose(registry_path: Path, cookies_path: Path) -> int:
    compose_script = _ROOT / "scripts" / "docker_fleet_compose.py"
    if not compose_script.is_file():
        return 0
    result = subprocess.run(
        [sys.executable, str(compose_script), "--registry", str(registry_path), "--cookies-file", str(cookies_path)],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        return 0
    line = (result.stdout or "").strip().splitlines()[-1] if result.stdout else ""
    if line:
        print(line)
    return 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cookies-file", default=str(DEFAULT_COOKIES_PATH))
    p.add_argument("--output", default=str(DEFAULT_REGISTRY_PATH))
    p.add_argument("--no-docker", action="store_true", help="Skip docker-compose.fleet.yml generation")
    args = p.parse_args()

    cookies_path = Path(args.cookies_file).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    if not cookies_path.is_file():
        print(f"Not found: {cookies_path}")
        raise SystemExit(1)

    count = migrate_cookies_txt_to_json(cookies_path, output_path)
    print(f"Migrated {count} account(s) -> {output_path}")
    print(f"Each account has a 'proxy' field (empty until you fill it).")

    if not args.no_docker:
        _regenerate_docker_compose(output_path, cookies_path)

    print()
    print("Next steps:")
    print("  1. (Optional) Edit accounts/accounts.json - add proxy per account")
    print("  2. Test one bot:")
    print("     python scripts/run_agent_brain.py --account-id <FIRST_ACCOUNT_ID>")
    print("  3. Start all bots (Docker):")
    print("     scripts\\start_fleet_docker.bat")
    print("  Or without Docker:")
    print("     python scripts/fleet_launcher.py --max-bots 10 --phase 1")


if __name__ == "__main__":
    main()
