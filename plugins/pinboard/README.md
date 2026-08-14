# pinboard

Findings that survive context compaction.

Long sessions compress. Middle turns get summarised or dropped — including the
turn where the agent finally worked out *the retry lives in `gateway/run.py:412`*.
The agent then re-derives it: slow, expensive, and sometimes it re-derives it
differently.

## Enable

```yaml
# ~/.hermes/config.yaml
plugins:
  enabled: [pinboard]
```

## Use

The agent calls the `pin` tool:

```python
pin(text="Root cause: retry backoff resets in gateway/run.py:412")
pin(text="Auth uses PyJWT, not python-jose")
```

Every subsequent turn begins with:

```
Pinned findings from earlier in this session (these survive context compaction;
treat them as established):
- [root-cause-retry-backoff] Root cause: retry backoff resets in gateway/run.py:412
- [auth-uses-pyjwt-not-pyth] Auth uses PyJWT, not python-jose
```

You can inspect the board yourself with `/pins`, or `/pins clear`.

## Re-injection, not protection

The obvious implementation is to teach the compressor that some messages are
special. That means changing delicate, well-tested selection logic — and it still
loses everything when a session is resumed in a fresh process.

Re-injection sidesteps both. **A pin never lives in compressible history at all.**
It's stored outside the transcript and added fresh to every turn, so compaction
has nothing to eat, and a resumed session reloads the board from disk.

Pins persist to `$HERMES_HOME/pinboard/<session>.json`.

## Why the board is small

Pins are re-sent **every turn**. An unbounded board is a permanent, growing tax on
the very context it exists to protect — so the caps are load-bearing:

| Cap | Value |
|---|---|
| Pins per session | 20 |
| Characters per pin | 240 |
| Characters total | 2400 |

Oldest evicted first when full. Re-pinning identical text is a no-op, so an agent
that pins the same conclusion twice doesn't consume two slots.

This is for **conclusions, not notes** — the handful of facts you'd hate to
re-derive. Anything longer belongs in a file.

## State

`$HERMES_HOME/pinboard/<session>.json`. Stale boards are pruned on session start
(older than 30 days, or beyond 2000 files, oldest first).

## Limits

- **Per session.** Cross-session knowledge is what the `memory` tool and
  `MEMORY.md` are for.
- **The agent decides what to pin.** A model that never pins gets no benefit; the
  tool description pushes toward root causes, file:line references, confirmed
  constraints and settled decisions.
- **Eviction is by age, not importance.** There's no ranking — a stale pin can
  push out a fresh one if the board is full.
