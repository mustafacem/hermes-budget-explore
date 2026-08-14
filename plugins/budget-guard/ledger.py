"""Per-session spend ledger and the policy that reads it.

Deliberately free of Hermes imports so the policy can be tested directly, and so
a pricing lookup failure can never take the agent down with it.

Two currencies, because one is not always available. Cost in USD is what an
operator actually wants to cap, but it is only knowable when the route has
pricing: subscription routes (Claude Pro, Codex OAuth, Nous Portal) report
``included``, and unknown models report nothing at all. A budget that silently
stops enforcing on exactly those routes would be worse than no budget, so the
ledger tracks tokens in parallel and the policy falls back to a token ceiling
whenever cost is unavailable.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

# Tiers, in ascending severity. Each is a fraction of the configured ceiling.
TIER_OK = "ok"
TIER_WARN = "warn"            # nudge the agent to start converging
TIER_NO_FANOUT = "no_fanout"  # deny delegation; one agent, no multiplication
TIER_STOP = "stop"            # deny all tools; the model must answer from context


@dataclass
class SessionSpend:
    """What one session has consumed so far."""

    cost_usd: float = 0.0
    tokens: int = 0
    api_calls: int = 0
    # True once any call reported a real dollar amount. Until then the session
    # is judged on tokens alone -- see the module docstring.
    has_cost_data: bool = False
    # Tiers already announced, so the agent is not told the same thing twice.
    announced: set = field(default_factory=set)

    def add(self, cost_usd: Optional[float], tokens: int) -> None:
        self.api_calls += 1
        self.tokens += max(0, int(tokens or 0))
        if cost_usd is not None:
            self.cost_usd += max(0.0, float(cost_usd))
            self.has_cost_data = True


class Ledger:
    """Thread-safe map of session id -> spend, persisted per session.

    post_api_request can fire concurrently when a turn issues parallel calls,
    and subagents bill against their parent's session, so every mutation is
    guarded.

    Spend is written to disk because the ceiling is advertised as *per session*
    and a session outlives the process that started it. Resuming a conversation
    in a fresh CLI process previously reset the budget to zero, which quietly
    turned a $5 cap into $5 per resume.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, SessionSpend] = {}
        self._root = root

    # -- persistence -------------------------------------------------------

    def _path(self, key: str) -> Optional[Path]:
        root = self._root
        if root is None:
            home = os.environ.get("HERMES_HOME")
            if not home:
                return None
            root = Path(home) / "budget"
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key or "default")[:120]
        return root / f"{safe}.json"

    def _load(self, key: str) -> SessionSpend:
        spend = self._sessions.get(key)
        if spend is not None:
            return spend
        spend = SessionSpend()
        path = self._path(key)
        if path and path.exists():
            try:
                raw = json.loads(path.read_text("utf-8"))
                spend.cost_usd = float(raw.get("cost_usd", 0.0) or 0.0)
                spend.tokens = int(raw.get("tokens", 0) or 0)
                spend.api_calls = int(raw.get("api_calls", 0) or 0)
                spend.has_cost_data = bool(raw.get("has_cost_data", False))
                spend.announced = set(raw.get("announced") or [])
            except Exception:
                spend = SessionSpend()
        self._sessions[key] = spend
        return spend

    def _save(self, key: str, spend: SessionSpend) -> None:
        path = self._path(key)
        if not path:
            return
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "cost_usd": spend.cost_usd, "tokens": spend.tokens,
                "api_calls": spend.api_calls, "has_cost_data": spend.has_cost_data,
                "announced": sorted(spend.announced),
            }), "utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def record(self, session_id: str, cost_usd: Optional[float], tokens: int) -> SessionSpend:
        key = session_id or "_default"
        with self._lock:
            spend = self._load(key)
            spend.add(cost_usd, tokens)
            self._save(key, spend)
            # Bound the map so a long-lived gateway serving many chats cannot
            # grow it without limit. Sessions are cheap structs; 512 is far
            # above any realistic concurrent count.
            if len(self._sessions) > 512:
                for stale in list(self._sessions)[:64]:
                    if stale != key:
                        self._sessions.pop(stale, None)
            return spend

    def get(self, session_id: str) -> SessionSpend:
        with self._lock:
            return self._load(session_id or "_default")

    def reset(self, session_id: str) -> None:
        key = session_id or "_default"
        with self._lock:
            self._sessions.pop(key, None)
            path = self._path(key)
            if path and path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass


    def prune(self, max_age_days: int = 30, max_files: int = 2000) -> int:
        """Drop state files for sessions that are long gone.

        This module writes one small file per session, so without pruning the
        directory grows for the life of the install. Bounded work: a single
        listdir, oldest-first removal, and any error is swallowed -- housekeeping
        must never be able to break a session.
        """
        root = self._path("probe")
        if root is None:
            return 0
        root = root.parent
        removed = 0
        try:
            entries = []
            cutoff = time.time() - max_age_days * 86400
            for f in root.glob("*.json"):
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                entries.append((mtime, f))
                if mtime < cutoff:
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
            survivors = sorted((m, f) for m, f in entries if f.exists())
            excess = len(survivors) - max_files
            for _, f in survivors[:max(0, excess)]:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        except OSError:
            return removed
        return removed

    def mark_announced(self, session_id: str, tier: str) -> bool:
        """Record that *tier* was announced; return True if this is the first time."""
        with self._lock:
            key = session_id or "_default"
            spend = self._load(key)
            if tier in spend.announced:
                return False
            spend.announced.add(tier)
            self._save(key, spend)
            return True


