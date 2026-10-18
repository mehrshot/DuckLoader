"""
The media cache on disk: for every link (and quality) already delivered,
the Telegram file_ids and caption data needed to send it again instantly,
without downloading anything.

It used to live only in memory (lost on every restart, 3000 entries max).
Every delivered result is also copied into the private cache channel, so
the files stay on Telegram and these file_ids keep working; this index is
what lets the bot find them again. SQLite (stdlib) instead of a JSON file:
one small row per link, no rewriting a large file on every download.
"""

import json
import logging
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

DB_FILE = "media_cache.db"
MAX_ITEMS = 200_000
PRUNE_EVERY = 500


class MediaCache:
    """The dict-like subset the bot uses: get(), [key] = value, pop()."""

    def __init__(self, path=DB_FILE, max_items=MAX_ITEMS):
        self._lock = threading.Lock()
        self._max_items = max_items
        self._writes = 0
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS media (key TEXT PRIMARY KEY, time REAL NOT NULL, data TEXT NOT NULL)"
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS media_time ON media (time)")
            self._db.commit()

    def get(self, key, default=None):
        if not key:
            return default
        with self._lock:
            row = self._db.execute("SELECT data FROM media WHERE key = ?", (str(key),)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return default

    def __setitem__(self, key, value):
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO media (key, time, data) VALUES (?, ?, ?)",
                (str(key), float(value.get("time") or time.time()), data),
            )
            self._writes += 1
            if self._writes % PRUNE_EVERY == 0:
                self._db.execute(
                    "DELETE FROM media WHERE key NOT IN (SELECT key FROM media ORDER BY time DESC LIMIT ?)",
                    (self._max_items,),
                )
            self._db.commit()

    def pop(self, key, default=None):
        value = self.get(key, default)
        if key:
            with self._lock:
                self._db.execute("DELETE FROM media WHERE key = ?", (str(key),))
                self._db.commit()
        return value

    def __len__(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM media").fetchone()[0]
