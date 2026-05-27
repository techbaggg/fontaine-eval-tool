from __future__ import annotations

import dataclasses
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from fontaine.adapters import github_rest
from fontaine.adapters.github_http_counter import (
    github_http_request_count,
    reset_github_http_request_count,
)
from fontaine.domain.licensing import build_license_info
from fontaine.domain.repo_facts import RepoFacts
from fontaine.domain.pr_analysis import run_pr_analysis, run_pr_analysis_for_explicit_targets
from fontaine.domain.f2p.batch import enrich_report_with_ts_f2p
from fontaine.domain.git_util import (
    git_default_branch,
    git_run,
    git_toplevel,
    line_count_for_file,
)
from fontaine.domain.repository_metrics import RepositoryMetrics, compute_repository_metrics
from fontaine.run_log import GatherPhaseTracker, progress, timed_phase


def _github_phase_tracker(
    *,
    include_pr_analysis: bool,
    pr_direct_targets: bool,
    pr_f2p: bool,
    include_repository_metrics: bool,
) -> GatherPhaseTracker:
    clone_est = (
        "~2–20 min (full history for metrics)"
        if include_repository_metrics
        else "~30 s–5 min"
    )
    phases: list[tuple[str, str, str | None]] = [
        ("gather.github_metadata", "GitHub repository metadata", "~5–30 s"),
        ("gather.git_clone", "Git clone for lines-of-code", clone_est),
        ("gather.license", "License signals", "~2–15 s"),
    ]
    if include_pr_analysis:
        if pr_direct_targets:
            phases.append(
                (
                    "gather.pr_targets",
                    "Fetch merged PRs for explicit targets",
                    "~10 s–3 min",
                )
            )
        else:
            phases.append(
                (
                    "gather.pr_screening",
                    "PR screening (GraphQL list + first-stage filters)",
                    "~1–15 min",
                )
            )
    if pr_f2p:
        phases.append(
            ("gather.f2p_enrich", "F2P/P2P test runs on local clone", "~5 min–hours")
        )
    return GatherPhaseTracker(phases)


def gather_repo_facts(
    *,
    local_root: Path | None,
    owner: str | None,
    repo: str | None,
    merged_to_default_only: bool,
    include_repository_metrics: bool = False,
    api_clone_depth: int = 1,
    pr_analysis: bool = False,
    pr_analysis_max_prs: int = 500,
    pr_analysis_merged_only: bool = False,
    pr_analysis_lookback_days: int = 730,
    pr_f2p: bool = False,
    pr_f2p_limit: int = 30,
    pr_f2p_timeout: int = 600,
    pr_f2p_merge_packages: bool = False,
    pr_direct_targets: list[int] | None = None,
    pr_f2p_workers: int = 1,
    npm_cache_dir: Path | None = None,
) -> RepoFacts:
    """Orchestrate git-on-disk or GitHub API helpers and return :class:`RepoFacts`."""
    reset_github_http_request_count()
    t0 = time.perf_counter()
    progress("Gather: starting (GitHub API / clone / PR screening as requested).")
    targets = list(pr_direct_targets or [])
    with timed_phase("gather.pipeline"):
        if pr_analysis or targets:
            if not owner or not repo:
                raise ValueError(
                    "--owner and --repo are required for PR analysis or --pr-target"
                )
            if (pr_f2p or targets) and local_root is None:
                raise ValueError("--repo-dir is required for --pr-f2p or --pr-target")
            gh_tracker = _github_phase_tracker(
                include_pr_analysis=True,
                pr_direct_targets=bool(targets),
                pr_f2p=pr_f2p,
                include_repository_metrics=include_repository_metrics,
            )
            # Bulk screening uses ``pr_analysis_max_prs`` as the GraphQL fetch cap; when F2P runs,
            # bump that cap to at least ``pr_f2p_limit`` so listing can supply enough candidates.
            pr_screening_fetch_cap = pr_analysis_max_prs
            if pr_f2p and not targets:
                pr_screening_fetch_cap = max(pr_analysis_max_prs, pr_f2p_limit)
            base = _gather_from_github_api(
                owner,
                repo,
                merged_to_default_only=merged_to_default_only,
                include_repository_metrics=include_repository_metrics,
                api_clone_depth=api_clone_depth,
                include_pr_analysis=True,
                pr_analysis_max_prs=pr_screening_fetch_cap,
                pr_analysis_merged_only=pr_analysis_merged_only,
                pr_analysis_lookback_days=pr_analysis_lookback_days,
                pr_f2p=pr_f2p,
                pr_f2p_repo_dir=local_root,
                pr_f2p_limit=pr_f2p_limit,
                pr_f2p_timeout=pr_f2p_timeout,
                pr_f2p_merge_packages=pr_f2p_merge_packages,
                pr_direct_targets=targets,
                pr_f2p_workers=pr_f2p_workers,
                npm_cache_dir=npm_cache_dir,
                phase_tracker=gh_tracker,
            )
        elif local_root is not None:
            base = _gather_from_local_clone(
                local_root,
                merged_to_default_only=merged_to_default_only,
                include_repository_metrics=include_repository_metrics,
                phase_tracker=GatherPhaseTracker(
                    [("gather.local_scan", "Scan local repository", "~10 s–3 min")]
                ),
            )
        else:
            gh_tracker = _github_phase_tracker(
                include_pr_analysis=False,
                pr_direct_targets=False,
                pr_f2p=False,
                include_repository_metrics=include_repository_metrics,
            )
            base = _gather_from_github_api(
                owner or "",
                repo or "",
                merged_to_default_only=merged_to_default_only,
                include_repository_metrics=include_repository_metrics,
                api_clone_depth=api_clone_depth,
                phase_tracker=gh_tracker,
            )
    return dataclasses.replace(
        base,
        elapsed_seconds=time.perf_counter() - t0,
        github_api_http_requests=github_http_request_count(),
    )


