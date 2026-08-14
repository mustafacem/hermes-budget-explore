# Four features for Hermes

All on `main`, **uncommitted**. Nothing is enabled by default — bundled plugins
are opt-in, and `mode="explore"` is a parameter the model chooses.

Core changes total **two lines**, both config-root registrations. Everything else
is new files on the public plugin surface.

---

## 1. `delegate_task(mode="explore")` — read-only subagents

```python
delegate_task(
    goal="Find every place the gateway retries a failed send",
    context="Start from gateway/run.py. Report file:line for each.",
    mode="explore",
)
```

The child gets `read_file`, `search_files`, `web_search`, `web_extract`,
`skills_list`, `skill_view`, `vision_analyze` — nothing else. No writes, no
shell, no MCP.

**Why it's safe to let the model choose it:** narrowing is one-directional. The
request goes through the same intersection with the parent's toolsets as any
other, so a parent without file access still can't produce a child that has it.
`explore` can only subtract. That's why the codebase deliberately withholds a
general `toolsets` parameter but this one is fine.

**Why it's useful:** exploration is where an agent spends most of its steps and
nearly all of its context tokens, while needing the least judgement per step —
and it's the only phase safe to replay or discard. That makes it the natural
piece to hand to a cheaper or local model via `delegation.base_url`.

**Where the saving comes from, and the contract that protects it.** The child
explores in its own context, which is discarded — the parent pays for the summary,
not for the twenty files the child opened. That evaporates if the child answers
with a narrative, so an explore child gets a distillation contract by default:
`findings`, `evidence` (file:line), `open_questions`, `examined`. `evidence` is
separate from `findings` on purpose: a claim the parent cannot locate is worse
than no claim, because checking it means redoing the exploration just paid for.
An explicit `output_schema` always wins — it is a default, not a policy.

Verified that `mode=explore` reaches the child builder and that the child's
prompt opens with `OUTPUT CONTRACT (machine-validated):` carrying that shape;
Hermes' existing validator enforces it.

Files: `toolsets.py`, `tools/delegate_tool.py`, docs in
`website/docs/user-guide/features/delegation.md`.

---

## 2. `budget-guard` — a hard spend ceiling

```yaml
plugins:
  enabled: [budget-guard]
budget:
  max_usd_per_session: 5.00
```

| Spent | Behaviour |
|---|---|
| 75% | agent is told its remaining budget and asked to converge |
| 90% | `delegate_task` denied — one agent, no multiplication |
| 100% | all tools denied; the model answers from what it has |

Every tier is recoverable. Denying a tool returns an ordinary tool message the
model reads and responds to — nothing truncated mid-call, no exception into the
agent loop. Worst case is a session that ends early with a real answer instead of
late with a bill.

**Two ceilings, and the second one matters.** Dollar cost is only knowable when
the route has pricing; subscription routes (Claude Pro, Codex OAuth, Nous Portal)
report `included` and unknown models report nothing. A budget that silently
stopped enforcing on exactly those routes would be worse than none — so tokens
are tracked in parallel and the stricter ceiling governs.

---

## 3. `/spend` — which tool calls actually cost the money

Hermes attributes usage per model and per task, never per tool.

```
tool               calls   result   re-sent  target
read_file              1      30k      570k  gateway/run.py
search_files          18       9k       86k  retry
read_file              1      30k       30k  docs/huge_late.md
```

Those two `read_file` rows are **the same size**. One cost **19x** the other,
purely because it entered the context early and was re-transmitted on every
later API call.

```
true cost = size x number of API calls that came after it
```

Ranking by result size would show them as equal and give exactly the wrong
advice. The useful lever is usually *when* you read something, not whether —
which is what feature #1 is for.

The report includes an estimator check comparing the 4-chars-per-token
approximation against measured input-token growth, so the error is visible
rather than assumed. Runs whether or not a ceiling is configured.

---

## 4. `agent-watchdog` — notices when progress stops

| Pathology | Meaning |
|---|---|
| `repeat` | same call, same args, **same result** |
| `cycle` | A-B-A-B alternation |
| `churn` | many distinct calls, none producing anything new |

