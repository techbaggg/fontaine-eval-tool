"""
Merged-PR sampling via GitHub GraphQL plus first-stage screening filters.

English title/body (ASCII ratio), dominant **language** against a supported whitelist,
Markdown-only PRs, minimum test files, difficulty (file count), **config/meta** path
classification (Helix-style), caps, minimum non-test source churn from GraphQL file
stats, and an optional **patch cyclomatic proxy** (control-flow tokens on added diff
lines) for F2P sampling.

Closing-issue validation is not implemented here (would need extra GraphQL/API calls).
"""

from __future__ import annotations

import math
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fontaine.adapters import github_rest
from fontaine.run_log import progress

# --- PR sample filter constants ---
MIN_TEST_FILES = 1
MAX_NON_TEST_FILES = 100
MAX_TEST_FILES = 15
MAX_CHANGED_FILES = 50
# Reject if len(test_files)+len(non_test) <= 5 (need 6+ non-asset files for meaningful difficulty).
DIFFICULTY_REJECT_MAX_TOTAL_NON_ASSET_FILES = 5
# Minimum non-test source line churn; hunks from a patch could be counted instead — here we use GraphQL +/- on source paths.
MIN_PR_CODE_CHANGES = 1

# Bulk PR listing: only load merged PRs with ``mergedAt`` within this window (GitHub GraphQL).
DEFAULT_PR_ANALYSIS_LOOKBACK_DAYS = 730
# ``run_pr_analysis`` GraphQL pagination: cap pages (50 PRs per page) to avoid hundreds of
# calls when lookback filters most rows (e.g. merged-only + UPDATED_AT order vs mergedAt).
_PR_ANALYSIS_MAX_GRAPHQL_PAGES = 100
# When merged-time lookback is on: stop if this many consecutive pages add no rows after lookback.
_PR_ANALYSIS_LOOKBACK_EMPTY_PAGE_LIMIT = 15

# Weight for ``approximate_cyclomatic_proxy_from_patch`` inside :func:`pr_complexity_score`
# (same order of magnitude as a few extra changed files).
_CY_COMPLEXITY_WEIGHT = 0.75

# Internal row key set during screening / PR-target enrichment for sampling weights.
_CY_KEY = "_cyclomatic_proxy"

# Per-PR dominant language must be one of these (extension-driven; see Project Helix repo-parser).
SUPPORTED_LANGUAGES_WHITELIST = frozenset(
    {
        "python",
        "java",
        "go",
        "javascript",
        "cpp",
        "typescript",
        "php",
        "ruby",
        "c",
        "csharp",
        "nix",
        "shell",
        "rust",
        "scala",
        "kotlin",
    }
)

# Order matters for ambiguous extensions (first match wins), aligned with helix-task-generator.
_LANGUAGE_EXTENSION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", (".py",)),
    ("javascript", (".js", ".jsx", ".mjs", ".cjs")),
    ("typescript", (".ts", ".tsx")),
    ("java", (".java",)),
    ("cpp", (".cpp", ".cc", ".cxx", ".hpp", ".h")),
    ("c", (".c",)),
    ("csharp", (".cs",)),
    ("go", (".go",)),
    ("rust", (".rs",)),
    ("ruby", (".rb",)),
    ("php", (".php",)),
    ("swift", (".swift",)),
    ("kotlin", (".kt", ".kts")),
    ("scala", (".scala",)),
    ("nix", (".nix",)),
    ("shell", (".sh", ".bash", ".zsh")),
    ("html", (".html", ".htm")),
    ("css", (".css", ".scss", ".sass")),
    ("sql", (".sql",)),
    ("yaml", (".yaml", ".yml")),
    ("json", (".json",)),
    ("xml", (".xml",)),
    ("markdown", (".md", ".markdown")),
)

_BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".rar",
        ".7z",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".bin",
        ".doc",
        ".docx",
    }
)

_DOC_EXTENSIONS = frozenset({".md", ".markdown", ".rst", ".txt", ".csv", ".json"})

# Basenames / tokens for CI, tooling, meta — counts as config/noise for gates (Helix spec.md style).
CONFIG_PATTERNS: tuple[str, ...] = (
    ".config",
    ".conf",
    ".properties",
    ".env",
    ".settings",
    ".prefs",
    ".rc",
    ".ini",
    ".cfg",
    ".toml",
    ".yaml",
    ".yml",
    ".xml",
    ".lock",
    ".npmrc",
    ".yarnrc",
    ".npmignore",
    ".nvmrc",
    ".prettierrc",
    ".prettierignore",
    ".eslintrc",
    ".eslintignore",
    ".babelrc",
    ".stylelintrc",
    ".browserslistrc",
    "jest.config",
    "vitest.config.",
    "cypress.config.",
    "playwright.config.",
    "karma.conf.js",
    "mocha.opts",
    "wdio.conf.js",
    "tslint.json",
    "pytest.ini",
    "nose.cfg",
    ".coveragerc",
    ".pylintrc",
    ".flake8",
    "mypy.ini",
    "ruff.toml",
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    ".gitconfig",
    ".editorconfig",
    ".mk",
    ".make",
    ".cmake",
    ".gradle",
    ".sbt",
    "makefile",
    "cmakelists.txt",
    "procfile",
    "jenkinsfile",
    "vagrantfile",
    ".travis.yml",
    ".circleci",
    "azure-pipelines.yml",
    ".codeclimate.yml",
    "sonar-project.properties",
    "readme",
    "license",
    "changelog",
    "contributing",
    ".dockerignore",
)

