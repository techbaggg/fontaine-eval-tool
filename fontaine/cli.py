from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fontaine.domain.gather import gather_repo_facts
from fontaine.env_config import apply_local_env_to_namespace
from fontaine.reporting import OutputFormat, write_report
from fontaine.run_log import configure_progress_logging


def _parse_pr_targets(raw: list[str] | None) -> list[int]:
    """Dedupe preserving first occurrence order."""
    if not raw:
        return []
    nums: list[int] = []
    for chunk in raw:
        for part in chunk.replace(",", " ").split():
            part = part.strip()
            if part:
                nums.append(int(part))
    seen: set[int] = set()
    out: list[int] = []
    for n in nums:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fontaine",
        description="Repo screening: acquisition summary on stdout; activity log to file by default.",
    )
    src = p.add_argument_group(
        "source",
        "Use --repo-dir for a local clone, or --owner and --repo for GitHub.",
    )
    src.add_argument(
        "--repo-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to a local git working tree of the **project under analysis** (for F2P "
        "and for inferring GitHub owner/repo from git remote when not set). Fontaine "
        "reads ``.local.env`` only from the Fontaine checkout; see FONTAINE_CONFIG_DIR.",
    )
    src.add_argument(
        "--owner",
        default=None,
        help="GitHub owner (remote). Omitted: from Fontaine ``.local.env`` or ``git remote`` on --repo-dir.",
    )
    src.add_argument(
        "--repo",
        default=None,
        help="GitHub repository name (remote). Omitted: from Fontaine ``.local.env`` or git remote on --repo-dir.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("-"),
        metavar="PATH",
        help="JSON output path when --format json (default: '-' = stdout).",
    )
    p.add_argument(
        "--merged-to-default-only",
        action="store_true",
        default=True,
        help="v1.0: only PRs merged into the default branch.",
    )
    p.add_argument(
        "--format",
        choices=("json", "human"),
        default="json",
        help="Output format (default: json; Fontaine ``.local.env`` may override). "
        "Human mode writes full detail only with --verbose.",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Emit detailed PR listing and metrics narrative to --human-detail (human format only).",
    )
    p.add_argument(
        "--human-detail",
        default="fontaine-detail.txt",
        metavar="PATH",
        help="Where to write the long human report when --verbose (default: fontaine-detail.txt). "
        "Use '-' to write detail to stderr.",
    )
    p.add_argument(
        "--activity-log",
        default="fontaine-activity.log",
        metavar="PATH",
        help="Progress/activity log file (default: fontaine-activity.log); TIMING lines "
        "(phase=duration_ms) are appended when phases complete. Use '-' for stderr only.",
    )
    p.add_argument(
        "--transaction-log",
        default=None,
        metavar="PATH",
        help="Optional log file for TIMING lines only (phase=… duration_ms=…) for offline analysis.",
    )
    p.add_argument(
        "--log-terminal",
        action="store_true",
        help="Also mirror activity lines to stderr (in addition to --activity-log).",
    )
    p.add_argument(
        "--metrics",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Include extended repository metrics (filesystem + git heuristics; GitHub mode uses full clone by default). "
        "Use --no-metrics to force off when enabled in Fontaine ``.local.env``.",
    )
    p.add_argument(
        "--api-clone-depth",
        type=int,
        default=1,
        metavar="N",
        help="History depth for LOC-only GitHub clones (default: 1).",
    )
    p.add_argument(
        "--pr-analysis",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="List PRs from GitHub GraphQL and apply Fontaine first-stage filters. "
        "Default listing: no states filter on the connection, CREATED_AT desc — see "
        "--pr-analysis-merged-only for merged-only. Use --no-pr-analysis to disable when set in Fontaine ``.local.env``.",
    )
    p.add_argument(
        "--pr-analysis-merged-only",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Use the GraphQL **merged** feed only: states=MERGED, UPDATED_AT desc (no open PRs). "
        "Omit (default) for the standard connection: all states, CREATED_AT desc — can mix in many "
        "open PRs. Set in Fontaine ``.local.env`` as ``PR_ANALYSIS_MERGED_ONLY=1``; use "
        "``--no-pr-analysis-merged-only`` to override the file.",
    )
    p.add_argument(
        "--pr-analysis-max-prs",
        type=int,
        default=500,
        metavar="N",
        help="Max PRs to scan from the listing (default: 500).",
    )
    p.add_argument(
        "--pr-analysis-lookback-days",
        type=int,
        default=730,
        metavar="N",
        help="Merged-time window in days (default: 730 ≈ 2 years). Bulk screening counts merged "
        "PRs with mergedAt in this window; open PRs are excluded from the scan cap when N>0 so "
        "lookback affects workload. Use 0 for no merged-time filter (open PRs may fill the cap). "
        "Omitted on the CLI: ``.local.env`` may set PR_ANALYSIS_LOOKBACK_DAYS / "
        "FONTAINE_PR_ANALYSIS_LOOKBACK_DAYS.",
    )
    p.add_argument(
        "--pr-target",
        dest="pr_targets_raw",
        action="append",
        metavar="N",
        help="Merged PR number(s) for direct F2P/P2P only (repeat flag or comma-separated). "
        "Skips bulk PR listing and first-stage filters. Implies --pr-analysis and --pr-f2p.",
    )
    p.add_argument(
        "--pr-f2p",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="After PR filters, run F2P/P2P on a local clone (--repo-dir). The runner is "
        "picked automatically (Jest/Vitest/Mocha vs pytest vs Maven) from the repo and changed files. "
        "JS: FONTAINE_F2P_SKIP_INSTALL=1, FONTAINE_F2P_NPM_SCRIPT, FONTAINE_F2P_JEST_RUN_IN_BAND, "
        "FONTAINE_F2P_MOCHA_ARGS. "
        "Python: FONTAINE_F2P_PYTHON, FONTAINE_F2P_SKIP_PIP_INSTALL, FONTAINE_F2P_SKIP_VENV, "
        "FONTAINE_F2P_PYTEST_ARGS; Poetry repos auto-use ``poetry install --with …`` when "
        "``poetry`` is on PATH (FONTAINE_F2P_USE_POETRY, FONTAINE_F2P_POETRY_GROUPS). "
        "Maven/Java: when host ``mvn``/``mvnw`` is missing, Fontaine runs ``mvn help:effective-pom`` "
        "(or uses host ``mvn``) to learn the JDK level, then ``docker pull`` + ``docker run`` the "
        "matching ``maven:3-eclipse-temurin-<N>``; override with FONTAINE_F2P_MAVEN_DOCKER_IMAGE / "
        "FONTAINE_F2P_MAVEN_JAVA_MAJOR; FONTAINE_F2P_MAVEN_SKIP_EFFECTIVE_POM=1 skips that step. "
        "FONTAINE_F2P_MAVEN_NO_DOCKER=1 or FONTAINE_F2P_MAVEN_USE_DOCKER=0 disables Docker. "
        "FONTAINE_F2P_DOCKER_PULL_MAVEN=1 forces ``docker pull`` of the Maven/JDK image when "
        "Fontaine runs ``mvn`` in Docker (host-only Maven is unchanged). "
        "FONTAINE_F2P_MAVEN_GOALS defaults to ``test``; ``clean`` is prepended when absent so "
        "gitignored ``target/`` does not leak Surefire output or classes between F2P stages. "
        "Use --no-pr-f2p to disable when set in Fontaine ``.local.env``.",
    )
    p.add_argument(
        "--pr-f2p-limit",
        type=int,
        default=30,
        metavar="N",
        help="Target number of passed PRs to run F2P/P2P on (default: 30; Fontaine ``.local.env`` "
        "may set PR_F2P_LIMIT). When there are more passes than N, draws a random sample "
        "biased toward medium-complexity PRs (bell curve on computed difficulty); when "
        "passes ≤ N, runs all passes in screening order (no sampling). Ignored when "
        "--pr-target is set (all listed PRs are run). Reproducibility: FONTAINE_F2P_SAMPLE_SEED.",
    )
    p.add_argument(
        "--pr-f2p-timeout",
        type=int,
        default=600,
        metavar="SEC",
        help="Per-stage test runner timeout in seconds (default: 600).",
    )
    p.add_argument(
        "--pr-f2p-merge-packages",
        action="store_true",
        help="Monorepo: discover package roots from changed **test** paths, run tests per "
        "package, merge with [path] prefixes (slower; closer to multi-package workflows).",
    )
    p.add_argument(
        "--pr-f2p-workers",
        type=int,
        default=1,
        metavar="N",
        help="Parallel F2P/P2P workers (each uses an isolated git clone via git clone --local). "
        "Default 1 (serial on your --repo-dir).",
    )
    p.add_argument(
        "--npm-cache-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Shared npm cache directory for installs (sets npm_config_cache). Recommended "
        "when using multiple workers.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    p = build_parser()
    args = p.parse_args(argv_list)
    apply_local_env_to_namespace(args, argv_list)

    tx = Path(args.transaction_log) if args.transaction_log else None
    if args.activity_log == "-":
        configure_progress_logging(
            activity_log=None,
            transaction_log=tx,
            log_terminal=True,
        )
    else:
        configure_progress_logging(
            activity_log=Path(args.activity_log),
            transaction_log=tx,
            log_terminal=args.log_terminal,
        )

    pr_targets = _parse_pr_targets(args.pr_targets_raw)
    if pr_targets:
        args.pr_analysis = True
        args.pr_f2p = True

    if args.pr_f2p_merge_packages and not args.pr_f2p:
        p.error("--pr-f2p-merge-packages requires --pr-f2p")
    if args.pr_f2p:
        if not args.pr_analysis:
            p.error("--pr-f2p requires --pr-analysis")
        if args.repo_dir is None:
            p.error("--pr-f2p requires --repo-dir")
        if args.owner is None or args.repo is None:
            p.error("--pr-f2p requires --owner and --repo")
    if args.pr_analysis:
        if args.owner is None or args.repo is None:
            p.error(
                "--pr-analysis requires GitHub owner and repo (use --owner/--repo, Fontaine "
                "``.local.env``, or --repo-dir with a github.com ``origin`` remote)"
            )
    elif args.owner is None or args.repo is None:
        if args.repo_dir is None:
            p.error("either --repo-dir or both --owner and --repo are required")

    if args.pr_f2p_workers < 1:
        p.error("--pr-f2p-workers must be >= 1")

    facts = gather_repo_facts(
        local_root=args.repo_dir,
        owner=args.owner,
        repo=args.repo,
        merged_to_default_only=args.merged_to_default_only,
        include_repository_metrics=args.metrics,
        api_clone_depth=args.api_clone_depth,
        pr_analysis=args.pr_analysis,
        pr_analysis_max_prs=args.pr_analysis_max_prs,
        pr_analysis_merged_only=args.pr_analysis_merged_only,
        pr_analysis_lookback_days=args.pr_analysis_lookback_days,
        pr_f2p=args.pr_f2p,
        pr_f2p_limit=args.pr_f2p_limit,
        pr_f2p_timeout=args.pr_f2p_timeout,
        pr_f2p_merge_packages=args.pr_f2p_merge_packages,
        pr_direct_targets=pr_targets or None,
        pr_f2p_workers=args.pr_f2p_workers,
        npm_cache_dir=args.npm_cache_dir,
    )
    write_report(
        facts,
        format=OutputFormat(args.format),
        destination=args.out,
        verbose=args.verbose,
        human_detail_path=Path(args.human_detail) if args.verbose else None,
    )
    return 0