**The design decision that matters:** `repeat` keys on the *result*, not the
call. If the output changed, that's progress however many times the command ran —
so `git status` between edits is left alone. Verified: 10 identical `git status`
calls with changing output never fire; 4 with identical output do.

Remaining honest false positive is deliberate polling, handled by exempting
waiting tools (`process`, `cronjob`, `kanban_heartbeat`) and loose thresholds.

Blocks **once** per pattern, then re-arms — a genuinely stuck model gets told, a
model with a good reason continues.

---

## 5. `pinboard` — findings that survive compaction

Long sessions compress away the turn where the agent worked out the root cause,
and it re-derives it — slowly, and sometimes differently.

```python
pin(text="Root cause: retry backoff resets in gateway/run.py:412")
```

**Re-injection, not protection.** Teaching the compressor that some messages are
special means changing delicate selection logic, and still loses everything on a
resumed session. Instead a pin never lives in compressible history at all — it's
stored outside the transcript and added fresh each turn. Persists to
`$HERMES_HOME/pinboard/<session>.json`.

Pins cost tokens every turn, so caps are load-bearing: 20 pins, 240 chars each,
2400 total. Conclusions, not notes.

---

## Tests

| suite | result |
|---|---|
| `test_delegate.py` (incl. new explore tests) | **75 passed** |
| `test_budget_guard_plugin.py` | **47 passed** |
| `test_agent_watchdog_plugin.py` | **21 passed** |
| `test_pinboard_plugin.py` | **26 passed** |
| **total** | **169 passed** |

**Regressions verified against a clean worktree at `HEAD`, not assumed:**

- `tests/tools/` — `F=41` on both trees, identical skips, `dots` differing by
  exactly **+7** (my new tests). The hang at 60% happens on stock `HEAD` too.
- `tests/plugins/` + `tests/tools/test_delegate.py` — clean `HEAD`: **20 failed,
  1402 passed**. This tree: **20 failed, 1486 passed**. Identical failures
  (`video_gen` / `hindsight` / `teams`, all unconfigured credentials), and
  exactly **+84 passing** — my new tests.

Zero new failures.

## Bugs caught by verifying rather than assuming

1. **`browser` toolset contains `web_search`** — my first explore deny-list would
   have silently stripped search from every exploring child.
2. **MCP toolsets were re-inherited** by narrowed children, defeating the point.
3. **`normalize_usage` reads attributes, not dict keys** — a dict usage payload
   counted as 0, so the budget would never fire.
4. **`api_mode` was never passed** — Anthropic usage (`input_tokens`) fell through
   the OpenAI branch (`prompt_tokens`) and counted 0.
5. **Attribution was gated on the budget being enabled** — `/spend` silently
   recorded nothing without a ceiling set.

3–5 are the same failure class: the feature keeps working, reports zero, and
nothing ever trips. All five now have regression tests.

## Live tests

Run against a real Hermes process in an isolated `HERMES_HOME`, with a scripted
OpenAI-compatible stub as the model (the model is the only stubbed part —
plugin discovery, hook dispatch and the tool loop are the genuine article; a real
model cannot be relied on to loop on cue or exhaust a budget on demand).

**`agent-watchdog`** — blocked a real repeat loop:
```
agent-watchdog: repeat on read_file (identical call returned an identical result 3 times)
agent.tool_executor: Tool read_file returned error: {"error": "Blocked once by agent-watchdog: ..."}
```

**`budget-guard`** — enforced the ceiling and the session *landed*:
```
Blocked by budget-guard: 1,200,000 of 1,000,000 tokens (120% of budget) — 3 API calls
Blocked by budget-guard: 1,600,000 of 1,000,000 tokens (160% of budget) — 4 API calls
Turn ended: reason=text_response(finish_reason=stop)
```
`exit=0`, final text answer — not a crash.

**`pinboard`** — full path: model discovered `pin` via `tool_search`, invoked it
through `tool_call`, and the finding persisted:
```json
[{"id": "root-cause-retry-resets-", "text": "Root cause: retry resets in run.py:412"}]
```
Injection was verified separately across a session resume (`pin_in_context=True`).

**`mode="explore"`** — verified against a REAL `AIAgent` parent (no mocks):

