# This is a fork of Hermes Agent

**Upstream:** [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) — MIT, © 2025 Nous Research.
Forked at commit [`e9579a989`](https://github.com/NousResearch/hermes-agent/commit/e9579a989).

Everything in this repository that is not listed below is upstream work by Nous
Research, unmodified. `LICENSE` is unchanged and still applies. This fork adds
five features and does not remove any.

---

## What this fork adds

| # | Feature | Surface |
|---|---|---|
| 1 | `delegate_task(mode="explore")` — read-only subagents with a distillation contract | `tools/delegate_tool.py`, `toolsets.py` |
| 2 | `budget-guard` — per-session spend ceiling with graduated enforcement | `plugins/budget-guard/` |
| 3 | `/spend` — per-tool cost attribution that prices context residency | `plugins/budget-guard/` |
| 4 | `agent-watchdog` — detects repeat / cycle / churn loops | `plugins/agent-watchdog/` |
| 5 | `pinboard` — findings that survive context compaction | `plugins/pinboard/` |

One upstream file is changed for a reason unrelated to the features.
`.github/workflows/install-e2e.yml` asks whether a user on a released version can
update to the current commit — it samples the repo's release tags and updates
from each. A repository with no releases has nothing to update *from*, which is
the normal state of a fork (git does not push tags by default) and of upstream
before its first release. It was failing twice a day here over a claim this repo
cannot make. The tag step now treats an empty tag list as an empty matrix rather
than an error, and the route jobs skip. `scripts/sandbox/pick-release-tags.sh`
is untouched and still exits non-zero, which is correct for callers that have
not fetched tags — the workflow can tell the two cases apart because it
configures the checkout, and the script cannot.

**Nothing is enabled by default.** The plugins are opt-in via `plugins.enabled`,
and `mode="explore"` is a parameter the model chooses. Core changes total **two
lines**, both config-root registrations in `hermes_cli/config.py`; everything else
is new files on the public plugin surface.

```
 4,119 insertions, 4 deletions, across 26 files vs the fork point
    18 new files (5 plugins + their tests and READMEs, FORK.md, HANDOFF.md)
     8 upstream files touched, 2 of them core (both config-root registrations)
   169 tests added, all passing
```

Full engineering writeup, including the bugs found and the design arguments:
[`HANDOFF.md`](HANDOFF.md).

---

## How much it improves things

Only measured numbers appear here. Where something was not measured, it says so.

### 1. Exploration context saving — measured

An exploring subagent works in its own context, which is discarded. The parent
pays for the child's summary, not for the twenty files the child opened.

Measured against a scripted-model harness, A/B, same task both arms:

| follow-on turns in the session | context saved |
|---|---|
| 0 | **−3%** (a small loss) |
| 6 | **52%** |
| 25 | **75%** |

**54,036 tokens saved per follow-on turn.** The negative result at zero turns is
the honest and important one: if the session ends right after the exploration,
delegating costs slightly *more* than doing it inline. The saving comes from not
re-transmitting the explored material on every subsequent API call, so it only
pays off if the session continues.

### 2. Cost attribution — a 19x error this makes visible

Hermes attributes usage per model and per task, never per tool. `/spend` prices
each tool call by how long its result stays resident:

```
true cost = result size x number of API calls that came after it
```

```
tool               calls   result   re-sent  target
read_file              1      30k      570k  gateway/run.py
read_file              1      30k       30k  docs/huge_late.md
```

Those two reads are **the same size**. One cost **19x** the other purely because
it entered the context early. Ranking by result size — the intuitive view — shows
them as equal and gives exactly the wrong advice.

### 3. Budget enforcement — verified to land, not crash

| Spent | Behaviour |
|---|---|
| 75% | agent is told its remaining budget and asked to converge |
| 90% | `delegate_task` denied — one agent, no multiplication |
| 100% | all tools denied; the model answers from what it has |

Live-verified: the ceiling fired and the session **ended with a real text answer
and `exit=0`**, not an exception. Tokens are tracked in parallel with dollars
because subscription routes report `included` — a dollar-only ceiling would
silently stop enforcing on exactly those routes.

### 4. Hybrid local + API operation — proven end to end

An API model plans and edits; a local model does the reading. Routed by
`mode="explore"`, so the local child is structurally read-only.

Real API parent (`nemotron-3-super-120b` via OpenRouter), real local child, no
stubs on either side. Ground truth: `MAX_RETRIES = 5` on line 1.

| local child | reported | correct |
|---|---|---|
| Qwen2.5-1.5B | `3`, line 10, invented a comment | ✗ |
| Qwen2.5-7B (RAM-streamed layers) | `5` | ✓ |
| Qwen2.5-7B (AirLLM, disk-streamed layers) | `5`, line 1, + evidence | ✓ |

The plumbing works at every size. **Model capability was the binding constraint,
not the design** — and the `evidence` field is what exposed the 1.5B's error,
because a cited `file:line` is checkable in one look while a bare claim is not.

Neither 7B run's agent loop terminated cleanly; see `HANDOFF.md` for exactly why
(harness token caps, then a stray tool call overflowing `max_seq_len`).

---

## What is *not* claimed

- **No live billing validation.** The token savings are measured; the resulting
  dollar savings are inferred from them, not observed on an invoice.
- **`explore` was not observed end-to-end in one session** — top-level
  `delegate_task` always detaches and the CLI exits before the child's first API
  call. It was verified against a real `AIAgent` parent instead.
- **The `warn` budget tier cannot fire inside the first turn** of a session, by
  construction. Blocking tiers are unaffected.
- Compaction was shown running *with* a pin active, not shrinking history — the
  scripted model reports a constant token count. The structural claim holds
  regardless: a pin never enters the transcript, so rewriting history cannot
  touch it.

## Regressions

Checked against a clean worktree at the fork point rather than assumed:

- clean `HEAD`: **20 failed, 1402 passed**
- this fork: **20 failed, 1486 passed**

Identical failures on both trees (`video_gen` / `hindsight` / `teams` — all
unconfigured credentials), and exactly **+84 passing**, which are the new tests.
**Zero new failures.**
