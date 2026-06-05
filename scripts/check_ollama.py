#!/usr/bin/env python3
"""Check whether Ollama is running and the configured model is installed."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env", override=False)

from playwright_automation.brain import (  # noqa: E402
    _configured_ollama_base_url,
    _default_model,
    _ollama_candidate_urls,
    resolve_ollama_base_url,
)

DEFAULT_MODEL = _default_model()


def _model_is_available(requested: str, installed: list[str]) -> tuple[bool, list[str]]:
    req = requested.strip()
    if not req:
        return False, []
    if req in installed:
        return True, [req]
    base = req.split(":", 1)[0]
    matches = [n for n in installed if n.split(":", 1)[0] == base]
    return bool(matches), matches


def main() -> int:
    base_url = resolve_ollama_base_url()
    if not base_url:
        tried = ", ".join(_ollama_candidate_urls())
        print("Ollama is NOT reachable. Tried:", tried)
        print("Configured:", _configured_ollama_base_url())
        print()
        print("Start the Ollama desktop app (Windows tray), then run this script again.")
        print("Or set OLLAMA_HOST=127.0.0.1:11434 in .env")
        return 1

    url = f"{base_url}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        print("Ollama responded at", base_url, "but /api/tags failed:", exc)
        return 1

    names = [m.get("name", "?") for m in data.get("models", [])]
    print("Ollama is running at", base_url)
    print("Installed models:", ", ".join(names) if names else "(none)")
    ok, matches = _model_is_available(DEFAULT_MODEL, names)
    if ok:
        if DEFAULT_MODEL in names:
            print(f"Configured model '{DEFAULT_MODEL}' is available for the bot.")
        else:
            print(
                f"Configured model '{DEFAULT_MODEL}' is available "
                f"(installed: {', '.join(matches)})."
            )
    else:
        print(f"Warning: OLLAMA_MODEL={DEFAULT_MODEL!r} not in list. Run: ollama pull {DEFAULT_MODEL}")
    print()
    print(f"Set OLLAMA_HOST / OLLAMA_BASE_URL to {base_url} in .env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
