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


def detect_post_language(text: str) -> str:
    """Return ``'bn'`` when Bangla characters are present in the post, else ``'en'``."""
    if _BANGLA_RE.search(text or ""):
        return "bn"
    return "en"


def _detect_language(text: str) -> str:
    return detect_post_language(text)


# Facebook UI chrome that pollutes ``inner_text`` and confuses the LLM.
_FB_UI_NOISE_RE: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bLike\b", re.I),
    re.compile(r"\bComment\b", re.I),
    re.compile(r"\bShare\b", re.I),
    re.compile(r"\bReact\b", re.I),
    re.compile(r"\bSee more\b", re.I),
    re.compile(r"\bSee less\b", re.I),
    re.compile(r"\bShared with\b", re.I),
    re.compile(r"\bSuggested for you\b", re.I),
    re.compile(r"\bSponsored\b", re.I),
    re.compile(r"\bলাইক\b"),
    re.compile(r"\bমন্তব্য\b"),
    re.compile(r"\bশেয়ার\b|শেয়ার"),
    re.compile(r"\b\d+\s*(comments?|shares?|reactions?|likes?)\b", re.I),
    re.compile(r"\b\d+\s*(minutes?|hours?|days?|weeks?)\s+ago\b", re.I),
    re.compile(r"\b\d+[hdwm]\b", re.I),
)


def clean_post_text(raw: str, *, max_chars: int = 400) -> str:
    """
    Strip Facebook UI labels / counts from scraped post text so the LLM sees
  only the author's message.
    """
    text = " ".join((raw or "").split())
    if not text:
        return ""
    for pat in _FB_UI_NOISE_RE:
        text = pat.sub(" ", text)
    text = " ".join(text.split())
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] or text[:max_chars]
    return text.strip()


_GENERIC_COMMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"^(ভালো|ভাল|nice|good|great|well\s*said|loved\s*it|wow|amazing|"
    r"সুন্দর\s*পোস্ট|ভালো\s*বল|ভালো\s*লাগ|চমৎকার|দারুণ|শুভেচ্ছা)\b",
    re.I,
)


def comment_is_too_generic(comment: str, post_text: str) -> bool:
    """True when the reply is a lazy one-liner that ignores specific post content."""
    c = (comment or "").strip()
    p = clean_post_text(post_text)
    if not c:
        return True
    bare = c.lower().strip("!।.?… ")
    lazy_bn = (
        "ভালো",
        "ভাল",
        "ভালো বললে",
        "ভালো লাগল",
        "সুন্দর পোস্ট",
        "চমৎকার",
        "দারুণ",
        "শুভেচ্ছা",
    )
    lazy_en = ("nice post", "good post", "well said", "great share", "loved it", "wow")
    if bare in lazy_bn + lazy_en:
        return True
    if _GENERIC_COMMENT_RE.match(c) and len(p) > 40:
        return True
    if len(c) <= 12 and len(p) > 55 and not re.search(r"[\u0980-\u09FF]{2,}|[a-z]{4,}", c, re.I):
        return True
    return False


def comment_seems_relevant(comment: str, post_text: str) -> bool:
    """Reject generic or off-topic comments when the post has clear content."""
    c = (comment or "").strip()
    p = clean_post_text(post_text)
    if not c:
        return False
    if not p or len(p) < 12:
        return True

    c_low = c.lower()
    p_low = p.lower()

    if comment_is_too_generic(c, p):
        return False

    bare = c_low.strip("!।.?… ")
    generic_bn = ("শুভেচ্ছা", "সুন্দর পোস্ট", "ভালো লাগল", "চমৎকার", "দারুণ")
    generic_en = ("nice post", "well said", "great share", "loved it", "congrats", "wow")
    if len(p) > 50 and bare in generic_bn + generic_en:
        return False

    angry_c = ("রাগ", "বিজিত", "ঘৃণা", "hate", "angry", "disgusting", "outrage")
    if any(w in c_low for w in angry_c) and not _looks_outraged(p):
        return False

    romance_c = ("প্রেম", "kiss", "romantic", "love you", "couple goals")
    if any(w in c_low for w in romance_c) and _detect_tone(p) not in ("love",):
        if not any(w in p_low for w in ("প্রেম", "love", "বিয়ে", "wedding", "valentine")):
            return False

    post_tokens = set(re.findall(r"[\w\u0980-\u09FF]{3,}", p_low))
    comment_tokens = set(re.findall(r"[\w\u0980-\u09FF]{3,}", c_low))
    if post_tokens and comment_tokens and (post_tokens & comment_tokens):
        return True

    if _detect_tone(p) != "neutral" and len(c) <= 40:
        return True

    return len(p) < 35


