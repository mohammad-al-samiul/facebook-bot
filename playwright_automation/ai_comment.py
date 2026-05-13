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
import random
import re
import ssl
from typing import Final

import httpx

from playwright_automation.actions import ReactionType

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

_FALLBACK_COMMENT: Final[str] = "Nice post!"

# Bangla Unicode block: U+0980 .. U+09FF.
_BANGLA_RE: Final[re.Pattern[str]] = re.compile(r"[\u0980-\u09FF]")


def _detect_language(text: str) -> str:
    """Return ``'bn'`` when Bangla characters are present, else ``'en'``."""
    if _BANGLA_RE.search(text or ""):
        return "bn"
    return "en"


# Tone buckets used by both the AI prompt (as a hint) and the fallback
# selector. Keywords are intentionally lowercase ASCII so we can match
# them after a single ``.lower()`` on the post text.
_TONE_KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "religious": (
        "god", "jesus", "amen", "bless", "blessed", "blessing", "pray", "prayer",
        "bible", "verse", "lord", "faith", "christ", "holy", "psalm", "allah",
        "alhamdulillah", "subhanallah", "mashallah", "inshallah", "ya allah",
    ),
    "funny": (
        "lol", "lmao", "rofl", "haha", "hahaha", "🤣", "😂", "joke", "funny",
        "meme", "savage",
    ),
    "sad": (
        "rip", "passed away", "died", "death", "loss", "grief", "tragedy",
        "condolence", "heartbroken", "missing you", "miss you",
    ),
    "achievement": (
        "graduated", "graduation", "promoted", "promotion", "got the job",
        "new job", "engaged", "married", "wedding", "anniversary", "birthday",
        "happy birthday", "congratulations", "congrats", "achievement",
    ),
    "food": (
        "recipe", "cooked", "cooking", "meal", "lunch", "dinner", "breakfast",
        "yummy", "delicious", "tasty", "burger", "pizza", "biryani",
    ),
    "news": (
        "breaking", "reported", "report", "sources say", "according to",
        "officials", "government", "minister", "election", "police",
    ),
    "travel": (
        "trip", "travel", "vacation", "holiday", "beach", "mountain",
        "visiting", "explored", "tour",
    ),
    "love": (
        "love you", "❤️", "💕", "my love", "soulmate", "valentine",
    ),
    "promo": (
        "sponsored", "follow us", "follow our", "discount", "% off",
        "limited offer", "buy now", "click the link", "dm us",
    ),
}


def _detect_tone(text: str) -> str:
    """Pick a coarse tone bucket so the comment can match the post's vibe."""
    lower = (text or "").lower()
    for tone, kws in _TONE_KEYWORDS.items():
        if any(k in lower for k in kws):
            return tone
    return "neutral"


_ANGRY_HINTS: Final[tuple[str, ...]] = (
    "disgusting",
    "outrage",
    "furious",
    "horrible",
    "shame on",
    "how dare",
    "messed up",
    "wtf is wrong",
    "so angry",
    "this is evil",
    "hate this",
    "unacceptable",
    "scandal",
    "corrupt",
    "😡",
    "🤬",
)


def _looks_outraged(text: str) -> bool:
    lower = (text or "").lower()
    return any(h in lower for h in _ANGRY_HINTS)


def _weighted_reaction(
    rng: random.Random,
    choices: tuple[tuple[ReactionType, float], ...],
) -> ReactionType:
    """Pick a reaction using non-negative weights (any positive scale)."""
    total = sum(w for _, w in choices)
    if total <= 0:
        return ReactionType.LIKE
    x = rng.random() * total
    for rt, w in choices:
        x -= w
        if x <= 0:
            return rt
    return choices[-1][0]


