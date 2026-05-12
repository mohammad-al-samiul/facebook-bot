"""
Gemini API-backed comment generator.

The ``get_ai_comment`` coroutine asks Gemini for a very short, natural,
human-like comment in Bengali (when the post text contains Bangla
characters) or English (otherwise). On any API / network / parse failure
it returns the generic fallback ``"Good post!"`` so callers can keep
operating without worrying about exceptions.

The API key can be configured in priority order:

1. ``GEMINI_API_KEY`` environment variable (recommended).
2. Built-in default — only kept for the developer convenience case where
   the user provided a key inline. Rotate / revoke it in production.
"""

from __future__ import annotations

import logging
import os
import re
import ssl
from typing import Final

import httpx

logger = logging.getLogger(__name__)


def _build_ssl_context() -> ssl.SSLContext | bool:
    """
    Build an SSL context that trusts the system CA store first (works on
    Windows machines where antivirus/proxy MITM is using a locally-trusted
    cert), then falls back to certifi, then to Python's default.

    Returning ``True`` means "use httpx's default verification".
    """
    try:
        import truststore  # type: ignore[import-not-found]

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi  # type: ignore[import-not-found]

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return True


_SSL_CONTEXT = _build_ssl_context()

# NOTE: prefer setting GEMINI_API_KEY in .env. The default below is kept
# only because the project requirements explicitly provided this key.
_GEMINI_DEFAULT_KEY: Final[str] = "AIzaSyCSZeSnJs6RUqUtQwu6WoXqoJ6QIRYk_A4"

_GEMINI_URL: Final[str] = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-flash-latest:generateContent"
)

_FALLBACK_COMMENT: Final[str] = "Good post!"

# Bangla Unicode block: U+0980 .. U+09FF.
_BANGLA_RE: Final[re.Pattern[str]] = re.compile(r"[\u0980-\u09FF]")


def _detect_language(text: str) -> str:
    """Return ``'bn'`` when Bangla characters are present, else ``'en'``."""
    if _BANGLA_RE.search(text or ""):
        return "bn"
    return "en"


def _build_prompt(snippet: str, lang: str) -> str:
    if lang == "bn":
        instruction = (
            "নিচের Facebook পোস্টের জন্য একটা খুব ছোট, স্বাভাবিক, মানুষের মতো "
            "কমেন্ট লিখো বাংলায়। দৈর্ঘ্য সর্বোচ্চ ১-৬ শব্দ বা একটা ছোট বাক্য। "
            "শুধু কমেন্টের টেক্সট লিখবে — কোনো উদ্ধৃতি বা ব্যাখ্যা নয়। "
            "ইমোজি ব্যবহার করতে পারো।"
        )
    else:
        instruction = (
            "Write a very short, natural, human-like comment (1-6 words max) "
            "in English for the Facebook post below. Output ONLY the comment "
            "text — no quotes, no explanation. Emoji is OK."
        )
    return f"{instruction}\n\nPost:\n{snippet}\n\nComment:"


def _extract_text(payload: dict) -> str | None:
    """Pull the first generated candidate text out of a Gemini response dict."""
    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    if not parts:
        return None
    text = (parts[0].get("text") or "").strip()
    if not text:
        return None
    # Strip wrapping quotes / take the first line only.
    text = text.strip("\"'\n ").splitlines()[0].strip()
    return text or None


async def get_ai_comment(post_text: str, *, timeout: float = 12.0) -> str:
    """
    Ask Gemini for a short, human-like comment. Returns the comment text on
    success, or ``"Good post!"`` on any failure (network, HTTP error, parse).
    """
    api_key = (os.environ.get("GEMINI_API_KEY") or _GEMINI_DEFAULT_KEY).strip()
    if not api_key:
        logger.warning("No Gemini API key configured — using fallback comment")
        return _FALLBACK_COMMENT

    snippet = (post_text or "").strip()
    if not snippet:
        # No post text we can pivot on — keep it simple and generic.
        return _FALLBACK_COMMENT

    snippet = snippet[:600]
    lang = _detect_language(snippet)
    prompt = _build_prompt(snippet, lang)

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]},
        ],
        "generationConfig": {
            "temperature": 0.9,
            "topP": 0.95,
            # gemini-flash-latest currently resolves to gemini-3-flash-preview,
            # which spends "thinking" tokens before output. With a tight
            # maxOutputTokens the entire budget gets consumed by thoughts and
            # we get MAX_TOKENS with no text. Disable thinking and keep a
            # generous output cap.
            "maxOutputTokens": 200,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    try:
        async with httpx.AsyncClient(timeout=timeout, verify=_SSL_CONTEXT) as client:
            resp = await client.post(
                _GEMINI_URL,
                params={"key": api_key},
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Gemini API HTTP %s: %s — using fallback comment",
            exc.response.status_code,
            exc.response.text[:200],
        )
        return _FALLBACK_COMMENT
    except Exception as exc:
        logger.warning(
            "Gemini API call failed (%s: %s) — using fallback comment",
            type(exc).__name__,
            exc,
        )
        return _FALLBACK_COMMENT

    text = _extract_text(data)
    if not text:
        logger.warning("Gemini response had no candidate text — using fallback comment")
        return _FALLBACK_COMMENT

    if len(text) > 80:
        text = text[:80].rstrip() + "..."
    return text


__all__ = ["get_ai_comment"]