def comment_matches_post_language(comment: str, post_text: str) -> bool:
    """True when the comment script matches the post (bn post → bn comment, en → en)."""
    post_lang = detect_post_language(post_text)
    comment_has_bn = bool(_BANGLA_RE.search(comment or ""))
    if post_lang == "bn":
        return comment_has_bn
    return not comment_has_bn


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
    "disgusting", "outrage", "furious", "horrible", "shame on", "how dare",
    "messed up", "wtf is wrong", "so angry", "this is evil", "hate this",
    "unacceptable", "scandal", "corrupt", "😡", "🤬",
)


def _looks_outraged(text: str) -> bool:
    lower = (text or "").lower()
    return any(h in lower for h in _ANGRY_HINTS)


def _weighted_reaction(
    rng: random.Random,
    choices: tuple[tuple[ReactionType, float], ...],
) -> ReactionType:
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
    Pick a reaction for the feed. **Like** = normal tap. **Love / Care / Haha /
    Angry** (and Wow/Sad) = long-press rail then chip — weights favour extended
    reactions over thumbs-up. Mobile chips use ``data-image-id`` in
    ``react_to_post``.
    """
    r = rng if rng is not None else random.Random()
    tone = _detect_tone(text)
    mad = _looks_outraged(text)

    if tone == "funny":
        return _weighted_reaction(
            r,
            (
                (ReactionType.HAHA, 48),
                (ReactionType.LOVE, 18),
                (ReactionType.WOW, 14),
                (ReactionType.CARE, 12),
                (ReactionType.LIKE, 8),
            ),
        )
    if tone == "sad":
        return _weighted_reaction(
            r,
            (
                (ReactionType.SAD, 38),
                (ReactionType.CARE, 42),
                (ReactionType.LOVE, 12),
                (ReactionType.LIKE, 8),
            ),
        )
    if tone == "love":
        return _weighted_reaction(
            r,
            (
                (ReactionType.LOVE, 52),
                (ReactionType.CARE, 32),
                (ReactionType.HAHA, 8),
                (ReactionType.LIKE, 8),
            ),
        )
    if tone == "achievement":
        return _weighted_reaction(
            r,
            (
                (ReactionType.LOVE, 38),
                (ReactionType.WOW, 22),
                (ReactionType.CARE, 18),
                (ReactionType.HAHA, 12),
                (ReactionType.LIKE, 10),
            ),
        )
    if tone == "food":
        return _weighted_reaction(
            r,
            (
                (ReactionType.LOVE, 28),
                (ReactionType.HAHA, 28),
                (ReactionType.WOW, 18),
                (ReactionType.CARE, 14),
                (ReactionType.LIKE, 12),
            ),
        )
    if tone == "news":
        if mad:
            return _weighted_reaction(
                r,
                (
                    (ReactionType.ANGRY, 40),
                    (ReactionType.WOW, 22),
                    (ReactionType.SAD, 16),
                    (ReactionType.CARE, 14),
                    (ReactionType.LIKE, 8),
                ),
            )
        return _weighted_reaction(
            r,
            (
                (ReactionType.WOW, 28),
                (ReactionType.CARE, 22),
                (ReactionType.SAD, 18),
                (ReactionType.HAHA, 14),
                (ReactionType.LOVE, 10),
                (ReactionType.LIKE, 8),
            ),
        )
    if tone == "travel":
        return _weighted_reaction(
            r,
            (
                (ReactionType.WOW, 34),
                (ReactionType.LOVE, 30),
                (ReactionType.CARE, 16),
                (ReactionType.HAHA, 12),
                (ReactionType.LIKE, 8),
            ),
        )
    if tone == "promo":
        return _weighted_reaction(
            r,
            (
                (ReactionType.HAHA, 22),
                (ReactionType.WOW, 18),
                (ReactionType.CARE, 18),
                (ReactionType.LOVE, 14),
                (ReactionType.LIKE, 28),
            ),
        )
    if tone == "religious":
        return _weighted_reaction(
            r,
            (
                (ReactionType.LOVE, 32),
                (ReactionType.CARE, 38),
                (ReactionType.WOW, 12),
                (ReactionType.HAHA, 10),
                (ReactionType.LIKE, 8),
            ),
        )
    if mad:
        return _weighted_reaction(
            r,
            (
                (ReactionType.ANGRY, 36),
                (ReactionType.WOW, 18),
                (ReactionType.HAHA, 14),
                (ReactionType.SAD, 14),
                (ReactionType.LOVE, 10),
                (ReactionType.LIKE, 8),
            ),
        )
    return _weighted_reaction(
        r,
        (
            (ReactionType.LOVE, 20),
            (ReactionType.HAHA, 20),
            (ReactionType.CARE, 18),
            (ReactionType.WOW, 14),
            (ReactionType.SAD, 8),
            (ReactionType.ANGRY, 8),
            (ReactionType.LIKE, 12),
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
        "en": ("Thanks for sharing this", "Interesting point", "Good to know", "Makes sense"),
        "bn": ("শেয়ার করার জন্য ধন্যবাদ", "বিষয়টা জানা গেল", "মনে রাখলাম"),
    },
}

_STOPWORDS: Final[frozenset[str]] = frozenset({
    "the", "and", "for", "that", "this", "with", "from", "have", "been", "will",
    "করে", "করা", "হয়", "হবে", "আছে", "একটা", "এই", "তার", "কিন্তু",
})


def _extract_post_keywords(snippet: str, *, max_kw: int = 3) -> list[str]:
    """Pick a few meaningful tokens from the post for contextual offline comments."""
    text = clean_post_text(snippet)
    if not text:
        return []
    tokens = re.findall(r"[\w\u0980-\u09FF]{3,}", text.lower())
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for tok in tokens:
        if tok in _STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        scored.append((len(tok), tok))
    scored.sort(reverse=True)
    return [t for _, t in scored[:max_kw]]


def _contextual_comment_fallback(snippet: str, *, avoid: tuple[str, ...] = ()) -> str:
    """Offline comment that references the post topic — not a bare 'ভালো'."""
    lang = detect_post_language(snippet)
    tone = _detect_tone(snippet)
    keywords = _extract_post_keywords(snippet)
    avoid_low = {a.lower().strip() for a in avoid if a}

    if keywords:
        kw = keywords[0]
        if lang == "bn":
            templates = (
                f"{kw} নিয়ে লিখেছেন, বুঝতে পারছি।",
                f"{kw} বিষয়টা আলোচনার দাবি রাখে।",
                f"এই {kw} নিয়ে ভাবতে হবে।",
                f"{kw} — দারুণ বিষয় বেছে নিয়েছেন।",
            )
        else:
            templates = (
                f"Good point about {kw}.",
                f"The {kw} angle makes sense.",
                f"Interesting take on {kw}.",
            )
        pool = [t for t in templates if t.lower() not in avoid_low]
        if pool:
            return random.choice(pool)

    fb = _fallback_for(snippet)
    if fb.lower() not in avoid_low:
        return fb
    bucket = _FALLBACK_BY_TONE.get(tone, _FALLBACK_BY_TONE["neutral"])
    pool = [p for p in bucket.get(lang, ()) if p.lower() not in avoid_low]
    return random.choice(pool) if pool else _FALLBACK_COMMENT


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
    snippet = clean_post_text((post_text or "").strip(), max_chars=500)
    fallback = _fallback_for(snippet)

    if not api_key:
        logger.warning("No Gemini API key configured — using fallback comment")
        return fallback

    if not snippet:
        return fallback
    lang = _detect_language(snippet)
    tone = _detect_tone(snippet)
    prompt = _build_prompt(snippet, lang, tone)
    logger.info("Gemini prompt: lang=%s tone=%s", lang, tone)

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]},
        ],
        "generationConfig": {
            "temperature": 0.45,
            "topP": 0.9,
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
    clean = clean_post_text(snippet)
    if not comment_matches_post_language(text, clean or snippet):
        logger.warning(
            "Gemini comment language mismatch (post=%s) — using tone fallback",
            lang,
        )
        return fallback
    if not comment_seems_relevant(text, clean or snippet):
        logger.warning("Gemini comment off-topic — using tone fallback")
        return fallback
    return text


_STATUS_STYLES: dict[str, dict[str, tuple[str, ...]]] = {
    "weather": {
        "bn": ("আজ বৃষ্টি হচ্ছে, চা আর বই—মনটা শান্ত।", "শীতের সকালে হালকা কুয়াশা, ভালো লাগছে।"),
        "en": ("Rainy afternoon and tea — cozy mood.", "Chilly morning but the sky looks calm."),
    },
    "food": {
        "bn": ("আজ বাড়িতে বিরিয়ানি—সবাই মিলে খাওয়ার প্ল্যান।", "মা হাতে রান্না করলেন, স্বাদ অসাধারণ।"),
        "en": ("Homemade biryani tonight — family dinner plan.", "Mom's cooking hit different today."),
    },
    "family": {
        "bn": ("ছোট বোনের হাসি দেখে দিনটা ভালো হয়ে গেল।", "বাবা-মায়ের দোয়া সবসময় পাশে আছে।"),
        "en": ("My little sister's laugh made my day.", "Grateful for my parents every single day."),
    },
    "work": {
        "bn": ("নতুন প্রজেক্ট শুরু—একটু চাপ কিন্তু উৎসাহ আছে।", "আজ অফিসে প্রেজেন্টেশন ভালো গেল।"),
        "en": ("Starting a new project — busy but excited.", "Presentation went better than I expected."),
    },
    "weekend": {
        "bn": ("শুক্রবার বিকেলে ফুটবল আর বন্ধুদের সাথে আড্ডা।", "উইকেন্ডে লং ড্রাইভ—মন চাইল।"),
        "en": ("Friday evening football with friends.", "Weekend road trip is calling."),
    },
    "gratitude": {
        "bn": ("আলহামদুলিল্লাহ সব ঠিক আছে—ছোট ছোট জিনিসেই শান্তি।", "যা আছে তাতেই খুশি, আর চাই না বেশি।"),
        "en": ("Alhamdulillah for the little things.", "Content with what I have today."),
    },
    "sports": {
        "bn": ("আজ রাতে ম্যাচ—সবাই রেডি?", "ক্রিকেট খেলতে মাঠে গেলাম, মজা হলো।"),
        "en": ("Big match tonight — who's watching?", "Played cricket with friends — great fun."),
    },
    "tech": {
        "bn": ("নতুন ফোন সেটআপ করছি, ক্যামেরা দারুণ।", "ল্যাপটপ আপডেট শেষ—কাজ আরও স্মুথ।"),
        "en": ("New phone setup — camera is amazing.", "Finally updated my laptop — so much faster."),
    },
    "question": {
        "bn": ("তোমরা ছুটিতে কোথায় যাচ্ছ? কমেন্টে বলো।", "সবার প্রিয় বিকেলের নাস্তা কী?"),
        "en": ("Where is everyone traveling this holiday?", "What's your go-to evening snack?"),
    },
    "memory": {
        "bn": ("হঠাৎ পুরনো স্কুলের দিন মনে পড়ে গেল।", "ছোটবেলার গ্রামের রাস্তা মিস করি।"),
        "en": ("Random flashback to school days today.", "Missing the village roads from childhood."),
    },
    "nature": {
        "bn": ("সন্ধ্যায় নদীর ধারে হাঁটা—মন ভালো হয়ে যায়।", "বাগানের গোলাপ ফুটেছে, সুন্দর লাগছে।"),
        "en": ("Evening walk by the river — peaceful.", "Roses bloomed in the garden today."),
    },
}


def pick_status_post_style(*, avoid: list[str] | None = None) -> str:
    """Pick a post topic style not used recently (for variety)."""
    avoid_set = {a.strip().lower() for a in (avoid or []) if a}
    keys = [k for k in _STATUS_STYLES if k not in avoid_set]
    if not keys:
        keys = list(_STATUS_STYLES.keys())
    return random.choice(keys)


async def generate_status_post(
    *,
    prefer_bn: bool | None = None,
    timeout: float = 60.0,
    avoid_styles: list[str] | None = None,
) -> tuple[str, str]:
    """
    Varied short status for the feed composer.

    Returns ``(text, style_key)`` — style rotates (food, sports, family, etc.).
    """
    import asyncio

    use_bn = prefer_bn if prefer_bn is not None else random.random() < 0.55
    lang = "bn" if use_bn else "en"
    style = pick_status_post_style(avoid=avoid_styles)
    pool = _STATUS_STYLES[style][lang]
    fallback = random.choice(pool)

    style_hints_bn = {
        "weather": "আবহাওয়া/মৌসুম",
        "food": "খাবার বা রান্না",
        "family": "পরিবার বা প্রিয়জন",
        "work": "কাজ বা পড়াশোনা",
        "weekend": "ছুটি বা আড্ডা",
        "gratitude": "কৃতজ্ঞতা",
        "sports": "খেলা/ক্রিকেট/ফুটবল",
        "tech": "টেক/গ্যাজেট",
        "question": "বন্ধুদের হালকা প্রশ্ন",
        "memory": "স্মৃতি/নস্টালজিয়া",
        "nature": "প্রকৃতি/ভ্রমণ",
    }
    style_hints_en = {
        "weather": "weather or season",
        "food": "food or cooking",
        "family": "family or loved ones",
        "work": "work or study",
        "weekend": "weekend plans",
        "gratitude": "gratitude",
        "sports": "sports or match",
        "tech": "tech or gadgets",
        "question": "a light question to friends",
        "memory": "a memory or nostalgia",
        "nature": "nature or outdoors",
    }
    hint = style_hints_bn.get(style, style) if use_bn else style_hints_en.get(style, style)
    avoid_txt = ", ".join(avoid_styles or []) or "none"

    try:
        from playwright_automation.brain import _chat, _default_model

        if use_bn:
            system = (
                "তুমি Facebook-এ মানুষের মতো **বিভিন্ন ধরনের** ছোট স্ট্যাটাস লেখো। "
                "প্রতিবার আলাদা টপিক ও ভাব। শুধু বাংলায়। ১–২ বাক্য, সর্বোচ্চ ১২০ অক্ষর। "
                "হ্যাশট্যাগ নয়। কটুক্তি/রাজনীতি নয়। শুধু পোস্ট টেক্সট।"
            )
            user = (
                f"এবারের টপিক: {hint}। আগের স্টাইল এড়াও: {avoid_txt}। "
                "সাধারণ 'ভালো দিন' টাইপ ক্লিশে লিখবে না। নতুন কিছু লেখো।"
            )
        else:
            system = (
                "You write varied Facebook status posts — different topic and tone each time. "
                "English only. 1–2 sentences, max 120 chars. No hashtags. No politics."
            )
            user = (
                f"Topic for this post: {hint}. Avoid repeating these recent styles: {avoid_txt}. "
                "Do NOT write another generic 'good day' post — be specific."
            )

        raw = await asyncio.wait_for(
            asyncio.to_thread(
                _chat,
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                model=_default_model(),
                format_json=False,
            ),
            timeout=timeout,
        )
        text = (raw or "").strip().strip('"').strip("'")
        if text and comment_matches_post_language(text, "আ" if use_bn else "Hello"):
            logger.info("Status from Ollama (%s, style=%s): %r", lang, style, text[:80])
            return text[:400], style
    except Exception as exc:
        logger.debug("Ollama status failed: %s", exc)

    logger.info("Using offline %s status fallback (style=%s)", lang, style)
    return fallback, style


async def generate_comment_for_post(
    post_text: str,
    *,
    timeout: float = 75.0,
    avoid_comments: tuple[str, ...] = (),
) -> str:
    """
    Post-relevant comment in the **same language** as the post (Bangla or English).

    Reads the post, tries Ollama (twice), then Gemini, then a contextual offline line.
    """
    import asyncio

    snippet = clean_post_text((post_text or "").strip(), max_chars=500)
    if not snippet:
        return _contextual_comment_fallback("", avoid=avoid_comments)

    lang = detect_post_language(snippet)
    keywords = _extract_post_keywords(snippet)
    logger.info(
        "Post for comment (%s, %d chars, kw=%s): %r",
        lang,
        len(snippet),
        keywords,
        snippet[:80],
    )

    def _accept(candidate: str) -> bool:
        if not candidate or candidate.lower().strip() in {a.lower().strip() for a in avoid_comments}:
            return False
        if not comment_matches_post_language(candidate, snippet):
            return False
        if comment_is_too_generic(candidate, snippet):
            return False
        return comment_seems_relevant(candidate, snippet)

    try:
        from playwright_automation.brain import analyze_post_and_respond, analyze_post_focused

        for attempt, fn in enumerate((analyze_post_and_respond, analyze_post_focused), start=1):
            try:
                analysis = await asyncio.wait_for(
                    asyncio.to_thread(fn, snippet, keywords=keywords),
                    timeout=timeout,
                )
                if _accept(analysis.comment):
                    logger.info("Comment from Ollama attempt %d (%s): %r", attempt, lang, analysis.comment[:60])
                    return analysis.comment
                logger.warning("Ollama attempt %d rejected — %r", attempt, analysis.comment[:50])
            except Exception as exc:
                logger.debug("Ollama attempt %d failed: %s", attempt, exc)
    except Exception as exc:
        logger.debug("Ollama import/call failed: %s", exc)

    try:
        gemini_text = await get_ai_comment(snippet, timeout=min(timeout, 30.0))
        if _accept(gemini_text):
            logger.info("Comment from Gemini (%s): %r", lang, gemini_text[:60])
            return gemini_text
        logger.warning("Gemini comment rejected — using contextual fallback")
    except Exception as exc:
        logger.debug("Gemini comment failed: %s", exc)

    fb = _contextual_comment_fallback(snippet, avoid=avoid_comments)
    logger.info("Contextual %s fallback: %r", lang, fb[:60])
    return fb


__all__ = [
    "clean_post_text",
    "comment_is_too_generic",
    "comment_matches_post_language",
    "comment_seems_relevant",
    "detect_post_language",
    "generate_comment_for_post",
    "generate_status_post",
    "get_ai_comment",
    "pick_reaction_for_post",
]
