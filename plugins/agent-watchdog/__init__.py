"""agent-watchdog — notices when an agent has stopped making progress.

`budget-guard` stops a session that is spending too much. This stops one that is
spending for no reason: the same call four times over, an A-B-A-B loop, a long
run of searches that turn up nothing new. Hermes already spots repeated *file*
reads and repeated patch failures; this generalises that to every tool.

The intervention is a nudge, not a kill. The first time a pattern is detected the
call is blocked once, with an explanation the model can act on, and then the
pattern is re-armed. So a model that was genuinely stuck gets told; a model with
a good reason to repeat itself carries on after one interruption. Nothing raises
into the agent loop.

Deliberate polling is the honest false positive here, and it is handled two ways:
tools whose purpose is to wait are exempt, and the repeat rule keys on the
*result* rather than the call -- if the output changed, that is progress, not a
loop.

Implemented on the public plugin surface -- no core changes:

    post_tool_call     folds each completed call into the history
    pre_tool_call      blocks once when a pattern is detected
    on_session_start   clears the history

Configure in ~/.hermes/config.yaml (all optional):

    watchdog:
      enabled: true
      repeat_threshold: 4     # identical call + identical result
      cycle_threshold: 3      # times an A-B(-C) cycle goes round
      churn_threshold: 12     # calls yielding nothing new
      exempt_tools: [my_poll]  # added to the built-in waiters
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .detector import ADVICE, POLLING_TOOLS, Watchdog, WatchdogConfig

logger = logging.getLogger(__name__)

_WATCHDOG = Watchdog()


def _config() -> WatchdogConfig:
    """Read thresholds from config.yaml. Never raises."""
    try:
        from cli import CLI_CONFIG

        raw = (CLI_CONFIG or {}).get("watchdog") or {}
    except Exception:
        return WatchdogConfig()
    if not isinstance(raw, dict):
        return WatchdogConfig()

    def _int(key: str, default: int) -> int:
        try:
            val = int(raw.get(key, default))
            return val if val > 0 else default
        except (TypeError, ValueError):
            logger.warning("watchdog.%s=%r is not an integer; using %d",
                           key, raw.get(key), default)
            return default

    enabled = raw.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"true", "1", "yes", "on"}
    extra = raw.get("exempt_tools") or []
    if isinstance(extra, str):
        extra = [extra]
    try:
        exempt = frozenset(POLLING_TOOLS) | {str(t).strip() for t in extra if str(t).strip()}
    except TypeError:
        logger.warning("watchdog.exempt_tools=%r is not a list; ignoring", extra)
        exempt = POLLING_TOOLS

    return WatchdogConfig(
        enabled=bool(enabled),
        repeat_threshold=_int("repeat_threshold", 4),
        cycle_threshold=_int("cycle_threshold", 3),
        churn_threshold=_int("churn_threshold", 12),
        exempt_tools=exempt,
    )


def _key(task_id: str, session_id: str) -> str:
    return session_id or task_id or "_default"


def on_pre_tool_call(
    *, tool_name: str = "", args: Any = None, task_id: str = "", session_id: str = "",
    **_: Any,
) -> Optional[Dict[str, str]]:
    """Block a call once when it looks like a loop."""
    try:
        _WATCHDOG.cfg = _config()
        if not _WATCHDOG.cfg.enabled or not tool_name:
            return None
        key = _key(task_id, session_id)
        finding = _WATCHDOG.check(key, tool_name, args)
        if finding is None:
            return None
        # Report a given pathology once, then re-arm. A model that ignores the
        # advice and keeps looping will be told again only after the pattern
        # breaks and re-forms, so this can never wedge a session.
        if not _WATCHDOG.mark_reported(key, finding.pathology):
            return None
        logger.info(
            "agent-watchdog: %s on %s (%s)",
            finding.pathology, finding.tool_name, finding.detail,
        )
        return {
            "action": "block",
            "message": (
                f"Blocked once by agent-watchdog: {finding.detail}. "
                f"{ADVICE[finding.pathology]}"
            ),
        }
    except Exception:
        logger.debug("agent-watchdog: pre_tool_call check failed", exc_info=True)
        return None


def on_post_tool_call(
    *, tool_name: str = "", args: Any = None, result: Any = "", task_id: str = "",
    session_id: str = "", **_: Any,
) -> None:
    """Fold the completed call into the history."""
    try:
        if not tool_name:
            return
        key = _key(task_id, session_id)
        _WATCHDOG.record(key, tool_name, args, result)
        # A call that got through and produced something new means the previous
        # pattern is broken; re-arm so a later relapse is reported again.
        _WATCHDOG.clear_reported(key, "repeat")
    except Exception:
        logger.debug("agent-watchdog: post_tool_call record failed", exc_info=True)


def on_session_start(*, session_id: str = "", **_: Any) -> None:
    _WATCHDOG.reset(session_id)


def register(ctx: Any) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("on_session_start", on_session_start)
