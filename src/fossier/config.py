"""Configuration loading: fossier.toml + environment + CLI overrides."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_PATHS = [
    "fossier.toml",
    ".github/fossier.toml",
]

# Default signal weights (auto-normalized to sum to 1.0)
DEFAULT_WEIGHTS: dict[str, float] = {
    "account_age": 0.09,
    "public_repos": 0.05,
    "contribution_history": 0.05,
    "open_prs_elsewhere": 0.09,
    "closed_prs_elsewhere": 0.10,
    "merged_prs_elsewhere": 0.08,
    "prior_interaction": 0.08,
    "activity_velocity": 0.08,
    "pr_content": 0.08,
    "follower_ratio": 0.05,
    "bot_signals": 0.04,
    "commit_email": 0.04,
    "pr_description": 0.05,
    "repo_stars": 0.04,
    "org_membership": 0.03,
    "commit_verification": 0.04,
    "contributor_stars": 0.04,
}


@dataclass
class ThresholdConfig:
    allow_score: float = 70.0
    deny_score: float = 40.0
    min_confidence: float = 0.5


@dataclass
class DenyActionConfig:
    close_pr: bool = True
    comment: bool = True
    label: str = "fossier:spam-likely"
    contact_url: str = ""


@dataclass
class ReviewActionConfig:
    comment: bool = True
    label: str = "fossier:needs-review"


@dataclass
class AllowActionConfig:
    label: str = ""  # empty = no label; set to e.g. "fossier:verified"
    comment: bool = False


@dataclass
class CacheTTLConfig:
    user_profile_hours: int = 24
    search_hours: int = 1
    collaborators_hours: int = 6


@dataclass
class Config:
    repo_owner: str = ""
    repo_name: str = ""
    repo_root: Path = field(default_factory=lambda: Path("."))
    db_path: str = ".fossier.db"
    github_token: str = ""

    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    signal_weights: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_WEIGHTS)
    )
    deny_action: DenyActionConfig = field(default_factory=DenyActionConfig)
    review_action: ReviewActionConfig = field(default_factory=ReviewActionConfig)
    allow_action: AllowActionConfig = field(default_factory=AllowActionConfig)
    cache_ttl: CacheTTLConfig = field(default_factory=CacheTTLConfig)

    trusted_users: set[str] = field(default_factory=set)
    blocked_users: set[str] = field(default_factory=set)
    trusted_orgs: set[str] = field(default_factory=set)
    bot_policy: str = "score"  # "score" (default), "allow", or "block"
    reject_ai_authored: bool = False  # auto-deny PRs with AI co-authored commits

    # Label applied by /fossier approve and /fossier vouch. When present on a PR,
    # the pipeline treats it as a hard override and skips auto-close/review
    # actions on subsequent runs (e.g. when new commits are pushed).
    # Set to "" to disable.
    manual_approval_label: str = "fossier:approved"

    registry_url: str = ""
    registry_api_key: str = ""
    registry_report_denials: bool = False
    registry_check_before_scoring: bool = False
    registry_block_threshold: int = 3  # reports needed to auto-block via registry

    flood_threshold: int = (
        3  # PRs/issues from same unknown user within window = spam flood
    )
    flood_window_hours: int = 1  # time window for flood detection

    verbose: bool = False
    dry_run: bool = False
    output_format: str = "text"  # text, json, table


def load_config(
    repo_root: Path | None = None,
    cli_overrides: dict | None = None,
) -> Config:
    """Load config from fossier.toml + env vars + CLI overrides."""
    # Auto-detect git repo root if not explicitly provided
    git_root = repo_root or _detect_git_root()
    root = git_root or Path(".")
    config = Config(repo_root=root)

    # Auto-detect owner/name from git remote
    if git_root:
        owner, name = _parse_git_remote(git_root)
        if owner and name:
            config.repo_owner = owner
            config.repo_name = name
            logger.debug("Detected repo from git: %s/%s", owner, name)

    # Anchor db_path to repo root
    if git_root and not os.path.isabs(config.db_path):
        config.db_path = str(git_root / config.db_path)

    # Load TOML config file
    for rel_path in _CONFIG_PATHS:
        path = root / rel_path
        if path.is_file():
            logger.debug("Loading config from %s", path)
            _apply_toml(config, path)
            break

    # Environment overrides
    _apply_env(config)

    # CLI overrides
    if cli_overrides:
        _apply_cli(config, cli_overrides)

    # Normalize weights to sum to 1.0
    _normalize_weights(config)

    return config


def _apply_toml(config: Config, path: Path) -> None:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    if "repo" in data:
        repo = data["repo"]
        if "owner" in repo:
            config.repo_owner = repo["owner"]
        if "name" in repo:
            config.repo_name = repo["name"]
        if "db_path" in repo:
            config.db_path = repo["db_path"]

    if "thresholds" in data:
        t = data["thresholds"]
        if "allow_score" in t:
            config.thresholds.allow_score = float(t["allow_score"])
        if "deny_score" in t:
            config.thresholds.deny_score = float(t["deny_score"])
        if "min_confidence" in t:
            config.thresholds.min_confidence = float(t["min_confidence"])

    if "weights" in data:
        for signal, weight in data["weights"].items():
            config.signal_weights[signal] = float(weight)

    if "actions" in data:
        actions = data["actions"]
        if "deny" in actions:
            d = actions["deny"]
            if "close_pr" in d:
                config.deny_action.close_pr = bool(d["close_pr"])
            if "comment" in d:
                config.deny_action.comment = bool(d["comment"])
            if "label" in d:
                config.deny_action.label = str(d["label"])
            if "contact_url" in d:
                config.deny_action.contact_url = str(d["contact_url"])
        if "review" in actions:
            r = actions["review"]
            if "comment" in r:
                config.review_action.comment = bool(r["comment"])
            if "label" in r:
                config.review_action.label = str(r["label"])
        if "allow" in actions:
            a = actions["allow"]
            if "label" in a:
                config.allow_action.label = str(a["label"])
            if "comment" in a:
                config.allow_action.comment = bool(a["comment"])

    if "cache_ttl" in data:
        c = data["cache_ttl"]
        if "user_profile_hours" in c:
            config.cache_ttl.user_profile_hours = int(c["user_profile_hours"])
        if "search_hours" in c:
            config.cache_ttl.search_hours = int(c["search_hours"])
        if "collaborators_hours" in c:
            config.cache_ttl.collaborators_hours = int(c["collaborators_hours"])

    if "trust" in data:
        trust = data["trust"]
        if "trusted_users" in trust:
            config.trusted_users = {u.lower() for u in trust["trusted_users"]}
        if "blocked_users" in trust:
            config.blocked_users = {u.lower() for u in trust["blocked_users"]}
        if "trusted_orgs" in trust:
            config.trusted_orgs = {o.lower() for o in trust["trusted_orgs"]}
        if "bot_policy" in trust:
            config.bot_policy = str(trust["bot_policy"])
        if "reject_ai_authored" in trust:
            config.reject_ai_authored = bool(trust["reject_ai_authored"])
        if "flood_threshold" in trust:
            config.flood_threshold = int(trust["flood_threshold"])
        if "flood_window_hours" in trust:
            config.flood_window_hours = int(trust["flood_window_hours"])
        if "manual_approval_label" in trust:
            config.manual_approval_label = str(trust["manual_approval_label"])

    if "registry" in data:
        reg = data["registry"]
        if "url" in reg:
            config.registry_url = str(reg["url"])
        if "api_key" in reg:
            config.registry_api_key = str(reg["api_key"])
            logger.warning(
                "Warning: Registry API key found and loaded from config file in plain text, prefer FOSSIER_REGISTRY_API_KEY"
            )
        if "report_denials" in reg:
            config.registry_report_denials = bool(reg["report_denials"])
        if "check_before_scoring" in reg:
            config.registry_check_before_scoring = bool(reg["check_before_scoring"])
        if "block_threshold" in reg:
            config.registry_block_threshold = int(reg["block_threshold"])


def _apply_env(config: Config) -> None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    if token:
        config.github_token = token

    contact_url = os.environ.get("FOSSIER_CONTACT_URL", "")
    if contact_url:
        config.deny_action.contact_url = contact_url

    registry_url = os.environ.get("FOSSIER_REGISTRY_URL", "")
    if registry_url:
        config.registry_url = registry_url
    registry_key = os.environ.get("FOSSIER_REGISTRY_API_KEY", "")
    if registry_key:
        config.registry_api_key = registry_key

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        if not config.repo_owner:
            config.repo_owner = owner
        if not config.repo_name:
            config.repo_name = name


def _apply_cli(config: Config, overrides: dict) -> None:
    if "repo" in overrides and overrides["repo"]:
        parts = overrides["repo"].split("/", 1)
        if len(parts) == 2:
            config.repo_owner, config.repo_name = parts

    if overrides.get("verbose"):
        config.verbose = True
    if overrides.get("dry_run"):
        config.dry_run = True
    if overrides.get("format"):
        config.output_format = overrides["format"]
    if overrides.get("db_path"):
        config.db_path = overrides["db_path"]


def _normalize_weights(config: Config) -> None:
    total = sum(config.signal_weights.values())
    if total > 0 and abs(total - 1.0) > 0.01:
        for signal in config.signal_weights:
            config.signal_weights[signal] /= total


# Matches GitHub remote URLs:
#   git@github.com:owner/repo.git
#   https://github.com/owner/repo.git
#   https://github.com/owner/repo
_GIT_REMOTE_RE = re.compile(r"(?:github\.com[:/])([^/]+)/([^/\s]+?)(?:\.git)?$")


def _detect_git_root() -> Path | None:
    """Find the git repo root from the current directory."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _parse_git_remote(repo_root: Path) -> tuple[str, str]:
    """Extract (owner, name) from the git remote origin URL."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            m = _GIT_REMOTE_RE.search(result.stdout.strip())
            if m:
                return m.group(1), m.group(2)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "", ""
