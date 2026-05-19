"""
Autonomous Facebook agent decision brain (Ollama).

Returns strict JSON decisions parsed into :class:`AgentDecision`.
Server-side rules enforce the 3k+ friend/follower law even if the model errs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from playwright_automation.brain import BrainError, _chat, _default_model, _extract_json_object, _ollama_base_url
from playwright_automation.facebook_graph import DEFAULT_MIN_AUDIENCE

LocationType = Literal["newsfeed", "group", "page", "profile", "notifications"]
ActionType = Literal[
    "scroll",
    "like",
    "comment",
    "send_friend_request",
    "accept_friend_request",
    "join_group",
    "create_post",
    "share_post",
    "navigate_to",
]

AGENT_SYSTEM_PROMPT = """You are the advanced brain of an autonomous, human-like Facebook AI Agent. Your goal is to look at the current state of a Facebook account and decide the next logical, natural, and human-like action to take.

You must strictly analyze the input data provided by the user (current page type, target's follower count, context) and follow the strict business logic provided below.

### STRICT LAWS OF OPERATION:
1. Friend Request Logic: You can ONLY accept or send friend requests if the target user has MORE THAN 3,000 (3k) followers or friends. If they have less, you MUST skip or decline.
2. Human Simulation: Humans do not repeat the same task forever. You must switch between Newsfeed, Pages, Groups, Profiles, and Self-Posting to look natural. Do not spam comments.
2b. Comment language: If the visible post contains Bengali script, comment_text must be in Bengali only. If the post is English-only, comment_text must be English only — never mix languages.
3. Behavior Variety: Mix up your actions (e.g., scroll without liking, read a post without commenting).
4. Feed-first: Actions **like**, **comment**, and **share_post** ONLY when `feed_has_posts` is true and you are on the **newsfeed** (https://www.facebook.com/). If `feed_has_posts` is false, use **navigate_to** with target_url https://www.facebook.com/ — never stay on groups_browse, explore, or discover pages.
5. Do NOT use **join_group** more than once every 10 actions. Prefer **scroll**, **like**, **comment** on the home feed.
6. For **navigate_to**, only use: https://www.facebook.com/ (newsfeed), https://www.facebook.com/notifications, or https://www.facebook.com/friends/requests. Never use explore, groups_browse, or groups/discover URLs.

### OUTPUT FORMAT:
You must reply ONLY with a valid JSON object. Do not include any conversational filler, markdown code blocks (like ```json), or extra text. Your output will be directly parsed by an automation script.

The JSON object must look exactly like this:
{
  "thought_process": "Brief explanation of why you chose this action based on human psychology.",
  "location": "newsfeed" | "group" | "page" | "profile" | "notifications",
  "action": "scroll" | "like" | "comment" | "send_friend_request" | "accept_friend_request" | "join_group" | "create_post" | "share_post" | "navigate_to",
  "target_url": "URL of the page/group/profile to navigate to (if action is navigate_to, create_post, or share_post), else null",
  "action_data": {
    "comment_text": "The exact comment string if action is comment (must match post context), else null",
    "post_content": "The exact post text or share caption if action is create_post or share_post, else null"
  }
}"""


@dataclass
class AgentActionData:
    comment_text: str | None = None
    post_content: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> "AgentActionData":
        if not isinstance(raw, dict):
            return cls()
        ct = raw.get("comment_text")
        pc = raw.get("post_content")
        return cls(
            comment_text=str(ct).strip() if ct else None,
            post_content=str(pc).strip() if pc else None,
        )


@dataclass
class AgentDecision:
    thought_process: str
    location: LocationType
    action: ActionType
    target_url: str | None
    action_data: AgentActionData

    def to_dict(self) -> dict[str, Any]:
        return {
            "thought_process": self.thought_process,
            "location": self.location,
            "action": self.action,
            "target_url": self.target_url,
            "action_data": {
                "comment_text": self.action_data.comment_text,
                "post_content": self.action_data.post_content,
            },
        }


@dataclass
class AgentState:
    """Snapshot passed to the brain each turn."""

    current_url: str
    location: LocationType
    visible_post_snippet: str | None = None
    target_audience_count: int | None = None
    pending_friend_requests: int = 0
    recent_actions: list[str] = field(default_factory=list)
    comments_this_session: int = 0
    likes_this_session: int = 0
    cycles_on_same_location: int = 0
    feed_has_posts: bool = False

    def to_prompt_context(self) -> str:
        lines = [
            f"current_url: {self.current_url}",
            f"detected_location: {self.location}",
            f"feed_has_posts: {self.feed_has_posts}",
            f"pending_friend_requests: {self.pending_friend_requests}",
            f"comments_this_session: {self.comments_this_session}",
            f"likes_this_session: {self.likes_this_session}",
            f"cycles_on_same_location: {self.cycles_on_same_location}",
            f"recent_actions (newest last): {', '.join(self.recent_actions[-12:]) or 'none'}",
        ]
        if self.visible_post_snippet:
            lines.append(f"visible_post_snippet: {self.visible_post_snippet[:500]}")
        if self.target_audience_count is not None:
            lines.append(f"target_audience_count (friends or followers): {self.target_audience_count}")
        else:
            lines.append("target_audience_count: unknown")
        return "\n".join(lines)


_VALID_LOCATIONS: frozenset[str] = frozenset(
    {"newsfeed", "group", "page", "profile", "notifications"},
)
_VALID_ACTIONS: frozenset[str] = frozenset(
    {
        "scroll",
        "like",
        "comment",
        "send_friend_request",
        "accept_friend_request",
        "join_group",
        "create_post",
        "share_post",
        "navigate_to",
    },
)


def _coerce_location(raw: str, fallback: LocationType) -> LocationType:
    key = (raw or "").strip().lower()
    return key if key in _VALID_LOCATIONS else fallback  # type: ignore[return-value]


def _coerce_action(raw: str) -> ActionType:
    key = (raw or "").strip().lower()
    if key in _VALID_ACTIONS:
        return key  # type: ignore[return-value]
    return "scroll"


def parse_agent_decision(payload: dict[str, Any], *, fallback_location: LocationType) -> AgentDecision:
    action_data = AgentActionData.from_dict(payload.get("action_data"))
    target = payload.get("target_url")
    return AgentDecision(
        thought_process=str(payload.get("thought_process") or "").strip() or "No rationale provided.",
        location=_coerce_location(str(payload.get("location") or ""), fallback_location),
        action=_coerce_action(str(payload.get("action") or "")),
        target_url=str(target).strip() if target else None,
        action_data=action_data,
    )


def enforce_agent_rules(decision: AgentDecision, state: AgentState) -> AgentDecision:
    """Hard-code business laws the model must not override."""
    aud = state.target_audience_count
    min_a = DEFAULT_MIN_AUDIENCE

    if decision.action in ("send_friend_request", "accept_friend_request"):
        if aud is not None and aud <= min_a:
            return AgentDecision(
                thought_process=(
                    f"Blocked {decision.action}: audience {aud} is not above {min_a}. "
                    "Scrolling instead per strict law."
                ),
                location=state.location,
                action="scroll",
                target_url=None,
                action_data=AgentActionData(),
            )
        if aud is None and decision.action == "send_friend_request":
            return AgentDecision(
                thought_process="Blocked send_friend_request: audience count unknown.",
                location=state.location,
                action="scroll",
                target_url=None,
                action_data=AgentActionData(),
            )

    recent = state.recent_actions[-16:]

    # Stuck scrolling / liking without comments: force a comment on the feed.
    if (
        state.feed_has_posts
        and state.location == "newsfeed"
        and decision.action in ("scroll", "like")
        and len(recent) >= 3
        and recent.count("comment") == 0
        and sum(1 for a in recent[-6:] if a in ("scroll", "like")) >= 3
    ):
        return AgentDecision(
            thought_process="Too much scrolling without engagement — commenting on a visible post.",
            location="newsfeed",
            action="comment",
            target_url=None,
            action_data=AgentActionData(),
        )

    if decision.action == "accept_friend_request" and state.pending_friend_requests <= 0:
        return AgentDecision(
            thought_process="No pending requests visible — not accepting.",
            location=state.location,
            action="scroll",
            target_url=None,
            action_data=AgentActionData(),
        )

    if not state.feed_has_posts and decision.action in (
        "like",
        "comment",
        "share_post",
        "join_group",
        "scroll",
    ):
        return AgentDecision(
            thought_process="No posts on this page — returning to home newsfeed.",
            location="newsfeed",
            action="navigate_to",
            target_url="https://www.facebook.com/",
            action_data=AgentActionData(),
        )

    if decision.action in ("like", "comment", "share_post") and (
        not state.feed_has_posts or state.location != "newsfeed"
    ):
        return AgentDecision(
            thought_process="Engagement only on home feed with visible posts.",
            location="newsfeed",
            action="navigate_to",
            target_url="https://www.facebook.com/",
            action_data=AgentActionData(),
        )

    if decision.action == "join_group" and recent.count("join_group") >= 1:
        return AgentDecision(
            thought_process="join_group used recently — staying on feed.",
            location="newsfeed",
            action="like" if state.feed_has_posts else "navigate_to",
            target_url=None if state.feed_has_posts else "https://www.facebook.com/",
            action_data=AgentActionData(),
        )

    if decision.action == "navigate_to" and decision.target_url:
        bad = ("explore", "groups_browse", "groups/discover", "groups/discovery")
        if any(b in decision.target_url.lower() for b in bad):
            return AgentDecision(
                thought_process="Blocked bad navigate URL — using home feed.",
                location="newsfeed",
                action="navigate_to",
                target_url="https://www.facebook.com/",
                action_data=AgentActionData(),
            )

    return decision


def decide_next_action(
    state: AgentState,
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 90.0,
) -> AgentDecision:
    """
    Ask Ollama for the next action JSON, parse it, and apply server-side rule enforcement.
    """
    user = (
        "Given the account state below, output exactly one next action as JSON only.\n\n"
        f"{state.to_prompt_context()}\n\n"
        "Remember: friend requests only if target_audience_count > 3000. "
        "Prefer variety if recent_actions repeat the same action."
    )
    raw = _chat(
        [
            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        model=model,
        base_url=base_url,
        timeout=timeout,
        format_json=True,
    )
    payload = _extract_json_object(raw)
    decision = parse_agent_decision(payload, fallback_location=state.location)
    return enforce_agent_rules(decision, state)


def offline_engagement_decision(
    state: AgentState,
    *,
    offline_step: int,
    comments_this_session: int = 0,
    likes_this_session: int = 0,
) -> AgentDecision:
    """
    Rotate like / comment / share / post when Ollama is down (no API calls).
    Text still comes from Gemini + offline templates in ``ai_comment``.
    """
    on_feed = state.feed_has_posts and state.location == "newsfeed"

    if not on_feed:
        return AgentDecision(
            thought_process="Ollama offline — returning to home feed.",
            location="newsfeed",
            action="navigate_to",
            target_url="https://www.facebook.com/",
            action_data=AgentActionData(),
        )

    plan: tuple[tuple[str, str], ...] = (
        ("comment", "Ollama offline — comment on feed (Gemini/Ollama text)."),
        ("like", "Ollama offline — like a feed post."),
        ("comment", "Ollama offline — another comment."),
        ("like", "Ollama offline — like."),
        ("share_post", "Ollama offline — share to profile."),
        ("create_post", "Ollama offline — status post."),
    )

    if comments_this_session < 1:
        action, thought = "comment", "Ollama offline — first comment this cycle."
    elif likes_this_session < comments_this_session:
        action, thought = "like", "Ollama offline — like after comment."
    else:
        action, thought = plan[offline_step % len(plan)]

    from typing import cast

    return AgentDecision(
        thought_process=thought,
        location="newsfeed",
        action=cast(ActionType, action),
        target_url=None,
        action_data=AgentActionData(),
    )


def fallback_decision(state: AgentState, *, reason: str) -> AgentDecision:
    """Backward-compatible alias — never scroll-only on Ollama errors."""
    _ = reason
    return offline_engagement_decision(state, offline_step=0)