def pick_reaction_for_post(
    text: str,
    rng: random.Random | None = None,
) -> ReactionType:
    """
    Choose a feed reaction aligned with post tone, with randomness so the
    bot does not always use the same emoji family.

    Drives Like / Love / Care / Haha / Wow / Sad / Angry via
    :func:`~playwright_automation.actions.react_to_post`.
    """
    r = rng if rng is not None else random.Random()
    tone = _detect_tone(text)
    mad = _looks_outraged(text)

    if tone == "funny":
        return _weighted_reaction(
            r,
            (
                (ReactionType.HAHA, 62),
                (ReactionType.WOW, 14),
                (ReactionType.LIKE, 16),
                (ReactionType.LOVE, 8),
            ),
        )
    if tone == "sad":
        return _weighted_reaction(
            r,
            (
                (ReactionType.SAD, 38),
                (ReactionType.CARE, 47),
                (ReactionType.LIKE, 15),
            ),
        )
    if tone == "love":
        return _weighted_reaction(
            r,
            (
                (ReactionType.LOVE, 58),
                (ReactionType.CARE, 27),
                (ReactionType.LIKE, 15),
            ),
        )
    if tone == "achievement":
        return _weighted_reaction(
            r,
            (
                (ReactionType.LOVE, 36),
                (ReactionType.LIKE, 30),
                (ReactionType.CARE, 17),
                (ReactionType.WOW, 17),
            ),
        )
    if tone == "food":
        return _weighted_reaction(
            r,
            (
                (ReactionType.LOVE, 28),
                (ReactionType.HAHA, 22),
                (ReactionType.LIKE, 35),
                (ReactionType.WOW, 15),
            ),
        )
    if tone == "news":
        if mad:
            return _weighted_reaction(
                r,
                (
                    (ReactionType.ANGRY, 35),
                    (ReactionType.WOW, 25),
                    (ReactionType.SAD, 15),
                    (ReactionType.LIKE, 25),
                ),
            )
        return _weighted_reaction(
            r,
            (
                (ReactionType.WOW, 28),
                (ReactionType.LIKE, 52),
                (ReactionType.CARE, 10),
                (ReactionType.SAD, 10),
            ),
        )
    if tone == "travel":
        return _weighted_reaction(
            r,
            (
                (ReactionType.WOW, 38),
                (ReactionType.LOVE, 27),
                (ReactionType.LIKE, 35),
            ),
        )
    if tone == "promo":
        return _weighted_reaction(
            r,
            (
                (ReactionType.LIKE, 72),
                (ReactionType.HAHA, 15),
                (ReactionType.WOW, 13),
            ),
        )
    if tone == "religious":
        return _weighted_reaction(
            r,
            (
                (ReactionType.LOVE, 22),
                (ReactionType.CARE, 30),
                (ReactionType.LIKE, 48),
            ),
        )
    # neutral (+ optional outrage tilt)
    if mad:
        return _weighted_reaction(
            r,
            (
                (ReactionType.ANGRY, 28),
                (ReactionType.LIKE, 33),
                (ReactionType.WOW, 15),
                (ReactionType.HAHA, 12),
                (ReactionType.LOVE, 12),
            ),
        )
    return _weighted_reaction(
        r,
        (
            (ReactionType.LIKE, 30),
            (ReactionType.LOVE, 20),
            (ReactionType.HAHA, 18),
            (ReactionType.WOW, 12),
            (ReactionType.CARE, 10),
            (ReactionType.SAD, 5),
            (ReactionType.ANGRY, 5),
        ),
    )


