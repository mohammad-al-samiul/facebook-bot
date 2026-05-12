#!/usr/bin/env python3
"""
Diagnose why MongoDB Atlas connection is failing.

Tests in order (stops at first failure with a clear root cause):

1. Is MONGO_URI being read from ``.env``?
2. SRV TXT/CNAME DNS lookup (Atlas cluster -> shard hosts).
3. TCP socket connection (port 27017) to every shard host.
4. TLS handshake on port 27017.
5. ``ping`` admin command (auth + replica set primary detection).
"""

from __future__ import annotations

import os
import socket
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env", override=False)


def _section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def _fail(reason: str, hint: str) -> None:
    print()
    print("[FAIL] Problem detected:")
    print(f"   {reason}")
    print()
    print("[HINT] Possible fix:")
    print(f"   {hint}")
    sys.exit(1)


def _ok(msg: str) -> None:
    print(f"   [OK] {msg}")


def main() -> None:
    uri = os.environ.get("MONGO_URI")
    if not uri:
        _fail("MONGO_URI not found in .env.", "Add MONGO_URI=mongodb+srv://... to the `.env` file.")

    _section("[1/5] MONGO_URI parse")
    parsed = urlparse(uri)
    if parsed.scheme not in ("mongodb", "mongodb+srv"):
        _fail(f"Unknown scheme: {parsed.scheme!r}", "URI must start with 'mongodb+srv://' or 'mongodb://'.")
    host = parsed.hostname
    is_srv = parsed.scheme == "mongodb+srv"
    print(f"   scheme : {parsed.scheme}")
    print(f"   host   : {host}")
    print(f"   db     : {(parsed.path or '/').lstrip('/') or '(none)'}")
    _ok("URI valid")

    _section("[2/5] DNS lookup (SRV records -> shard hosts)")
    try:
        import dns.resolver  # type: ignore[import-not-found]
    except ImportError:
        _fail(
            "`dnspython` is not installed — `mongodb+srv://` URIs cannot be resolved without it.",
            "Run: pip install dnspython  (or `pip install \"pymongo[srv]\"`)",
        )

    shard_hosts: list[str] = []
    if is_srv:
        try:
            answers = dns.resolver.resolve(f"_mongodb._tcp.{host}", "SRV", lifetime=10)
            for r in answers:
                target = str(r.target).rstrip(".")
                port = int(r.port)
                shard_hosts.append(f"{target}:{port}")
                print(f"   SRV: {target}:{port}")
        except Exception as exc:
            _fail(
                f"SRV lookup failed ({type(exc).__name__}: {exc})",
                "The DNS resolver cannot fetch the Atlas _mongodb._tcp record. "
                "Try a public DNS (8.8.8.8 / 1.1.1.1); your ISP DNS may be "
                "blocking it.",
            )
        if not shard_hosts:
            _fail("No SRV records found.", "Atlas cluster name may be wrong — verify it in the Atlas console.")
    else:
        shard_hosts = [f"{host}:{parsed.port or 27017}"]
        _ok("non-SRV URI — direct hosts")

    _ok(f"Found {len(shard_hosts)} shard host(s)")

    _section("[3/5] TCP socket connection (port 27017)")
    tcp_results: list[tuple[str, bool, str]] = []
    for hp in shard_hosts:
        h, _, p = hp.partition(":")
        port = int(p) if p else 27017
        t0 = time.monotonic()
        try:
            sock = socket.create_connection((h, port), timeout=8)
            sock.close()
            ms = (time.monotonic() - t0) * 1000
            tcp_results.append((hp, True, f"{ms:.0f}ms"))
            _ok(f"{hp} — connected ({ms:.0f}ms)")
        except socket.timeout:
            tcp_results.append((hp, False, "timeout"))
            print(f"   [X] {hp} — TIMEOUT")
        except OSError as exc:
            tcp_results.append((hp, False, str(exc)))
            print(f"   [X] {hp} — {exc}")

    reachable = [r for r in tcp_results if r[1]]
    if not reachable:
        _fail(
            "TCP port 27017 is not reachable on any shard host.",
            "This is the most common cause:\n"
            "   - Many Bangladeshi ISPs (Robi, GP, BL, BTCL) block outbound port 27017.\n"
            "   - Test from mobile data or a VPN. If it works there, ISP block is confirmed.\n"
            "   - Fix: use a VPN (Cloudflare WARP free / Proton / NordVPN, etc.)\n"
            "     or switch to the Atlas free 'Serverless' tier (uses an HTTPS API).",
        )

    _section("[4/5] TLS handshake")
    sample_host = reachable[0][0]
    h, _, p = sample_host.partition(":")
    port = int(p) if p else 27017
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((h, port), timeout=10) as raw:
            with ctx.wrap_socket(raw, server_hostname=h) as tls:
                cert = tls.getpeercert()
                _ok(f"TLS OK — peer subject={cert.get('subject', [])[:2] if cert else 'n/a'}")
    except ssl.SSLError as exc:
        _fail(
            f"TLS handshake failed: {exc}",
            "An antivirus / corporate proxy is performing MITM — disable TLS interception.",
        )
    except Exception as exc:
        _fail(f"TLS connection error: {type(exc).__name__}: {exc}", "Check your local firewall / antivirus.")

    _section("[5/5] MongoDB ``ping`` (auth + Primary detection)")
    from pymongo import MongoClient
    from pymongo.errors import OperationFailure, ServerSelectionTimeoutError

    try:
        with MongoClient(uri, serverSelectionTimeoutMS=15000) as c:
            t0 = time.monotonic()
            c.admin.command("ping")
            ms = (time.monotonic() - t0) * 1000
            _ok(f"ping succeeded ({ms:.0f}ms)")
            db_name = os.environ.get("MONGO_DB", "fb-bot")
            coll = c[db_name]["bots"]
            total = coll.count_documents({})
            _ok(f"db={db_name!r} bots count: {total}")
    except OperationFailure as exc:
        _fail(
            f"Authentication failed (code {exc.code}): {exc.details}",
            "Username/password in MONGO_URI may be wrong, or the database user "
            "lacks read permission — check Atlas -> Database Access.",
        )
    except ServerSelectionTimeoutError as exc:
        _fail(
            f"Replica set Primary unavailable: {exc}",
            "TCP connected, but the Atlas cluster is not returning a Primary.\n"
            "   - Open Atlas console -> Database -> cluster status.\n"
            "   - If it says 'Paused', click Resume (free tier pauses after 7 days idle).",
        )

    print()
    print("[OK] All checks passed — MongoDB is reachable.")


if __name__ == "__main__":
    main()
