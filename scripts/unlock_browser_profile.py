#!/usr/bin/env python3
"""Release Chromium profile locks and stop stale Playwright Chrome processes."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from playwright_automation.account_session import DEFAULT_ACCOUNT_ID  # noqa: E402
from playwright_automation.browser_profile import (  # noqa: E402
    browser_user_data_dir,
    kill_chrome_using_profile,
    prepare_persistent_profile,
    release_chromium_locks,
)


async def _main(account_id: str, *, kill: bool) -> int:
    profile_dir = (_ROOT / "profiles" / account_id).resolve()
    browser_dir = browser_user_data_dir(profile_dir)
    if not profile_dir.is_dir():
        print(f"No profile folder: {profile_dir}")
        return 1

    print(f"Account dir: {profile_dir}")
    print(f"Browser dir: {browser_dir}")

    if kill:
        n = kill_chrome_using_profile(browser_dir)
        print(f"Stopped {n} chrome.exe process(es) using this profile.")
        await asyncio.sleep(1.0)

    locks = release_chromium_locks(browser_dir)
    print(f"Removed {locks} lock file(s).")
    await prepare_persistent_profile(browser_dir, force_kill=False)
    print("Profile ready — run: python scripts/run_agent_brain.py")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Unlock Chromium profile for run_agent_brain.")
    p.add_argument("--account-id", default=DEFAULT_ACCOUNT_ID)
    p.add_argument(
        "--kill-chrome",
        action="store_true",
        help="Stop chrome.exe processes bound to this profile (recommended)",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(_main(args.account_id, kill=args.kill_chrome)))


if __name__ == "__main__":
    main()
