"""Tests for the budget-guard plugin.

The property that matters is that the guard degrades a session gracefully
instead of breaking it: every tier must be reachable, must escalate in the
right order, and must never raise into the agent loop. A budget guard that
throws is worse than no budget guard, because it converts an overspend into a
crash.

Covers:
  * ledger arithmetic and tier thresholds, including the token fallback used
    when a route reports no price (subscription plans, unknown models)
  * the pre_tool_call block directives, and that fan-out is denied a tier
    earlier than everything else
  * pre_llm_call announcing each tier exactly once
  * dormancy when no ceiling is configured
  * bundled discovery through PluginManager
"""

import importlib.util
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]


def _load_plugin():
    """Import the hyphenated plugin package directly from the repo path."""
    pkg_dir = REPO / "plugins" / "budget-guard"
    spec = importlib.util.spec_from_file_location(
        "budget_guard_under_test",
        pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["budget_guard_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bg(tmp_path, monkeypatch):
    # The ledger persists to $HERMES_HOME/budget; point it at a tmpdir so tests
    # neither litter a real home nor inherit spend from each other.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mod = _load_plugin()
    from budget_guard_under_test.ledger import Ledger
    mod._LEDGER = Ledger(root=tmp_path / "budget")
    yield mod
    sys.modules.pop("budget_guard_under_test", None)


# --------------------------------------------------------------------------
# ledger + tiers
# --------------------------------------------------------------------------


class TestTiers:
    def test_thresholds_escalate_in_order(self, bg):
        from budget_guard_under_test.ledger import (
            BudgetConfig, SessionSpend, TIER_NO_FANOUT, TIER_OK, TIER_STOP,
            TIER_WARN, tier_for,
        )
        cfg = BudgetConfig(max_usd=10.0)
        cases = [(1.0, TIER_OK), (7.6, TIER_WARN), (9.1, TIER_NO_FANOUT),
                 (10.0, TIER_STOP), (12.0, TIER_STOP)]
        for spent, expected in cases:
            s = SessionSpend(cost_usd=spent, has_cost_data=True)
            assert tier_for(s, cfg) == expected, f"${spent}"

    def test_dormant_without_a_ceiling(self, bg):
        from budget_guard_under_test.ledger import (
            BudgetConfig, SessionSpend, TIER_OK, tier_for,
        )
        cfg = BudgetConfig()
        assert not cfg.enabled
        huge = SessionSpend(cost_usd=999.0, tokens=10**9, has_cost_data=True)
        assert tier_for(huge, cfg) == TIER_OK

    def test_token_ceiling_governs_when_price_is_unknown(self, bg):
        """Subscription and unknown routes report no dollar cost. The guard must
        still bite, otherwise it silently stops working on exactly those plans."""
        from budget_guard_under_test.ledger import (
            BudgetConfig, SessionSpend, TIER_STOP, tier_for,
        )
        cfg = BudgetConfig(max_usd=5.0, max_tokens=1000)
        spend = SessionSpend(cost_usd=0.0, tokens=1200, has_cost_data=False)
        assert tier_for(spend, cfg) == TIER_STOP

    def test_stricter_of_the_two_ceilings_wins(self, bg):
        from budget_guard_under_test.ledger import (
            BudgetConfig, SessionSpend, TIER_STOP, tier_for,
        )
        cfg = BudgetConfig(max_usd=100.0, max_tokens=1000)
        # Well under the dollar cap, far over the token cap.
        spend = SessionSpend(cost_usd=0.02, tokens=5000, has_cost_data=True)
        assert tier_for(spend, cfg) == TIER_STOP

    def test_unpriced_calls_still_count_tokens(self, bg):
        from budget_guard_under_test.ledger import SessionSpend
        s = SessionSpend()
        s.add(None, 500)          # unpriced route
        s.add(0.25, 500)          # priced route
        assert s.tokens == 1000
        assert s.cost_usd == 0.25
        assert s.has_cost_data is True
        assert s.api_calls == 2

    def test_ledger_isolates_sessions(self, bg):
        from budget_guard_under_test.ledger import Ledger
        led = Ledger()
        led.record("a", 1.0, 10)
        led.record("b", 5.0, 50)
        assert led.get("a").cost_usd == 1.0
        assert led.get("b").cost_usd == 5.0
        led.reset("a")
        assert led.get("a").cost_usd == 0.0
        assert led.get("b").cost_usd == 5.0


# --------------------------------------------------------------------------
# enforcement
# --------------------------------------------------------------------------


class TestEnforcement:
    def _with_budget(self, bg, monkeypatch, **kw):
        from budget_guard_under_test.ledger import BudgetConfig
        cfg = BudgetConfig(**kw)
        monkeypatch.setattr(bg, "_config", lambda: cfg)
        return cfg

    def test_no_blocking_below_the_warn_line(self, bg, monkeypatch):
        self._with_budget(bg, monkeypatch, max_usd=10.0)
        bg._LEDGER.reset("s")
        bg._LEDGER.record("s", 1.0, 100)
        assert bg.on_pre_tool_call(tool_name="terminal", session_id="s") is None

    def test_fanout_denied_before_everything_else(self, bg, monkeypatch):
        """At 90% the agent may keep working but must not multiply itself."""
        self._with_budget(bg, monkeypatch, max_usd=10.0)
        bg._LEDGER.reset("s")
        bg._LEDGER.record("s", 9.2, 100)
        blocked = bg.on_pre_tool_call(tool_name="delegate_task", session_id="s")
        assert blocked and blocked["action"] == "block"
        assert "budget" in blocked["message"].lower()
        # ordinary work continues at this tier
        assert bg.on_pre_tool_call(tool_name="read_file", session_id="s") is None

    def test_everything_denied_at_the_ceiling(self, bg, monkeypatch):
        self._with_budget(bg, monkeypatch, max_usd=10.0)
        bg._LEDGER.reset("s")
        bg._LEDGER.record("s", 10.5, 100)
        for tool in ("read_file", "terminal", "delegate_task", "web_search"):
            out = bg.on_pre_tool_call(tool_name=tool, session_id="s")
            assert out and out["action"] == "block", tool

    def test_block_message_tells_the_model_what_to_do(self, bg, monkeypatch):
        """The directive is read by the model, so it has to be actionable."""
        self._with_budget(bg, monkeypatch, max_usd=1.0)
        bg._LEDGER.reset("s")
        bg._LEDGER.record("s", 2.0, 100)
        msg = bg.on_pre_tool_call(tool_name="read_file", session_id="s")["message"]
        assert "reply to the user" in msg.lower()

    def test_each_tier_announced_exactly_once(self, bg, monkeypatch):
        self._with_budget(bg, monkeypatch, max_usd=10.0)
        bg._LEDGER.reset("s")
        bg._LEDGER.record("s", 7.7, 100)
        first = bg.on_pre_llm_call(session_id="s")
        assert first and "budget" in first["context"].lower()
        assert bg.on_pre_llm_call(session_id="s") is None, "repeated the same warning"
        # escalating to a new tier speaks again
        bg._LEDGER.record("s", 2.0, 100)
        assert bg.on_pre_llm_call(session_id="s") is not None

    def test_dormant_guard_never_blocks_or_speaks(self, bg, monkeypatch):
        self._with_budget(bg, monkeypatch)  # no ceiling
        bg._LEDGER.reset("s")
        bg._LEDGER.record("s", 999.0, 10**9)
        assert bg.on_pre_tool_call(tool_name="terminal", session_id="s") is None
        assert bg.on_pre_llm_call(session_id="s") is None

    def test_hooks_swallow_bad_input(self, bg, monkeypatch):
        """A guard that raises turns an overspend into a crash."""
        self._with_budget(bg, monkeypatch, max_usd=1.0)
        bg.on_post_api_request(session_id="s", usage=object(), model="nope",
                               provider="nope", base_url="")
        bg.on_post_api_request()                      # no kwargs at all
        bg.on_pre_tool_call()          # no kwargs; must not raise
        bg.on_session_start(session_id="s")
        bg.on_session_end(session_id="s")


class TestSlashCommand:
    def test_reports_status_and_suggests_config_when_off(self, bg, monkeypatch):
        from budget_guard_under_test.ledger import BudgetConfig
        monkeypatch.setattr(bg, "_config", lambda: BudgetConfig())
        out = bg._budget_command(session_id="s")
        assert "max_usd_per_session" in out

    def test_renders_a_progress_bar_when_on(self, bg, monkeypatch):
        from budget_guard_under_test.ledger import BudgetConfig
        monkeypatch.setattr(bg, "_config", lambda: BudgetConfig(max_usd=10.0))
        bg._LEDGER.reset("s")
        bg._LEDGER.record("s", 5.0, 1000)
        out = bg._budget_command(session_id="s")
        assert "█" in out and "$5.0000 of $10.00" in out


class TestDiscovery:
    def test_registers_only_valid_hooks(self, bg):
        from hermes_cli.plugins import VALID_HOOKS

        seen = []

        class Ctx:
            def register_hook(self, name, fn):
                seen.append(name)

            def register_command(self, *a, **k):
                pass

        bg.register(Ctx())
        assert seen, "no hooks registered"
        for name in seen:
            assert name in VALID_HOOKS, f"{name} is not a valid hook"
        assert "post_api_request" in seen, "cost tracking requires post_api_request"

    def test_bundled_plugin_is_discovered(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        assert "budget-guard" in mgr._plugins
        assert mgr._plugins["budget-guard"].manifest.source == "bundled"

    def test_budget_is_an_accepted_config_root(self):
        """Without this, `budget:` in config.yaml is reported as unknown."""
        from hermes_cli.config import _KNOWN_ROOT_KEYS
        assert "budget" in _KNOWN_ROOT_KEYS


class TestUsageParsing:
    """Regression guards for two bugs that made the guard silently measure zero.

    Both are the same class of failure: the ledger kept working, reported 0, and
    nothing ever tripped. A spend limiter that reads zero is worse than none,
    because it looks installed.
    """

    def test_dict_usage_is_counted(self, bg):
        """normalize_usage reads attributes; a dict payload read as all-zero."""
        cost, tokens = bg._cost_of(
            {"prompt_tokens": 50000, "completion_tokens": 1500},
            "gpt-4o", "openai", "", "",
        )
        assert tokens == 51500
        assert cost is not None and cost > 0

    def test_anthropic_shape_needs_api_mode(self, bg):
        """Anthropic reports input_tokens/output_tokens. Without api_mode the
        OpenAI branch looks for prompt_tokens and finds nothing."""
        _, tokens = bg._cost_of(
            {"input_tokens": 120000, "output_tokens": 2000},
            "claude-sonnet-4", "anthropic", "", "anthropic_messages",
        )
        assert tokens == 122000

    def test_unpriced_route_still_reports_tokens(self, bg):
        """Subscription/unknown routes must fall back to the token ceiling."""
        cost, tokens = bg._cost_of(
            {"prompt_tokens": 10000, "completion_tokens": 500},
            "some-model-nobody-prices", "custom", "", "",
        )
        assert cost is None
        assert tokens == 10500

    def test_garbage_usage_does_not_raise(self, bg):
        assert bg._cost_of(object(), "m", "p", "", "") == (None, 0)


class TestAttribution:
    """/spend — which tool calls actually cost the money.

    The claim being tested is that *position* dominates *size*: a result that
    enters the context early is re-transmitted on every later API call, so it
    costs far more than an identical result added at the end. A report that
    ranked by size alone would give the opposite advice.
    """

    def _session(self, bg, key="s"):
        bg._ATTRIB.reset(key)
        return key

    def test_early_result_costs_far_more_than_an_identical_late_one(self, bg):
        s = self._session(bg)
        bg.on_post_tool_call(tool_name="read_file", args={"path": "early.py"},
                             result="x" * 40000, session_id=s)
        for _ in range(10):
            bg.on_post_api_request(session_id=s, provider="openai", model="gpt-4o",
                                   usage={"prompt_tokens": 50000, "completion_tokens": 100})
        bg.on_post_tool_call(tool_name="read_file", args={"path": "late.py"},
                             result="x" * 40000, session_id=s)
        bg.on_post_api_request(session_id=s, provider="openai", model="gpt-4o",
                               usage={"prompt_tokens": 60000, "completion_tokens": 100})

        ranked, _ = bg._ATTRIB.rank(s)
        by_target = {c.detail: c for c in ranked}
        early, late = by_target["early.py"], by_target["late.py"]
        assert early.first_tokens == late.first_tokens, "same size, by construction"
        assert early.resent_tokens > late.resent_tokens * 5
        assert ranked[0].detail == "early.py", "ranking must surface position"

    def test_repeated_calls_on_one_target_group_together(self, bg):
        s = self._session(bg)
        for _ in range(12):
            bg.on_post_tool_call(tool_name="read_file", args={"path": "same.py"},
                                 result="y" * 1000, session_id=s)
            bg.on_post_api_request(session_id=s, provider="openai", model="gpt-4o",
                                   usage={"prompt_tokens": 1000, "completion_tokens": 10})
        ranked, _ = bg._ATTRIB.rank(s)
        rows = [c for c in ranked if c.detail == "same.py"]
        assert len(rows) == 1, "twelve reads of one file should be one line"
        assert rows[0].calls == 12

    def test_attribution_works_without_a_configured_ceiling(self, bg, monkeypatch):
        """Regression: /spend is a diagnostic. Gating it on the budget being
        enabled made it silently record nothing."""
        from budget_guard_under_test.ledger import BudgetConfig
        monkeypatch.setattr(bg, "_config", lambda: BudgetConfig())  # no ceiling
        s = self._session(bg)
        bg.on_post_tool_call(tool_name="read_file", args={"path": "a.py"},
                             result="z" * 8000, session_id=s)
        bg.on_post_api_request(session_id=s, provider="openai", model="gpt-4o",
                               usage={"prompt_tokens": 9000, "completion_tokens": 50})
        bg.on_post_api_request(session_id=s, provider="openai", model="gpt-4o",
                               usage={"prompt_tokens": 9500, "completion_tokens": 50})
        ranked, total = bg._ATTRIB.rank(s)
        assert ranked and total > 0, "attribution must not depend on a ceiling"

    def test_calibration_compares_estimate_to_measured_growth(self, bg):
        s = self._session(bg)
        bg.on_post_tool_call(tool_name="read_file", args={"path": "a.py"},
                             result="q" * 4000, session_id=s)
        for pt in (10000, 11000, 12000):
            bg.on_post_api_request(session_id=s, provider="openai", model="gpt-4o",
                                   usage={"prompt_tokens": pt, "completion_tokens": 10})
        calib = bg._ATTRIB.calibration(s)
        assert calib is not None
        estimated, measured = calib
        assert estimated == 1000       # 4000 chars / 4
        assert measured == 2000        # 1000 + 1000 of positive delta

    def test_calibration_needs_two_calls(self, bg):
        s = self._session(bg)
        bg.on_post_api_request(session_id=s, provider="openai", model="gpt-4o",
                               usage={"prompt_tokens": 100, "completion_tokens": 1})
        assert bg._ATTRIB.calibration(s) is None

    def test_report_renders_and_warns_on_a_bad_estimate(self, bg):
        from budget_guard_under_test.attribution import format_report, ToolCost
        ranked = [ToolCost("read_file", "big.py", 1, 30000, 570000, 19)]
        out = format_report(ranked, 570000, (90000, 1000))   # 90x off
        assert "big.py" in out and "570k" in out
        assert "ordering" in out, "a wildly-off estimate must be flagged"

    def test_sessions_do_not_bleed(self, bg):
        a, b = self._session(bg, "a"), self._session(bg, "b")
        bg.on_post_tool_call(tool_name="read_file", args={"path": "a.py"},
                             result="x" * 4000, session_id=a)
        bg.on_post_api_request(session_id=a, provider="openai", model="gpt-4o",
                               usage={"prompt_tokens": 100, "completion_tokens": 1})
        ranked_b, total_b = bg._ATTRIB.rank(b)
        assert not ranked_b and total_b == 0

    def test_detail_label_prefers_the_meaningful_argument(self, bg):
        assert bg._detail_of("read_file", {"path": "x.py", "offset": 1}) == "x.py"
        assert bg._detail_of("search_files", {"pattern": "retry"}) == "retry"
        assert bg._detail_of("terminal", {"command": "pytest -q"}) == "pytest -q"
        assert bg._detail_of("weird", {"nothing": "useful"}) == ""
        assert bg._detail_of("weird", "not-a-dict") == ""

    def test_hooks_never_raise_on_junk(self, bg):
        s = self._session(bg)
        bg.on_post_tool_call(tool_name="x", args=object(), result=object(), session_id=s)
        bg.on_post_tool_call()
        bg._spend_command(session_id=s)
        bg._spend_command("notanumber", session_id=s)


class TestSessionKeyConsistency:
    """The ledger is written by one hook and read by another.

    If those hooks resolved the session differently, spend would be filed under
    one key and looked up under another — the ceiling would silently never fire,
    with every unit test still green. This pins the resolution down.
    """

    def test_spend_recorded_by_task_id_is_enforced_by_task_id(self, bg, monkeypatch):
        from budget_guard_under_test.ledger import BudgetConfig
        monkeypatch.setattr(bg, "_config", lambda: BudgetConfig(max_usd=1.0))
        bg._LEDGER.reset("t-only")
        # Recorded with task_id and no session_id...
        bg.on_post_api_request(task_id="t-only", provider="openai", model="gpt-4o",
                               usage={"prompt_tokens": 900000, "completion_tokens": 9000})
        # ...must be visible to the enforcement hook given the same task_id.
        out = bg.on_pre_tool_call(tool_name="read_file", task_id="t-only")
        assert out and out["action"] == "block", "ledger key diverged between hooks"

    def test_session_id_wins_when_both_are_present(self, bg):
        assert bg._key("sess", "task") == "sess"
        assert bg._key("", "task") == "task"
        assert bg._key("", "") == "_default"

    def test_attribution_uses_the_same_key(self, bg):
        bg._ATTRIB.reset("t2")
        bg.on_post_tool_call(tool_name="read_file", args={"path": "a.py"},
                             result="x" * 4000, task_id="t2")
        bg.on_post_api_request(task_id="t2", provider="openai", model="gpt-4o",
                               usage={"prompt_tokens": 5000, "completion_tokens": 10})
        bg.on_post_api_request(task_id="t2", provider="openai", model="gpt-4o",
                               usage={"prompt_tokens": 6000, "completion_tokens": 10})
        ranked, total = bg._ATTRIB.rank("t2")
        assert ranked and total > 0


class TestSlashCommandSession:
    """Slash commands arrive as ``handler(arg)`` with no session context.

    Without a fallback, /budget and /spend read an empty default ledger and
    report a healthy session no matter how much was spent.
    """

    def test_budget_falls_back_to_the_last_seen_session(self, bg, monkeypatch):
        from budget_guard_under_test.ledger import BudgetConfig
        monkeypatch.setattr(bg, "_config", lambda: BudgetConfig(max_usd=10.0))
        bg._LEDGER.reset("live-session")
        bg.on_post_api_request(session_id="live-session", provider="openai",
                               model="gpt-4o",
                               usage={"prompt_tokens": 500000, "completion_tokens": 500})
        out = bg._budget_command()          # no session context, as in real use
        assert "API calls" in out
        assert "0 API calls" not in out, "command read an empty default ledger"

    def test_spend_falls_back_too(self, bg):
        bg._ATTRIB.reset("live2")
        bg.on_post_tool_call(tool_name="read_file", args={"path": "a.py"},
                             result="x" * 8000, session_id="live2")
        bg.on_post_api_request(session_id="live2", provider="openai", model="gpt-4o",
                               usage={"prompt_tokens": 9000, "completion_tokens": 10})
        bg.on_post_api_request(session_id="live2", provider="openai", model="gpt-4o",
                               usage={"prompt_tokens": 9500, "completion_tokens": 10})
        assert "a.py" in bg._spend_command()

    def test_explicit_session_still_wins(self, bg, monkeypatch):
        from budget_guard_under_test.ledger import BudgetConfig
        monkeypatch.setattr(bg, "_config", lambda: BudgetConfig(max_usd=10.0))
        bg._LEDGER.reset("X")
        bg.on_post_api_request(session_id="X", provider="openai", model="gpt-4o",
                               usage={"prompt_tokens": 100, "completion_tokens": 1})
        assert "1 API calls" in bg._budget_command(session_id="X")


class TestLedgerPersistence:
    """The ceiling is advertised as per *session*, and a session outlives the
    process that started it. Before this, resuming a conversation in a fresh CLI
    process reset the budget to zero — quietly turning a $5 cap into $5 per
    resume. Caught by a live two-turn test, not by any unit test.
    """

    def test_spend_survives_a_new_process(self, bg, tmp_path):
        from budget_guard_under_test.ledger import Ledger
        bg._LEDGER.record("sess", 1.25, 400000)
        fresh = Ledger(root=tmp_path / "budget")      # simulates a restart
        spend = fresh.get("sess")
        assert spend.tokens == 400000
        assert spend.cost_usd == 1.25
        assert spend.api_calls == 1

    def test_announced_tiers_survive_too(self, bg, tmp_path):
        from budget_guard_under_test.ledger import Ledger
        bg._LEDGER.record("sess", 1.0, 10)
        assert bg._LEDGER.mark_announced("sess", "warn") is True
        fresh = Ledger(root=tmp_path / "budget")
        assert fresh.mark_announced("sess", "warn") is False, "would re-nag on resume"

    def test_reset_clears_disk_too(self, bg, tmp_path):
        from budget_guard_under_test.ledger import Ledger
        bg._LEDGER.record("gone", 1.0, 100)
        bg._LEDGER.reset("gone")
        assert Ledger(root=tmp_path / "budget").get("gone").tokens == 0

    def test_sessions_persist_independently(self, bg, tmp_path):
        from budget_guard_under_test.ledger import Ledger
        bg._LEDGER.record("a", 1.0, 100)
        bg._LEDGER.record("b", 2.0, 200)
        fresh = Ledger(root=tmp_path / "budget")
        assert fresh.get("a").tokens == 100
        assert fresh.get("b").tokens == 200

    def test_corrupt_file_does_not_crash(self, bg, tmp_path):
        from budget_guard_under_test.ledger import Ledger
        d = tmp_path / "budget"; d.mkdir(parents=True, exist_ok=True)
        (d / "broken.json").write_text("{not json")
        assert Ledger(root=d).get("broken").tokens == 0


class TestHousekeeping:
    """One state file per session accumulates forever without pruning — a leak
    introduced by making the ledger persistent."""

    def test_old_files_are_removed(self, bg, tmp_path):
        import os, time
        from budget_guard_under_test.ledger import Ledger
        led = Ledger(root=tmp_path / "budget")
        led.record("recent", 1.0, 10)
        led.record("ancient", 1.0, 10)
        old = tmp_path / "budget" / "ancient.json"
        stale = time.time() - 60 * 86400
        os.utime(old, (stale, stale))
        removed = led.prune(max_age_days=30)
        assert removed >= 1
        assert not old.exists()
        assert (tmp_path / "budget" / "recent.json").exists()

    def test_file_count_is_capped(self, bg, tmp_path):
        from budget_guard_under_test.ledger import Ledger
        led = Ledger(root=tmp_path / "budget")
        for i in range(40):
            led.record(f"s{i}", 1.0, 10)
        led.prune(max_age_days=9999, max_files=10)
        assert len(list((tmp_path / "budget").glob("*.json"))) <= 10

    def test_prune_survives_a_missing_directory(self, bg, tmp_path):
        from budget_guard_under_test.ledger import Ledger
        assert Ledger(root=tmp_path / "never-created").prune() == 0


class TestConcurrency:
    """Subagents bill against their parent's session from worker threads, so
    post_api_request can fire concurrently on one key."""

    def test_parallel_records_do_not_lose_spend(self, bg, tmp_path):
        import threading
        from budget_guard_under_test.ledger import Ledger
        led = Ledger(root=tmp_path / "budget")
        threads = [threading.Thread(target=lambda: [led.record("shared", 0.01, 100)
                                                    for _ in range(50)])
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        spend = led.get("shared")
        assert spend.api_calls == 400, f"lost updates: {spend.api_calls}"
        assert spend.tokens == 40000

    def test_parallel_announce_yields_one_winner(self, bg, tmp_path):
        import threading
        from budget_guard_under_test.ledger import Ledger
        led = Ledger(root=tmp_path / "budget")
        led.record("s", 1.0, 10)
        wins = []
        def go():
            if led.mark_announced("s", "warn"):
                wins.append(1)
        threads = [threading.Thread(target=go) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(wins) == 1, "the agent would be told the same thing twice"
