"""pinboard — findings that survive context compaction.

Long sessions compress. Middle turns get summarised or dropped, and the turn
where the agent finally worked out *the retry lives in gateway/run.py:412* is as
droppable as any other. The agent then re-derives it, which is slow, expensive,
and sometimes it re-derives it differently.

This gives the agent somewhere to put the handful of facts that must not be lost.

The mechanism is re-injection rather than protection. A pin never sits in
compressible history at all: it is stored outside the transcript and added fresh
to every turn, so compaction has nothing to eat and a resumed session still has
it. That also means pins are paid for on every turn, which is why the board is
small and hard-capped -- twenty pins, 240 characters each. It is for conclusions,
not notes.

Implemented on the public plugin surface -- no core changes:

    pin tool         the agent adds/lists/removes findings
    pre_llm_call     injects the board into each turn
    on_session_start clears the board for a genuinely new session

Pins persist to ``$HERMES_HOME/pinboard/<session>.json`` so they outlive a
restart.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .board import MAX_CHARS, MAX_PINS, Board

logger = logging.getLogger(__name__)

_BOARD = Board()

# Slash commands arrive as ``handler(arg_string)`` with no session context, so
# /pins would otherwise read an empty default board. The hooks do see the real
# session id, so remember the last one.
_LAST_SESSION = {"key": ""}

PIN_SCHEMA = {
    "name": "pin",
    "description": (
        "Pin a short finding so it survives context compaction. Long sessions "
        "summarise away their middle, including hard-won conclusions. Pin the "
        "few facts you would hate to re-derive — a root cause, a file:line, a "
        "confirmed constraint, a decision already made. Pinned text is re-sent "
        "on every turn, so keep it to conclusions, not narration."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "list", "remove", "clear"],
                "description": "Defaults to 'add'.",
            },
            "text": {
                "type": "string",
                "description": (
                    f"The finding, for action='add'. One sentence, "
                    f"self-contained, max {MAX_CHARS} characters."
                ),
            },
            "id": {
                "type": "string",
                "description": "Which pin to remove, for action='remove'.",
            },
        },
    },
}


def _session_of(kwargs: Dict[str, Any]) -> str:
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "default")


def pin_tool(args: Any = None, **kwargs: Any) -> str:
    """Handler for the `pin` tool.

    The registry dispatches tools as ``handler(args_dict, **context)`` -- the
    model's arguments arrive as one positional dict, not as keywords. Keyword
    form is also accepted so the handler stays directly callable from tests and
    from ``ctx.dispatch_tool``.
    """
    if isinstance(args, dict):
        params = args
    elif isinstance(args, str) and args:
        params = {"text": args}          # bare-string convenience
    else:
        params = {}
    action = params.get("action", kwargs.pop("action", "add"))
    text = params.get("text", kwargs.pop("text", ""))
    id = params.get("id", kwargs.pop("id", ""))

    session = _session_of(kwargs)
    if session != "default":
        _LAST_SESSION["key"] = session
    act = str(action or "add").strip().lower()
    try:
        if act == "add":
            if not str(text or "").strip():
                return "Error: pin(action='add') needs `text`."
            p = _BOARD.add(session, text)
            return (
                f"Pinned [{p.id}]: {p.text}\n"
                f"({len(_BOARD.list(session))}/{MAX_PINS} pins; these are "
                f"re-sent each turn and survive compaction.)"
            )
        if act == "list":
            pins = _BOARD.list(session)
            if not pins:
                return "No pins yet."
            return "\n".join(f"[{p.id}] {p.text}" for p in pins)
        if act == "remove":
            if not str(id or "").strip():
                return "Error: pin(action='remove') needs `id`."
            return f"Removed [{id}]." if _BOARD.remove(session, id) else f"No pin [{id}]."
        if act == "clear":
            return f"Cleared {_BOARD.clear(session)} pins."
        return f"Error: unknown action {action!r}. Use add, list, remove or clear."
    except ValueError as e:
        return f"Error: {e}"
    except Exception:
        logger.debug("pinboard: pin tool failed", exc_info=True)
        return "Error: could not update the pinboard."


def on_pre_llm_call(*, session_id: str = "", **_: Any) -> Optional[Dict[str, str]]:
    """Re-inject the board so pinned findings outlive compaction."""
    try:
        if session_id:
            _LAST_SESSION["key"] = session_id
        block = _BOARD.render(session_id or "default")
        return {"context": block} if block else None
    except Exception:
        logger.debug("pinboard: injection failed", exc_info=True)
        return None


def on_session_start(*, session_id: str = "", **_: Any) -> None:
    # A genuinely new session starts empty; a resumed one reloads from disk.
    _BOARD.clear(session_id or "default")
    try:
        # One small file per session accumulates otherwise. Cheap, bounded, and
        # never allowed to fail a session start.
        _BOARD.prune()
    except Exception:
        logger.debug("pinboard: prune failed", exc_info=True)


def _pins_command(args: str = "", **kwargs: Any) -> str:
    session = _session_of(kwargs)
    if session == "default" and _LAST_SESSION["key"]:
        session = _LAST_SESSION["key"]
    arg = (args or "").strip().lower()
    if arg == "clear":
        return f"Cleared {_BOARD.clear(session)} pins."
    pins = _BOARD.list(session)
    if not pins:
        return "No pins. The agent can add them with the `pin` tool."
    head = f"{len(pins)} pinned finding(s), re-sent each turn:"
    return head + "\n" + "\n".join(f"  [{p.id}] {p.text}" for p in pins)


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="pin",
        toolset="pinboard",
        schema=PIN_SCHEMA,
        handler=pin_tool,
        description="Pin a finding so it survives context compaction",
        emoji="📌",
    )
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("on_session_start", on_session_start)
    try:
        ctx.register_command("pins", _pins_command,
                             description="Show pinned findings",
                             args_hint="[clear]")
    except Exception:
        logger.debug("pinboard: /pins command unavailable", exc_info=True)
