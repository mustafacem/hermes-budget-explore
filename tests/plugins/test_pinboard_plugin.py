"""Tests for the pinboard plugin.

Two properties carry the feature.

First, pins have to survive the thing they exist for: compaction and process
restart. That is tested by reloading a fresh Board over the same directory,
which is what a resumed session does.

Second, the board has to stay small. Pins are re-sent on every single turn, so
an unbounded board is a permanent, growing tax on the context it is meant to
protect. The caps are load-bearing, not decoration.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load():
    pkg = REPO / "plugins" / "pinboard"
    spec = importlib.util.spec_from_file_location(
        "pinboard_under_test", pkg / "__init__.py",
        submodule_search_locations=[str(pkg)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pinboard_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def pb(tmp_path):
    mod = _load()
    from pinboard_under_test.board import Board
    mod._BOARD = Board(root=tmp_path / "pinboard")
    yield mod
    sys.modules.pop("pinboard_under_test", None)


class TestSurvival:
    def test_pins_survive_a_restart(self, pb, tmp_path):
        """The point of the feature. A resumed session reloads from disk."""
        from pinboard_under_test.board import Board
        pb.pin_tool(text="Root cause is in gateway/run.py:412", session_id="s")
        pb._BOARD = Board(root=tmp_path / "pinboard")     # fresh process
        pins = pb._BOARD.list("s")
        assert len(pins) == 1
        assert "gateway/run.py:412" in pins[0].text

    def test_board_is_injected_every_turn(self, pb):
        pb.pin_tool(text="Auth uses PyJWT", session_id="s")
        for _ in range(3):
            out = pb.on_pre_llm_call(session_id="s")
            assert out and "PyJWT" in out["context"]

    def test_nothing_injected_when_empty(self, pb):
        assert pb.on_pre_llm_call(session_id="s") is None

    def test_sessions_are_isolated(self, pb):
        pb.pin_tool(text="only for a", session_id="a")
        assert pb.on_pre_llm_call(session_id="b") is None


class TestCaps:
    """Pins cost tokens every turn, so the caps are the safety property."""

    def test_pin_count_is_capped(self, pb):
        from pinboard_under_test.board import MAX_PINS
        for i in range(MAX_PINS + 15):
            pb.pin_tool(text=f"finding number {i}", session_id="s")
        assert len(pb._BOARD.list("s")) <= MAX_PINS

    def test_oldest_is_evicted_first(self, pb):
        from pinboard_under_test.board import MAX_PINS
        for i in range(MAX_PINS + 3):
            pb.pin_tool(text=f"finding number {i}", session_id="s")
        texts = [p.text for p in pb._BOARD.list("s")]
        assert "finding number 0" not in texts
        assert f"finding number {MAX_PINS + 2}" in texts

    def test_total_size_is_capped(self, pb):
        from pinboard_under_test.board import MAX_TOTAL_CHARS
        for i in range(40):
            pb.pin_tool(text=f"{i} " + "x" * 200, session_id="s")
        total = sum(len(p.text) for p in pb._BOARD.list("s"))
        assert total <= MAX_TOTAL_CHARS

    def test_long_text_is_truncated(self, pb):
        from pinboard_under_test.board import MAX_CHARS
        pb.pin_tool(text="y" * 5000, session_id="s")
        assert len(pb._BOARD.list("s")[0].text) <= MAX_CHARS

    def test_repinning_is_idempotent(self, pb):
        for _ in range(5):
            pb.pin_tool(text="the same finding", session_id="s")
        assert len(pb._BOARD.list("s")) == 1


class TestToolBehaviour:
    def test_add_list_remove_clear(self, pb):
        add = pb.pin_tool(action="add", text="first finding", session_id="s")
        assert "Pinned" in add
        pin_id = pb._BOARD.list("s")[0].id
        assert "first finding" in pb.pin_tool(action="list", session_id="s")
        assert "Removed" in pb.pin_tool(action="remove", id=pin_id, session_id="s")
        assert pb._BOARD.list("s") == []
        pb.pin_tool(text="another", session_id="s")
        assert "Cleared 1" in pb.pin_tool(action="clear", session_id="s")

    def test_helpful_errors(self, pb):
        assert "needs `text`" in pb.pin_tool(action="add", text="   ", session_id="s")
        assert "needs `id`" in pb.pin_tool(action="remove", session_id="s")
        assert "unknown action" in pb.pin_tool(action="frobnicate", session_id="s")
        assert "No pin" in pb.pin_tool(action="remove", id="nope", session_id="s")

    def test_ids_are_unique_and_readable(self, pb):
        pb.pin_tool(text="retry logic is broken", session_id="s")
        pb.pin_tool(text="retry logic is broken in another way", session_id="s")
        ids = [p.id for p in pb._BOARD.list("s")]
        assert len(set(ids)) == len(ids)
        assert any("retry" in i for i in ids)

    def test_whitespace_is_normalised(self, pb):
        pb.pin_tool(text="  spread   over\n\nlines  ", session_id="s")
        assert pb._BOARD.list("s")[0].text == "spread over lines"

    def test_handler_never_raises(self, pb):
        pb.pin_tool()
        pb.pin_tool(action=None, text=None, session_id="s")
        pb.on_pre_llm_call()
        pb.on_session_start(session_id="s")


class TestRegistration:
    def test_registers_tool_hooks_and_command(self, pb):
        from hermes_cli.plugins import VALID_HOOKS
        tools, hooks, cmds = [], [], []

        class Ctx:
            def register_tool(self, name, toolset, schema, handler, **kw):
                tools.append((name, toolset))
                assert callable(handler)
                assert schema["name"] == name

            def register_hook(self, name, fn):
                hooks.append(name)

            def register_command(self, name, handler, **kw):
                cmds.append(name)

        pb.register(Ctx())
        assert ("pin", "pinboard") in tools
        assert "pre_llm_call" in hooks
        for h in hooks:
            assert h in VALID_HOOKS
        assert "pins" in cmds

    def test_schema_is_well_formed(self, pb):
        props = pb.PIN_SCHEMA["parameters"]["properties"]
        assert set(props["action"]["enum"]) == {"add", "list", "remove", "clear"}
        assert "text" in props and "id" in props

    def test_bundled_plugin_is_discovered(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        from hermes_cli import plugins as pmod
        mgr = pmod.PluginManager()
        mgr.discover_and_load()
        assert "pinboard" in mgr._plugins
        assert mgr._plugins["pinboard"].manifest.source == "bundled"


class TestDispatchContract:
    """How Hermes actually calls the handler.

    The registry dispatches tools as ``handler(args_dict, **context)`` — the
    model's arguments arrive as ONE positional dict, not as keywords. The first
    version of this handler took keywords, so every unit test passed while a
    live session died with "'dict' object has no attribute 'strip'". These pin
    the real contract.
    """

    def test_positional_dict_is_the_real_contract(self, pb):
        out = pb.pin_tool({"action": "add", "text": "found it in run.py:412"},
                          session_id="s")
        assert "Pinned" in out
        assert pb._BOARD.list("s")[0].text == "found it in run.py:412"

    def test_keyword_form_still_works(self, pb):
        """Kept callable for tests and ctx.dispatch_tool."""
        out = pb.pin_tool(text="keyword style", session_id="s")
        assert "Pinned" in out

    def test_positional_dict_actions(self, pb):
        pb.pin_tool({"text": "first"}, session_id="s")
        pid = pb._BOARD.list("s")[0].id
        assert "first" in pb.pin_tool({"action": "list"}, session_id="s")
        assert "Removed" in pb.pin_tool({"action": "remove", "id": pid}, session_id="s")

    def test_non_dict_first_arg_does_not_crash(self, pb):
        assert isinstance(pb.pin_tool(None, session_id="s"), str)
        assert isinstance(pb.pin_tool(12345, session_id="s"), str)
        assert "Pinned" in pb.pin_tool("bare string finding", session_id="s")


class TestSlashCommandSession:
    """Slash commands are invoked as ``handler(arg)`` with NO session context.

    Without a fallback /pins reads an empty default board and reports nothing,
    which looks exactly like "the feature does not work".
    """

    def test_pins_falls_back_to_the_last_seen_session(self, pb):
        pb.pin_tool({"text": "remembered finding"}, session_id="real-session")
        pb.on_pre_llm_call(session_id="real-session")     # hooks see the real id
        out = pb._pins_command()                          # command sees nothing
        assert "remembered finding" in out

    def test_explicit_session_still_wins(self, pb):
        pb.pin_tool({"text": "in A"}, session_id="A")
        pb.pin_tool({"text": "in B"}, session_id="B")
        assert "in A" in pb._pins_command(session_id="A")
        assert "in B" in pb._pins_command(session_id="B")


class TestHousekeeping:
    def test_old_boards_are_pruned(self, pb, tmp_path):
        import os, time
        pb._BOARD.add("recent", "still relevant")
        pb._BOARD.add("ancient", "long gone")
        old = tmp_path / "pinboard" / "ancient.json"
        stale = time.time() - 90 * 86400
        os.utime(old, (stale, stale))
        assert pb._BOARD.prune(max_age_days=30) >= 1
        assert not old.exists()
        assert (tmp_path / "pinboard" / "recent.json").exists()

    def test_board_count_is_capped(self, pb, tmp_path):
        for i in range(30):
            pb._BOARD.add(f"s{i}", f"finding {i}")
        pb._BOARD.prune(max_age_days=9999, max_files=8)
        assert len(list((tmp_path / "pinboard").glob("*.json"))) <= 8

    def test_session_start_prunes_without_failing(self, pb):
        pb.on_session_start(session_id="whatever")
