"""CLI interface: argparse command dispatch."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from fossier.config import load_config
from fossier.db import Database
from fossier.github_api import GitHubAPI
from fossier.models import Contributor, Decision, Outcome, TrustTier
from fossier.outcomes import execute_outcome, format_decision_json, format_decision_text
from fossier.scoring import score_contributor
from fossier.trust import resolve_tier
from fossier.trustdown import add_denounce, add_vouch

logger = logging.getLogger(__name__)

# Exit codes
EXIT_ALLOW = 0
EXIT_DENY = 1
EXIT_REVIEW = 2
EXIT_ERROR = 3


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Setup logging
    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
    )

    if not hasattr(args, "func"):
        parser.print_help()
        return EXIT_ERROR

    try:
        return args.func(args)
    except KeyboardInterrupt:
        return EXIT_ERROR
    except Exception as e:
        logger.error("Error: %s", e)
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        return EXIT_ERROR


def _build_parser() -> argparse.ArgumentParser:
    # Common flags inherited by all subcommands
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )
    common.add_argument(
        "--format", "-f", choices=["text", "json", "table"], default="text"
    )
    common.add_argument("--dry-run", action="store_true", help="Don't execute actions")
    common.add_argument("--repo", "-r", help="Repository as owner/repo")
    common.add_argument("--db-path", help="Path to database file")

    parser = argparse.ArgumentParser(
        prog="fossier",
        description="GitHub spam prevention for open source repositories",
        parents=[common],
    )

    sub = parser.add_subparsers(title="commands")

    # check
    p_check = sub.add_parser("check", parents=[common], help="Full evaluation pipeline")
    p_check.add_argument("username", help="GitHub username to evaluate")
    p_check.add_argument("--pr", type=int, help="PR number for content analysis")
    p_check.set_defaults(func=_cmd_check)

    # score
    p_score = sub.add_parser("score", parents=[common], help="Scoring only (debug)")
    p_score.add_argument("username", help="GitHub username to score")
    p_score.add_argument("--pr", type=int, help="PR number for content analysis")
    p_score.set_defaults(func=_cmd_score)

    # tier
    p_tier = sub.add_parser("tier", parents=[common], help="Tier resolution only")
    p_tier.add_argument("username", help="GitHub username to check")
    p_tier.set_defaults(func=_cmd_tier)

    # history
    p_history = sub.add_parser("history", parents=[common], help="Score/decision history from DB")
    p_history.add_argument("username", help="GitHub username")
    p_history.set_defaults(func=_cmd_history)

    # vouch
    p_vouch = sub.add_parser("vouch", parents=[common], help="Add user to VOUCHED.td")
    p_vouch.add_argument("username", help="GitHub username to vouch for")
    p_vouch.add_argument("--reason", "-m", default="", help="Reason for vouching")
    p_vouch.set_defaults(func=_cmd_vouch)

    # denounce
    p_denounce = sub.add_parser("denounce", parents=[common], help="Denounce user in VOUCHED.td")
    p_denounce.add_argument("username", help="GitHub username to denounce")
    p_denounce.add_argument(
        "--reason", "-m", required=True, help="Reason for denouncement"
    )
    p_denounce.set_defaults(func=_cmd_denounce)

    # db subcommands
    p_db = sub.add_parser("db", parents=[common], help="Database operations")
    db_sub = p_db.add_subparsers(title="db commands")

    p_migrate = db_sub.add_parser("migrate", parents=[common], help="Run schema migrations")
    p_migrate.set_defaults(func=_cmd_db_migrate)

    p_stats = db_sub.add_parser("stats", parents=[common], help="Show contributor/decision counts")
    p_stats.set_defaults(func=_cmd_db_stats)

    return parser


def _get_config(args: argparse.Namespace) -> dict:
    return {
        "repo": getattr(args, "repo", None),
        "verbose": getattr(args, "verbose", False),
        "dry_run": getattr(args, "dry_run", False),
        "format": getattr(args, "format", "text"),
        "db_path": getattr(args, "db_path", None),
    }


def _cmd_check(args: argparse.Namespace) -> int:
    """Full evaluation pipeline: tier → score (if needed) → outcome → actions."""
    config = load_config(cli_overrides=_get_config(args))
    db = Database(config.db_path)
    db.connect()
    api = GitHubAPI(config, db)

    try:
        username = args.username.lower()

        # Resolve trust tier
        tier, reason = resolve_tier(username, config, db, api)
        contributor = Contributor(
            username=username,
            repo_owner=config.repo_owner,
            repo_name=config.repo_name,
            trust_tier=tier,
        )

        score_result = None

        if tier == TrustTier.BLOCKED:
            outcome = Outcome.DENY
            contributor.blocked_reason = reason
        elif tier in (TrustTier.TRUSTED, TrustTier.KNOWN):
            outcome = Outcome.ALLOW
        else:
            # Unknown → run scoring
            score_result = score_contributor(api, config, username, args.pr)
            outcome = score_result.outcome
            contributor.latest_score = score_result.total_score
            if outcome == Outcome.ALLOW:
                contributor.trust_tier = TrustTier.KNOWN

        decision = Decision(
            contributor=contributor,
            trust_tier=tier,
            outcome=outcome,
            reason=reason
            if tier != TrustTier.UNKNOWN
            else f"Score: {score_result.total_score}"
            if score_result
            else reason,
            score_result=score_result,
            pr_number=args.pr,
        )

        # Record in DB
        contributor_id = db.upsert_contributor(contributor)
        score_history_id = None
        if score_result:
            score_history_id = db.record_score(contributor_id, score_result, args.pr)
        db.record_decision(contributor_id, decision, score_history_id)

        # Execute outcome actions
        execute_outcome(decision, config, api)

        # Output
        _output_decision(decision, config)

        return _outcome_exit_code(outcome)
    finally:
        api.close()
        db.close()


def _cmd_score(args: argparse.Namespace) -> int:
    """Score only (debug command)."""
    config = load_config(cli_overrides=_get_config(args))
    db = Database(config.db_path)
    db.connect()
    api = GitHubAPI(config, db)

    try:
        score_result = score_contributor(api, config, args.username.lower(), args.pr)

        if config.output_format == "json":
            print(
                json.dumps(
                    {
                        "username": args.username.lower(),
                        "total_score": score_result.total_score,
                        "confidence": score_result.confidence,
                        "outcome": score_result.outcome.value,
                        "signals": score_result.signal_breakdown,
                    },
                    indent=2,
                )
            )
        else:
            print(
                f"Score for {args.username}: {score_result.total_score}/100 "
                f"(confidence: {score_result.confidence:.0%})"
            )
            print(f"Outcome: {score_result.outcome.value.upper()}")
            print()
            for s in score_result.signals:
                if s.success:
                    print(
                        f"  {s.name:25s} {s.normalized:.2f}  (raw: {s.raw_value}, weight: {s.weight:.2f})"
                    )
                else:
                    print(f"  {s.name:25s} FAILED  ({s.error})")

        return _outcome_exit_code(score_result.outcome)
    finally:
        api.close()
        db.close()


def _cmd_tier(args: argparse.Namespace) -> int:
    """Tier resolution only."""
    config = load_config(cli_overrides=_get_config(args))
    db = Database(config.db_path)
    db.connect()
    api = GitHubAPI(config, db)

    try:
        tier, reason = resolve_tier(args.username.lower(), config, db, api)

        if config.output_format == "json":
            print(
                json.dumps(
                    {
                        "username": args.username.lower(),
                        "tier": tier.value,
                        "reason": reason,
                    }
                )
            )
        else:
            print(f"User:   {args.username}")
            print(f"Tier:   {tier.value}")
            print(f"Reason: {reason}")

        return EXIT_ALLOW
    finally:
        api.close()
        db.close()


def _cmd_history(args: argparse.Namespace) -> int:
    """Show decision/score history from DB."""
    config = load_config(cli_overrides=_get_config(args))
    db = Database(config.db_path)
    db.connect()

    try:
        history = db.get_history(
            config.repo_owner, config.repo_name, args.username.lower()
        )

        if not history:
            print(f"No history found for {args.username}")
            return EXIT_ALLOW

        if config.output_format == "json":
            print(json.dumps(history, indent=2))
        else:
            for entry in history:
                print(
                    f"[{entry['decided_at']}] "
                    f"tier={entry['trust_tier']} "
                    f"outcome={entry['outcome']} "
                    f"score={entry.get('total_score', 'N/A')} "
                    f"pr=#{entry.get('pr_number', 'N/A')}"
                )
                print(f"  Reason: {entry['reason']}")
                print()

        return EXIT_ALLOW
    finally:
        db.close()


def _cmd_vouch(args: argparse.Namespace) -> int:
    """Add vouch to VOUCHED.td."""
    config = load_config(cli_overrides=_get_config(args))
    path = add_vouch(config.repo_root, args.username)
    print(f"Vouched for {args.username} in {path}")
    return EXIT_ALLOW


def _cmd_denounce(args: argparse.Namespace) -> int:
    """Add denouncement to VOUCHED.td."""
    config = load_config(cli_overrides=_get_config(args))
    path = add_denounce(config.repo_root, args.username, args.reason)
    print(f"Denounced {args.username} in {path}")
    return EXIT_ALLOW


def _cmd_db_migrate(args: argparse.Namespace) -> int:
    """Run schema migrations."""
    config = load_config(cli_overrides=_get_config(args))
    db = Database(config.db_path)
    db.connect()
    print("Database migrations complete")
    db.close()
    return EXIT_ALLOW


def _cmd_db_stats(args: argparse.Namespace) -> int:
    """Show DB stats."""
    config = load_config(cli_overrides=_get_config(args))
    db = Database(config.db_path)
    db.connect()

    try:
        stats = db.get_stats(config.repo_owner, config.repo_name)

        if config.output_format == "json":
            print(json.dumps(stats, indent=2))
        else:
            print("Contributors by tier:")
            for tier in TrustTier:
                print(f"  {tier.value:10s} {stats.get(tier.value, 0)}")
            print()
            print("Decisions by outcome:")
            for outcome in Outcome:
                print(
                    f"  {outcome.value:10s} {stats.get(f'decisions_{outcome.value}', 0)}"
                )

        return EXIT_ALLOW
    finally:
        db.close()


def _output_decision(decision: Decision, config) -> None:
    if config.output_format == "json":
        print(json.dumps(format_decision_json(decision), indent=2))
    else:
        print(format_decision_text(decision))


def _outcome_exit_code(outcome: Outcome) -> int:
    return {
        Outcome.ALLOW: EXIT_ALLOW,
        Outcome.DENY: EXIT_DENY,
        Outcome.REVIEW: EXIT_REVIEW,
    }[outcome]
