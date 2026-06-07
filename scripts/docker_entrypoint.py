#!/usr/bin/env python3
"""Docker container entrypoint — stagger start, then run one bot."""

from __future__ import annotations

import os
import subprocess
import sys
import time


def main() -> None:
    account_id = (os.environ.get("ACCOUNT_ID") or "").strip()
    stagger_min = int(os.environ.get("FLEET_STAGGER_MIN", "15"))
    stagger_max = int(os.environ.get("FLEET_STAGGER_MAX", "90"))

    if account_id and stagger_max > 0:
        suffix = "".join(ch for ch in account_id[-4:] if ch.isdigit()) or "0"
        span = max(stagger_max - stagger_min + 1, 1)
        delay = stagger_min + int(suffix) % span
        print(f"[fleet] account={account_id} stagger={delay}s before start", flush=True)
        time.sleep(delay)

    if not account_id:
        print("ACCOUNT_ID is required", file=sys.stderr)
        raise SystemExit(1)

    mode = os.environ.get("FLEET_AGENT_MODE", "structured")
    cmd = [
        sys.executable,
        "scripts/run_agent_brain.py",
        "--account-id",
        account_id,
        "--fleet-mode",
        "--headless",
        "--close-on-exit",
        "--mode",
        mode,
        "--registry-file",
        "/app/accounts/accounts.json",
        "--cookies-file",
        "/app/cookies.txt",
    ]
    os.execv(sys.executable, cmd)


if __name__ == "__main__":
    main()
