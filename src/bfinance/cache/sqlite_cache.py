"""
Persistent SQLite caching layer with TTL support for bfinance.
"""

import json
import os
import sqlite3
import time
from typing import Any, Optional
from pathlib import Path


class SQLiteCache:
    """
    Lightweight, embedded SQLite cache for bfinance upstream queries.
    Stores serialized JSON payloads with expiration timestamps.
    """

    def __init__(self, db_path: Optional[str] = None, default_ttl_hours: float = 24.0):
        if db_path is None:
            # Store in user home directory under ~/.bfinance/cache.db
            home_dir = Path.home() / ".bfinance"
            home_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = str(home_dir / "cache.db")
        else:
            self.db_path = db_path

        self.default_ttl_hours = default_ttl_hours
        self._init_db()

    def _init_db(self):
        """Create cache table and indexes if not exists."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS bfinance_cache (
                    cache_key TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_category ON bfinance_cache(category)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_expires ON bfinance_cache(expires_at)"
            )
            conn.commit()

    def get(self, cache_key: str) -> Optional[Any]:
        """Retrieve unexpired cached payload."""
        now = time.time()
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT payload, expires_at FROM bfinance_cache WHERE cache_key = ?",
                    (cache_key,),
                )
                row = cursor.fetchone()
                if row:
                    payload_str, expires_at = row
                    if expires_at > now:
                        return json.loads(payload_str)
                    else:
                        # Expired, clean it up
                        cursor.execute("DELETE FROM bfinance_cache WHERE cache_key = ?", (cache_key,))
                        conn.commit()
        except Exception:
            return None
        return None

    def set(
        self,
        cache_key: str,
        payload: Any,
        category: str = "general",
        ttl_hours: Optional[float] = None,
    ):
        """Store serialized payload with TTL."""
        if ttl_hours is None:
            ttl_hours = self.default_ttl_hours

        now = time.time()
        expires_at = now + (ttl_hours * 3600.0)
        payload_str = json.dumps(payload)

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO bfinance_cache (cache_key, category, payload, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        payload = excluded.payload,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at
                    """,
                    (cache_key, category, payload_str, now, expires_at),
                )
                conn.commit()
        except Exception:
            pass

    def clear(self, category: Optional[str] = None):
        """Clear all or category-specific cache entries."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                if category:
                    cursor.execute("DELETE FROM bfinance_cache WHERE category = ?", (category,))
                else:
                    cursor.execute("DELETE FROM bfinance_cache")
                conn.commit()
        except Exception:
            pass
