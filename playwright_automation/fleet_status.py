"""Per-bot fleet health status persisted under ``profiles/<id>/fleet_status.json``."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_STARTING = "starting"
STATUS_RUNNING = "running"
STATUS_CHECKPOINT = "checkpoint"
STATUS_STOPPED = "stopped"
STATUS_ERROR = "error"
STATUS_CRASHED = "crashed"


@dataclass
class QuotaSnapshot:
    friends_sent: int = 0
    friends_target: int = 0
    posts_today: int = 0
    posts_target: int = 0
    shares_today: int = 0
    shares_target: int = 0


@dataclass
class FleetBotStatus:
    account_id: str
    state: str = STATUS_STARTING
    pid: int = 0
    mode: str = ""
    proxy_configured: bool = False
    last_activity: str = ""
    last_error: str = ""
    restart_count: int = 0
    checkpoint: bool = False
    quotas: QuotaSnapshot = field(default_factory=QuotaSnapshot)
    updated_at: str = ""

    def touch(self, *, state: str | None = None, error: str = "") -> None:
        if state:
            self.state = state
        if error:
            self.last_error = error[:500]
        self.last_activity = datetime.now(timezone.utc).isoformat()
        self.updated_at = self.last_activity


def status_path(profile_dir: Path) -> Path:
    return profile_dir / "fleet_status.json"


def write_status(profile_dir: Path, status: FleetBotStatus) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    status.updated_at = datetime.now(timezone.utc).isoformat()
    if not status.last_activity:
        status.last_activity = status.updated_at
    path = status_path(profile_dir)
    path.write_text(
        json.dumps(asdict(status), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_status(profile_dir: Path) -> FleetBotStatus | None:
    path = status_path(profile_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    quotas_raw = data.get("quotas") or {}
    return FleetBotStatus(
        account_id=str(data.get("account_id", "")),
        state=str(data.get("state", STATUS_STOPPED)),
        pid=int(data.get("pid", 0)),
        mode=str(data.get("mode", "")),
        proxy_configured=bool(data.get("proxy_configured")),
        last_activity=str(data.get("last_activity", "")),
        last_error=str(data.get("last_error", "")),
        restart_count=int(data.get("restart_count", 0)),
        checkpoint=bool(data.get("checkpoint")),
        quotas=QuotaSnapshot(
            friends_sent=int(quotas_raw.get("friends_sent", 0)),
            friends_target=int(quotas_raw.get("friends_target", 0)),
            posts_today=int(quotas_raw.get("posts_today", 0)),
            posts_target=int(quotas_raw.get("posts_target", 0)),
            shares_today=int(quotas_raw.get("shares_today", 0)),
            shares_target=int(quotas_raw.get("shares_target", 0)),
        ),
        updated_at=str(data.get("updated_at", "")),
    )


def load_quotas_from_profile(profile_dir: Path) -> QuotaSnapshot:
    snap = QuotaSnapshot()
    friend_path = profile_dir / "daily_friend_quota.json"
    post_path = profile_dir / "daily_post_quota.json"
    share_path = profile_dir / "daily_share_quota.json"
    try:
        if friend_path.is_file():
            f = json.loads(friend_path.read_text(encoding="utf-8"))
            snap.friends_sent = int(f.get("friends_sent_today", 0))
            snap.friends_target = int(f.get("daily_friend_target", 0))
    except Exception:
        pass
    try:
        if post_path.is_file():
            p = json.loads(post_path.read_text(encoding="utf-8"))
            snap.posts_today = int(p.get("posts_today", 0))
            snap.posts_target = int(p.get("daily_post_target", 0))
    except Exception:
        pass
    try:
        if share_path.is_file():
            s = json.loads(share_path.read_text(encoding="utf-8"))
            snap.shares_today = int(s.get("shares_today", 0))
    except Exception:
        pass
    snap.shares_target = 20
    return snap


def collect_fleet_statuses(profiles_root: Path) -> list[FleetBotStatus]:
    out: list[FleetBotStatus] = []
    if not profiles_root.is_dir():
        return out
    for profile_dir in sorted(profiles_root.iterdir()):
        if not profile_dir.is_dir():
            continue
        st = read_status(profile_dir)
        if st:
            out.append(st)
    return out


def send_alert(message: str, *, account_id: str = "", state: str = "") -> None:
    """Optional webhook alert (Telegram/Discord/generic POST)."""
    url = (os.environ.get("FLEET_ALERT_WEBHOOK") or "").strip()
    if not url:
        return
    try:
        import httpx

        payload: dict[str, Any] = {
            "text": message,
            "account_id": account_id,
            "state": state,
            "ts": time.time(),
        }
        httpx.post(url, json=payload, timeout=10.0)
    except Exception:
        pass