def _build_prompt(snippet: str, lang: str, tone: str) -> str:
    """Build a Gemini prompt that reacts to the actual post content + tone."""
    tone_hints_en: dict[str, str] = {
        "religious": "The post is religious / inspirational — respond with a warm, "
            "respectful comment (e.g. Amen, Blessed, Praying for you). No sarcasm.",
        "funny": "The post is funny / a meme — respond with a playful, lighthearted "
            "comment. Laughing emoji is fine.",
        "sad": "The post is sad / talks about loss — respond with sincere condolence "
            "or support. Do NOT use laughing or party emoji.",
        "achievement": "The post celebrates an achievement / milestone — respond with "
            "warm congratulations.",
        "food": "The post is about food / cooking — respond with appetite, curiosity "
            "or a friendly compliment.",
        "news": "The post is news / current events — respond with a brief, calm "
            "reaction or thought. Don't joke about tragedy.",
        "travel": "The post is travel / scenery — respond with admiration or curiosity.",
        "love": "The post is romantic / about loved ones — respond warmly, supportive.",
        "promo": "The post looks like a promotion / ad — respond with mild interest or "
            "a neutral one-liner. Do NOT sound like spam.",
        "neutral": "Respond with a short, friendly, human reaction that fits the post.",
    }
    tone_hints_bn: dict[str, str] = {
        "religious": "এই পোস্টটা ধর্মীয় / অনুপ্রেরণামূলক — শ্রদ্ধাশীল ও উষ্ণ "
            "মন্তব্য দাও (যেমন: আমিন, আলহামদুলিল্লাহ, সুন্দর কথা)। কোনো ব্যঙ্গ নয়।",
        "funny": "এই পোস্টটা মজার / মিম — হালকা মেজাজে রসিকতা মেশানো মন্তব্য দাও। "
            "হাসির ইমোজি ব্যবহার করতে পারো।",
        "sad": "এই পোস্টটা দুঃখের / শোকের — আন্তরিক সমবেদনা প্রকাশ করো। "
            "হাসি বা পার্টির ইমোজি একদম নয়।",
        "achievement": "এই পোস্টে কেউ সাফল্য / মাইলস্টোন উদযাপন করছে — "
            "উষ্ণভাবে অভিনন্দন জানাও।",
        "food": "এই পোস্টটা খাবার / রান্না সংক্রান্ত — আগ্রহ বা প্রশংসা প্রকাশ করো।",
        "news": "এই পোস্টটা সংবাদ / চলমান ঘটনা — সংক্ষিপ্ত, শান্ত প্রতিক্রিয়া দাও। "
            "ট্র্যাজেডি নিয়ে রসিকতা নয়।",
        "travel": "এই পোস্টটা ভ্রমণ / প্রকৃতি — মুগ্ধতা বা কৌতূহল প্রকাশ করো।",
        "love": "এই পোস্টটা ভালোবাসা / প্রিয়জন নিয়ে — উষ্ণ ও সমর্থনমূলক মন্তব্য।",
        "promo": "এই পোস্টটা প্রচারমূলক / বিজ্ঞাপন — হালকা আগ্রহ বা নিরপেক্ষ "
            "এক লাইনের মন্তব্য। স্প্যামের মতো শোনাবে না।",
        "neutral": "পোস্টের সাথে মানানসই একটা ছোট, বন্ধুত্বপূর্ণ, মানুষের মতো "
            "মন্তব্য দাও।",
    }

    if lang == "bn":
        tone_hint = tone_hints_bn.get(tone, tone_hints_bn["neutral"])
        instruction = (
            "তুমি একজন সাধারণ Facebook ইউজার। নিচের পোস্টটা পড়ো এবং "
            "**পোস্টের বিষয়বস্তুর সাথে সরাসরি সম্পর্কিত** একটা ছোট, "
            "স্বাভাবিক, মানুষের মতো বাংলা কমেন্ট লিখো। "
            "দৈর্ঘ্য ২-১২ শব্দ বা ১টা ছোট বাক্য। "
            f"{tone_hint} "
            "শুধু কমেন্টের টেক্সট লিখবে — কোনো উদ্ধৃতি চিহ্ন বা ব্যাখ্যা নয়। "
            "একটা মানানসই ইমোজি দিতে পারো।"
        )
    else:
        tone_hint = tone_hints_en.get(tone, tone_hints_en["neutral"])
        instruction = (
            "You are a regular Facebook user. Read the post below and write a "
            "short, natural, human-like English comment that is **directly "
            "relevant to the post's content** (refer to what it's actually "
            "about — not a generic 'Nice!'). "
            "Length: 2-12 words or one short sentence. "
            f"{tone_hint} "
            "Output ONLY the comment text — no quotes, no explanation. "
            "One fitting emoji is fine."
        )
    return f"{instruction}\n\nPost:\n{snippet}\n\nComment:"


