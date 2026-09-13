"""Morning-briefing tool implementation (get_briefing).

Generates the rich German morning briefing in-chat. Reads mail, calendar and
open Google Tasks and renders the reference format (source-tagged calendar,
conflict fixes, top-5 actions, display-only deep-link proposals).
"""

from typing import Dict, Optional

from src.tools._common import _parse_tool_args


async def do_get_briefing(content: str, owner: Optional[str] = None) -> Dict:
    """Generate the morning briefing for the user (German).

    Optional JSON args: {"max_emails": <int>} to cap how many email headers
    are pulled (default 15).
    """
    args = _parse_tool_args(content) if (content or "").strip().startswith("{") else {}
    if not isinstance(args, dict):
        args = {}
    max_emails = 15
    try:
        max_emails = int(args.get("max_emails") or 15)
    except (TypeError, ValueError):
        max_emails = 15

    from services.briefing.briefing_engine import generate_morning_briefing

    text = await generate_morning_briefing(owner=owner or "", max_emails=max_emails)
    return {"output": text[:20000], "exit_code": 0}