@dataclass
class BudgetConfig:
    """Resolved thresholds. Disabled unless a ceiling is actually set."""

    max_usd: Optional[float] = None
    max_tokens: Optional[int] = None
    warn_at: float = 0.75
    no_fanout_at: float = 0.90
    stop_at: float = 1.0
    # Tools that stay available even at TIER_STOP. Denying everything would be
    # correct but unkind: the agent should still be able to tell the user what
    # it managed to do.
    always_allow: Tuple[str, ...] = ("todo",)

    @property
    def enabled(self) -> bool:
        return bool(self.max_usd) or bool(self.max_tokens)


def fraction_used(spend: SessionSpend, cfg: BudgetConfig) -> float:
    """How much of the ceiling is gone, as a fraction. Highest signal wins.

    When both ceilings are configured the stricter one governs, so a session
    cannot slip past a token cap just because its dollar cost is unknown.
    """
    fractions = []
    if cfg.max_usd and spend.has_cost_data:
        fractions.append(spend.cost_usd / cfg.max_usd)
    if cfg.max_tokens:
        fractions.append(spend.tokens / cfg.max_tokens)
    if not fractions:
        return 0.0
    return max(fractions)


def tier_for(spend: SessionSpend, cfg: BudgetConfig) -> str:
    if not cfg.enabled:
        return TIER_OK
    used = fraction_used(spend, cfg)
    if used >= cfg.stop_at:
        return TIER_STOP
    if used >= cfg.no_fanout_at:
        return TIER_NO_FANOUT
    if used >= cfg.warn_at:
        return TIER_WARN
    return TIER_OK


def describe(spend: SessionSpend, cfg: BudgetConfig) -> str:
    """One-line human/model-readable status."""
    parts = []
    if cfg.max_usd and spend.has_cost_data:
        parts.append(f"${spend.cost_usd:.4f} of ${cfg.max_usd:.2f}")
    elif spend.has_cost_data:
        parts.append(f"${spend.cost_usd:.4f} spent")
    if cfg.max_tokens:
        parts.append(f"{spend.tokens:,} of {cfg.max_tokens:,} tokens")
    else:
        parts.append(f"{spend.tokens:,} tokens")
    pct = fraction_used(spend, cfg)
    return f"{', '.join(parts)} ({pct:.0%} of budget) across {spend.api_calls} API calls"
