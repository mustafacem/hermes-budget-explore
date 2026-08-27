"""Storage for pinned findings.

No Hermes imports so it can be tested directly.

The design decision worth explaining is that pins are re-injected into every
turn rather than protected inside the transcript. Protecting history means
teaching the compressor which messages are special -- a change to delicate,
well-tested selection logic, and one that still fails when the whole session is
resumed in a fresh process.

Re-injection sidesteps both. A pin never lives in compressible history at all; it
is added fresh each turn from a store that outlives the context window. The cost
is that pins are paid for on every turn, which is why the board is small and
hard-capped: this is for the handful of facts that must not be lost, not for
notes.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

MAX_PINS = 20
MAX_CHARS = 240          # per pin
MAX_TOTAL_CHARS = 2400   # whole board; the per-turn tax has to stay bounded


@dataclass
class Pin:
    id: str
    text: str
    created: float


def _slug(text: str, existing: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:24] or "pin"
    cand, n = base, 2
    while cand in existing:
        cand = f"{base}-{n}"
        n += 1
    return cand


class Board:
    """Per-session pins, persisted so they survive a restart."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._mem: Dict[str, List[Pin]] = {}
        self._root = root

    # -- persistence -------------------------------------------------------

    def _path(self, session: str) -> Optional[Path]:
        root = self._root
        if root is None:
            home = os.environ.get("HERMES_HOME")
            if not home:
                return None
            root = Path(home) / "pinboard"
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session or "default")[:120]
        return root / f"{safe}.json"

    def _load(self, session: str) -> List[Pin]:
        if session in self._mem:
            return self._mem[session]
        pins: List[Pin] = []
        path = self._path(session)
        if path and path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                pins = [Pin(**p) for p in raw][:MAX_PINS]
            except Exception:
                pins = []
        self._mem[session] = pins
        return pins

    def _save(self, session: str, pins: List[Pin]) -> None:
        path = self._path(session)
        if not path:
            return
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(p) for p in pins]), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    # -- operations --------------------------------------------------------

    def add(self, session: str, text: str) -> Pin:
        text = " ".join(str(text or "").split())[:MAX_CHARS]
        if not text:
            raise ValueError("a pin needs some text")
        with self._lock:
            pins = self._load(session)
            for p in pins:
                if p.text == text:
                    return p            # idempotent; re-pinning is a no-op
            pin = Pin(_slug(text, {p.id for p in pins}), text, time.time())
            pins.append(pin)
            # Oldest out first when full. A pin that still matters can be
            # re-pinned; silently keeping twenty stale ones would be worse.
            while len(pins) > MAX_PINS or sum(len(p.text) for p in pins) > MAX_TOTAL_CHARS:
                if len(pins) <= 1:
                    break
                pins.pop(0)
            self._save(session, pins)
            return pin

    def remove(self, session: str, pin_id: str) -> bool:
        with self._lock:
            pins = self._load(session)
            for i, p in enumerate(pins):
                if p.id == pin_id:
                    pins.pop(i)
                    self._save(session, pins)
                    return True
            return False

    def list(self, session: str) -> List[Pin]:
        with self._lock:
            return list(self._load(session))

    def clear(self, session: str) -> int:
        with self._lock:
            n = len(self._load(session))
            self._mem[session] = []
            # Only touch disk if there was something to clear. on_session_start
            # fires for every session, and writing an empty board each time
            # would litter HERMES_HOME with junk files.
            if n:
                self._save(session, [])
            return n


    def prune(self, max_age_days: int = 30, max_files: int = 2000) -> int:
        """Drop state files for sessions that are long gone.

        This module writes one small file per session, so without pruning the
        directory grows for the life of the install. Bounded work: a single
        listdir, oldest-first removal, and any error is swallowed -- housekeeping
        must never be able to break a session.
        """
        root = self._path("probe")
        if root is None:
            return 0
        root = root.parent
        removed = 0
        try:
            entries = []
            cutoff = time.time() - max_age_days * 86400
            for f in root.glob("*.json"):
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                entries.append((mtime, f))
                if mtime < cutoff:
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
            survivors = sorted((m, f) for m, f in entries if f.exists())
            excess = len(survivors) - max_files
            for _, f in survivors[:max(0, excess)]:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        except OSError:
            return removed
        return removed

    def render(self, session: str) -> str:
        """The block injected into each turn. Empty when nothing is pinned."""
        pins = self.list(session)
        if not pins:
            return ""
        lines = [f"- [{p.id}] {p.text}" for p in pins]
        return (
            "Pinned findings from earlier in this session (these survive "
            "context compaction; treat them as established):\n" + "\n".join(lines)
        )