def _count_lines_of_code(worktree: Path) -> int:
    top = git_toplevel(worktree)
    scope = worktree.resolve()
    r = git_run(worktree, "ls-files", "-z", check=False)
    if r.returncode != 0:
        return 0
    total = 0
    for rel in r.stdout.split("\0"):
        if not rel:
            continue
        path = (top / rel).resolve()
        try:
            path.relative_to(scope)
        except ValueError:
            continue
        if not path.is_file():
            continue
        total += line_count_for_file(path)
    return total


_MERGE_PR_SUBJECT = re.compile(r"merge pull request #(\d+)\b", re.IGNORECASE)
_SUBJECT_PR_SUFFIX = re.compile(r"\(#(\d+)\)\s*$")


def _inferred_merged_pr_count(root: Path, default_branch: str) -> int:
    r = git_run(
        root,
        "log",
        default_branch,
        "--first-parent",
        "--format=%s",
        check=False,
    )
    if r.returncode != 0:
        return 0
    seen: set[int] = set()
    for line in r.stdout.splitlines():
        subject = line.strip()
        if not subject:
            continue
        m = _MERGE_PR_SUBJECT.search(subject)
        if m:
            seen.add(int(m.group(1)))
            continue
        m2 = _SUBJECT_PR_SUFFIX.search(subject)
        if m2:
            seen.add(int(m2.group(1)))
    return len(seen)


def _count_merge_commits_first_parent(root: Path, default_branch: str) -> int:
    r = git_run(
        root,
        "log",
        default_branch,
        "--first-parent",
        "--merges",
        "--oneline",
        check=False,
    )
    if r.returncode != 0:
        return 0
    return len([ln for ln in r.stdout.splitlines() if ln.strip()])


def _gather_from_local_clone(
    root: Path,
    *,
    merged_to_default_only: bool,
    include_repository_metrics: bool,
    phase_tracker: GatherPhaseTracker,
) -> RepoFacts:
    progress("Gather: scanning local repository %s", root)
    with phase_tracker.step("gather.local_scan", metrics=int(include_repository_metrics)):
        root = root.resolve()
        default_branch = git_default_branch(root)
        repo_name = root.name
        loc = _count_lines_of_code(root)
        merge_commits = _count_merge_commits_first_parent(root, default_branch)
        inferred = _inferred_merged_pr_count(root, default_branch)
        metrics = None
        if include_repository_metrics:
            metrics = compute_repository_metrics(root)
        lic = build_license_info(
            github_spdx=None,
            filesystem_root=root.resolve(),
        )
    return RepoFacts(
        repo_name=repo_name,
        default_branch=default_branch,
        lines_of_code=loc,
        merged_pr_count=merge_commits,
        all_pr_count=None,
        inferred_merged_pr_count=inferred,
        local_root=root,
        facts_source="local",
        lines_of_code_note=None,
        merged_into_default_only=merged_to_default_only,
        repository_metrics=metrics,
        license_info=lic,
        merged_pr_year_matrix=None,
        merged_pr_year_matrix_end_year=None,
    )


