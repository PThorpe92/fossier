"""Tests for database layer."""

from fossier.db import Database
from fossier.models import (
    Contributor,
    Decision,
    Outcome,
    ScoreResult,
    SignalResult,
    TrustTier,
)


def test_connect_and_migrate(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.connect()
    # Should create tables without error
    result = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in result.fetchall()}
    assert "contributors" in tables
    assert "score_history" in tables
    assert "decisions" in tables
    assert "api_cache" in tables
    assert "schema_version" in tables
    db.close()


def test_upsert_and_get_contributor(db):
    c = Contributor(
        username="alice",
        repo_owner="owner",
        repo_name="repo",
        trust_tier=TrustTier.KNOWN,
    )
    cid = db.upsert_contributor(c)
    assert cid > 0

    got = db.get_contributor("owner", "repo", "alice")
    assert got is not None
    assert got.username == "alice"
    assert got.trust_tier == TrustTier.KNOWN


def test_upsert_updates_existing(db):
    c = Contributor(
        username="bob",
        repo_owner="owner",
        repo_name="repo",
        trust_tier=TrustTier.UNKNOWN,
    )
    id1 = db.upsert_contributor(c)

    c.trust_tier = TrustTier.KNOWN
    c.latest_score = 85.0
    id2 = db.upsert_contributor(c)
    assert id1 == id2

    got = db.get_contributor("owner", "repo", "bob")
    assert got.trust_tier == TrustTier.KNOWN
    assert got.latest_score == 85.0


def test_record_score(db):
    c = Contributor(
        username="charlie",
        repo_owner="owner",
        repo_name="repo",
        trust_tier=TrustTier.UNKNOWN,
    )
    cid = db.upsert_contributor(c)

    score = ScoreResult(
        total_score=72.5,
        confidence=0.85,
        signals=[
            SignalResult("account_age", 365, 1.0, 0.15),
            SignalResult("public_repos", 10, 0.5, 0.10),
        ],
        outcome=Outcome.ALLOW,
    )
    sid = db.record_score(cid, score, pr_number=42)
    assert sid > 0


def test_record_decision_and_history(db):
    c = Contributor(
        username="dave",
        repo_owner="owner",
        repo_name="repo",
        trust_tier=TrustTier.UNKNOWN,
    )
    cid = db.upsert_contributor(c)

    score = ScoreResult(total_score=55.0, confidence=0.7, outcome=Outcome.REVIEW)
    sid = db.record_score(cid, score, pr_number=10)

    decision = Decision(
        contributor=c,
        trust_tier=TrustTier.UNKNOWN,
        outcome=Outcome.REVIEW,
        reason="Score: 55.0",
        score_result=score,
        pr_number=10,
    )
    db.record_decision(cid, decision, sid)

    history = db.get_history("owner", "repo", "dave")
    assert len(history) == 1
    assert history[0]["outcome"] == "review"
    assert history[0]["total_score"] == 55.0


def test_cache_set_and_get(db):
    db.set_cached("test-key", '{"foo": "bar"}', "2099-01-01 00:00:00", etag="abc")
    cached = db.get_cached("test-key")
    assert cached is not None
    assert cached["data"] == {"foo": "bar"}
    assert cached["etag"] == "abc"


def test_cache_expired(db):
    db.set_cached("old-key", '{}', "2000-01-01 00:00:00")
    assert db.get_cached("old-key") is None


def test_get_stats(db):
    c = Contributor(
        username="eve",
        repo_owner="owner",
        repo_name="repo",
        trust_tier=TrustTier.KNOWN,
    )
    db.upsert_contributor(c)
    stats = db.get_stats("owner", "repo")
    assert stats["known"] == 1


def test_contributor_not_found(db):
    assert db.get_contributor("x", "y", "nonexistent") is None
