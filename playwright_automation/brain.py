"""
Local LLM integration via Ollama (e.g. Llama 3.1) for post comments/reactions and Messenger replies.

Requires Ollama running (default ``http://127.0.0.1:11434``) and a pulled model, e.g.::

    ollama pull llama3.1:8b
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from playwright_automation.actions import ReactionType
from playwright_automation.ai_comment import (
    clean_post_text,
    comment_is_too_generic,
    comment_matches_post_language,
    comment_seems_relevant,
    detect_post_language,
)

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "llama3.1:8b"


class BrainError(RuntimeError):
    """Raised when Ollama is unreachable or returns an unusable response."""


@dataclass(frozen=True, slots=True)
class PostAnalysis:
    """Short comment plus a reaction aligned with :class:`~playwright_automation.actions.ReactionType`."""

    comment: str
    reaction_type: ReactionType


def _configured_ollama_base_url() -> str:
    """
    URL from env only (no probing).

    Priority:
    1. ``OLLAMA_BASE_URL``
    2. ``OLLAMA_HOST`` (CLI style host:port)
    3. Default ``http://127.0.0.1:11434``
    """
    explicit = (os.environ.get("OLLAMA_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    host = (os.environ.get("OLLAMA_HOST") or "").strip()
    if host:
        if host.startswith("http://") or host.startswith("https://"):
            return host.rstrip("/")
        return f"http://{host}".rstrip("/")
    return DEFAULT_OLLAMA_BASE_URL


_OLLAMA_PROBE_FALLBACKS: tuple[str, ...] = (
    DEFAULT_OLLAMA_BASE_URL,
    "http://127.0.0.1:18000",
)
_resolved_ollama_base_url: str | None = None


def _ollama_candidate_urls() -> list[str]:
    configured = _configured_ollama_base_url()
    out: list[str] = []
    for url in (configured, *_OLLAMA_PROBE_FALLBACKS):
        u = url.rstrip("/")
        if u not in out:
            out.append(u)
    return out


def resolve_ollama_base_url(*, timeout: float = 3.0) -> str | None:
    """First reachable Ollama base URL (env first, then 11434 / 18000)."""
    global _resolved_ollama_base_url
    if _resolved_ollama_base_url and ollama_is_available(
        base_url=_resolved_ollama_base_url, timeout=timeout
    ):
        return _resolved_ollama_base_url
    configured = _configured_ollama_base_url()
    for url in _ollama_candidate_urls():
        if ollama_is_available(base_url=url, timeout=timeout):
            _resolved_ollama_base_url = url
            if url != configured:
                import logging

                logging.getLogger(__name__).info(
                    "Ollama reachable at %s (configured %s was down — using fallback)",
                    url,
                    configured,
                )
            return url
    _resolved_ollama_base_url = None
    return None


def _ollama_base_url() -> str:
    """Effective Ollama URL (probes once, then caches)."""
    return resolve_ollama_base_url() or _configured_ollama_base_url()


def _default_model() -> str:
    return os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)


def ollama_is_available(*, base_url: str | None = None, timeout: float = 4.0) -> bool:
    """True when Ollama responds on the chat API port (used before brain mode)."""
    url = f"{(base_url or _configured_ollama_base_url()).rstrip('/')}/api/tags"
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(url)
        return r.status_code < 400
    except Exception:
        return False


_ollama_last_call: float = 0.0


def _fleet_ollama_throttle() -> None:
    """Optional min gap between Ollama calls when many fleet workers share one server."""
    global _ollama_last_call
    raw = (os.environ.get("FLEET_OLLAMA_MIN_INTERVAL_SEC") or "0").strip()
    try:
        interval = float(raw)
    except ValueError:
        return
    if interval <= 0:
        return
    now = time.monotonic()
    wait = interval - (now - _ollama_last_call)
    if wait > 0:
        time.sleep(wait)
    _ollama_last_call = time.monotonic()


def _chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 120.0,
    format_json: bool = False,
) -> str:
    _fleet_ollama_throttle()
    url = f"{(base_url or _ollama_base_url()).rstrip('/')}/api/chat"
    body: dict[str, Any] = {
        "model": model or _default_model(),
        "messages": messages,
        "stream": False,
    }
    if format_json:
        body["format"] = "json"
    body["options"] = {"temperature": 0.35, "top_p": 0.9}

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
    snippet = clean_post_text(post_text.strip(), max_chars=450)
    if not snippet:
        snippet = post_text.strip()[:450]
    lang = detect_post_language(snippet)
    if lang == "bn":
        system = (
            "তুমি Facebook পোস্টে মানুষের মতো প্রতিক্রিয়া লেখো। কমেন্ট **শুধু বাংলায়**। "
            "কমেন্ট অবশ্যই পোস্টের **আসল বিষয়** নিয়ে হবে — পোস্টে যা লেখা সেটার সাথে "
            "সরাসরি সম্পর্কিত। অন্য বিষয় (প্রেম, রাগ, রাজনীতি) উদ্ভাবনা করবে না যদি "
            "পোস্টে না থাকে। সাধারণ 'শুভেচ্ছা' শুধু শুভেচ্ছা/জন্মদিন পোস্টে। "
            "হ্যাশট্যাগ নয়। শুধু valid JSON।"
        )
        user = (
            "নিচের পোস্টের **মূল বার্তা** পড়ে সেই বিষয়েই ছোট বাংলা কমেন্ট লেখো "
            "(২–১২ শব্দ, সর্বোচ্চ ১২০ অক্ষর) এবং reaction_type বেছে নাও।\n\n"
            f"Allowed reaction_type exactly: {allowed}\n"
            "Prefer like or love for normal posts; angry only if the post is clearly negative.\n\n"
            'JSON only: {"comment": "<বাংলা কমেন্ট>", "reaction_type": "<one of allowed>"}\n\n'
            f"Post:\n{snippet}"
        )
    else:
        system = (
            "You draft Facebook engagement. English only. The comment MUST directly "
            "address what the post is actually about — do not invent unrelated topics. "
            "No generic 'Nice post!' on specific content. No hashtags. JSON only."
        )
        user = (
            "Read the post below and write a short English comment (2–12 words) that "
            "clearly relates to the post's topic, plus one reaction_type.\n\n"
            f"Allowed reaction_type exactly: {allowed}\n"
            "Prefer like or love; angry only if the post is clearly negative.\n\n"
            'JSON only: {"comment": "<English comment>", "reaction_type": "<one of allowed>"}\n\n'
            f"Post:\n{snippet}"
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

    if not comment_matches_post_language(comment, snippet):
        raise BrainError(
            f"Model comment language mismatch (expected {lang}): {comment!r}",
        )

    if comment_is_too_generic(comment, snippet):
        raise BrainError(f"Model comment too generic for post: {comment!r}")

    if not comment_seems_relevant(comment, snippet):
        raise BrainError(f"Model comment off-topic for post: {comment!r}")

    if len(comment) > 400:
        comment = comment[:397] + "..."

    return PostAnalysis(comment=comment, reaction_type=reaction)


def analyze_post_focused(
    post_text: str,
    *,
    keywords: list[str] | None = None,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 90.0,
) -> PostAnalysis:
    """
    Second-pass Ollama call: force a comment that mentions the post topic / keywords.
    """
    allowed = ", ".join(rt.value for rt in ReactionType)
    snippet = clean_post_text(post_text.strip(), max_chars=450)
    if not snippet:
        snippet = post_text.strip()[:450]
    lang = detect_post_language(snippet)
    kw_hint = ", ".join(keywords or []) or snippet[:80]

    if lang == "bn":
        system = (
            "তুমি Facebook পোস্ট পড়ে **নির্দিষ্ট** বাংলা কমেন্ট লেখো। "
            "'ভালো', 'সুন্দর পোস্ট', 'চমৎকার' একা লিখবে না। "
            "পোস্টের বিষয় বা নাম/জায়গা/ঘটনা উল্লেখ করো। শুধু JSON।"
        )
        user = (
            f"পোস্টের মূল শব্দ/বিষয়: {kw_hint}\n\n"
            "এই পোস্টের **বিষয়** নিয়ে ২–১৫ শব্দের বাংলা কমেন্ট + reaction_type।\n"
            f"Allowed reaction_type: {allowed}\n"
            'JSON: {"comment": "...", "reaction_type": "..."}\n\n'
            f"Post:\n{snippet}"
        )
    else:
        system = (
            "Write a specific English Facebook comment about THIS post. "
            "Forbidden alone: Nice post, Good, Wow, Well said. "
            "Mention the topic or a detail from the post. JSON only."
        )
        user = (
            f"Key topics from post: {kw_hint}\n\n"
            "Short English comment (2–15 words) that clearly refers to the post, "
            f"plus reaction_type from: {allowed}\n"
            'JSON: {"comment": "...", "reaction_type": "..."}\n\n'
            f"Post:\n{snippet}"
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
        raise BrainError("Focused model returned empty comment.")
    if not comment_matches_post_language(comment, snippet):
        raise BrainError(f"Focused comment language mismatch: {comment!r}")
    if comment_is_too_generic(comment, snippet):
        raise BrainError(f"Focused comment still generic: {comment!r}")
    if not comment_seems_relevant(comment, snippet):
        raise BrainError(f"Focused comment off-topic: {comment!r}")
    if len(comment) > 400:
        comment = comment[:397] + "..."
    return PostAnalysis(comment=comment, reaction_type=reaction)


def pick_share_group(
    post_text: str,
    group_names: list[str],
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 60.0,
) -> str | None:
    """Return one group name from ``group_names`` that best fits the post (or None)."""
    groups = [g.strip() for g in group_names if (g or "").strip()][:25]
    if not groups:
        return None
    snippet = (post_text or "").strip()[:400]
    system = (
        "Pick exactly ONE Facebook group from the list that best matches the post topic. "
        "Output JSON only: {\"group_name\": \"<exact name from list>\"}"
    )
    user = (
        f"Post:\n{snippet or '(no text)'}\n\nGroups:\n"
        + "\n".join(f"- {g}" for g in groups)
    )
    try:
        raw = _chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=model,
            base_url=base_url,
            timeout=timeout,
            format_json=True,
        )
        payload = _extract_json_object(raw)
        gn = str(payload.get("group_name", "")).strip()
        for g in groups:
            if g.lower() == gn.lower() or gn.lower() in g.lower():
                return g
        return gn if gn else None
    except Exception:
        return groups[0] if groups else None


def decide_share_destination(
    post_text: str,
    group_names: list[str],
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 60.0,
) -> dict[str, str | None]:
    """
  Pick share target: ``timeline`` (Facebook feed/profile) or ``group`` with a group name.

    Returns ``{"target": "timeline"|"group", "group_name": str|None}``.
    """
    groups = [g.strip() for g in group_names if (g or "").strip()][:25]
    snippet = (post_text or "").strip()[:400]
    if not groups:
        return {"target": "timeline", "group_name": None}

    system = (
        "You choose where to share a Facebook post. Output JSON only. "
        "Pick \"timeline\" for general posts (Share to Facebook / news feed). "
        "Pick \"group\" only when the post clearly fits one listed group "
        "(topic match). If unsure, prefer timeline."
    )
    user = (
        f"Post:\n{snippet or '(no text)'}\n\n"
        f"Groups you may share to:\n"
        + "\n".join(f"- {g}" for g in groups)
        + '\n\nJSON: {"target": "timeline"|"group", "group_name": "<exact name or null>"}'
    )
    try:
        raw = _chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=model,
            base_url=base_url,
            timeout=timeout,
            format_json=True,
        )
        payload = _extract_json_object(raw)
    except Exception:
        return {"target": "timeline", "group_name": None}

    target = str(payload.get("target", "timeline")).strip().lower()
    group_name = payload.get("group_name")
    if target == "group" and group_name:
        gn = str(group_name).strip()
        for g in groups:
            if g.lower() == gn.lower() or gn.lower() in g.lower():
                return {"target": "group", "group_name": g}
        return {"target": "group", "group_name": gn}
    return {"target": "timeline", "group_name": None}


def generate_share_caption(
    post_text: str,
    *,
    keywords: tuple[str, ...] = (),
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 90.0,
) -> str:
    """
    Short caption when re-sharing someone else's post to the logged-in user's timeline.

    Returns plain caption text (same language as the post).
    """
    snippet = clean_post_text((post_text or "").strip(), max_chars=450)
    if not snippet:
        snippet = (post_text or "").strip()[:450]
    lang = detect_post_language(snippet)
    kw_hint = ""
    if keywords:
        kw_hint = "\nKey topics from the post: " + ", ".join(keywords[:6])
    if lang == "bn":
        system = (
            "তুমি Facebook-এ অন্য কারো পোস্ট **নিজের প্রোফাইল/টাইমলাইনে** শেয়ার করছ। "
            "ক্যাপশন **শুধু বাংলায়** — পোস্টে যে বিষয়/নাম/ঘটনা আছে সেটার সাথে সরাসরি "
            "সম্পর্কিত ১ বাক্য (৫–২৮ শব্দ)। সাধারণ 'শেয়ার করলাম' ছাড়া পোস্টের বিষয় "
            "উল্লেখ করো। হ্যাশট্যাগ নয়। JSON only."
        )
        user = (
            "নিচের পোস্টের **বিষয় অনুযায়ী** টাইমলাইন শেয়ার ক্যাপশন লেখো "
            "(পোস্টের মূল কথা বোঝা যাবে এমন)।\n\n"
            'JSON only: {"caption": "<বাংলা ক্যাপশন>"}\n\n'
            f"Post:\n{snippet}{kw_hint}"
        )
    else:
        system = (
            "You write a SHORT Facebook share caption when reposting someone else's post "
            "to **your own timeline/profile**. English only. One sentence (5–22 words) "
            "that clearly refers to THIS post's topic — mention a specific detail from "
            "the post, not generic 'worth sharing'. No hashtags. JSON only."
        )
        user = (
            "Write a share caption that matches what this post is actually about.\n\n"
            'JSON only: {"caption": "<English caption>"}\n\n'
            f"Post:\n{snippet}{kw_hint}"
        )

    raw = _chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=model,
        base_url=base_url,
        timeout=timeout,
        format_json=True,
    )
    payload = _extract_json_object(raw)
    caption = str(payload.get("caption", "")).strip()
    if not caption:
        raise BrainError(f"Model returned empty share caption. Payload: {payload!r}")
    if not comment_matches_post_language(caption, snippet):
        raise BrainError(f"Share caption language mismatch: {caption!r}")
    if comment_is_too_generic(caption, snippet):
        raise BrainError(f"Share caption too generic for post: {caption!r}")
    if not comment_seems_relevant(caption, snippet):
        raise BrainError(f"Share caption off-topic: {caption!r}")
    if len(caption) > 300:
        caption = caption[:297] + "..."
    return caption


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
