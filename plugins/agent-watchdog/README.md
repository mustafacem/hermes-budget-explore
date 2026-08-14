# agent-watchdog

Notices when an agent has stopped making progress.

`budget-guard` stops a session spending too much. This stops one spending for
**no reason**: the same call four times over, an A-B-A-B loop, a long run of
searches turning up nothing new.

Hermes already spots repeated *file* reads and repeated patch failures. This
generalises that to every tool.

## Enable

```yaml
# ~/.hermes/config.yaml
plugins:
  enabled: [agent-watchdog]
```

## What it detects

| Pathology | Meaning |
|---|---|
| `repeat` | the same call, same arguments, **same result** — nothing changed and nothing will |
| `cycle` | A-B-A-B (or A-B-C-A-B-C) alternation without progress |
| `churn` | many distinct calls, none producing a result the session hasn't already seen |

`churn` is the one hardest to notice by eye, because every individual call looks
new. It's the signature of searching in the wrong place.

## It nudges, it doesn't kill

The first time a pattern is detected the call is blocked **once**, with an
explanation the model can act on, and then the pattern re-arms. A model that was
genuinely stuck gets told. A model with a good reason to repeat itself carries on
after a single interruption. Nothing raises into the agent loop.

## Not interrupting real work

The obvious way to build this wrong is to key on the *call*. Then `git status`
between edits looks like a loop, and the watchdog fights the agent.

So `repeat` keys on the **result**. If the output changed, that's progress — no
matter how many times the same command ran. Only a call returning what it already
returned is evidence of being stuck.

That leaves one honest false positive: deliberate polling, where identical results
are exactly what you expect while waiting. Handled two ways — tools whose job is
to wait (`process`, `cronjob`, `kanban_heartbeat`) are exempt, and thresholds are
deliberately loose. A watchdog that interrupts real work is worse than one that
fires late.

## Tuning

```yaml
watchdog:
  enabled: true
  repeat_threshold: 4     # identical call + identical result
  cycle_threshold: 3      # times an A-B(-C) cycle goes round
  churn_threshold: 12     # consecutive calls yielding nothing new
  exempt_tools: [my_poll] # added to the built-in waiters, not replacing them
```

`exempt_tools` matters if you run a custom or MCP tool whose job is to wait.
Identical results are the *expected* shape there, and without an escape hatch
you would get a false positive with no way to switch it off.

## How it works

| Hook | Role |
|---|---|
| `post_tool_call` | folds each completed call into the history |
| `pre_tool_call` | blocks once when a pattern is detected |
| `on_session_start` | clears the history |

Per-task state is bounded (256 tasks, 2048 call signatures, 4096 result hashes)
so a long-lived gateway can't grow it without limit.

## Limits

- **Period-4 and longer cycles aren't chased.** By the time one has run enough
  times to be certain, `repeat` and `churn` have both fired anyway.
- **State is per-session, in memory.** A restart forgets the history.
- **A determined model can ignore the advice.** The block is one call, not a cap
  — `budget-guard` is the backstop for that.
