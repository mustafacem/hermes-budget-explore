"""Tests for the agent-watchdog plugin.

The risk with a loop detector is not that it misses loops — it is that it
interrupts real work. Most of what follows is therefore about *not* firing:
repeated calls whose results change, deliberate polling, and short exploratory
repetition all have to pass through untouched.

The second property under test is that a detection can never wedge a session.
The watchdog blocks a call once per pattern and then re-arms, so a model with a
good reason to repeat itself continues after a single interruption.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load():
    pkg = REPO / "plugins" / "agent-watchdog"
    spec = importlib.util.spec_from_file_location(
        "agent_watchdog_under_test", pkg / "__init__.py",
        submodule_search_locations=[str(pkg)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_watchdog_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def wd():
    mod = _load()
    yield mod
    sys.modules.pop("agent_watchdog_under_test", None)


@pytest.fixture()
def det():
    from agent_watchdog_under_test.detector import Watchdog, WatchdogConfig
    return Watchdog, WatchdogConfig


# --------------------------------------------------------------------------
# must NOT fire
# --------------------------------------------------------------------------


class TestDoesNotInterruptRealWork:
    def test_same_call_changing_result_is_progress(self, wd, det):
        """`git status` between edits returns something new each time. That is
        work, not a loop, and is the main false positive to avoid."""
        Watchdog, Cfg = det
        w = Watchdog(Cfg(repeat_threshold=3))
        for i in range(10):
            assert w.check("t", "terminal", {"cmd": "git status"}) is None
            w.record("t", "terminal", {"cmd": "git status"}, f"output {i}")

    def test_short_repetition_is_tolerated(self, wd, det):
        """Re-reading a file two or three times while working is normal."""
        Watchdog, Cfg = det
        w = Watchdog(Cfg(repeat_threshold=4))
        for _ in range(3):
            assert w.check("t", "read_file", {"path": "a.py"}) is None
            w.record("t", "read_file", {"path": "a.py"}, "contents")

    def test_polling_tools_are_exempt(self, wd, det):
        """Identical results are the expected shape when you are waiting."""
        Watchdog, Cfg = det
        w = Watchdog(Cfg(repeat_threshold=2))
        for _ in range(20):
            assert w.check("t", "process", {"action": "status"}) is None
            w.record("t", "process", {"action": "status"}, "still running")

    def test_distinct_calls_never_trip_the_repeat_rule(self, wd, det):
        Watchdog, Cfg = det
        w = Watchdog(Cfg(repeat_threshold=2, churn_threshold=99))
        for i in range(20):
            assert w.check("t", "read_file", {"path": f"f{i}.py"}) is None
            w.record("t", "read_file", {"path": f"f{i}.py"}, f"body {i}")

    def test_disabled_never_fires(self, wd, det):
        Watchdog, Cfg = det
        w = Watchdog(Cfg(enabled=False, repeat_threshold=2))
        for _ in range(10):
            assert w.check("t", "read_file", {"p": "a"}) is None
            w.record("t", "read_file", {"p": "a"}, "same")


# --------------------------------------------------------------------------
# must fire
# --------------------------------------------------------------------------


class TestDetection:
    def test_repeat_fires_on_identical_result(self, wd, det):
        Watchdog, Cfg = det
        w = Watchdog(Cfg(repeat_threshold=4))
        finding = None
        for _ in range(6):
            finding = w.check("t", "read_file", {"path": "a.py"})
            if finding:
                break
            w.record("t", "read_file", {"path": "a.py"}, "identical")
        assert finding is not None
        assert finding.pathology == "repeat"
        assert finding.count >= 4

    def test_cycle_fires_on_alternation(self, wd, det):
        Watchdog, Cfg = det
        w = Watchdog(Cfg(cycle_threshold=3, repeat_threshold=99, churn_threshold=99))
        found = None
        for i in range(12):
            tool, arg = ("read_file", {"p": "x"}) if i % 2 == 0 else ("patch", {"p": "x"})
            found = w.check("t", tool, arg)
            if found:
                break
            w.record("t", tool, arg, f"r{i}")
        assert found is not None and found.pathology == "cycle"

    def test_churn_fires_when_nothing_is_new(self, wd, det):
        """Distinct calls, but every result was already seen. Motion, no progress."""
        Watchdog, Cfg = det
        w = Watchdog(Cfg(repeat_threshold=99, cycle_threshold=99, churn_threshold=6))
        w.record("t", "search_files", {"q": "seed"}, "NOTHING FOUND")
        found = None
        for i in range(20):
            found = w.check("t", "search_files", {"q": f"term{i}"})
            if found:
                break
            w.record("t", "search_files", {"q": f"term{i}"}, "NOTHING FOUND")
        assert found is not None and found.pathology == "churn"

    def test_argument_order_does_not_change_identity(self, wd, det):
        from agent_watchdog_under_test.detector import signature
        assert signature("t", {"a": 1, "b": 2}) == signature("t", {"b": 2, "a": 1})

    def test_tasks_are_isolated(self, wd, det):
        Watchdog, Cfg = det
        w = Watchdog(Cfg(repeat_threshold=3))
        for _ in range(5):
            w.record("task-a", "read_file", {"p": "x"}, "same")
        assert w.check("task-a", "read_file", {"p": "x"}) is not None
        assert w.check("task-b", "read_file", {"p": "x"}) is None


# --------------------------------------------------------------------------
# intervention behaviour
# --------------------------------------------------------------------------


class TestIntervention:
    def _cfg(self, wd, monkeypatch, **kw):
        from agent_watchdog_under_test.detector import WatchdogConfig
        monkeypatch.setattr(wd, "_config", lambda: WatchdogConfig(**kw))

    def test_blocks_once_then_rearms(self, wd, monkeypatch):
        """A single interruption. A model with a good reason continues."""
        self._cfg(wd, monkeypatch, repeat_threshold=3)
        wd._WATCHDOG.reset("s")
        for _ in range(4):
            wd.on_post_tool_call(tool_name="read_file", args={"p": "a"},
                                 result="same", session_id="s")
        first = wd.on_pre_tool_call(tool_name="read_file", args={"p": "a"}, session_id="s")
        assert first and first["action"] == "block"
        second = wd.on_pre_tool_call(tool_name="read_file", args={"p": "a"}, session_id="s")
        assert second is None, "watchdog wedged the session instead of re-arming"

    def test_block_message_is_actionable(self, wd, monkeypatch):
        self._cfg(wd, monkeypatch, repeat_threshold=3)
        wd._WATCHDOG.reset("s")
        for _ in range(4):
            wd.on_post_tool_call(tool_name="read_file", args={"p": "a"},
                                 result="same", session_id="s")
        msg = wd.on_pre_tool_call(tool_name="read_file", args={"p": "a"},
                                  session_id="s")["message"]
        assert "will not change" in msg or "change your approach" in msg
        assert "wait explicitly" in msg, "polling advice missing"

    def test_hooks_never_raise(self, wd, monkeypatch):
        """A detector bug must not take the session with it."""
        self._cfg(wd, monkeypatch, repeat_threshold=2)
        wd.on_pre_tool_call()
        wd.on_post_tool_call()
        wd.on_pre_tool_call(tool_name="x", args=object(), session_id="s")
        wd.on_post_tool_call(tool_name="x", args=object(), result=object(), session_id="s")
        wd.on_session_start(session_id="s")

    def test_unhashable_arguments_are_handled(self, wd, det):
        """json.dumps cannot serialise everything a tool might be handed."""
        from agent_watchdog_under_test.detector import signature
        assert isinstance(signature("t", {"fn": lambda: 1}), str)
        assert isinstance(signature("t", object()), str)


class TestDiscovery:
    def test_registers_only_valid_hooks(self, wd):
        from hermes_cli.plugins import VALID_HOOKS
        seen = []

        class Ctx:
            def register_hook(self, name, fn):
                seen.append(name)

        wd.register(Ctx())
        assert seen
        for name in seen:
            assert name in VALID_HOOKS, f"{name} is not a valid hook"

    def test_bundled_plugin_is_discovered(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        assert "agent-watchdog" in mgr._plugins
        assert mgr._plugins["agent-watchdog"].manifest.source == "bundled"

    def test_watchdog_is_an_accepted_config_root(self):
        from hermes_cli.config import _KNOWN_ROOT_KEYS
        assert "watchdog" in _KNOWN_ROOT_KEYS


class TestExemptToolsConfigurable:
    """POLLING_TOOLS covers the built-in waiters. An operator running a custom or
    MCP polling tool would otherwise get a false positive with no way to switch
    it off — the worst kind, because the watchdog fights work it cannot be told
    to ignore."""

    def test_custom_tool_can_be_exempted(self, wd, det):
        Watchdog, Cfg = det
        from agent_watchdog_under_test.detector import POLLING_TOOLS
        w = Watchdog(Cfg(repeat_threshold=2,
                         exempt_tools=frozenset(POLLING_TOOLS) | {"mcp_wait_for_build"}))
        for _ in range(8):
            assert w.check("t", "mcp_wait_for_build", {"job": 1}) is None
            w.record("t", "mcp_wait_for_build", {"job": 1}, "still building")

    def test_builtin_waiters_survive_a_custom_list(self, wd, monkeypatch):
        """Adding an exemption must extend the defaults, not replace them."""
        import cli
        monkeypatch.setattr(cli, "CLI_CONFIG",
                            {"watchdog": {"exempt_tools": ["mcp_wait"]}}, raising=False)
        cfg = wd._config()
        assert "mcp_wait" in cfg.exempt_tools
        assert "process" in cfg.exempt_tools, "built-in waiter was dropped"

    def test_string_form_is_accepted(self, wd, monkeypatch):
        import cli
        monkeypatch.setattr(cli, "CLI_CONFIG",
                            {"watchdog": {"exempt_tools": "solo_tool"}}, raising=False)
        assert "solo_tool" in wd._config().exempt_tools

    def test_junk_config_falls_back_to_defaults(self, wd, monkeypatch):
        import cli
        monkeypatch.setattr(cli, "CLI_CONFIG",
                            {"watchdog": {"exempt_tools": 12345}}, raising=False)
        cfg = wd._config()
        assert "process" in cfg.exempt_tools
