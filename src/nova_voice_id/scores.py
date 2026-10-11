"""scores.db: one row per request, for tuning the thresholds.

Rows hold the status, names and scores, never audio or words. Rows older than
30 days are deleted. Call these methods from the storage thread only.
"""

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

KEEP_DAYS = 30
MAX_STATS_ROWS = 5000
PURGE_EVERY_S = 3600.0

COLUMNS = (
    "time",
    "status",
    "person",
    "score",
    "second",
    "second_score",
    "margin",
    "speech_s",
    "model",
)


class ScoreLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._db: sqlite3.Connection | None = None
        self._last_purge = 0.0

    def _connect(self) -> sqlite3.Connection:
        if self._db is None:
            if not self._path.exists():
                fd = os.open(self._path, os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
            os.chmod(self._path, 0o600)
            # Used only from the storage thread, which is not the thread that
            # may have created this object.
            db = sqlite3.connect(self._path, check_same_thread=False)
            db.execute(
                "CREATE TABLE IF NOT EXISTS scores ("
                "time REAL NOT NULL, status TEXT NOT NULL, person TEXT, score REAL, "
                "second TEXT, second_score REAL, margin REAL, speech_s REAL, model TEXT)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS scores_time ON scores (time)")
            db.commit()
            self._db = db
        return self._db

    def add(self, row: dict[str, Any]) -> None:
        db = self._connect()
        db.execute(
            f"INSERT INTO scores ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
            [row.get(column) for column in COLUMNS],
        )
        db.commit()
        if time.time() - self._last_purge > PURGE_EVERY_S:
            self.purge()

    def purge(self) -> int:
        db = self._connect()
        cursor = db.execute("DELETE FROM scores WHERE time < ?", (time.time() - KEEP_DAYS * 86400,))
        db.commit()
        self._last_purge = time.time()
        return cursor.rowcount

    def delete_person(self, person: str) -> int:
        db = self._connect()
        cursor = db.execute("DELETE FROM scores WHERE person = ? OR second = ?", (person, person))
        db.commit()
        return cursor.rowcount

    def delete_all(self) -> None:
        db = self._connect()
        db.execute("DELETE FROM scores")
        db.commit()

    def rows(self, days: float) -> tuple[list[dict[str, Any]], bool]:
        """Rows from the last `days` days, newest first, and whether more exist."""
        db = self._connect()
        cursor = db.execute(
            f"SELECT {', '.join(COLUMNS)} FROM scores WHERE time >= ? ORDER BY time DESC LIMIT ?",
            (time.time() - days * 86400, MAX_STATS_ROWS + 1),
        )
        rows = [dict(zip(COLUMNS, values, strict=True)) for values in cursor.fetchall()]
        return rows[:MAX_STATS_ROWS], len(rows) > MAX_STATS_ROWS

    def count(self) -> int:
        return self._connect().execute("SELECT COUNT(*) FROM scores").fetchone()[0]

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
