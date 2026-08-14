"""Which tool calls actually cost the money.

Hermes tracks usage per model and per task. Neither answers the question an
operator actually asks after a large bill, which is *what did I pay for*.

The naive answer -- rank tool results by size -- is wrong, and wrong in a way
that matters. A tool result is not paid for once. It enters the conversation and
is then re-transmitted as input on every subsequent API call in the session, so
its true cost is

    size x (number of API calls that came after it)

A 30k-token file read on the second call of a forty-call session is not a 30k
expense; it is closer to 1.1M tokens of re-transmission. The same read at the end
costs almost nothing. Position dominates size, which is the opposite of what the
obvious ranking tells you, and it is why "just read fewer files" is bad advice
while "read the big ones late, or not at all" is good advice.

Prompt caching reduces the *price* of those re-sends, often steeply, but not the
volume -- and cache hits are not guaranteed across turns or providers. This
module therefore reports token volume, and is explicit that it is an upper bound
on what you were billed rather than a billing figure.

Result sizes are estimated at 4 characters per token. `calibration()` reports the
estimate against the measured input-token growth so the error is visible instead
of assumed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

CHARS_PER_TOKEN = 4.0

# Bound per-session history. Beyond this the oldest tool events are folded into
# an "earlier calls" bucket rather than dropped, so totals stay honest.
MAX_EVENTS = 4000


@dataclass
class ToolEvent:
    seq: int
    tool: str
    detail: str
    est_tokens: int


@dataclass
class ApiEvent:
    seq: int
    input_tokens: int


@dataclass
class SessionTrace:
    seq: int = 0
    tools: List[ToolEvent] = field(default_factory=list)
    apis: List[ApiEvent] = field(default_factory=list)
    # Folded-away history so long sessions still report correct totals.
    overflow_tokens: int = 0
    overflow_calls: int = 0


@dataclass
class ToolCost:
    tool: str
    detail: str
    calls: int
    first_tokens: int      # what the results themselves contained
    resent_tokens: int     # first_tokens amortised over later API calls
    resends: int


class Attribution:
    """Ordered per-session log of tool results and API calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._traces: Dict[str, SessionTrace] = {}

    def _trace(self, key: str) -> SessionTrace:
        tr = self._traces.get(key)
        if tr is None:
            tr = SessionTrace()
            self._traces[key] = tr
            if len(self._traces) > 256:
                for stale in list(self._traces)[:32]:
                    if stale != key:
                        self._traces.pop(stale, None)
        return tr

    def record_tool(self, key: str, tool: str, detail: str, result_chars: int) -> None:
        with self._lock:
            tr = self._trace(key or "_default")
            tr.seq += 1
            est = int(max(0, result_chars) / CHARS_PER_TOKEN)
            tr.tools.append(ToolEvent(tr.seq, tool, detail, est))
            if len(tr.tools) > MAX_EVENTS:
                dropped = tr.tools[: len(tr.tools) - MAX_EVENTS]
                tr.tools = tr.tools[len(tr.tools) - MAX_EVENTS :]
                tr.overflow_tokens += sum(e.est_tokens for e in dropped)
                tr.overflow_calls += len(dropped)

    def record_api(self, key: str, input_tokens: int) -> None:
        with self._lock:
            tr = self._trace(key or "_default")
            tr.seq += 1
            tr.apis.append(ApiEvent(tr.seq, max(0, int(input_tokens or 0))))
            if len(tr.apis) > MAX_EVENTS:
                tr.apis = tr.apis[len(tr.apis) - MAX_EVENTS :]

    def reset(self, key: str) -> None:
        with self._lock:
            self._traces.pop(key or "_default", None)

    # -- reporting ---------------------------------------------------------

    def rank(self, key: str, top: int = 8) -> Tuple[List[ToolCost], int]:
        """Tool costs, heaviest first, plus the session's total re-sent tokens.

        Grouped by (tool, detail) so twelve reads of the same file appear as one
        line -- that is the thing you would act on, not twelve separate ones.
        """
        with self._lock:
            tr = self._trace(key or "_default")
            api_seqs = [a.seq for a in tr.apis]
            grouped: Dict[Tuple[str, str], ToolCost] = {}
            for ev in tr.tools:
                # How many API calls came after this result entered the context.
                resends = sum(1 for s in api_seqs if s > ev.seq)
                gkey = (ev.tool, ev.detail)
                acc = grouped.get(gkey)
                if acc is None:
                    acc = ToolCost(ev.tool, ev.detail, 0, 0, 0, 0)
                    grouped[gkey] = acc
                acc.calls += 1
                acc.first_tokens += ev.est_tokens
                acc.resent_tokens += ev.est_tokens * resends
                acc.resends += resends
            ranked = sorted(grouped.values(), key=lambda c: -c.resent_tokens)
            total = sum(c.resent_tokens for c in grouped.values())
            return ranked[:top], total

    def calibration(self, key: str) -> Optional[Tuple[int, int]]:
        """(estimated first-transmission tokens, measured input growth).

        The measured figure is the sum of positive input-token deltas between
        consecutive API calls -- roughly what actually entered the context. If
        the two are far apart the 4-chars-per-token estimate is off for this
        workload and the ranking should be read as ordinal, not absolute.
        """
        with self._lock:
            tr = self._trace(key or "_default")
            if len(tr.apis) < 2:
                return None
            measured = 0
            for prev, cur in zip(tr.apis, tr.apis[1:]):
                delta = cur.input_tokens - prev.input_tokens
                if delta > 0:
                    measured += delta
            estimated = sum(e.est_tokens for e in tr.tools) + tr.overflow_tokens
            return estimated, measured


def humanise(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def format_report(ranked: List[ToolCost], total: int,
                  calib: Optional[Tuple[int, int]]) -> str:
    if not ranked:
        return "No tool calls recorded for this session yet."
    lines = [
        "Estimated context cost by tool — result size x how many API calls "
        "re-sent it.",
        "",
        f"{'tool':<18}{'calls':>6}{'result':>9}{'re-sent':>10}  target",
    ]
    for c in ranked:
        detail = c.detail if len(c.detail) <= 34 else c.detail[:31] + "..."
        lines.append(
            f"{c.tool:<18}{c.calls:>6}{humanise(c.first_tokens):>9}"
            f"{humanise(c.resent_tokens):>10}  {detail}"
        )
    lines.append("")
    lines.append(f"Total re-sent: ~{humanise(total)} tokens")
    if calib:
        est, measured = calib
        if measured:
            ratio = est / measured
            lines.append(
                f"Estimator check: {humanise(est)} estimated vs "
                f"{humanise(measured)} measured context growth "
                f"({ratio:.2f}x)."
            )
            if not (0.5 <= ratio <= 2.0):
                lines.append(
                    "  The estimate is well off for this workload — read the "
                    "ranking as an ordering, not as token counts."
                )
    lines.append(
        "Caveat: token volume, not billing. Prompt caching usually discounts "
        "re-sends steeply, so treat this as an upper bound on what you paid."
    )
    return "\n".join(lines)
