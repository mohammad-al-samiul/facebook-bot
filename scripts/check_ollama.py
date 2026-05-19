#!/usr/bin/env python3
"""Check whether Ollama is already running (you usually do NOT need ``ollama serve``)."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env", override=False)

from playwright_automation.brain import _default_model, _ollama_base_url

DEFAULT_URL = _ollama_base_url()
DEFAULT_MODEL = _default_model()


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
        print(f"Use the same host as ollama serve, e.g. export OLLAMA_HOST=127.0.0.1:18000")
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
    print(f"Bot .env should set OLLAMA_HOST / OLLAMA_BASE_URL to {DEFAULT_URL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