# ``filter_pr_files`` "code_files" cap (Helix): changed files that are not plain data blobs.
_DATA_FILE_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".csv",
        ".json",
        ".xml",
        ".yaml",
        ".yml",
        ".md",
        ".txt",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
    }
)

_SOURCE_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".mjs",
        ".cjs",
        ".java",
        ".scala",
        ".kt",
        ".kts",
        ".c",
        ".cpp",
        ".cc",
        ".cxx",
        ".h",
        ".hpp",
        ".hh",
        ".hxx",
        ".inl",
        ".rs",
        ".go",
        ".rb",
        ".php",
        ".phtml",
        ".cs",
        ".swift",
        ".cob",
        ".cbl",
        ".cpy",
        ".cobol",
        ".m",
        ".mm",
        ".vue",
        ".svelte",
    }
)


def parse_github_datetime_optional(raw: Any) -> datetime | None:
    """Parse ISO8601 from GraphQL (``…Z``) to timezone-aware UTC."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        s = raw.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def lookback_cutoff_utc(lookback_days: int) -> datetime | None:
    """``None`` means no merged-time filter (``lookback_days <= 0``)."""
    if lookback_days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=lookback_days)


def merged_at_lookback_log_suffix(pr_analysis_lookback_days: int) -> str:
    """
    Semicolon-led fragment for activity-log lines (consistent across bulk + explicit runs).

    Examples: ``"; mergedAt ≥ 2024-04-13 UTC (730d lookback)"`` or ``"; no merged-time lookback"``.
    """
    cutoff = lookback_cutoff_utc(pr_analysis_lookback_days)
    if cutoff is None:
        return "; no merged-time lookback"
    return (
        f"; mergedAt ≥ {cutoff.date().isoformat()} UTC "
        f"({pr_analysis_lookback_days}d lookback)"
    )


def row_passes_merged_at_lookback(row: dict[str, Any], cutoff: datetime | None) -> bool:
    """
    If ``cutoff`` is ``None`` (lookback disabled), every row passes.

    If ``cutoff`` is set (positive lookback window), only **merged** rows with
    ``mergedAt >= cutoff`` pass. Rows without ``mergedAt`` (open or not merged) are
    **excluded** so they cannot consume the ``--pr-analysis-max-prs`` quota—otherwise
    the default all-states GraphQL feed would fill the sample with open PRs and the
    merged-time window would have no effect on count or runtime.
    """
    if cutoff is None:
        return True
    mt = parse_github_datetime_optional(row.get("mergedAt"))
    if mt is None:
        return False
    return mt >= cutoff


def _is_bot_login(login: str) -> bool:
    if not login:
        return False
    lower = login.lower()
    if login.endswith("[bot]"):
        return True
    return lower in {
        "dependabot",
        "renovate",
        "codecov",
        "greenkeeper",
        "snyk-bot",
        "pyup-bot",
        "whitesource",
        "mergify",
        "stale",
        "github-actions",
        "allcontributors",
        "imgbot",
        "k8s-ci-robot",
        "k8s-bot",
        "k8s-mergebot",
    }


def _is_english_by_ascii_ratio(text: str) -> bool:
    """Treat text as English-like if ASCII ratio >= 0.9. Empty passes."""
    if not text or not text.strip():
        return True
    total = len(text)
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return (ascii_chars / total) >= 0.9


def _is_test_path(path: str) -> bool:
    """Universal path/filename heuristic for likely test files."""
    if not path:
        return False
    norm = path.replace("\\", "/")
    lower = norm.lower()
    base = os.path.basename(norm)
    base_lower = base.lower()
    name_no_ext_lower = os.path.splitext(base_lower)[0]
    name_no_ext_orig = os.path.splitext(base)[0]

    if re.search(
        r"(^|[\\/])(test|tests|spec|specs|__tests__|__test__)([\\/]|$)",
        lower,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"(\.test\.|\.spec\.|_test\.|_spec\.|\.snap$)",
        base_lower,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(r"(_test\.[^./]+|_spec\.[^./]+)$", base_lower, flags=re.IGNORECASE):
        return True
    if re.search(
        r"(^|[._-])(test|spec)([._-]|$)",
        name_no_ext_lower,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"(Test|Tests|TestCase|Spec|Specs)$",
        name_no_ext_orig,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _dotless_config_pattern_matches(pattern: str, basename_lower: str) -> bool:
    """
    Match CONFIG_PATTERNS entries with no dots (readme, license, makefile, …).

    Requires exact basename or ``stem.ext`` where stem equals the pattern and ``ext`` is not a
    **source** extension — so ``license.py``, ``readme.ts``, and ``changelog.js`` stay source
    code, while ``LICENSE``, ``README.md``, ``Makefile.am`` remain meta/config.
    """
    if basename_lower == pattern:
        return True
    if "." not in basename_lower:
        return False
    stem = basename_lower.rsplit(".", 1)[0]
    if stem != pattern:
        return False
    suf = Path(basename_lower).suffix.lower()
    if suf in _SOURCE_EXTENSIONS:
        return False
    return True


def _config_pattern_matches_basename(pattern: str, basename_lower: str) -> bool:
    """Match a CONFIG_PATTERNS entry against a path basename (lowercased)."""
    # Dotless tokens (readme, license, makefile): see :func:`_dotless_config_pattern_matches`.
    if not pattern.startswith(".") and "." not in pattern:
        return _dotless_config_pattern_matches(pattern, basename_lower)
    # Patterns with dots or a leading dot: suffix match (.ini, pytest.ini, azure-pipelines.yml).
    if basename_lower.endswith(pattern):
        return True
    # Dot-leading tokens (.ini, .env): suffix-only (handled above). ``pattern in basename``
    # falsely matches inside longer names (e.g. ``initialize`` ↔ ``.ini``).
    if pattern.startswith("."):
        return False
    # Internal dots (jest.config., azure-pipelines.yml): substring is intentional for prefixes.
    return pattern in basename_lower


def _is_config_or_asset_path(path: str) -> bool:
    """Binary/docs plus tooling/meta filenames (Helix ``is_config_or_asset``)."""
    lower = path.lower()
    ext = "." + lower.rsplit(".", 1)[-1] if "." in lower else ""
    if ext in _BINARY_EXTENSIONS or ext in _DOC_EXTENSIONS:
        return True
    basename = lower.rsplit("/", 1)[-1]
    for pattern in CONFIG_PATTERNS:
        if _config_pattern_matches_basename(pattern, basename):
            return True
    return False


def _is_data_file_path(path: str) -> bool:
    """Extension in the data/doc set used for changed-file caps (Helix ``is_data_file``)."""
    return Path(path).suffix.lower() in _DATA_FILE_EXTENSIONS


def _is_source_path(path: str) -> bool:
    """Known source-code extension (Helix ``SOURCE_EXTENSIONS`` + Fontaine extras)."""
    return Path(path).suffix.lower() in _SOURCE_EXTENSIONS


def _language_key_from_path(path: str) -> str | None:
    ext = Path(path).suffix.lower()
    if not ext:
        return None
    for lang, exts in _LANGUAGE_EXTENSION_GROUPS:
        if ext in exts:
            return lang
    return None


def _is_markdown_only_pr(nodes: list[dict[str, Any]]) -> bool:
    """Every changed path is ``.md`` / ``.markdown``."""
    paths = [str(n.get("path") or "").strip() for n in nodes if str(n.get("path") or "").strip()]
    if not paths:
        return False
    for p in paths:
        suf = Path(p).suffix.lower()
        if suf not in (".md", ".markdown"):
            return False
    return True


def _dominant_language_key(nodes: list[dict[str, Any]]) -> str | None:
    """
    Most common language key from :func:`_language_key_from_path` over changed files.

    Skips paths that :func:`_is_config_or_asset_path` treats as config/meta/docs/binary so
    dominant language matches file gates and complexity (e.g. many ``.yaml`` CI edits must not
    outvote a few ``.py`` files).
    """
    counts: dict[str, int] = {}
    for n in nodes:
        path = str(n.get("path") or "").strip()
        if not path or _is_config_or_asset_path(path):
            continue
        lang = _language_key_from_path(path)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda x: (-x[1], x[0]))[0][0]


def _language_whitelist_reject_reason(nodes: list[dict[str, Any]]) -> str | None:
    """``markdown_only`` or ``unsupported_language`` when the PR is not acceptable."""
    if _is_markdown_only_pr(nodes):
        return "markdown_only"
    dom = _dominant_language_key(nodes)
    if dom is None:
        return "unsupported_language"
    if dom not in SUPPORTED_LANGUAGES_WHITELIST:
        return "unsupported_language"
    return None


def _approx_source_delta(nodes: list[dict[str, Any]]) -> int:
    """GraphQL additions+deletions on likely source files excluding tests, data, and config/meta."""
    total = 0
    for f in nodes:
        path = str(f.get("path") or "")
        if _is_data_file_path(path):
            continue
        if _is_config_or_asset_path(path):
            continue
        if _is_test_path(path):
            continue
        if not _is_source_path(path):
            continue
        total += int(f.get("additions") or 0) + int(f.get("deletions") or 0)
    return total


_CYCLOMATIC_LINE_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bif\s*\(",
        r"\belse\s*:",
        r"\belse\s+if\s*\(",
        r"\bwhile\s*\(",
        r"\bfor\s*\(",
        r"\bswitch\s*\(",
        r"\bcase\s+",
        r"\bcatch\s*\(",
        # ``\b`` is wrong for ``&&`` / ``||`` (``&``/``|`` are not word chars), so spaced
        # ``x && y`` never matched. Allow optional whitespace; avoid matching ``&&&`` / ``|||``.
        r"(?<![&])\s*&&\s*(?![&])",
        r"(?<![|])\s*\|\|\s*(?![|])",
        r"\?\s*.*\s*:",  # ternary (noisy but cheap)
    )
)


def approximate_cyclomatic_proxy_from_patch(patch: str) -> float:
    """
    Rough control-flow density on **added** lines of a unified diff (``+`` hunks only).

    Matches the idea used in Helix repo-parser: count branching-like tokens in new code.
    Language-agnostic and imperfect (comments/strings can contribute); meant only as a
    relative signal for :func:`pr_complexity_score`, not as a McCabe-accurate metric.
    """
    if not patch:
        return 0.0
    complexity = 1.0  # base, aligned with common cyclomatic conventions
    for line in patch.split("\n"):
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        for pat in _CYCLOMATIC_LINE_PATTERNS:
            complexity += float(len(pat.findall(added)))
    return complexity


def ensure_cyclomatic_proxy_on_row(
    token: str,
    owner: str,
    repo: str,
    row: dict[str, Any],
    *,
    patch: str | None = None,
) -> None:
    """
    Attach ``_cyclomatic_proxy`` on ``row`` for :func:`pr_complexity_score`.

    When ``patch`` is provided (e.g. already fetched in :func:`run_pr_analysis`), no extra HTTP.
    Otherwise fetches compare diff via REST.
    """
    if _CY_KEY in row:
        return
    if patch is not None:
        row[_CY_KEY] = approximate_cyclomatic_proxy_from_patch(patch)
        return
    p = github_rest.fetch_pull_request_patch(
        token,
        owner,
        repo,
        base_sha=str(row.get("baseRefOid") or ""),
        head_sha=str(row.get("headRefOid") or ""),
    )
    row[_CY_KEY] = approximate_cyclomatic_proxy_from_patch(p or "")


def pr_complexity_score(row: dict[str, Any]) -> float:
    """
    Heuristic difficulty from GraphQL file stats (higher ⇒ more complex).

    Combines non-test source churn, total non-data churn, file counts, test-file count,
    and when present the patch cyclomatic proxy (``_cyclomatic_proxy``) from
    :func:`ensure_cyclomatic_proxy_on_row`.
    """
    nodes = row.get("files_nodes") or []
    src_delta = _approx_source_delta(nodes)
    non_data: list[dict[str, Any]] = []
    for f in nodes:
        path = str(f.get("path") or "")
        if _is_data_file_path(path):
            continue
        if _is_config_or_asset_path(path):
            continue
        non_data.append(f)
    churn = sum(
        int(f.get("additions") or 0) + int(f.get("deletions") or 0) for f in non_data
    )
    test_n = sum(1 for f in non_data if _is_test_path(str(f.get("path") or "")))
    cy = float(row.get(_CY_KEY) or 0.0)
    return (
        float(src_delta)
        + 0.25 * float(churn)
        + 3.0 * float(len(non_data))
        + 2.0 * float(test_n)
        + _CY_COMPLEXITY_WEIGHT * cy
    )


def _weighted_sample_ranks_without_replacement(
    rng: random.Random,
    n: int,
    weights: list[float],
    k: int,
) -> list[int]:
    """Pick ``k`` distinct ranks from ``0..n-1`` without replacement, P(j) ∝ weights[j]."""
    if k >= n:
        return list(range(n))
    if k <= 0:
        return []
    idxs = list(range(n))
    w = [float(x) for x in weights]
    out: list[int] = []
    for _ in range(k):
        if not idxs:
            break
        total = sum(w)
        if total <= 0:
            pick = rng.randrange(len(idxs))
        else:
            r = rng.random() * total
            acc = 0.0
            pick = 0
            for j in range(len(w)):
                acc += w[j]
                if r <= acc:
                    pick = j
                    break
        chosen_rank = idxs.pop(pick)
        w.pop(pick)
        out.append(chosen_rank)
    return out


def _f2p_sample_rng() -> random.Random:
    raw = (os.environ.get("FONTAINE_F2P_SAMPLE_SEED") or "").strip()
    if raw:
        try:
            return random.Random(int(raw, 0))
        except ValueError:
            return random.Random(hash(raw) & 0xFFFFFFFFFFFFFFFF)
    return random.Random()


def sample_indices_for_f2p(
    accepted_rows: tuple[dict[str, Any], ...],
    limit: int,
) -> list[int]:
    """
    Row indices into ``accepted_rows`` for F2P/P2P.

    If ``limit`` is greater than or equal to the number of passed PRs, returns
    ``[0, 1, …, n-1]`` in screening order (no sampling). Otherwise returns ``limit``
    distinct indices preferring **medium-complexity** PRs: ranks by
    :func:`pr_complexity_score` are weighted with a Gaussian bell over rank position.

    Set ``FONTAINE_F2P_SAMPLE_SEED`` (integer string) for reproducible samples.
    """
    n = len(accepted_rows)
    if limit >= n or n == 0:
        return list(range(n))
    if limit <= 0:
        return []

    scores = [pr_complexity_score(r) for r in accepted_rows]
    sorted_indices = sorted(range(n), key=lambda i: (scores[i], i))

    mu = (n - 1) / 2.0
    sigma = max((n - 1) / 4.0, 0.5)
    weights = [
        math.exp(-0.5 * ((j - mu) / sigma) ** 2) for j in range(n)
    ]

    rng = _f2p_sample_rng()
    chosen_ranks = _weighted_sample_ranks_without_replacement(rng, n, weights, limit)
    picked = [sorted_indices[r] for r in chosen_ranks]
    picked.sort(key=lambda i: (scores[i], i))
    return picked


def file_gates_reject_reason(nodes: list[dict[str, Any]]) -> str | None:
    """
    Return a slug if this PR fails file-count gates, else ``None``.

    Classifies paths like Helix ``filter_pr_files``: tests exclude config/meta;
    non-test caps exclude config/meta; changed-file cap excludes data blobs only.
    """

    def _path_of(f: dict[str, Any]) -> str:
        return str(f.get("path") or "")

    test_files = [
        f
        for f in nodes
        if _is_test_path(_path_of(f)) and not _is_config_or_asset_path(_path_of(f))
    ]
    non_test_files = [
        f
        for f in nodes
        if not _is_test_path(_path_of(f)) and not _is_config_or_asset_path(_path_of(f))
    ]

    if len(test_files) < MIN_TEST_FILES:
        return "fewer_than_min_test_files"
    if len(non_test_files) > MAX_NON_TEST_FILES:
        return "more_than_max_non_test_files"

    total_non_asset = len(test_files) + len(non_test_files)
    if total_non_asset <= DIFFICULTY_REJECT_MAX_TOTAL_NON_ASSET_FILES:
        return "difficulty_not_hard"

    if len(test_files) > MAX_TEST_FILES:
        return "too_many_test_files"

    code_files = [
        _path_of(f)
        for f in nodes
        if _path_of(f) and not _is_data_file_path(_path_of(f))
    ]
    if len(code_files) > MAX_CHANGED_FILES:
        return "too_many_changed_files"

    return None


# Slugs match :func:`file_gates_reject_reason` and the hand-rolled gates in
# :func:`run_pr_analysis` (``bot_pr``, etc.).
_FIRST_STAGE_REJECT_LABELS: dict[str, str] = {
    "bot_pr": "bot or automated author",
    "content_not_in_english": "title/body may not be English (need ~90% ASCII)",
    "markdown_only": "only Markdown files changed",
    "unsupported_language": "dominant file language not on supported whitelist",
    "fewer_than_min_test_files": "not enough test files",
    "more_than_max_non_test_files": "too many non-test files",
    "difficulty_not_hard": "difficulty not high enough (at most five non-asset files, need six+)",
    "too_many_test_files": "too many test files",
    "too_many_changed_files": "too many changed files",
    "code_changes_not_sufficient": "non-test source churn below minimum",
    "full_patch_retrieval": "could not retrieve unified diff for base...head (REST compare)",
    "lookback_excluded": "merged before PR-analysis lookback window",
}


def first_stage_reject_label(reason_code: str) -> str:
    """Stable slug → human text for stderr and reports."""
    return _FIRST_STAGE_REJECT_LABELS.get(reason_code, reason_code)


@dataclass(frozen=True, slots=True)
class FirstStageRejected:
    number: int
    title: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class PrAnalysisLine:
    number: int
    title: str
    complexity_score: float | None = None
    """Heuristic from :func:`pr_complexity_score` after first-pass acceptance (GraphQL + patch proxy)."""
    # Set when ``--pr-f2p`` runs TypeScript F2P/P2P analysis.
    f2p_count: int | None = None
    p2p_count: int | None = None
    f2p_error: str | None = None
    f2p_diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class PrAnalysisReport:
    total_analyzed: int
    """PR rows after merged-time lookback (bulk) or requested targets count (explicit mode)."""
    considered_for_filters: int
    """PR rows evaluated against first-stage filters (one increment per fetched PR in bulk mode)."""
    passed_first_filters: int
    """Eligible for deeper processing (F2P when enabled)."""
    rejection_breakdown: tuple[tuple[str, int], ...]
    """Sorted (reason_code, count) for first-stage filters."""
    passed: tuple[PrAnalysisLine, ...]
    first_stage_rejected: tuple[FirstStageRejected, ...] = ()
    """Each PR that failed first-stage filters, in fetch order (newest first for the active listing)."""
    pr_target_mode: bool = False
    """Set when ``--pr-target`` was used (bulk screen skipped)."""
    f2p_limit: int | None = None
    """When ``--pr-f2p`` runs, target sample/run cap (same as CLI ``--pr-f2p-limit``); ``None`` otherwise."""
    pr_analysis_lookback_days: int | None = None
    """Merged-time window for bulk listing / targets (``0`` = unlimited). ``None`` when PR analysis disabled."""


def screening_pass_or_reason(
    *,
    token: str,
    owner: str,
    repo: str,
    pr: dict[str, Any],
) -> str | None:
    """
    Apply full first-stage screening to one GraphQL PR row, including REST patch fetch.

    On success, sets ``_cyclomatic_proxy`` on ``pr`` and returns ``None``.
    """
    title_raw = str(pr.get("title") or "").strip()
    author = pr.get("author") or {}
    login = str(author.get("login") or "")
    is_bot = author.get("__typename") == "Bot" or _is_bot_login(login)
    if is_bot:
        return "bot_pr"

    title = title_raw
    body = str(pr.get("body") or "")
    if not (_is_english_by_ascii_ratio(title) and _is_english_by_ascii_ratio(body)):
        return "content_not_in_english"

    nodes = pr.get("files_nodes") or []
    lang_reason = _language_whitelist_reject_reason(nodes)
    if lang_reason:
        return lang_reason

    file_reason = file_gates_reject_reason(nodes)
    if file_reason:
        return file_reason

    patch = github_rest.fetch_pull_request_patch(
        token,
        owner,
        repo,
        base_sha=str(pr.get("baseRefOid") or ""),
        head_sha=str(pr.get("headRefOid") or ""),
    )
    if not patch:
        return "full_patch_retrieval"

    if _approx_source_delta(nodes) < MIN_PR_CODE_CHANGES:
        return "code_changes_not_sufficient"

    ensure_cyclomatic_proxy_on_row(token, owner, repo, pr, patch=patch)
    return None


def run_pr_analysis(
    *,
    token: str,
    owner: str,
    repo: str,
    default_branch: str,
    merged_to_default_only: bool,
    max_prs_to_scan: int,
    merged_prs_screening_only: bool = False,
    pr_analysis_lookback_days: int = DEFAULT_PR_ANALYSIS_LOOKBACK_DAYS,
) -> tuple[PrAnalysisReport, tuple[dict[str, Any], ...]]:
    """Paginate PRs from GraphQL and apply first-stage screening filters.

    By default uses the **default** ``pullRequests`` connection (no ``states`` filter) and
    **CREATED_AT** order.
    Set ``merged_prs_screening_only=True`` for the historic merged-only, ``UPDATED_AT`` feed.

    ``merged_to_default_only`` is kept for API compatibility with gather counts only.

    ``pr_analysis_lookback_days`` filters **merged** rows by ``mergedAt`` (drops merges older
    than the window; ``0`` disables merged-time filtering). When lookback is enabled (days > 0),
    PRs without ``mergedAt`` (e.g. open) are **not** counted toward the scan sample so the cap
    applies to merged work inside the window.

    **Listing feed:** If a merged-time window is active (days > 0), this function **always uses
    GitHub's merged-only PR connection** (``states: MERGED``, ``UPDATED_AT`` desc), even when
    ``merged_prs_screening_only`` is false. The default all-states ``CREATED_AT`` feed is
    dominated by open PRs at the top; none pass ``mergedAt`` filtering, so pagination hits the
    consecutive-empty-page guard almost immediately and ``total_analyzed`` collapses to a tiny
    number. Use lookback **0** if you need the default open+closed listing.

    Pagination stops when enough rows are collected, the GraphQL cursor ends, a **page cap**
    is hit (see module constants), or — with lookback enabled — **many consecutive pages**
    add no qualifying rows (guards against unbounded calls when yield per page is ~zero).

    Sample acceptance requires a non-empty **git compare** patch (REST) after file gates.

    Returns ``(report, accepted_row_dicts)`` — accepted rows align with ``report.passed``.
    """
    cutoff = lookback_cutoff_utc(pr_analysis_lookback_days)
    # Default CREATED_AT listing + mergedAt lookback yields pages of open PRs (0 rows kept).
    graphql_merged_only = merged_prs_screening_only or (cutoff is not None)
    if cutoff is not None and not merged_prs_screening_only:
        progress(
            "PR screening: merged-time lookback active — using merged-only GraphQL PR feed "
            "(states=MERGED, UPDATED_AT desc) instead of the default listing so pagination "
            "reaches merged PRs inside the window.",
        )
    lb_note = merged_at_lookback_log_suffix(pr_analysis_lookback_days)
    progress(
        "PR screening: fetching up to %s PR(s) (GraphQL, %s)%s...",
        max_prs_to_scan,
        "merged only, UPDATED_AT desc"
        if graphql_merged_only
        else "default PR listing, CREATED_AT desc",
        lb_note,
    )
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    pages_fetched = 0
    consecutive_empty_lookback_pages = 0

    while len(rows) < max_prs_to_scan:
        pages_fetched += 1
        if pages_fetched > _PR_ANALYSIS_MAX_GRAPHQL_PAGES:
            progress(
                "PR screening: stopping — GraphQL page cap (%s pages ≈ %s PRs examined) "
                "reached with %s row(s) after merged-time lookback "
                "(increase --pr-analysis-max-prs, widen lookback, or use merged-only listing "
                "aligned with mergedAt via shorter windows).",
                _PR_ANALYSIS_MAX_GRAPHQL_PAGES,
                _PR_ANALYSIS_MAX_GRAPHQL_PAGES * 50,
                len(rows),
            )
            break
        chunk, _, next_cursor = github_rest.fetch_prs_analysis_page(
            token,
            owner,
            repo,
            cursor=cursor,
            page_size=50,
            merged_only=graphql_merged_only,
        )
        if not chunk:
            break
        before = len(rows)
        for pr in chunk:
            if not row_passes_merged_at_lookback(pr, cutoff):
                continue
            rows.append(pr)
            if len(rows) >= max_prs_to_scan:
                break
        if cutoff is not None:
            if len(rows) == before:
                consecutive_empty_lookback_pages += 1
                if consecutive_empty_lookback_pages >= _PR_ANALYSIS_LOOKBACK_EMPTY_PAGE_LIMIT:
                    progress(
                        "PR screening: stopping — %s consecutive GraphQL pages added no PRs "
                        "inside merged-time lookback (have %s row(s); "
                        "UPDATED_AT order can diverge from mergedAt — avoiding unbounded calls).",
                        consecutive_empty_lookback_pages,
                        len(rows),
                    )
                    break
            else:
                consecutive_empty_lookback_pages = 0
        if len(rows) >= max_prs_to_scan:
            break
        if not next_cursor:
            break
        cursor = next_cursor

    total_analyzed = len(rows)
    progress(
        "PR screening: %s PR row(s) in sample after merged-time window%s; applying first-stage "
        "filters (default branch %s%s)...",
        total_analyzed,
        lb_note,
        default_branch,
        "; merged-only list" if graphql_merged_only else "; default PR list (no states filter)",
    )

    rejections: dict[str, int] = {}
    considered_for_filters = 0

    passed_out: list[PrAnalysisLine] = []
    accepted_rows: list[dict[str, Any]] = []
    rejected_detail: list[FirstStageRejected] = []

    def record_reject(pr_row: dict[str, Any], reason_code: str, title_display: str) -> None:
        rejections[reason_code] = rejections.get(reason_code, 0) + 1
        rejected_detail.append(
            FirstStageRejected(
                number=int(pr_row["number"]),
                title=title_display,
                reason_code=reason_code,
            )
        )
        progress(
            "PR screening: #%s rejected — reason=%s (%s): %s",
            int(pr_row["number"]),
            reason_code,
            first_stage_reject_label(reason_code),
            title_display.replace("\r", " ").replace("\n", " ").strip()[:120],
        )

    for pr in rows:
        considered_for_filters += 1

        title_raw = str(pr.get("title") or "").strip()
        title_display = title_raw or "(no title)"

        reason = screening_pass_or_reason(token=token, owner=owner, repo=repo, pr=pr)
        if reason:
            record_reject(pr, reason, title_display)
            continue

        score = pr_complexity_score(pr)
        progress(
            "PR screening: #%s accepted — complexity_score=%.2f: %s",
            int(pr["number"]),
            score,
            title_display.replace("\r", " ").replace("\n", " ").strip()[:120],
        )
        passed_out.append(
            PrAnalysisLine(
                number=int(pr["number"]),
                title=title_display,
                complexity_score=score,
            )
        )
        accepted_rows.append(pr)

    breakdown_tuple = tuple(sorted(rejections.items(), key=lambda x: (-x[1], x[0])))

    report = PrAnalysisReport(
        total_analyzed=total_analyzed,
        considered_for_filters=considered_for_filters,
        passed_first_filters=len(passed_out),
        rejection_breakdown=breakdown_tuple,
        passed=tuple(passed_out),
        first_stage_rejected=tuple(rejected_detail),
        pr_target_mode=False,
        f2p_limit=None,
        pr_analysis_lookback_days=pr_analysis_lookback_days,
    )
    progress(
        "PR screening: %s PR(s) passed first-stage filters (of %s considered for filters).",
        len(passed_out),
        considered_for_filters,
    )
    return report, tuple(accepted_rows)


def run_pr_analysis_for_explicit_targets(
    *,
    token: str,
    owner: str,
    repo: str,
    target_numbers: list[int],
    pr_analysis_lookback_days: int = DEFAULT_PR_ANALYSIS_LOOKBACK_DAYS,
) -> tuple[PrAnalysisReport, tuple[dict[str, Any], ...]]:
    """
    Run the same first-stage pipeline as :func:`run_pr_analysis` for explicit PR numbers
    (``--pr-target``), including language and file gates, patch fetch, and cyclomatic proxy.
    """
    rejections: dict[str, int] = {}
    rejected_detail: list[FirstStageRejected] = []
    passed_out: list[PrAnalysisLine] = []
    accepted_rows: list[dict[str, Any]] = []
    considered = 0
    n_t = len(target_numbers)
    cutoff = lookback_cutoff_utc(pr_analysis_lookback_days)
    lb_note = merged_at_lookback_log_suffix(pr_analysis_lookback_days)
    progress(
        "PR screening (explicit targets): %s PR number(s) requested%s...",
        n_t,
        lb_note,
    )

    for num in target_numbers:
        considered += 1
        pr = github_rest.fetch_merged_pull_request_row(token, owner, repo, number=num)
        title_display = str(pr.get("title") or "").strip() or "(no title)"
        if cutoff is not None:
            mt = parse_github_datetime_optional(pr.get("mergedAt"))
            if mt is not None and mt < cutoff:
                rejections["lookback_excluded"] = rejections.get("lookback_excluded", 0) + 1
                rejected_detail.append(
                    FirstStageRejected(
                        number=int(pr["number"]),
                        title=title_display,
                        reason_code="lookback_excluded",
                    )
                )
                progress(
                    "PR screening (target): #%s rejected — reason=%s (%s): %s",
                    int(pr["number"]),
                    "lookback_excluded",
                    first_stage_reject_label("lookback_excluded"),
                    title_display.replace("\r", " ").replace("\n", " ").strip()[:120],
                )
                continue
        reason = screening_pass_or_reason(token=token, owner=owner, repo=repo, pr=pr)
        if reason:
            rejections[reason] = rejections.get(reason, 0) + 1
            rejected_detail.append(
                FirstStageRejected(
                    number=int(pr["number"]),
                    title=title_display,
                    reason_code=reason,
                )
            )
            progress(
                "PR screening (target): #%s rejected — reason=%s (%s): %s",
                int(pr["number"]),
                reason,
                first_stage_reject_label(reason),
                title_display.replace("\r", " ").replace("\n", " ").strip()[:120],
            )
            continue

        score = pr_complexity_score(pr)
        progress(
            "PR screening (target): #%s accepted — complexity_score=%.2f: %s",
            int(pr["number"]),
            score,
            title_display.replace("\r", " ").replace("\n", " ").strip()[:120],
        )
        passed_out.append(
            PrAnalysisLine(
                number=int(pr["number"]),
                title=title_display,
                complexity_score=score,
            )
        )
        accepted_rows.append(pr)

    breakdown_tuple = tuple(sorted(rejections.items(), key=lambda x: (-x[1], x[0])))
    report = PrAnalysisReport(
        total_analyzed=n_t,
        considered_for_filters=considered,
        passed_first_filters=len(passed_out),
        rejection_breakdown=breakdown_tuple,
        passed=tuple(passed_out),
        first_stage_rejected=tuple(rejected_detail),
        pr_target_mode=True,
        f2p_limit=None,
        pr_analysis_lookback_days=pr_analysis_lookback_days,
    )
    progress(
        "PR screening (explicit targets): %s PR(s) passed first-stage filters "
        "(of %s requested).",
        len(passed_out),
        n_t,
    )
    return report, tuple(accepted_rows)
