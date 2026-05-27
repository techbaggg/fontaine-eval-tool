from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fontaine.domain.licensing import LicenseInfo
from fontaine.domain.pr_analysis import PrAnalysisReport
from fontaine.domain.repository_metrics import RepositoryMetrics


@dataclass(frozen=True, slots=True)
class RepoFacts:
    """Plain summary for inventory / reporting."""

    repo_name: str
    default_branch: str
    lines_of_code: int
    merged_pr_count: int
    # Local: approximate PR #s from git log; GitHub API: always None (Search gives merged count).
    inferred_merged_pr_count: int | None
    merged_pr_count_any_branch: int | None = None
    """GitHub Search: ``is:merged`` with no ``base:`` (merged into any branch). ``None`` in local mode."""
    all_pr_count: int | None = None  # GitHub Search is:pr (all states); None in local mode

    local_root: Path | None = None
    facts_source: str = "local"
    """``local`` (git filesystem) or ``github_api``."""

    stars: int | None = None
    language_bytes: dict[str, int] | None = None
    language_total_bytes: int | None = None
    repository_disk_size_kb: int | None = None
    lines_of_code_note: str | None = None
    """Set when ``lines_of_code`` is not from a literal line count (e.g. API-only run)."""

    merged_into_default_only: bool = True
    """Whether merged PR counts were scoped to the default branch (GitHub Search ``base:``)."""

    repository_metrics: RepositoryMetrics | None = None
    """Present when ``--metrics`` is enabled."""

    elapsed_seconds: float = 0.0
    """Wall-clock seconds spent inside :func:`fontaine.domain.gather.gather_repo_facts`."""

    github_api_http_requests: int = 0
    """Number of HTTP requests sent to ``api.github.com`` (REST + GraphQL) during this gather."""

    pr_analysis: PrAnalysisReport | None = None
    """Merged-PR screening (GraphQL + first-stage filters; see ``--pr-analysis``)."""

    license_info: LicenseInfo | None = None
    """Detected license signals (GitHub API + optional local scan)."""

    merged_pr_year_matrix: tuple[int, ...] | None = None
    """Sixteen ``Search`` ``total_count`` values (row-major 4×4) for merged PRs by calendar year."""
    merged_pr_year_matrix_end_year: int | None = None
    """UTC calendar year used as the matrix anchor (``<= end_year - 15`` … ``end_year``)."""
