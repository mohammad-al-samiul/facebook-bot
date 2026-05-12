"""
Local LLM integration via Ollama (e.g. Llama 3.1) for post comments/reactions and Messenger replies.

Requires Ollama running (default ``http://127.0.0.1:11434``) and a pulled model, e.g.::

    ollama pull llama3.1
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from playwright_automation.actions import ReactionType

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "llama3.1"


class BrainError(RuntimeError):
    """Raised when Ollama is unreachable or returns an unusable response."""


@dataclass(frozen=True, slots=True)
class PostAnalysis:
    """Short comment plus a reaction aligned with :class:`~playwright_automation.actions.ReactionType`."""

    comment: str
    reaction_type: ReactionType


def _ollama_base_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL).rstrip("/")


def _default_model() -> str:
    return os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)


def _chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 120.0,
    format_json: bool = False,
) -> str:
    url = f"{(base_url or _ollama_base_url()).rstrip('/')}/api/chat"
    body: dict[str, Any] = {
        "model": model or _default_model(),
        "messages": messages,
        "stream": False,
    }
    if format_json:
        body["format"] = "json"

    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=body)
    except httpx.RequestError as e:
        raise BrainError(f"Could not reach Ollama at {url}: {e}") from e

    if r.status_code >= 400:
        raise BrainError(f"Ollama HTTP {r.status_code}: {r.text[:500]}")

    data = r.json()
    msg = data.get("message") or {}
    content = (msg.get("content") or "").strip()
    if not content:
        raise BrainError("Ollama returned an empty assistant message.")
    return content


def _coerce_reaction(raw: str) -> ReactionType:
    key = (raw or "").strip().lower()
    for rt in ReactionType:
        if rt.value == key:
            return rt
    # tolerate synonyms / labels
    synonyms = {
        "thumbs up": ReactionType.LIKE,
        "thumb up": ReactionType.LIKE,
        "heart": ReactionType.LOVE,
        "laugh": ReactionType.HAHA,
        "lol": ReactionType.HAHA,
        "surprised": ReactionType.WOW,
        "wow face": ReactionType.WOW,
    }
    return synonyms.get(key, ReactionType.LIKE)


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise BrainError(f"Model did not return JSON. Raw: {text[:400]!r}")
    try:
        return json.loads(m.group())
    except json.JSONDecodeError as e:
        raise BrainError(f"Invalid JSON in model output: {text[:400]!r}") from e


def analyze_post_and_respond(
    post_text: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 120.0,
) -> PostAnalysis:
    """
    Send feed post text to Llama and return a brief, natural comment plus a reaction choice.

    The model must reply with JSON only: ``{"comment": "...", "reaction_type": "like|love|..."}``
    where ``reaction_type`` is one of: like, love, care, haha, wow, sad, angry.
    """
    allowed = ", ".join(rt.value for rt in ReactionType)
    system = (
        "You help draft Facebook-style engagement. Be warm, concise, and human — no hashtags, "
        "no emojis unless one fits naturally, no lecturing. Output valid JSON only."
    )
    user = (
        "Read this social post and decide a suitable short public comment (1–2 sentences max, "
        "under 220 characters) and one reaction type for the Like button.\n\n"
        f"Allowed reaction_type values exactly: {allowed}\n\n"
        'Respond as JSON: {"comment": "<text>", "reaction_type": "<one of allowed>"}\n\n'
        f"Post:\n{post_text.strip()}"
    )

    raw = _chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=model,
        base_url=base_url,
        timeout=timeout,
        format_json=True,
    )
    payload = _extract_json_object(raw)

    comment = str(payload.get("comment", "")).strip()
    reaction = _coerce_reaction(str(payload.get("reaction_type", "like")))

    if not comment:
        raise BrainError(f"Model returned empty comment. Payload: {payload!r}")

    if len(comment) > 400:
        comment = comment[:397] + "..."

    return PostAnalysis(comment=comment, reaction_type=reaction)


def handle_chat(
    message_text: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 120.0,
) -> str:
    """
    Generate a short, natural reply suitable for Facebook Messenger (casual DM tone).

    Returns plain text only (no JSON).
    """
    system = (
        "You write brief Facebook Messenger replies: friendly, natural, lowercase or mixed case "
        "as people do in chats. No bullet lists, no assistant disclaimers, no 'As an AI'. "
        "Keep it under ~350 characters unless the message clearly needs more."
    )
    user = f"Incoming message:\n{message_text.strip()}\n\nWrite one appropriate reply."
    raw = _chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=model,
        base_url=base_url,
        timeout=timeout,
        format_json=False,
    )
    reply = raw.strip()
    if not reply:
        raise BrainError("Model returned an empty chat reply.")
    return reply
