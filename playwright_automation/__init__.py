"""Modular Playwright automation: stealth, UA rotation, Ollama brain, and BaseBot."""

from playwright_automation.account_registry import (
    AccountRecord,
    load_account,
    load_registry,
    parse_proxy_url,
    resolve_proxy,
)
from playwright_automation.account_session import (
    DEFAULT_ACCOUNT_ID,
    DEFAULT_COOKIES_PATH,
    FEED_URL,
    looks_logged_in,
    parse_account_block_from_cookies,
)
from playwright_automation.fleet_status import FleetBotStatus, read_status, write_status
from playwright_automation.agent_brain import AgentDecision, AgentState, decide_next_action
from playwright_automation.agent_executor import AgentSession, agent_step, gather_agent_state
from playwright_automation.brain import (
    BrainError,
    PostAnalysis,
    analyze_post_and_respond,
    handle_chat,
)
from playwright_automation.actions import (
    GENERIC_COMMENTS,
    ReactionType,
    comment_on_post,
    create_feed_post,
    human_click,
    human_like_scroll,
    human_scroll,
    smooth_scroll,
    human_type,
    random_delay,
    react_to_post,
    share_post,
)
from playwright_automation.ai_comment import (
    detect_post_language,
    generate_comment_for_post,
    generate_status_post,
    get_ai_comment,
)
from playwright_automation.bot_core import BaseBot
from playwright_automation.facebook_graph import (
    DEFAULT_MIN_AUDIENCE,
    DEFAULT_MIN_FRIENDS,
    AccountRestrictedError,
    FollowStatus,
    FriendRequestStatus,
    raise_if_account_restricted,
)
from playwright_automation.post_engagement import (
    PostEngagementResult,
    SessionState,
    engage_with_next_posts,
    pick_reaction_probability_weights,
)
from playwright_automation.stealth_config import (
    StealthBundle,
    build_stealth,
    fingerprint_init_script,
)
from playwright_automation.user_agent_rotation import RotatedProfile, UserAgentRotator

__all__ = [
    "AccountRestrictedError",
    "AgentDecision",
    "AgentSession",
    "AgentState",
    "BaseBot",
    "BrainError",
    "AccountRecord",
    "DEFAULT_ACCOUNT_ID",
    "DEFAULT_COOKIES_PATH",
    "DEFAULT_MIN_AUDIENCE",
    "DEFAULT_MIN_FRIENDS",
    "FEED_URL",
    "FleetBotStatus",
    "load_account",
    "load_registry",
    "parse_proxy_url",
    "read_status",
    "resolve_proxy",
    "write_status",
    "FollowStatus",
    "FriendRequestStatus",
    "GENERIC_COMMENTS",
    "PostAnalysis",
    "PostEngagementResult",
    "ReactionType",
    "RotatedProfile",
    "SessionState",
    "StealthBundle",
    "UserAgentRotator",
    "agent_step",
    "analyze_post_and_respond",
    "decide_next_action",
    "gather_agent_state",
    "build_stealth",
    "comment_on_post",
    "create_feed_post",
    "engage_with_next_posts",
    "fingerprint_init_script",
    "detect_post_language",
    "generate_comment_for_post",
    "generate_status_post",
    "get_ai_comment",
    "handle_chat",
    "human_click",
    "human_like_scroll",
    "human_scroll",
    "looks_logged_in",
    "parse_account_block_from_cookies",
    "smooth_scroll",
    "human_type",
    "pick_reaction_probability_weights",
    "raise_if_account_restricted",
    "random_delay",
    "react_to_post",
    "share_post",
]
