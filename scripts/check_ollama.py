#!/usr/bin/env python3
"""Check whether Ollama is already running (you usually do NOT need ``ollama serve``)."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")


def _model_is_available(requested: str, installed: list[str]) -> tuple[bool, list[str]]:
    """Return whether ``requested`` is installed (exact or same base name)."""
    req = requested.strip()
    if not req:
        return False, []
    if req in installed:
        return True, [req]
    base = req.split(":", 1)[0]
    matches = [n for n in installed if n.split(":", 1)[0] == base]
    return bool(matches), matches


def main() -> int:
    url = f"{DEFAULT_URL}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        print("Ollama is NOT reachable at", DEFAULT_URL)
        print("  ", exc)
        print()
        print("Start the Ollama desktop app (Windows tray), then run this script again.")
        print("Do not run 'ollama serve' unless nothing is listening on port 11434.")
        return 1

    names = [m.get("name", "?") for m in data.get("models", [])]
    print("Ollama is already running at", DEFAULT_URL)
    print("Installed models:", ", ".join(names) if names else "(none)")
    ok, matches = _model_is_available(DEFAULT_MODEL, names)
    if ok:
        if DEFAULT_MODEL in names:
            print(f"Configured model '{DEFAULT_MODEL}' is available for the bot.")
        else:
            print(
                f"Configured model '{DEFAULT_MODEL}' is available "
                f"(installed: {', '.join(matches)}).",
            )
    else:
        print(f"Warning: OLLAMA_MODEL={DEFAULT_MODEL!r} not in list. Run: ollama pull {DEFAULT_MODEL}")
    print()
    print("You do NOT need: ollama serve")
    print("(That error only means port 11434 is already in use — which is correct.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

