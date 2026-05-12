"""Modular Playwright automation: stealth defaults, UA rotation, and BaseBot."""

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
    human_click,
    human_like_scroll,
    human_scroll,
    human_type,
    random_delay,
    react_to_post,
)
from playwright_automation.ai_comment import get_ai_comment
from playwright_automation.bot_core import BaseBot
from playwright_automation.database import (
    AsyncBotDatabase,
    get_bot_config,
    load_session,
    log_action,
    save_session,
)
from playwright_automation.facebook_graph import (
    AccountRestrictedError,
    FollowStatus,
    FriendRequestStatus,
    raise_if_account_restricted,
)
from playwright_automation.post_engagement import (
    PostEngagementResult,
    SessionState,
    engage_with_next_posts,
)
from playwright_automation.stealth_config import (
    StealthBundle,
    build_stealth,
    fingerprint_init_script,
)
from playwright_automation.user_agent_rotation import RotatedProfile, UserAgentRotator

__all__ = [
    "AccountRestrictedError",
    "AsyncBotDatabase",
    "BaseBot",
    "BrainError",
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
    "analyze_post_and_respond",
    "build_stealth",
    "comment_on_post",
    "engage_with_next_posts",
    "fingerprint_init_script",
    "get_ai_comment",
    "get_bot_config",
    "handle_chat",
    "human_click",
    "human_like_scroll",
    "human_scroll",
    "human_type",
    "load_session",
    "log_action",
    "raise_if_account_restricted",
    "random_delay",
    "react_to_post",
    "save_session",
]