def _shallow_clone(
    owner: str,
    repo: str,
    token: str,
    default_branch: str,
    dest: Path,
    *,
    depth: int,
) -> None:
    """
    ``git clone`` with limited history. PAT via ``x-access-token``. Raises on failure.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    depth_args: list[str] = []
    if depth > 0:
        depth_args = ["--depth", str(depth)]
    branch_args: list[str] = [
        "git",
        "clone",
        *depth_args,
        "--single-branch",
        "--branch",
        default_branch,
        url,
        str(dest),
    ]
    first = subprocess.run(branch_args, capture_output=True, text=True, timeout=900, env=env)
    if first.returncode != 0:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        # Retry without `--branch` (misnamed remote default or transient ref issues).
        fallback = subprocess.run(
            ["git", "clone", *depth_args, url, str(dest)],
            capture_output=True,
            text=True,
            timeout=900,
            env=env,
        )
        if fallback.returncode != 0:
            detail = (fallback.stderr or fallback.stdout or first.stderr or "")[:800]
            msg = f"git clone failed: {detail}".strip()
            raise RuntimeError(msg)


def _clone_github_for_loc_and_metrics(
    owner: str,
    repo: str,
    token: str,
    default_branch: str,
    *,
    depth: int,
    compute_metrics: bool,
) -> tuple[int, str | None, RepositoryMetrics | None]:
    """
    Clone into a temp dir; return LOC, optional error note, optional RepositoryMetrics.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="fontaine-clone-") as tmp:
            root = Path(tmp) / "repo"
            _shallow_clone(
                owner, repo, token, default_branch, root, depth=depth
            )
            loc = _count_lines_of_code(root)
            metrics = None
            if compute_metrics:
                metrics = compute_repository_metrics(
                    root,
                    github_owner=owner,
                    github_repo=repo,
                    github_token=token,
                )
            return loc, None, metrics
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        return 0, str(e), None


