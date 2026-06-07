#!/usr/bin/env python3
"""
Launch one subprocess per Facebook account with staggered startup, auto-restart, and status tracking.

Fleet defaults: headless, structured mode (low LLM load), close browser on exit.

Run::

    python scripts/fleet_launcher.py
    python scripts/fleet_launcher.py --max-bots 10 --phase 1
    python scripts/fleet_launcher.py --status
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from playwright_automation.account_registry import (  # noqa: E402
    DEFAULT_REGISTRY_PATH,
    AccountRecord,
    list_account_ids,
    load_account,
    load_registry,
)
from playwright_automation.fleet_status import (  # noqa: E402
    STATUS_CRASHED,
    STATUS_RUNNING,
    STATUS_STARTING,
    STATUS_STOPPED,
    collect_fleet_statuses,
    read_status,
    send_alert,
)

log = logging.getLogger("fleet_launcher")

PHASE_LIMITS = {1: 10, 2: 50, 3: 600}
RUN_SCRIPT = _ROOT / "scripts" / "run_agent_brain.py"


@dataclass
class ManagedBot:
    record: AccountRecord
    process: subprocess.Popen[Any] | None = None
    restart_count: int = 0
    last_start: float = 0.0


@dataclass
class FleetConfig:
    max_bots: int = 0
    phase: int = 1
    stagger_min: float = 30.0
    stagger_max: float = 120.0
    wave_size: int = 5
    max_restarts: int = 3
    restart_delay: float = 60.0
    mode: str = "structured"
    headless: bool = True
    registry_path: Path = field(default_factory=lambda: DEFAULT_REGISTRY_PATH)
    account_ids: list[str] = field(default_factory=list)
    dry_run: bool = False


def _phase_limit(phase: int, max_bots: int) -> int:
    cap = PHASE_LIMITS.get(phase, 600)
    if max_bots > 0:
        return min(max_bots, cap)
    return cap


def _resolve_accounts(cfg: FleetConfig) -> list[AccountRecord]:
    if cfg.account_ids:
        by_id = {r.account_id: r for r in load_registry(cfg.registry_path)}
        out: list[AccountRecord] = []
        for aid in cfg.account_ids:
            rec = by_id.get(aid)
            if rec:
                out.append(rec)
            else:
                log.warning("Account %s not in registry — skipped", aid)
        return out

    records = load_registry(cfg.registry_path)
    if records:
        return records[: _phase_limit(cfg.phase, cfg.max_bots)]

    ids = list_account_ids(registry_path=cfg.registry_path)
    limit = _phase_limit(cfg.phase, cfg.max_bots)
    out: list[AccountRecord] = []
    for aid in ids[:limit]:
        rec = load_account(aid, registry_path=cfg.registry_path)
        if rec:
            out.append(rec)
    return out


def _bot_command(rec: AccountRecord, cfg: FleetConfig) -> list[str]:
    cmd = [
        sys.executable,
        str(RUN_SCRIPT),
        "--account-id",
        rec.account_id,
        "--mode",
        cfg.mode,
        "--fleet-mode",
        "--close-on-exit",
    ]
    if cfg.headless:
        cmd.append("--headless")
    if rec.proxy_url:
        cmd.append("--proxy")
        cmd.append(rec.proxy_url)
    if cfg.registry_path.is_file():
        cmd.extend(["--registry-file", str(cfg.registry_path)])
    return cmd


def _bot_env(rec: AccountRecord) -> dict[str, str]:
    env = os.environ.copy()
    env["FLEET_MODE"] = "1"
    env["ACCOUNT_ID"] = rec.account_id
    if rec.password:
        env["PASSWORD"] = rec.password
    if rec.proxy_url:
        env["PROXY_URL"] = rec.proxy_url
    # Shared Ollama pool: minimum gap between LLM calls per bot process.
    env.setdefault("FLEET_OLLAMA_MIN_INTERVAL_SEC", "8.0")
    return env


def _start_bot(rec: AccountRecord, cfg: FleetConfig) -> ManagedBot:
    mb = ManagedBot(record=rec, last_start=time.time())
    profile_dir = _ROOT / "profiles" / rec.account_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    from playwright_automation.fleet_status import FleetBotStatus, write_status

    write_status(
        profile_dir,
        FleetBotStatus(
            account_id=rec.account_id,
            state=STATUS_STARTING,
            mode=cfg.mode,
            proxy_configured=bool(rec.proxy_url),
        ),
    )

    proc = subprocess.Popen(
        _bot_command(rec, cfg),
        cwd=str(_ROOT),
        env=_bot_env(rec),
    )
    mb.process = proc
    log.info("Started bot %s (pid=%s)", rec.account_id, proc.pid)
    return mb


def _is_running(mb: ManagedBot) -> bool:
    return mb.process is not None and mb.process.poll() is None


def _print_fleet_status() -> None:
    profiles_root = _ROOT / "profiles"
    rows = collect_fleet_statuses(profiles_root)
    if not rows:
        print("No fleet status files found under profiles/")
        return
    print(f"{'ACCOUNT':<20} {'STATE':<12} {'MODE':<12} {'PROXY':<6} {'RESTARTS':<8} LAST_ACTIVITY")
    print("-" * 90)
    for st in rows:
        print(
            f"{st.account_id:<20} {st.state:<12} {st.mode:<12} "
            f"{'yes' if st.proxy_configured else 'no':<6} {st.restart_count:<8} {st.last_activity}"
        )


def run_fleet(cfg: FleetConfig) -> None:
    accounts = _resolve_accounts(cfg)
    if not accounts:
        log.error(
            "No accounts found. Add accounts/accounts.json or run: "
            "python scripts/migrate_cookies_to_registry.py"
        )
        raise SystemExit(1)

    limit = _phase_limit(cfg.phase, cfg.max_bots)
    accounts = accounts[:limit]
    log.info(
        "Fleet phase %d | launching %d bot(s) | mode=%s | stagger=%.0f-%.0f s | wave=%d",
        cfg.phase,
        len(accounts),
        cfg.mode,
        cfg.stagger_min,
        cfg.stagger_max,
        cfg.wave_size,
    )

    if cfg.dry_run:
        for rec in accounts:
            log.info("[dry-run] %s | proxy=%s | cmd=%s", rec.account_id, bool(rec.proxy_url), _bot_command(rec, cfg))
        return

    managed: dict[str, ManagedBot] = {}
    pending = list(accounts)
    wave_count = 0

    try:
        while pending or managed:
            # Start next wave
            while pending and len([m for m in managed.values() if _is_running(m)]) < cfg.wave_size:
                rec = pending.pop(0)
                if not rec.password:
                    log.warning(
                        "Account %s has no password in registry — "
                        "ensure accounts.json or PASSWORD env",
                        rec.account_id,
                    )
                mb = _start_bot(rec, cfg)
                managed[rec.account_id] = mb
                wave_count += 1
                if pending:
                    delay = random.uniform(cfg.stagger_min, cfg.stagger_max)
                    log.info("Stagger wait %.1fs before next bot", delay)
                    time.sleep(delay)

            # Monitor running bots
            for aid, mb in list(managed.items()):
                if _is_running(mb):
                    profile_dir = _ROOT / "profiles" / aid
                    st = read_status(profile_dir)
                    if st and st.checkpoint:
                        send_alert(
                            f"Bot {aid} hit Facebook checkpoint",
                            account_id=aid,
                            state=STATUS_RUNNING,
                        )
                    continue

                exit_code = mb.process.poll() if mb.process else -1
                profile_dir = _ROOT / "profiles" / aid
                from playwright_automation.fleet_status import FleetBotStatus, write_status

                if exit_code == 0:
                    write_status(
                        profile_dir,
                        FleetBotStatus(
                            account_id=aid,
                            state=STATUS_STOPPED,
                            restart_count=mb.restart_count,
                        ),
                    )
                    log.info("Bot %s exited cleanly — not restarting", aid)
                    del managed[aid]
                    continue

                mb.restart_count += 1
                state = STATUS_CRASHED
                write_status(
                    profile_dir,
                    FleetBotStatus(
                        account_id=aid,
                        state=state,
                        restart_count=mb.restart_count,
                        last_error=f"exit code {exit_code}",
                    ),
                )
                send_alert(
                    f"Bot {aid} crashed (exit {exit_code}), restart {mb.restart_count}/{cfg.max_restarts}",
                    account_id=aid,
                    state=state,
                )

                if mb.restart_count > cfg.max_restarts:
                    log.error("Bot %s exceeded max restarts — giving up", aid)
                    del managed[aid]
                    continue

                log.warning(
                    "Restarting bot %s in %.0fs (attempt %d/%d)",
                    aid,
                    cfg.restart_delay,
                    mb.restart_count,
                    cfg.max_restarts,
                )
                time.sleep(cfg.restart_delay)
                new_mb = _start_bot(mb.record, cfg)
                new_mb.restart_count = mb.restart_count
                managed[aid] = new_mb

            if managed:
                time.sleep(5.0)
    except KeyboardInterrupt:
        log.info("Fleet shutdown requested — stopping all bots")
        for mb in managed.values():
            if mb.process and mb.process.poll() is None:
                mb.process.terminate()
                try:
                    mb.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    mb.process.kill()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--registry-file", default=str(DEFAULT_REGISTRY_PATH))
    p.add_argument("--account-id", action="append", dest="account_ids", default=[])
    p.add_argument("--max-bots", type=int, default=0, help="Cap bots (0 = phase default)")
    p.add_argument("--phase", type=int, choices=(1, 2, 3), default=1)
    p.add_argument("--stagger-min", type=float, default=30.0)
    p.add_argument("--stagger-max", type=float, default=120.0)
    p.add_argument("--wave-size", type=int, default=5)
    p.add_argument("--max-restarts", type=int, default=3)
    p.add_argument("--restart-delay", type=float, default=60.0)
    p.add_argument(
        "--mode",
        choices=("structured", "brain"),
        default="structured",
        help="Fleet default: structured (low LLM load)",
    )
    p.add_argument("--brain", dest="mode", action="store_const", const="brain")
    p.add_argument("--no-headless", action="store_true")
    p.add_argument("--status", action="store_true", help="Print fleet status and exit")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.status:
        _print_fleet_status()
        return

    cfg = FleetConfig(
        max_bots=args.max_bots,
        phase=args.phase,
        stagger_min=args.stagger_min,
        stagger_max=args.stagger_max,
        wave_size=args.wave_size,
        max_restarts=args.max_restarts,
        restart_delay=args.restart_delay,
        mode=args.mode,
        headless=not args.no_headless,
        registry_path=Path(args.registry_file).expanduser().resolve(),
        account_ids=args.account_ids,
        dry_run=args.dry_run,
    )
    run_fleet(cfg)


if __name__ == "__main__":
    main()
