"""Pathology detection over a stream of tool calls.

No Hermes imports, so the rules can be tested directly and a detector bug can
never take a session down with it.

Three pathologies, in rising order of how hard they are to spot:

  REPEAT    the same call, with the same arguments, returning the same result.
            Nothing changed and nothing will.

  CYCLE     A, B, A, B ... two or three calls alternating. Common when a model
            reads a file, tries a fix, re-reads, tries the same fix.

  CHURN     many distinct calls, none of which produced a result the session has
            not already seen. Motion without progress -- the hardest of the
            three to notice by eye, because every individual call looks new.

The load-bearing design decision is that REPEAT keys on the *result*, not the
call. A repeated call whose output changed is progress, not a loop -- `ls` after
a write, `git status` mid-edit. Only a call that returns what it returned before
is evidence of being stuck.

That still leaves one honest false positive: deliberate polling, where identical
results are exactly what you expect while waiting for something to change. Tools
whose job is to wait are exempt (see POLLING_TOOLS), and the thresholds are
deliberately loose, because a watchdog that interrupts real work is worse than
one that fires late.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional, Tuple

PATHOLOGY_REPEAT = "repeat"
PATHOLOGY_CYCLE = "cycle"
PATHOLOGY_CHURN = "churn"

# Waiting is the whole point of these, so identical results in a row are the
# expected shape rather than a symptom.
POLLING_TOOLS = frozenset({"process", "cronjob", "kanban_heartbeat"})


def signature(tool_name: str, args: Any) -> str:
    """Stable hash of a call. Argument order must not change the identity."""
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        blob = repr(args)
    return hashlib.sha1(f"{tool_name}\x00{blob}".encode("utf-8", "replace")).hexdigest()[:16]


def result_hash(result: Any) -> str:
    text = result if isinstance(result, str) else repr(result)
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class WatchdogConfig:
    enabled: bool = True
    # Tools exempt from the repeat rule. Defaults cover the built-in waiters;
    # an operator with a custom or MCP polling tool must be able to add to this,
    # otherwise they get a false positive they cannot switch off.
    exempt_tools: frozenset = POLLING_TOOLS
    # Identical call + identical result this many times before intervening.
    # Loose on purpose: three is normal in exploratory work.
    repeat_threshold: int = 4
    # How many times an A-B(-C) cycle must go round.
    cycle_threshold: int = 3
    # Consecutive calls yielding no result the session has not seen before.
    churn_threshold: int = 12
    # Ring buffer length for cycle detection.
    history: int = 24


@dataclass
class Finding:
    pathology: str
    tool_name: str
    count: int
    detail: str


@dataclass
class _TaskState:
    # signature -> (call count, last result hash, identical-result streak)
    calls: Dict[str, Tuple[int, Optional[str], int]] = field(default_factory=dict)
    recent: Deque[str] = field(default_factory=lambda: deque(maxlen=24))
    seen_results: set = field(default_factory=set)
    churn_streak: int = 0
    # Pathologies already reported, so the agent is told once per pattern.
    reported: set = field(default_factory=set)


class Watchdog:
    """Per-task call history and the rules over it. Thread-safe."""

    def __init__(self, cfg: Optional[WatchdogConfig] = None) -> None:
        self.cfg = cfg or WatchdogConfig()
        self._lock = threading.Lock()
        self._tasks: Dict[str, _TaskState] = {}

    # -- bookkeeping -------------------------------------------------------

    def _state(self, task_id: str) -> _TaskState:
        st = self._tasks.get(task_id or "_default")
        if st is None:
            st = _TaskState(recent=deque(maxlen=self.cfg.history))
            self._tasks[task_id or "_default"] = st
            # Bound the map for long-lived gateways serving many chats.
            if len(self._tasks) > 256:
                for stale in list(self._tasks)[:32]:
                    if stale != (task_id or "_default"):
                        self._tasks.pop(stale, None)
        return st

    def record(self, task_id: str, tool_name: str, args: Any, result: Any) -> None:
        """Fold a completed call into the history."""
        sig = signature(tool_name, args)
        rhash = result_hash(result)
        with self._lock:
            st = self._state(task_id)
            count, last_hash, streak = st.calls.get(sig, (0, None, 0))
            streak = streak + 1 if last_hash == rhash else 1
            st.calls[sig] = (count + 1, rhash, streak)
            st.recent.append(sig)
            # Churn: a call that produced a result the task has never seen is
            # progress, whatever else it did.
            if rhash in st.seen_results:
                st.churn_streak += 1
            else:
                st.seen_results.add(rhash)
                st.churn_streak = 0
                if len(st.seen_results) > 4096:
                    st.seen_results.clear()
            # Cap the per-call map so a session touching thousands of distinct
            # calls cannot grow it without bound.
            if len(st.calls) > 2048:
                st.calls.clear()

    def reset(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id or "_default", None)

    # -- rules -------------------------------------------------------------

    def check(self, task_id: str, tool_name: str, args: Any) -> Optional[Finding]:
        """Inspect a call that is about to run. None means it looks healthy."""
        if not self.cfg.enabled or tool_name in (self.cfg.exempt_tools or POLLING_TOOLS):
            return None
        sig = signature(tool_name, args)
        with self._lock:
            st = self._state(task_id)
            _, _, streak = st.calls.get(sig, (0, None, 0))

            if streak >= self.cfg.repeat_threshold:
                return Finding(
                    PATHOLOGY_REPEAT, tool_name, streak,
                    f"identical call returned an identical result {streak} times",
                )

            cyc = self._cycle_length(st)
            if cyc:
                period, turns = cyc
                return Finding(
                    PATHOLOGY_CYCLE, tool_name, turns,
                    f"a {period}-call cycle has repeated {turns} times",
                )

            if st.churn_streak >= self.cfg.churn_threshold:
                return Finding(
                    PATHOLOGY_CHURN, tool_name, st.churn_streak,
                    f"{st.churn_streak} consecutive calls produced no result "
                    f"this session had not already seen",
                )
        return None

    def _cycle_length(self, st: _TaskState) -> Optional[Tuple[int, int]]:
        """Detect a period-2 or period-3 cycle at the tail of the history.

        Longer periods are not worth chasing: by the time a 4-step loop has run
        enough times to be certain, the repeat and churn rules have both fired.
        """
        hist = list(st.recent)
        for period in (2, 3):
            need = period * self.cfg.cycle_threshold
            if len(hist) < need:
                continue
            window = hist[-need:]
            block = window[:period]
            if len(set(block)) < period:
                continue  # not a real alternation, the repeat rule owns this
            if all(window[i] == block[i % period] for i in range(need)):
                return period, self.cfg.cycle_threshold
        return None

    def mark_reported(self, task_id: str, pathology: str) -> bool:
        """True the first time *pathology* is reported for this task."""
        with self._lock:
            st = self._state(task_id)
            if pathology in st.reported:
                return False
            st.reported.add(pathology)
            return True

    def clear_reported(self, task_id: str, pathology: str) -> None:
        with self._lock:
            self._state(task_id).reported.discard(pathology)


ADVICE = {
    PATHOLOGY_REPEAT: (
        "You have made this exact call several times and received the same "
        "result each time; it will not change on its own. Either act on the "
        "result you already have, or change your approach. If you are waiting "
        "for something external, wait explicitly instead of re-polling."
    ),
    PATHOLOGY_CYCLE: (
        "You are alternating between the same small set of calls without "
        "making progress. Step back and reconsider the approach rather than "
        "repeating the sequence."
    ),
    PATHOLOGY_CHURN: (
        "A long run of calls has produced nothing this session had not already "
        "seen. You are likely searching in the wrong place. State what you are "
        "looking for and try a different strategy, or report what you have."
    ),
}
