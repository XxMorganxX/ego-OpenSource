"""Local bookkeeping: what we last pushed, what is still waiting to be pushed."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.execute("CREATE TABLE IF NOT EXISTS notes (rel_path TEXT PRIMARY KEY, external_id TEXT NOT NULL, sha TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL)")
        self._db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
        self._db.commit()

    def known(self) -> dict[str, tuple[str, str]]:
        return {r[0]: (r[1], r[2]) for r in self._db.execute("SELECT rel_path, external_id, sha FROM notes")}

    def remember(self, rel: str, ext: str, sha: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO notes (rel_path, external_id, sha) VALUES (?, ?, ?)", (rel, ext, sha))
        self._db.commit()

    def forget(self, rel: str) -> None:
        self._db.execute("DELETE FROM notes WHERE rel_path = ?", (rel,)); self._db.commit()

    def queue_push(self, kind: str, payload: dict) -> int:
        cur = self._db.execute("INSERT INTO pending (kind, payload) VALUES (?, ?)", (kind, json.dumps(payload)))
        self._db.commit(); return int(cur.lastrowid)

    def pending_pushes(self) -> list[tuple[int, str, dict]]:
        return [(r[0], r[1], json.loads(r[2])) for r in self._db.execute("SELECT id, kind, payload FROM pending ORDER BY id")]

    def drop_push(self, pid: int) -> None:
        self._db.execute("DELETE FROM pending WHERE id = ?", (pid,)); self._db.commit()

    def get_kv(self, k: str) -> str | None:
        row = self._db.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
        return row[0] if row else None

    def set_kv(self, k: str, v: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, v)); self._db.commit()