| mode | tools resolved | mutating leaked |
|---|---|---|
| `default` | 14 | `execute_code, patch, process, skill_manage, terminal, write_file` |
| `explore` | 5 | none — `read_file, search_files, skill_view, skills_list, vision_analyze` |

A full end-to-end session could not observe the child, because top-level
`delegate_task` always detaches (its `background` parameter is documented as
deprecated and ignored) and the CLI exits before the child's first API call.

**All three watchdog pathologies fired live**, each isolated by setting the other
two thresholds to 99 so only the rule under test could trigger:

```
agent-watchdog: repeat on read_file (identical call returned an identical result 3 times)
agent-watchdog: cycle  on read_file (a 2-call cycle has repeated 3 times)
agent-watchdog: churn  on search_files (5 consecutive calls produced no result this session had not already seen)
```

**Compaction.** A single session pinned a finding, then read a 128KB file until
`context compression started` appeared in the log. The pin reached the model on
every call afterwards. Note the compressor then logged *"made no progress"* —
the stub reports a constant token count, so there was nothing for it to shrink.
So this shows compaction running with a pin active, not compaction actually
shrinking history. The claim holds structurally regardless: a pin never enters
the transcript, so rewriting history cannot touch it.

### Four bugs only the live tests could find

1. **The `pin` handler had the wrong signature.** The registry dispatches tools
   as `handler(args_dict, **context)` — one positional dict, not keywords. Every
   unit test passed (they called it with keywords) while a live session died with
   `'dict' object has no attribute 'strip'`.
2. **Slash commands get no session context.** They are invoked as `handler(arg)`,
   so `/budget`, `/spend` and `/pins` read an empty default session and reported
   a healthy session no matter what had been spent. Hooks do see the real id, so
   all three now fall back to the last session the hooks saw.
3. **A plugin toolset passed via `-t` was rejected as unknown.** The CLI validates
   toolset names before plugins register theirs. `pinboard` is now declared
   statically in `toolsets.py`, so it validates whether or not the plugin loaded.
4. **The budget ledger was per-process.** A two-turn live test showed turn 2 in a
   fresh process reporting `400,000 tokens across 1 API calls` — it had forgotten
   turn 1 entirely. For a ceiling advertised as *per session*, that quietly turned
   a $5 cap into $5 *per resume*. The ledger now persists to
   `$HERMES_HOME/budget/<session>.json`, which also fixed the `warn` notice: it
   is emitted by `pre_llm_call`, which fires at the *start* of a turn, so it can
   only ever fire once spend from a previous turn is on the books.

All three have regression tests. A fourth, smaller fix: `pinboard` was writing an
empty JSON file on every session start.

### Not a bug, worth knowing

`pin` is **deferred behind `tool_search`**, like every non-core tool. A model has
to discover it before calling it. That is by design; the first live attempt failed
only because the stub emitted a `pin` call the model had never been offered.

## Improvements after the first live pass

- **Ledger persistence** (see bug 4). The cap is now genuinely per session.
- **State-file pruning.** Making the ledger persistent introduced a leak: one
  small JSON per session, forever. Both `budget-guard` and `pinboard` now prune
  on session start — older than 30 days, or beyond 2000 files, oldest first.
  Verified live: 5 planted stale files in each directory were gone after one
  session. Bounded work, and never allowed to fail a session start.
- **`watchdog.exempt_tools` is configurable.** The exempt list was hardcoded to
  the built-in waiters, so anyone running a custom or MCP polling tool got a
  false positive with no way to switch it off. Config *extends* the defaults
  rather than replacing them — there is a test for that specifically, because
  replacing them silently would re-break `process`.
- **Concurrency covered.** Subagents bill against their parent's session from
  worker threads, so `post_api_request` can fire concurrently on one key. Tests
  now drive 8 threads x 50 records and assert no lost updates, and that a
  contested `mark_announced` yields exactly one winner (otherwise the agent gets
  told the same thing twice).

## The hybrid run

Finally executed with a real API model and a real local model, no stubs on either
side:

* **Parent** — `nvidia/nemotron-3-super-120b-a12b:free` on OpenRouter
* **Child** — `Qwen2.5-1.5B-Instruct`, local, via `delegation.base_url`
* Routed by `mode="explore"`, so the child was read-only

It worked mechanically, end to end. The local model's own log:

