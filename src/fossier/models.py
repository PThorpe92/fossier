"""Core data models for fossier."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TrustTier(str, Enum):
    BLOCKED = "blocked"
    TRUSTED = "trusted"
    KNOWN = "known"
    UNKNOWN = "unknown"


class Outcome(str, Enum):
    ALLOW = "allow"
    REVIEW = "review"
    DENY = "deny"


@dataclass
class Contributor:
    username: str
    repo_owner: str
    repo_name: str
    platform: str = "github"
    trust_tier: TrustTier = TrustTier.UNKNOWN
    latest_score: float | None = None
    blocked_reason: str | None = None
    db_id: int | None = None


@dataclass
class SignalResult:
    name: str
    raw_value: float | str | bool
    normalized: float  # 0.0-1.0, where 1.0 = trustworthy
    weight: float
    success: bool = True
    error: str | None = None


@dataclass
class ScoreResult:
    total_score: float  # 0-100
    confidence: float  # 0.0-1.0
    signals: list[SignalResult] = field(default_factory=list)
    outcome: Outcome = Outcome.REVIEW

    @property
    def signal_breakdown(self) -> dict:
        return {
            s.name: {
                "raw": s.raw_value,
                "normalized": round(s.normalized, 3),
                "weight": round(s.weight, 3),
                "success": s.success,
                "error": s.error,
            }
            for s in self.signals
        }


@dataclass
class Decision:
    contributor: Contributor
    trust_tier: TrustTier
    outcome: Outcome
    reason: str
    score_result: ScoreResult | None = None
    pr_number: int | None = None
    db_id: int | None = None
