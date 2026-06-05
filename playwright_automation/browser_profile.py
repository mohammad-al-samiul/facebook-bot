"""Chromium profile lock cleanup and stale process handling (Windows-safe)."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger(__name__)

# Chromium lock artifacts that block launch_persistent_context when stale.
_LOCK_NAMES: tuple[str, ...] = (
    "lockfile",
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
)


def release_chromium_locks(user_data_dir: Path) -> int:
    """Remove stale Chromium lock files. Returns count removed."""
    removed = 0
    if not user_data_dir.is_dir():
        return 0
    targets = [user_data_dir, user_data_dir / "Default"]
    for base in targets:
        if not base.is_dir():
            continue
        for name in _LOCK_NAMES:
            path = base / name
            try:
                if path.exists() or path.is_symlink():
                    path.unlink(missing_ok=True)
                    removed += 1
                    _log.info("Removed stale Chromium lock: %s", path)
            except OSError as exc:
                _log.debug("Could not remove lock %s: %s", path, exc)
    return removed


def kill_chrome_using_profile(user_data_dir: Path) -> int:
    """
    Terminate Chrome/Chromium processes bound to ``user_data_dir``.

    Only targets Playwright/automation profiles (path contains ``profiles``).
    """
    resolved = str(user_data_dir.resolve())
    if "profiles" not in resolved.replace("\\", "/").lower():
        _log.debug("Skip kill — not under profiles/: %s", resolved)
        return 0

    killed = 0
    if sys.platform == "win32":
        # Match command lines containing this user-data-dir (forward or back slashes).
        needle = resolved.replace("'", "''")
        ps = (
            f"$p = '{needle}'; "
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -and ($_.CommandLine -like ('*' + $p + '*')) } | "
            "ForEach-Object { "
            "  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
            "  $_.ProcessId "
            "}"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if line.isdigit():
                    killed += 1
            if killed:
                _log.info("Stopped %d chrome.exe process(es) for profile %s", killed, resolved)
        except Exception as exc:
            _log.debug("kill_chrome_using_profile failed: %s", exc)
    else:
        try:
            proc = subprocess.run(
                ["pgrep", "-f", resolved],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            for pid in (proc.stdout or "").split():
                if pid.strip().isdigit():
                    os.kill(int(pid.strip()), 9)
                    killed += 1
        except Exception as exc:
            _log.debug("kill_chrome_using_profile failed: %s", exc)
    return killed


async def prepare_persistent_profile(
    user_data_dir: Path,
    *,
    force_kill: bool = False,
    settle_sec: float = 0.8,
) -> None:
    """Clear locks and optionally stop stale browsers before Playwright launch."""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    if force_kill:
        kill_chrome_using_profile(user_data_dir)
        await asyncio.sleep(settle_sec)
    release_chromium_locks(user_data_dir)


def browser_user_data_dir(account_profile_dir: Path) -> Path:
    """
    Directory passed to ``launch_persistent_context``.

    Uses ``account_profile_dir`` when it already contains Chromium data (legacy),
    otherwise ``account_profile_dir/chromium`` for new accounts.
    """
    account_profile_dir = account_profile_dir.resolve()
    legacy_markers = (
        account_profile_dir / "Default",
        account_profile_dir / "Local State",
    )
    if any(m.exists() for m in legacy_markers):
        return account_profile_dir
    return account_profile_dir / "chromium"