```
in=5062 out=83  tools=['read_file']     <- local model chose the tool
in=5316 out=138 tools=[]                <- read the real file
in=5040 out=131 tools=[]                <- filled in the contract
```

and it returned the distillation shape, correctly formed:

```json
{"findings": ["MAX_RETRIES is defined in the following line: ... MAX_RETRIES = 3"],
 "evidence": ["...proj/gateway.py:10"], "open_questions": [], "examined": []}
```

**And the answer is wrong.** Ground truth is `MAX_RETRIES = 5` on line 1. The
child reported 3, on line 10, and invented a comment that does not exist in the
file. Line 10 is `def _backoff(attempt):`.

So the honest result is split:

* The **plumbing works**: API parent, local child, real tool calls, real file
  read, contract honoured, 356s round trip.
* The **local model is not good enough** for the exploration role at this size.

The `evidence` field is what exposed it. Because the child had to cite
`gateway.py:10`, the claim was checkable in one look. Without it, "MAX_RETRIES is
3" reads as plausible and is unfalsifiable without redoing the exploration — which
is the entire reason that field exists.

### Re-run at 7B: the model size was the binding constraint

The 1.5B fits only because it is tiny; a 7B needs ~15GB in fp16 or ~4.9GB in
4-bit, and this card has ~3.6GB free once the desktop and editor have taken their
share. Layer streaming — hold the weights off-GPU and move one layer at a time
through it — is the only way it runs at all. Both variants were measured:

| backend | weights live in | s/token | 7B runs? |
|---|---|---|---|
| fp16 resident | GPU | fast | no — needs 15GB |
| 4-bit resident | GPU | fast | no — needs ~4.9GB |
| `accelerate.cpu_offload` | host RAM | **1.7** | yes |
| `airllm` 3.1.0 | disk (15GB of shards) | **9.2** | yes |

AirLLM is 5.4x slower here because it re-reads each layer from disk per forward
pass. That is the trade it exists to make — it buys models larger than RAM — and
this box has 62GB, so the RAM-based path dominates for a 7B. Same technique,
different tier of the memory hierarchy.

**Both 7B backends corrected the 1.5B's error.** Ground truth is `MAX_RETRIES = 5`
on line 1:

| child | reported | correct |
|---|---|---|
| Qwen2.5-1.5B (CPU) | `3`, line 10, invented a comment | no |
| Qwen2.5-7B (RAM-streamed) | `5` | yes |
| Qwen2.5-7B (AirLLM, disk-streamed) | `5`, line 1, evidence `1|MAX_RETRIES = 5` | yes |

```json
{"findings": ["MAX_RETRIES is defined as 5",
              "MAX_RETRIES is defined on line 1"],
 "evidence": ["1|MAX_RETRIES = 5"], "open_questions": [], "examined": []}
```

So the earlier verdict stands corrected in the direction it was guessed: capability,
not the design, was what failed at 1.5B. That guess should not have been written
down before it was run — it is recorded here because it was subsequently tested.

**Neither 7B run terminated cleanly, for reasons above the model.** The first was
my harness: `max_new_tokens=140` truncated the contract mid-JSON. With that raised,
the AirLLM run emitted a stray `skill_view` call alongside a correct contract; the
skill document it pulled back pushed the next prompt to AirLLM's `max_seq_len`
(8192), which truncated it, and the model then generated unrelated text to the
token cap. Useful output arrives on call 2; the loop does not converge after it.

At 9.2s/token a full run is ~1 hour, so this was stopped once the answer was in
rather than left to grind.

## Not done

- **No live cost validation.** The mechanisms work; proving the *savings* needs a
  real run against real billing.
- **Slash commands are interactive-only** — they do not execute under `chat -q`,
  so `/budget`, `/spend` and `/pins` were verified by unit test rather than live.
- **The `warn` tier cannot fire inside the first turn of a session.** It is
  delivered by `pre_llm_call`, which runs before that turn's API calls are
  recorded. Blocking tiers are unaffected — they run per tool call, so a runaway
  single turn is still stopped.
- **`explore` was not observed end-to-end in a session**, only against a real
  parent agent (see above).
- Bundled plugins need explicit opt-in via `plugins.enabled`.
