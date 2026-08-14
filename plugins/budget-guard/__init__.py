"""budget-guard — a hard spend ceiling for a Hermes session.

An agent that can call itself, spawn subagents, and loop on tools can spend a
surprising amount before anyone notices. Timeouts do not help: the failure mode
is not a hang, it is steady, plausible-looking progress at $3 a minute.

This plugin gives a session a ceiling and makes the agent *land* rather than
crash into it. Enforcement is graded, and every tier is recoverable:

    75%   warn        the agent is told its remaining budget and asked to converge
    90%   no_fanout   delegate_task is denied -- one agent, no multiplication
    100%  stop        all tools are denied; the model answers from what it has

Nothing is ever truncated mid-call and no exception is raised into the loop.
Denying a tool returns an ordinary tool message, which the model reads and
responds to, so the worst case is a session that ends early with a real answer
instead of one that ends late with a bill.

Implemented entirely on the public plugin surface -- no core changes:

    post_api_request   real provider usage, converted to USD via the same
                       pricing tables the cost display uses
    pre_llm_call       injects the budget line into the turn (the one hook
                       whose return value is honoured)
    pre_tool_call      returns a block directive at the upper tiers
    on_session_start   resets the ledger for a fresh session
    on_session_end     logs the final line

Configure in ~/.hermes/config.yaml:

    budget:
      max_usd_per_session: 5.00
      max_tokens_per_session: 2000000   # fallback ceiling; also a cap in its own right
      warn_at: 0.75
      no_fanout_at: 0.90
      stop_at: 1.0

Set neither ceiling and the plugin stays dormant.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .attribution import Attribution, format_report
from .ledger import (
    BudgetConfig,
    Ledger,
    TIER_NO_FANOUT,
    TIER_OK,
    TIER_STOP,
    TIER_WARN,
    describe,
    fraction_used,
    tier_for,
)

logger = logging.getLogger(__name__)

_LEDGER = Ledger()
_ATTRIB = Attribution()

# Slash-command handlers are invoked as ``handler(arg_string)`` with no session
# context, so /budget and /spend cannot know which session they are being asked
# about. The hooks do know, and they fire constantly, so the most recent session
# they saw is the right answer for an interactive command.
_LAST_SESSION = {"key": ""}

# Tools whose whole purpose is to multiply spend. Denied one tier earlier than
# everything else, because a single delegate_task can fan out to N children and
# blow the remaining budget before the next check.
_FANOUT_TOOLS = frozenset({"delegate_task", "cronjob"})


def _config() -> BudgetConfig:
    """Read thresholds from config.yaml. Never raises -- a broken budget block
    disables the guard rather than breaking the session."""
    try:
        from cli import CLI_CONFIG

        raw = (CLI_CONFIG or {}).get("budget") or {}
    except Exception:
        return BudgetConfig()

    if not isinstance(raw, dict):
        return BudgetConfig()

    def _num(key: str, default=None):
        val = raw.get(key, default)
        if val is None:
            return default
        try:
            return float(val)
        except (TypeError, ValueError):
            logger.warning("budget.%s=%r is not a number; ignoring", key, val)
            return default

    max_usd = _num("max_usd_per_session")
    max_tokens = _num("max_tokens_per_session")
    cfg = BudgetConfig(
        max_usd=max_usd if max_usd and max_usd > 0 else None,
        max_tokens=int(max_tokens) if max_tokens and max_tokens > 0 else None,
        warn_at=_num("warn_at", 0.75) or 0.75,
        no_fanout_at=_num("no_fanout_at", 0.90) or 0.90,
        stop_at=_num("stop_at", 1.0) or 1.0,
    )
    return cfg


class _AttrView:
    """Expose a mapping's keys as attributes.

    ``normalize_usage`` reads its input with ``getattr``, so a usage payload
    that arrives as a plain dict -- which some providers and proxies do send --
    would read as all-zero and the guard would never fire. Silently measuring
    nothing is the worst failure mode available to a spend limiter, so dicts are
    adapted rather than trusted to be objects.
    """

    __slots__ = ("_d",)

    def __init__(self, d: dict) -> None:
        self._d = d

    def __getattr__(self, name: str) -> Any:
        val = self._d.get(name)
        return _AttrView(val) if isinstance(val, dict) else val


def _cost_of(
    usage: Any, model: str, provider: str, base_url: str, api_mode: str = ""
) -> tuple[Optional[float], int]:
    """Convert a raw provider usage payload into (usd, tokens).

    Returns (None, tokens) when the route has no pricing -- subscription plans
    report 'included', unfamiliar models report nothing. Callers must treat a
    None cost as "track tokens only", not as zero.
    """
    tokens = 0
    try:
        from agent.usage_pricing import estimate_usage_cost, normalize_usage

        if isinstance(usage, dict):
            usage = _AttrView(usage)
        # api_mode selects the token-field layout: Anthropic reports
        # input_tokens/output_tokens, Codex nests cache detail differently, and
        # OpenAI uses prompt_tokens/completion_tokens. Omitting it silently
        # routes Anthropic usage through the OpenAI branch and counts zero.
        canonical = normalize_usage(
            usage, provider=provider or None, api_mode=api_mode or None
        )
        tokens = int(getattr(canonical, "total_tokens", 0) or 0)
        if not tokens:
            tokens = int(getattr(canonical, "input_tokens", 0) or 0) + int(
                getattr(canonical, "output_tokens", 0) or 0
            )
        result = estimate_usage_cost(
            model, canonical, provider=provider or None, base_url=base_url or None
        )
        amount = getattr(result, "amount_usd", None)
        return (float(amount) if amount is not None else None), tokens
    except Exception:
        logger.debug("budget-guard: could not price usage", exc_info=True)
        return None, tokens



def _key(session_id: str = "", task_id: str = "") -> str:
    """One key resolution for every hook.

    The ledger is written by post_api_request and read by pre_tool_call. If those
    two ever resolved the session differently the accumulated spend would be
    filed under one key and looked up under another, and the ceiling would
    silently never fire. Both hooks carry session_id and task_id, so the order is
    fixed here once rather than repeated per call site.
    """
    return str(session_id or task_id or "_default")


def _input_tokens_of(usage: Any, provider: str, api_mode: str) -> int:
    """Input tokens for this call, cache reads included.

    Attribution needs the size of what was *sent*, which is what a re-transmitted
    tool result contributes to. Cache reads count: caching changes the price of
    those tokens, not whether they crossed the wire.
    """
    try:
        from agent.usage_pricing import normalize_usage

        if isinstance(usage, dict):
            usage = _AttrView(usage)
        c = normalize_usage(usage, provider=provider or None, api_mode=api_mode or None)
        return (
            int(getattr(c, "input_tokens", 0) or 0)
            + int(getattr(c, "cache_read_tokens", 0) or 0)
            + int(getattr(c, "cache_write_tokens", 0) or 0)
        )
    except Exception:
        return 0


def _detail_of(tool_name: str, args: Any) -> str:
    """A short, stable label so repeated calls on the same target group together.

    Grouping twelve reads of one file into a single line is the whole point --
    that is the thing an operator would act on.
    """
    if not isinstance(args, dict):
        return ""
    for field in ("path", "file_path", "pattern", "query", "url", "command", "cmd", "goal"):
        val = args.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()[:80]
    return ""


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def on_post_api_request(
    *,
    session_id: str = "",
    task_id: str = "",
    provider: str = "",
    base_url: str = "",
    model: str = "",
    usage: Any = None,
    api_mode: str = "",
    **_: Any,
) -> None:
    """Accumulate spend. Fires once per API call, including subagent calls.

    Attribution is recorded even with no ceiling configured: /spend is a
    diagnostic and is useful whether or not you are capping anything. Only the
    ledger that drives enforcement is gated on the budget being enabled.
    """
    if usage is None:
        return
    key = _key(session_id, task_id)
    _LAST_SESSION["key"] = key
    _ATTRIB.record_api(key, _input_tokens_of(usage, provider, api_mode))
    cfg = _config()
    if not cfg.enabled:
        return
    cost, tokens = _cost_of(usage, model, provider, base_url, api_mode)
    if cost is None and not tokens:
        return
    _LEDGER.record(key, cost, tokens)


def on_pre_llm_call(*, session_id: str = "", task_id: str = "", **_: Any) -> Optional[Dict[str, str]]:
    """Tell the agent where it stands, once per tier.

    Injected rather than logged because the model is the thing that has to
    change behaviour. Repeating the same warning every turn would just burn
    context, so each tier announces once.
    """
    cfg = _config()
    if not cfg.enabled:
        return None
    key = _key(session_id, task_id)
    spend = _LEDGER.get(key)
    tier = tier_for(spend, cfg)
    if tier == TIER_OK:
        return None
    if not _LEDGER.mark_announced(key, tier):
        return None

    status = describe(spend, cfg)
    if tier == TIER_WARN:
        return {
            "context": (
                f"[budget] {status}. You are approaching this session's spend "
                f"ceiling. Prefer finishing with what you already know over "
                f"further investigation, and avoid spawning subagents."
            )
        }
    if tier == TIER_NO_FANOUT:
        return {
            "context": (
                f"[budget] {status}. Delegation is now disabled for this "
                f"session. Work directly and start converging on an answer."
            )
        }
    return {
        "context": (
            f"[budget] {status}. The spend ceiling is reached and tools are "
            f"now disabled. Answer with what you have, and state plainly what "
            f"you did not get to."
        )
    }


def on_pre_tool_call(
    *, tool_name: str = "", args: Any = None, task_id: str = "", session_id: str = "", **_: Any
) -> Optional[Dict[str, str]]:
    """Deny tools once the ceiling is hit. Returns a block directive, never raises."""
    cfg = _config()
    if not cfg.enabled:
        return None
    spend = _LEDGER.get(_key(session_id, task_id))
    tier = tier_for(spend, cfg)
    if tier in (TIER_OK, TIER_WARN):
        return None

    status = describe(spend, cfg)
    if tier == TIER_NO_FANOUT:
        if tool_name in _FANOUT_TOOLS:
            return {
                "action": "block",
                "message": (
                    f"Blocked by budget-guard: {status}. Delegation multiplies "
                    f"spend and the session is close to its ceiling. Continue "
                    f"directly instead."
                ),
            }
        return None

    # TIER_STOP
    if tool_name in cfg.always_allow:
        return None
    return {
        "action": "block",
        "message": (
            f"Blocked by budget-guard: {status}. The session spend ceiling is "
            f"reached. No further tool calls will run — reply to the user with "
            f"your findings so far and note what remains unfinished."
        ),
    }


def on_post_tool_call(
    *, tool_name: str = "", args: Any = None, result: Any = "",
    task_id: str = "", session_id: str = "", **_: Any,
) -> None:
    """Record how much context a tool result added, for /spend."""
    try:
        if not tool_name:
            return
        text = result if isinstance(result, str) else repr(result)
        _ATTRIB.record_tool(
            _key(session_id, task_id), tool_name, _detail_of(tool_name, args), len(text)
        )
    except Exception:
        logger.debug("budget-guard: attribution record failed", exc_info=True)


def on_session_start(*, session_id: str = "", task_id: str = "", **_: Any) -> None:
    key = _key(session_id, task_id)
    _LEDGER.reset(key)
    _ATTRIB.reset(key)
    try:
        _LEDGER.prune()
    except Exception:
        logger.debug("budget-guard: prune failed", exc_info=True)


def on_session_end(*, session_id: str = "", task_id: str = "", **_: Any) -> None:
    cfg = _config()
    if not cfg.enabled:
        return
    spend = _LEDGER.get(_key(session_id, task_id))
    if spend.api_calls:
        logger.info("budget-guard: session ended — %s", describe(spend, cfg))


# ---------------------------------------------------------------------------
# Slash command
# ---------------------------------------------------------------------------


def _command_session(kwargs: Dict[str, Any]) -> str:
    """Session for an interactive command, falling back to the last one seen."""
    explicit = _key(str(kwargs.get("session_id") or ""), str(kwargs.get("task_id") or ""))
    if explicit != "_default":
        return explicit
    return _LAST_SESSION["key"] or "_default"


def _budget_command(args: str = "", **kwargs: Any) -> str:
    session_id = _command_session(kwargs)
    cfg = _config()
    spend = _LEDGER.get(session_id)
    if not cfg.enabled:
        return (
            "Budget guard is off. Set a ceiling in ~/.hermes/config.yaml:\n\n"
            "  budget:\n"
            "    max_usd_per_session: 5.00\n"
            f"\nThis session so far: {describe(spend, cfg)}"
        )
    tier = tier_for(spend, cfg)
    label = {
        TIER_OK: "ok",
        TIER_WARN: "approaching the ceiling",
        TIER_NO_FANOUT: "delegation disabled",
        TIER_STOP: "ceiling reached — tools disabled",
    }[tier]
    bar_width = 24
    filled = min(bar_width, int(round(fraction_used(spend, cfg) * bar_width)))
    bar = "█" * filled + "·" * (bar_width - filled)
    return f"[{bar}] {label}\n{describe(spend, cfg)}"



def _spend_command(args: str = "", **kwargs: Any) -> str:
    """/spend — which tool calls actually cost the money."""
    session_id = _command_session(kwargs)
    try:
        top = int(args.strip()) if args.strip().isdigit() else 8
    except Exception:
        top = 8
    ranked, total = _ATTRIB.rank(session_id, top=top)
    report = format_report(ranked, total, _ATTRIB.calibration(session_id))
    cfg = _config()
    spend = _LEDGER.get(session_id)
    if spend.api_calls:
        report += f"\n\nSession total: {describe(spend, cfg)}"
    return report


def register(ctx: Any) -> None:
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)
    try:
        ctx.register_command("budget", _budget_command,
                             description="Show this session's spend against its ceiling")
        ctx.register_command("spend", _spend_command,
                             description="Which tool calls cost the most context",
                             args_hint="[top-N]")
    except Exception:
        # Slash-command registration is a convenience, not a requirement.
        logger.debug("budget-guard: /budget command unavailable", exc_info=True)
