from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from fossier.config import load_config
from fossier.db import Database
from fossier.github_api import GitHubAPI
from fossier.models import Decision, Outcome, TrustTier
from fossier.outcomes import execute_outcome, format_decision_json, format_decision_text
from fossier.pipeline import evaluate_contributor
from fossier.scoring import score_contributor
from fossier.trust import TrustResolver
from fossier.trustdown import add_denounce, add_vouch, parse_vouched

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
    # Silence httpx request logging unless verbose
    if not getattr(args, "verbose", False):
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

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
    p_history = sub.add_parser(
        "history", parents=[common], help="Score/decision history from DB"
    )
    p_history.add_argument("username", help="GitHub username")
    p_history.set_defaults(func=_cmd_history)

    # vouch
    p_vouch = sub.add_parser("vouch", parents=[common], help="Add user to VOUCHED.td")
    p_vouch.add_argument("username", help="GitHub username to vouch for")
    p_vouch.add_argument("--reason", "-m", default="", help="Reason for vouching")
    p_vouch.set_defaults(func=_cmd_vouch)

    # denounce
    p_denounce = sub.add_parser(
        "denounce", parents=[common], help="Denounce user in VOUCHED.td"
    )
    p_denounce.add_argument("username", help="GitHub username to denounce")
    p_denounce.add_argument(
        "--reason", "-m", required=True, help="Reason for denouncement"
    )
    p_denounce.set_defaults(func=_cmd_denounce)

    # reject
    p_reject = sub.add_parser(
        "reject",
        parents=[common],
        help="Reject a contributor: denounce locally and report to the global registry",
    )
    p_reject.add_argument("username", help="GitHub username to reject")
    p_reject.add_argument("--reason", "-m", required=True, help="Reason for rejection")
    p_reject.add_argument("--pr", type=int, help="Associated PR number (optional)")
    p_reject.set_defaults(func=_cmd_reject)

    # vouch-all
    p_vouch_all = sub.add_parser(
        "vouch-all",
        parents=[common],
        help="Vouch for all existing repo contributors",
    )
    p_vouch_all.add_argument(
        "--limit", "-n", type=int, default=0,
        help="Only vouch for the top N contributors (by commit count). 0 = all",
    )
    p_vouch_all.set_defaults(func=_cmd_vouch_all)

    # init
    p_init = sub.add_parser(
        "init", parents=[common], help="Initialize fossier config and files"
    )
    p_init.set_defaults(func=_cmd_init)

    # scan
    p_scan = sub.add_parser("scan", parents=[common], help="Bulk-evaluate all open PRs")
    p_scan.add_argument(
        "--execute",
        action="store_true",
        help="Execute outcome actions (comment/label/close) for each PR",
    )
    p_scan.set_defaults(func=_cmd_scan)

    # report
    p_report = sub.add_parser(
        "report", parents=[common], help="Show spam rates and signal distributions"
    )
    p_report.add_argument(
        "--days", type=int, default=30, help="Time window in days (default: 30)"
    )
    p_report.set_defaults(func=_cmd_report)

    # db subcommands
    p_db = sub.add_parser("db", parents=[common], help="Database operations")
    db_sub = p_db.add_subparsers(title="db commands")

    p_migrate = db_sub.add_parser(
        "migrate", parents=[common], help="Run schema migrations"
    )
    p_migrate.set_defaults(func=_cmd_db_migrate)

    p_stats = db_sub.add_parser(
        "stats", parents=[common], help="Show contributor/decision counts"
    )
    p_stats.set_defaults(func=_cmd_db_stats)

    p_prune = db_sub.add_parser(
        "prune", parents=[common], help="Remove expired cache entries"
    )
    p_prune.set_defaults(func=_cmd_db_prune)

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
    api.validate_token()
    resolver = TrustResolver(config, db, api)

    try:
        decision = evaluate_contributor(args.username, resolver, pr_number=args.pr)

        # Execute outcome actions
        execute_outcome(decision, config, api)

        # Output
        _output_decision(decision, config)

        return _outcome_exit_code(decision.outcome)
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
    username = args.username.lower()

    try:
        resolver = TrustResolver(config, db, api)
        tier, reason = resolver.resolve_tier(username)

        if config.output_format == "json":
            print(
                json.dumps(
                    {
                        "username": username,
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


def _cmd_reject(args: argparse.Namespace) -> int:
    """Reject a contributor: denounce locally and report to the global registry."""
    config = load_config(cli_overrides=_get_config(args))
    username = args.username.lower()

    # 1. Denounce locally in VOUCHED.td
    path = add_denounce(config.repo_root, username, args.reason)
    print(f"Denounced {username} in {path}")

    # 2. Report to global registry if configured
    if config.registry_url and config.registry_api_key:
        from fossier.registry_client import RegistryClient

        reg = RegistryClient(config.registry_url, config.registry_api_key)
        try:
            success = reg.report_spam(
                username=username,
                repo_owner=config.repo_owner,
                repo_name=config.repo_name,
                score=0.0,
                reason=f"Manual rejection: {args.reason}",
                pr_number=getattr(args, "pr", None),
            )
            if success:
                print(f"Reported {username} to the global registry")
            else:
                print("Warning: failed to report to the global registry")
        finally:
            reg.close()
    else:
        print(
            "Tip: configure [registry] in fossier.toml to also report "
            "rejections to the global registry"
        )

    return EXIT_ALLOW


def _cmd_vouch_all(args: argparse.Namespace) -> int:
    """Vouch for all existing contributors to the repository."""
    config = load_config(cli_overrides=_get_config(args))
    db = Database(config.db_path)
    db.connect()
    api = GitHubAPI(config, db)

    try:
        if not config.repo_owner or not config.repo_name:
            logger.error(
                "Repository not configured. Use --repo owner/repo or set up fossier.toml"
            )
            return EXIT_ERROR

        contributors = api.get_contributors(config.repo_owner, config.repo_name)
        if not contributors:
            print("No contributors found for this repository")
            return EXIT_ALLOW

        limit = getattr(args, "limit", 0)
        if limit > 0:
            contributors = contributors[:limit]

        existing = parse_vouched(config.repo_root)
        added = 0
        for username in contributors:
            if username in existing.vouched:
                continue
            if config.dry_run:
                print(f"  Would vouch for {username}")
            else:
                add_vouch(config.repo_root, username)
            added += 1

        already = len(contributors) - added
        if config.dry_run:
            print(f"\nDry run: would vouch for {added} new contributor(s) ({already} already vouched)")
        else:
            print(f"Vouched for {added} new contributor(s) ({already} already vouched)")
        return EXIT_ALLOW
    finally:
        api.close()
        db.close()


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


def _cmd_db_prune(args: argparse.Namespace) -> int:
    """Remove expired cache entries."""
    config = load_config(cli_overrides=_get_config(args))
    db = Database(config.db_path)
    db.connect()

    try:
        removed = db.prune_cache()
        print(f"Pruned {removed} expired cache entries")
        return EXIT_ALLOW
    finally:
        db.close()


def _cmd_report(args: argparse.Namespace) -> int:
    """Show spam rates, decision stats, and signal distributions."""
    config = load_config(cli_overrides=_get_config(args))
    db = Database(config.db_path)
    db.connect()

    try:
        if not config.repo_owner or not config.repo_name:
            logger.error(
                "Repository not configured. Use --repo owner/repo or set up fossier.toml"
            )
            return EXIT_ERROR

        days = getattr(args, "days", 30)
        stats = db.get_report_stats(config.repo_owner, config.repo_name, days)

        if config.output_format == "json":
            print(json.dumps(stats, indent=2))
        else:
            _print_report_text(stats)

        return EXIT_ALLOW
    finally:
        db.close()


def _print_report_text(stats: dict) -> None:
    """Format and print the report in human-readable text."""
    days = stats.get("days", 30)
    print(f"Fossier Report (last {days} days)")
    print("=" * 40)
    print()

    # Contributors
    print(f"Total contributors: {stats.get('total_contributors', 0)}")
    tiers = stats.get("contributors_by_tier", {})
    for tier, count in tiers.items():
        print(f"  {tier:10s} {count}")
    print()

    # Decisions
    total = stats.get("total_decisions", 0)
    print(f"Total decisions: {total}")
    outcomes = stats.get("decisions_by_outcome", {})
    for outcome, count in outcomes.items():
        pct = f"({count / total * 100:.0f}%)" if total > 0 else ""
        print(f"  {outcome:10s} {count:4d}  {pct}")
    print()

    # Spam rate
    print(f"Spam rate: {stats.get('spam_rate', 0):.1f}%")
    print()

    # Average scores
    avg = stats.get("avg_score_by_outcome", {})
    if any(v is not None for v in avg.values()):
        print("Average scores by outcome:")
        for outcome, score in avg.items():
            if score is not None:
                print(f"  {outcome:10s} {score:.1f}")
        print()

    # Top denied
    top_denied = stats.get("top_denied_users", [])
    if top_denied:
        print("Top denied users:")
        for entry in top_denied:
            print(f"  @{entry['username']:20s} {entry['deny_count']} denials")
        print()

    # Recent decisions
    recent = stats.get("recent_decisions", [])
    if recent:
        print("Recent decisions:")
        for d in recent:
            score_str = f"score={d['score']:.1f}" if d["score"] is not None else ""
            print(
                f"  [{d['decided_at']}] @{d['username']:15s} "
                f"{d['outcome']:6s} {score_str}"
            )


def _cmd_init(args: argparse.Namespace) -> int:
    """Interactive setup: create fossier.toml and VOUCHED.td."""
    config = load_config(cli_overrides=_get_config(args))
    root = config.repo_root

    # Create fossier.toml
    toml_path = root / "fossier.toml"
    if toml_path.exists():
        print(f"fossier.toml already exists at {toml_path}")
    else:
        owner = config.repo_owner or "your-org"
        name = config.repo_name or "your-repo"
        toml_path.write_text(
            f'[repo]\nowner = "{owner}"\nname = "{name}"\n\n'
            "[thresholds]\nallow_score = 70.0\ndeny_score = 40.0\n"
            "min_confidence = 0.5\n\n"
            "[actions.deny]\nclose_pr = true\ncomment = true\n"
            'label = "fossier:spam-likely"\n\n'
            "[actions.review]\ncomment = true\n"
            'label = "fossier:needs-review"\n'
        )
        print(f"Created {toml_path}")

    # Create VOUCHED.td
    td_path = root / "VOUCHED.td"
    if td_path.exists():
        print(f"VOUCHED.td already exists at {td_path}")
    else:
        td_path.write_text(
            "# VOUCHED.td: trust declarations\n"
            "# Lines starting with + vouch for a user\n"
            "# Lines starting with - denounce a user (reason required)\n"
            "#\n"
            "# Examples:\n"
            "# + trusteduser\n"
            "# - spammer  Known SEO link spam\n"
        )
        print(f"Created {td_path}")

    # Optionally create GitHub Action workflow
    workflow_dir = root / ".github" / "workflows"
    workflow_path = workflow_dir / "fossier.yml"
    if workflow_path.exists():
        print(f"Workflow already exists at {workflow_path}")
    else:
        workflow_dir.mkdir(parents=True, exist_ok=True)
        workflow_path.write_text(
            "name: Fossier PR Check\n\n"
            "on:\n"
            "  pull_request_target:\n"
            "    types: [opened, synchronize]\n\n"
            "permissions:\n"
            "  pull-requests: write\n"
            "  issues: write\n\n"
            "jobs:\n"
            "  check:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: PThorpe92/fossier@main\n"
            "        with:\n"
            "          github-token: ${{ secrets.GITHUB_TOKEN }}\n"
        )
        print(f"Created {workflow_path}")

    # Create scan workflow (workflow_dispatch)
    scan_path = workflow_dir / "fossier-scan.yml"
    if scan_path.exists():
        print(f"Scan workflow already exists at {scan_path}")
    else:
        workflow_dir.mkdir(parents=True, exist_ok=True)
        scan_path.write_text(
            "name: Fossier Scan All PRs\n\n"
            "on:\n"
            "  workflow_dispatch:\n"
            "    inputs:\n"
            "      dry-run:\n"
            '        description: "Dry run - evaluate but don\'t take actions"\n'
            "        type: boolean\n"
            "        default: false\n\n"
            "permissions:\n"
            "  contents: read\n"
            "  pull-requests: write\n"
            "  issues: write\n\n"
            "jobs:\n"
            "  scan:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n\n"
            "      - name: Set up uv\n"
            "        uses: astral-sh/setup-uv@v4\n\n"
            "      - name: Set up Python\n"
            "        uses: actions/setup-python@v5\n"
            "        with:\n"
            '          python-version: "3.13"\n\n'
            "      - name: Install fossier\n"
            "        run: uv pip install --system .\n\n"
            "      - name: Restore DB cache\n"
            "        uses: actions/cache@v4\n"
            "        with:\n"
            "          path: .fossier.db\n"
            "          key: fossier-db-${{ github.repository }}\n"
            "          restore-keys: |\n"
            "            fossier-db-${{ github.repository }}\n\n"
            "      - name: Scan open PRs\n"
            "        env:\n"
            "          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n"
            "        run: |\n"
            '          FLAGS="--format table"\n'
            '          if [ "${{ inputs.dry-run }}" = "true" ]; then\n'
            '            FLAGS="$FLAGS --dry-run"\n'
            "          else\n"
            '            FLAGS="$FLAGS --execute"\n'
            "          fi\n"
            "          fossier scan $FLAGS\n"
        )
        print(f"Created {scan_path}")

    # Run DB migration
    db = Database(config.db_path)
    db.connect()
    db.close()
    print("Database initialized")

    return EXIT_ALLOW


def _cmd_scan(args: argparse.Namespace) -> int:
    """Bulk-evaluate all open PRs on the repo."""
    config = load_config(cli_overrides=_get_config(args))
    db = Database(config.db_path)
    db.connect()
    api = GitHubAPI(config, db)
    should_execute = getattr(args, "execute", False)

    try:
        if not config.repo_owner or not config.repo_name:
            logger.error(
                "Repository not configured. Use --repo owner/repo or set up fossier.toml"
            )
            return EXIT_ERROR

        # Fetch all open PRs (paginated)
        all_prs: list[dict] = []
        page = 1
        while True:
            data = api.get(
                f"/repos/{config.repo_owner}/{config.repo_name}/pulls",
                params={"state": "open", "per_page": "100", "page": str(page)},
            )
            if not data or not isinstance(data, list):
                break
            all_prs.extend(data)
            if len(data) < 100:
                break
            page += 1

        if not all_prs:
            print("No open PRs found")
            return EXIT_ALLOW

        resolver = TrustResolver(config, db, api)
        results = []
        for pr_data in all_prs:
            pr_number = pr_data["number"]
            username = pr_data["user"]["login"].lower()

            decision = evaluate_contributor(username, resolver, pr_number)
            results.append(decision)

            if should_execute:
                execute_outcome(decision, config, api)

            if config.output_format == "text":
                outcome_str = _colorize_outcome(decision.outcome)
                print(
                    f"PR #{pr_number:4d}  @{username:20s}  "
                    f"{decision.trust_tier.value:8s}  {outcome_str}"
                )

        if config.output_format == "json":
            print(json.dumps([format_decision_json(d) for d in results], indent=2))
        elif config.output_format == "table":
            _print_decisions_table(results)

        print(f"\nScanned {len(results)} open PRs")
        return EXIT_ALLOW
    finally:
        api.close()
        db.close()


_ANSI_COLORS = {
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def _supports_color() -> bool:
    """Check if terminal supports ANSI colors."""
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty"):
        return False
    return sys.stdout.isatty()


def _colorize_outcome(outcome: Outcome) -> str:
    """Return outcome text with ANSI color if terminal supports it."""
    text = outcome.value.upper()
    if not _supports_color():
        return text
    color_map = {
        Outcome.ALLOW: _ANSI_COLORS["green"],
        Outcome.REVIEW: _ANSI_COLORS["yellow"],
        Outcome.DENY: _ANSI_COLORS["red"],
    }
    color = color_map.get(outcome, "")
    return f"{color}{_ANSI_COLORS['bold']}{text}{_ANSI_COLORS['reset']}"


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    """Format data as a simple ASCII table."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    lines = []
    header_line = " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    lines.append(header_line)
    lines.append("-+-".join("-" * w for w in widths))
    for row in rows:
        line = " | ".join(
            (row[i] if i < len(row) else "").ljust(widths[i])
            for i in range(len(headers))
        )
        lines.append(line)
    return "\n".join(lines)


def _print_decisions_table(decisions: list[Decision]) -> None:
    """Print decisions as an ASCII table."""
    headers = ["PR", "User", "Tier", "Outcome", "Score", "Reason"]
    rows = []
    for d in decisions:
        score = f"{d.score_result.total_score:.1f}" if d.score_result else "—"
        rows.append(
            [
                f"#{d.pr_number}" if d.pr_number else "—",
                d.contributor.username,
                d.trust_tier.value,
                d.outcome.value.upper(),
                score,
                d.reason[:40],
            ]
        )
    print(_format_table(headers, rows))


def _output_decision(decision: Decision, config) -> None:
    if config.output_format == "json":
        print(json.dumps(format_decision_json(decision), indent=2))
    elif config.output_format == "table":
        _print_decisions_table([decision])
    else:
        text = format_decision_text(decision)
        if _supports_color():
            for outcome in Outcome:
                plain = outcome.value.upper()
                colored = _colorize_outcome(outcome)
                text = text.replace(f"Outcome:  {plain}", f"Outcome:  {colored}")
        print(text)


def _outcome_exit_code(outcome: Outcome) -> int:
    return {
        Outcome.ALLOW: EXIT_ALLOW,
        Outcome.DENY: EXIT_DENY,
        Outcome.REVIEW: EXIT_REVIEW,
    }[outcome]
