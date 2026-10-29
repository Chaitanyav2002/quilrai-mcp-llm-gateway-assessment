"""Token aware sliding window rate limiter backed by SQLite on disk."""

import hashlib
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited, retry after {retry_after:.1f}s")
        self.retry_after = retry_after


class RequestTooLarge(Exception):
    pass


class TokenRateLimiter:
    def __init__(self, path: str | Path, limit: int = 50_000, window: float = 60, clock=time.time):
        self.path = str(path)
        self.limit = limit
        self.window = window
        self.clock = clock
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, "
                       "ts REAL NOT NULL, tokens INTEGER NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_tenant_ts ON usage (tenant, ts)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_ts ON usage (ts)")

    def _connect(self) -> sqlite3.Connection:
        # autocommit mode, transactions are opened by hand with BEGIN IMMEDIATE
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    @staticmethod
    def _tenant(api_key: str) -> str:
        return hashlib.sha256(api_key.encode()).hexdigest()

    def reserve(self, api_key: str, tokens: int) -> int:
        """Takes tokens from the tenant's window and returns the reservation id."""
        if tokens > self.limit:
            raise RequestTooLarge(f"request needs {tokens} tokens, the limit is {self.limit} per window")
        tenant, now = self._tenant(api_key), self.clock()
        with closing(self._connect()) as db:
            # IMMEDIATE locks before the read, so two requests (or processes) can't both pass the check
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM usage WHERE ts <= ?", (now - self.window,))
                used = db.execute("SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE tenant = ?", (tenant,)).fetchone()[0]
                if used + tokens > self.limit:
                    rows = db.execute("SELECT ts, tokens FROM usage WHERE tenant = ? ORDER BY ts", (tenant,)).fetchall()
                    raise RateLimited(self._retry_after(rows, used + tokens - self.limit, now))
                cur = db.execute("INSERT INTO usage (tenant, ts, tokens) VALUES (?, ?, ?)", (tenant, now, tokens))
                db.execute("COMMIT")
                return cur.lastrowid
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _retry_after(self, rows, needed: int, now: float) -> float:
        freed = 0
        for ts, tokens in rows:
            freed += tokens
            if freed >= needed:
                return max(0.0, ts + self.window - now)
        return self.window

    def settle(self, reservation: int, actual_tokens: int) -> None:
        with closing(self._connect()) as db:
            db.execute("UPDATE usage SET tokens = ? WHERE id = ?", (actual_tokens, reservation))

    def release(self, reservation: int) -> None:
        with closing(self._connect()) as db:
            db.execute("DELETE FROM usage WHERE id = ?", (reservation,))

    def used(self, api_key: str) -> int:
        with closing(self._connect()) as db:
            row = db.execute("SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE tenant = ? AND ts > ?",
                             (self._tenant(api_key), self.clock() - self.window)).fetchone()
        return row[0]
