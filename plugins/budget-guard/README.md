# budget-guard

A hard spend ceiling for a Hermes session.

An agent that can loop on tools and spawn subagents can spend a lot before anyone
notices. Timeouts don't catch it, because the failure mode isn't a hang — it's
steady, plausible-looking progress at a few dollars a minute.

This plugin gives a session a ceiling and makes the agent **land** rather than
crash into it.

## Enable

```yaml
# ~/.hermes/config.yaml
plugins:
  enabled: [budget-guard]

budget:
  max_usd_per_session: 5.00
```

Without a ceiling the plugin stays dormant. Bundled plugins are opt-in.

## What happens as the budget goes

| Spent | Tier | Behaviour |
|---|---|---|
| < 75% | — | nothing; the guard is silent |
| 75% | `warn` | the agent is told its remaining budget and asked to converge |
| 90% | `no_fanout` | `delegate_task` and `cronjob` are denied — one agent, no multiplication |
| 100% | `stop` | all tools are denied; the model answers from what it has |

Fan-out is cut a tier early on purpose: one `delegate_task` can spawn N children
and blow the rest of the budget before the next check runs.

Every tier is recoverable. Denying a tool returns an ordinary tool message that
the model reads and responds to — nothing is truncated mid-call and no exception
is raised into the agent loop. The worst case is a session that ends early with a
real answer, instead of one that ends late with a bill.

## Two ceilings, because cost isn't always knowable

```yaml
budget:
  max_usd_per_session: 5.00
  max_tokens_per_session: 2000000
```

Dollar cost is what you actually want to cap, but it's only knowable when the
route has pricing. Subscription routes (Claude Pro, Codex OAuth, Nous Portal)
report `included`, and unfamiliar models report nothing at all. A budget that
silently stopped enforcing on exactly those routes would be worse than no budget,
so tokens are tracked in parallel and the **stricter of the two ceilings governs**.

Set only `max_tokens_per_session` if you're entirely on subscription routes.

## Live status

```
/budget
```

```
[██████████████████·░░░░░] approaching the ceiling
$3.8120 of $5.00, 412,905 tokens (76% of budget) across 31 API calls
```


## `/spend` — which calls actually cost the money

```
/spend
```

```
tool               calls   result   re-sent  target
read_file              1      30k      570k  gateway/run.py
search_files          18       9k       86k  retry
read_file              1      30k       30k  docs/huge_late.md

Total re-sent: ~686k tokens
Estimator check: 69k estimated vs 40k measured context growth (1.73x).
```

Those two `read_file` rows are **the same size**. One cost 19x the other, purely
because it entered the context early and was re-transmitted on every later API
call. A report ranked by result size would have shown them as equal and given
you exactly the wrong advice.

A tool result is not paid for once:

```
true cost  =  size  x  number of API calls that came after it
```

So the useful lever is usually *when* you read something, not whether — read the
big things late, or in a subagent whose context you throw away. (That is what
`delegate_task(mode="explore")` is for.)

The estimator check compares the 4-chars-per-token approximation against measured
input-token growth, so the error is visible rather than assumed. If it drifts far
outside 0.5–2x, the report says so and the ranking should be read as an ordering
rather than as token counts.

Attribution runs whether or not a ceiling is configured — it is a diagnostic, not
an enforcement mechanism.

## Tuning

```yaml
budget:
  max_usd_per_session: 5.00
  warn_at: 0.75        # nudge
  no_fanout_at: 0.90   # deny delegation
  stop_at: 1.0         # deny all tools
```

## How it works

Entirely on the public plugin surface — no core changes:

| Hook | Role |
|---|---|
| `post_api_request` | real provider usage → USD via the same pricing tables the cost display uses |
| `pre_llm_call` | injects the budget line into the turn (the one hook whose return value is honoured) |
| `pre_tool_call` | returns a block directive at the upper tiers |
| `on_session_start` | resets the ledger |
| `on_session_end` | logs the final line |

Each tier announces itself **once**. Repeating the same warning every turn would
just burn the context you're trying to conserve.

## State

Spend persists to `$HERMES_HOME/budget/<session>.json`, because the ceiling is
advertised as **per session** and a session outlives the process that started it.
Without this, resuming a conversation in a fresh CLI process reset the budget to
zero — quietly turning a $5 cap into $5 *per resume*.

Stale files are pruned on session start (older than 30 days, or beyond 2000
files, oldest first). Housekeeping is bounded and can never fail a session.

## Limits

- **Cost is an estimate**, from the same pricing tables Hermes uses elsewhere. It
  is not a billing source of truth, and it lags the provider's own accounting.
- **Enforcement is per session**, not per account or per day. Two concurrent
  sessions each get their own ceiling.
- **Subagents bill to their parent's session**, which is what you want for a
  ceiling — but it means a large fan-out can cross a tier between checks. That's
  why `no_fanout` fires at 90% rather than at 100%.
- **The `warn` nudge cannot arrive inside the first turn of a session.** It is
  delivered by `pre_llm_call`, which runs *before* that turn's API calls are
  recorded, so there is nothing to warn about yet. The blocking tiers are
  unaffected — they run per tool call — so a runaway single turn is still
  stopped, it just gets stopped rather than warned.