def _gather_from_github_api(
    owner: str,
    repo: str,
    *,
    merged_to_default_only: bool,
    include_repository_metrics: bool,
    api_clone_depth: int,
    include_pr_analysis: bool = False,
    pr_analysis_max_prs: int = 500,
    pr_analysis_merged_only: bool = False,
    pr_analysis_lookback_days: int = 730,
    pr_f2p: bool = False,
    pr_f2p_repo_dir: Path | None = None,
    pr_f2p_limit: int = 30,
    pr_f2p_timeout: int = 600,
    pr_f2p_merge_packages: bool = False,
    pr_direct_targets: list[int] | None = None,
    pr_f2p_workers: int = 1,
    npm_cache_dir: Path | None = None,
    phase_tracker: GatherPhaseTracker | None = None,
) -> RepoFacts:
    if phase_tracker is None:
        phase_tracker = _github_phase_tracker(
            include_pr_analysis=include_pr_analysis,
            pr_direct_targets=bool(pr_direct_targets),
            pr_f2p=pr_f2p,
            include_repository_metrics=include_repository_metrics,
        )
    token = github_rest.resolve_github_token()
    progress("GitHub: fetching repository metadata for %s/%s...", owner, repo)
    with phase_tracker.step("gather.github_metadata", owner=owner, repo=repo):
        raw = github_rest.fetch_repo_facts_remote(
            token,
            owner,
            repo,
            merged_to_default_only=merged_to_default_only,
        )
    total_bytes = int(raw.get("language_total_bytes") or 0)
    langs = raw.get("language_bytes") or {}
    default_branch = str(raw["default_branch"])

    # Git-derived metrics need full history on the analyzed branch; shallow clones
    # skew rev-list, earliest-commit stats, sampled logs, and churn.
    depth = 0 if include_repository_metrics else api_clone_depth
    progress(
        "Git clone: shallow clone for LOC%s (depth=%s)...",
        " + metrics" if include_repository_metrics else "",
        depth,
    )
    with phase_tracker.step(
        "gather.git_clone",
        depth=depth,
        metrics=int(include_repository_metrics),
    ):
        loc, clone_err, repo_metrics = _clone_github_for_loc_and_metrics(
            owner,
            repo,
            token,
            default_branch,
            depth=depth,
            compute_metrics=include_repository_metrics,
        )
    if clone_err:
        note = f"LOC unavailable (git clone failed). {clone_err[:400]}"
        repo_metrics = None
        progress("Git clone: failed — %s", clone_err[:200])
    else:
        note = None
        progress("Git clone: done — LOC=%s", loc)

    with phase_tracker.step("gather.license"):
        gh_spdx = github_rest.fetch_repository_license_spdx(token, owner, repo)
    scan_root = pr_f2p_repo_dir.resolve() if pr_f2p_repo_dir else None
    license_info = build_license_info(
        github_spdx=gh_spdx,
        filesystem_root=scan_root,
    )

    merged_pr_year_matrix: tuple[int, ...] | None = None
    merged_pr_year_matrix_end_year: int | None = None
    try:
        progress(
            "GitHub: merged PR counts by calendar year (16 Search queries; scope matches "
            "merged PR summary)%s...",
            f", base:{default_branch}" if merged_to_default_only else "",
        )
        ym_counts, ym_end = github_rest.fetch_merged_pr_year_matrix_counts(
            token,
            owner,
            repo,
            base_branch=default_branch if merged_to_default_only else None,
        )
        merged_pr_year_matrix = tuple(ym_counts)
        merged_pr_year_matrix_end_year = ym_end
    except github_rest.GitHubApiError as e:
        progress(
            "GitHub: merged PR year matrix skipped — %s",
            str(e).replace("\n", " ")[:200],
        )

    pa = None
    targets = list(pr_direct_targets or [])
    if include_pr_analysis:
        if targets:
            progress(
                "PR target mode: fetching %s merged PR(s) by number (skipping bulk screen).",
                len(targets),
            )
            with phase_tracker.step("gather.pr_targets", count=len(targets)):
                pa, accepted_rows = run_pr_analysis_for_explicit_targets(
                    token=token,
                    owner=owner,
                    repo=repo,
                    target_numbers=targets,
                    pr_analysis_lookback_days=pr_analysis_lookback_days,
                )
        else:
            with phase_tracker.step(
                "gather.pr_screening",
                max_prs=pr_analysis_max_prs,
                merged_only=int(pr_analysis_merged_only),
            ):
                pa, accepted_rows = run_pr_analysis(
                    token=token,
                    owner=owner,
                    repo=repo,
                    default_branch=default_branch,
                    merged_to_default_only=merged_to_default_only,
                    max_prs_to_scan=pr_analysis_max_prs,
                    merged_prs_screening_only=pr_analysis_merged_only,
                    pr_analysis_lookback_days=pr_analysis_lookback_days,
                )
        if pr_f2p:
            if targets:
                # Explicit targets: screening may reject some numbers — F2P runs only on ``passed``.
                cli_cap = len(targets)
                scheduled = len(pa.passed)
                cap_note = (
                    f", {cli_cap - scheduled} target(s) rejected in first-stage screening"
                    if scheduled < cli_cap
                    else ""
                )
            else:
                cli_cap = pr_f2p_limit
                scheduled = min(cli_cap, len(pa.passed))
                cap_note = "" if scheduled == cli_cap else f", CLI cap {cli_cap}"
            progress(
                "F2P/P2P: batch starting (%s PR(s)%s, workers=%s); local clone %s",
                scheduled,
                cap_note,
                pr_f2p_workers,
                pr_f2p_repo_dir,
            )
            root = pr_f2p_repo_dir
            if root is None:
                raise ValueError("pr_f2p requires pr_f2p_repo_dir")
            with phase_tracker.step(
                "gather.f2p_enrich",
                workers=pr_f2p_workers,
                scheduled=scheduled,
            ):
                pa = enrich_report_with_ts_f2p(
                    pa,
                    accepted_rows,
                    root.resolve(),
                    limit=cli_cap,
                    test_timeout=pr_f2p_timeout,
                    merge_affected_packages=pr_f2p_merge_packages,
                    workers=pr_f2p_workers,
                    npm_cache_dir=npm_cache_dir,
                )
            progress("F2P/P2P: batch complete.")

    return RepoFacts(
        repo_name=str(raw["repo_name"]),
        default_branch=default_branch,
        lines_of_code=loc,
        merged_pr_count=int(raw["merged_pr_count"]),
        merged_pr_count_any_branch=(
            int(raw["merged_pr_count_any_branch"])
            if raw.get("merged_pr_count_any_branch") is not None
            else None
        ),
        all_pr_count=int(raw["all_pr_count"]),
        inferred_merged_pr_count=None,
        local_root=None,
        facts_source="github_api",
        stars=int(raw.get("stars") or 0),
        language_bytes=langs if langs else None,
        language_total_bytes=total_bytes if total_bytes else None,
        repository_disk_size_kb=int(raw.get("repository_disk_size_kb") or 0),
        lines_of_code_note=note,
        merged_into_default_only=merged_to_default_only,
        repository_metrics=(
            repo_metrics if include_repository_metrics else None
        ),
        pr_analysis=pa,
        license_info=license_info,
        merged_pr_year_matrix=merged_pr_year_matrix,
        merged_pr_year_matrix_end_year=merged_pr_year_matrix_end_year,
    )
