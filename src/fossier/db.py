"""Database layer: schema, migrations, and query helpers using pyturso."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import turso

from fossier.models import Contributor, Decision, Outcome, ScoreResult, TrustTier

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contributors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_owner      TEXT NOT NULL,
    repo_name       TEXT NOT NULL,
    username        TEXT NOT NULL,
    platform        TEXT NOT NULL DEFAULT 'github',
    trust_tier      TEXT NOT NULL CHECK (trust_tier IN ('trusted','known','unknown','blocked')),
    latest_score    REAL,
    first_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at    TEXT NOT NULL DEFAULT (datetime('now')),
    blocked_reason  TEXT,
    UNIQUE(repo_owner, repo_name, username, platform)
);

CREATE TABLE IF NOT EXISTS score_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contributor_id  INTEGER NOT NULL REFERENCES contributors(id),
    pr_number       INTEGER,
    total_score     REAL NOT NULL,
    signal_breakdown TEXT NOT NULL,
    confidence      REAL NOT NULL,
    scored_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contributor_id  INTEGER NOT NULL REFERENCES contributors(id),
    pr_number       INTEGER,
    trust_tier      TEXT NOT NULL,
    outcome         TEXT NOT NULL CHECK (outcome IN ('allow','review','deny')),
    reason          TEXT NOT NULL,
    score_history_id INTEGER REFERENCES score_history(id),
    decided_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS api_cache (
    cache_key       TEXT PRIMARY KEY,
    response_json   TEXT NOT NULL,
    etag            TEXT,
    cached_at       TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL
);
"""