# Tone-matched fallback comments used when the Gemini API call fails.
# Each tone has both Bangla and English variants so we can still produce a
# vaguely-relevant reply even when offline / rate limited.
_FALLBACK_BY_TONE: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "religious": {
        "en": ("Amen 🙏", "Blessed 🙏", "So true!", "Praise God!", "Amen and amen."),
        "bn": ("আমিন 🙏", "মাশাল্লাহ", "সুন্দর কথা", "আলহামদুলিল্লাহ", "সত্যি কথা"),
    },
    "funny": {
        "en": ("Haha 😂", "Lol 😂", "Too good!", "Hilarious 🤣", "Cracked me up"),
        "bn": ("হাহা 😂", "মজাই মজা 🤣", "দারুণ হইসে", "হাসি থামছে না"),
    },
    "sad": {
        "en": ("So sorry 💔", "Heartbreaking", "Praying for you", "Stay strong", "Condolences 🙏"),
        "bn": ("খুব কষ্টের 💔", "সমবেদনা রইল", "প্রার্থনায় রাখলাম", "শক্ত থাকো"),
    },
    "achievement": {
        "en": ("Congrats! 🎉", "So proud of you!", "Well deserved 👏", "Amazing news!", "Big win 🎉"),
        "bn": ("অভিনন্দন! 🎉", "দারুণ খবর 👏", "সাবাশ!", "অনেক শুভেচ্ছা"),
    },
    "food": {
        "en": ("Looks delicious 😋", "Yummy!", "Save me a plate!", "Mouth watering 🤤"),
        "bn": ("লোভনীয় দেখাচ্ছে 😋", "খেতে ইচ্ছে করছে", "মুখ পানি চলে আসলো 🤤"),
    },
    "news": {
        "en": ("Important update", "Good to know", "Following this", "Stay safe everyone"),
        "bn": ("গুরুত্বপূর্ণ খবর", "জানা থাকল", "চোখ রাখলাম", "সবাই নিরাপদে থাকো"),
    },
    "travel": {
        "en": ("Beautiful spot!", "Looks amazing 😍", "On my list now!", "Stunning view"),
        "bn": ("সুন্দর জায়গা 😍", "ছবিগুলো অসাধারণ", "ঘুরতে যেতে ইচ্ছে করছে"),
    },
    "love": {
        "en": ("So sweet ❤️", "Couple goals", "Beautiful 💕", "Love this"),
        "bn": ("কী মিষ্টি ❤️", "অসাধারণ 💕", "ভালোবাসা রইল"),
    },
    "promo": {
        "en": ("Interesting", "Will check it out", "Sounds useful", "Noted"),
        "bn": ("দেখব", "ভালো উদ্যোগ", "কাজে লাগবে"),
    },
    "neutral": {
        "en": ("Nice post!", "Well said", "Great share", "Appreciated", "Loved it 👍"),
        "bn": ("সুন্দর পোস্ট", "ভালো বললে", "চমৎকার", "মন ছুঁয়ে গেল"),
    },
}


def _fallback_for(snippet: str) -> str:
    """Pick a tone+language matched fallback when Gemini is unavailable."""
    import random as _r  # local import keeps the module's top free of state

    if not snippet or not snippet.strip():
        return _FALLBACK_COMMENT
    lang = _detect_language(snippet)
    tone = _detect_tone(snippet)
    bucket = _FALLBACK_BY_TONE.get(tone, _FALLBACK_BY_TONE["neutral"])
    pool = bucket.get(lang) or bucket.get("en") or (_FALLBACK_COMMENT,)
    return _r.choice(pool)


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
    Ask Gemini for a short, post-relevant, human-like comment. Returns the
    comment text on success, or a tone-matched fallback (e.g. "Amen 🙏" for
    a religious post, "Haha 😂" for a funny post) on any failure.
    """
    api_key = (os.environ.get("GEMINI_API_KEY") or _GEMINI_DEFAULT_KEY).strip()
    snippet = (post_text or "").strip()
    fallback = _fallback_for(snippet)

    if not api_key:
        logger.warning("No Gemini API key configured — using fallback comment")
        return fallback

    if not snippet:
        return fallback

    snippet = snippet[:600]
    lang = _detect_language(snippet)
    tone = _detect_tone(snippet)
    prompt = _build_prompt(snippet, lang, tone)
    logger.info("Gemini prompt: lang=%s tone=%s", lang, tone)

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
        return fallback
    except Exception as exc:
        logger.warning(
            "Gemini API call failed (%s: %s) — using fallback comment",
            type(exc).__name__,
            exc,
        )
        return fallback

    text = _extract_text(data)
    if not text:
        logger.warning("Gemini response had no candidate text — using fallback comment")
        return fallback

    if len(text) > 120:
        text = text[:120].rstrip() + "..."
    return text


__all__ = ["get_ai_comment", "pick_reaction_for_post"]