class Database:
    def __init__(self, db_path: str = ".fossier.db"):
        self.db_path = db_path
        self._conn: turso.Connection | None = None

    def connect(self) -> None:
        self._conn = turso.connect(self.db_path)
        self._migrate()

    def close(self) -> None:
        if self._conn:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)", [])
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> turso.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    def _migrate(self) -> None:
        # Check if schema_version table exists
        result = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        rows = result.fetchall()

        if not rows:
            logger.info("Initializing database schema v%d", CURRENT_SCHEMA_VERSION)
            self.conn.executescript(_SCHEMA_V1)
            self.conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                [CURRENT_SCHEMA_VERSION],
            )
            self.conn.commit()
            return

        result = self.conn.execute("SELECT MAX(version) FROM schema_version")
        row = result.fetchone()
        current = row[0] if row and row[0] else 0

        if current < CURRENT_SCHEMA_VERSION:
            # Run incremental migrations here as schema evolves
            logger.info(
                "Database at v%d, migrating to v%d", current, CURRENT_SCHEMA_VERSION
            )
            self.conn.commit()

    def get_contributor(
        self, repo_owner: str, repo_name: str, username: str
    ) -> Contributor | None:
        result = self.conn.execute(
            """SELECT id, repo_owner, repo_name, username, platform,
                      trust_tier, latest_score, blocked_reason
               FROM contributors
               WHERE repo_owner = ? AND repo_name = ? AND username = ?""",
            [repo_owner, repo_name, username.lower()],
        )
        row = result.fetchone()
        if not row:
            return None
        return Contributor(
            db_id=row[0],
            repo_owner=row[1],
            repo_name=row[2],
            username=row[3],
            platform=row[4],
            trust_tier=TrustTier(row[5]),
            latest_score=row[6],
            blocked_reason=row[7],
        )

    def upsert_contributor(self, contributor: Contributor) -> int:
        self.conn.execute(
            """INSERT INTO contributors (repo_owner, repo_name, username, platform,
                                         trust_tier, latest_score, blocked_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(repo_owner, repo_name, username, platform)
               DO UPDATE SET trust_tier = excluded.trust_tier,
                             latest_score = excluded.latest_score,
                             blocked_reason = excluded.blocked_reason,
                             last_seen_at = datetime('now')""",
            [
                contributor.repo_owner,
                contributor.repo_name,
                contributor.username.lower(),
                contributor.platform,
                contributor.trust_tier.value,
                contributor.latest_score,
                contributor.blocked_reason,
            ],
        )
        self.conn.commit()
        result = self.conn.execute(
            """SELECT id FROM contributors
               WHERE repo_owner = ? AND repo_name = ? AND username = ?""",
            [
                contributor.repo_owner,
                contributor.repo_name,
                contributor.username.lower(),
            ],
        )
        row = result.fetchone()
        return row[0]

    def record_score(
        self, contributor_id: int, score: ScoreResult, pr_number: int | None = None
    ) -> int:
        self.conn.execute(
            """INSERT INTO score_history (contributor_id, pr_number, total_score,
                                          signal_breakdown, confidence)
               VALUES (?, ?, ?, ?, ?)""",
            [
                contributor_id,
                pr_number,
                score.total_score,
                json.dumps(score.signal_breakdown),
                score.confidence,
            ],
        )
        self.conn.commit()
        result = self.conn.execute("SELECT last_insert_rowid()")
        return result.fetchone()[0]

    def record_decision(
        self,
        contributor_id: int,
        decision: Decision,
        score_history_id: int | None = None,
    ) -> int:
        self.conn.execute(
            """INSERT INTO decisions (contributor_id, pr_number, trust_tier,
                                      outcome, reason, score_history_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                contributor_id,
                decision.pr_number,
                decision.trust_tier.value,
                decision.outcome.value,
                decision.reason,
                score_history_id,
            ],
        )
        self.conn.commit()
        result = self.conn.execute("SELECT last_insert_rowid()")
        return result.fetchone()[0]

    def get_history(self, repo_owner: str, repo_name: str, username: str) -> list[dict]:
        result = self.conn.execute(
            """SELECT d.decided_at, d.trust_tier, d.outcome, d.reason, d.pr_number,
                      s.total_score, s.confidence, s.signal_breakdown
               FROM decisions d
               JOIN contributors c ON d.contributor_id = c.id
               LEFT JOIN score_history s ON d.score_history_id = s.id
               WHERE c.repo_owner = ? AND c.repo_name = ? AND c.username = ?
               ORDER BY d.decided_at DESC""",
            [repo_owner, repo_name, username.lower()],
        )
        rows = result.fetchall()
        return [
            {
                "decided_at": row[0],
                "trust_tier": row[1],
                "outcome": row[2],
                "reason": row[3],
                "pr_number": row[4],
                "total_score": row[5],
                "confidence": row[6],
                "signal_breakdown": json.loads(row[7]) if row[7] else None,
            }
            for row in rows
        ]

    def prune_cache(self) -> int:
        """Remove expired cache entries. Returns number of entries removed."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        result = self.conn.execute(
            "SELECT COUNT(*) FROM api_cache WHERE expires_at <= ?", [now]
        )
        count = result.fetchone()[0]
        if count > 0:
            self.conn.execute("DELETE FROM api_cache WHERE expires_at <= ?", [now])
            self.conn.commit()
        return count

    def get_cached(self, cache_key: str) -> dict | None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        result = self.conn.execute(
            """SELECT response_json, etag FROM api_cache
               WHERE cache_key = ? AND expires_at > ?""",
            [cache_key, now],
        )
        row = result.fetchone()
        if not row:
            return None
        return {"data": json.loads(row[0]), "etag": row[1]}

    def get_cached_expired(self, cache_key: str) -> dict | None:
        """Return an expired cache entry if it has an etag (for conditional requests)."""
        result = self.conn.execute(
            """SELECT response_json, etag FROM api_cache
               WHERE cache_key = ? AND etag IS NOT NULL""",
            [cache_key],
        )
        row = result.fetchone()
        if not row:
            return None
        return {"data": json.loads(row[0]), "etag": row[1]}

    def set_cached(
        self,
        cache_key: str,
        response_json: str,
        expires_at: str,
        etag: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO api_cache (cache_key, response_json, etag, expires_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(cache_key)
               DO UPDATE SET response_json = excluded.response_json,
                             etag = excluded.etag,
                             cached_at = datetime('now'),
                             expires_at = excluded.expires_at""",
            [cache_key, response_json, etag, expires_at],
        )
        self.conn.commit()

    def get_stats(self, repo_owner: str, repo_name: str) -> dict:
        stats = {}
        for tier in TrustTier:
            result = self.conn.execute(
                """SELECT COUNT(*) FROM contributors
                   WHERE repo_owner = ? AND repo_name = ? AND trust_tier = ?""",
                [repo_owner, repo_name, tier.value],
            )
            stats[tier.value] = result.fetchone()[0]

        for outcome in Outcome:
            result = self.conn.execute(
                """SELECT COUNT(*) FROM decisions d
                   JOIN contributors c ON d.contributor_id = c.id
                   WHERE c.repo_owner = ? AND c.repo_name = ? AND d.outcome = ?""",
                [repo_owner, repo_name, outcome.value],
            )
            stats[f"decisions_{outcome.value}"] = result.fetchone()[0]

        return stats
