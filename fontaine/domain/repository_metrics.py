"""
Filesystem + git signals for repository screening.

Standalone heuristics for inventory, CI, testing, and git-history style signals.
"""

from __future__ import annotations

from collections import defaultdict
import json
import re
import statistics
from dataclasses import dataclass
from pathlib import Path

from fontaine.adapters.github_http_counter import record_github_http_request
from fontaine.domain.git_util import git_run, git_toplevel, logical_line_count

# --- Path classification ---
_VENDOR_DIRS = frozenset(
    {
        "node_modules",
        "vendor",
        ".pnpm-store",
        "Pods",
        ".carthage",
        "venv",
        ".venv",
        "__pycache__",
        ".gradle",
        "dist",
        "build",
        "target",
        ".git",
    }
)


def _looks_like_vendor(rel: str) -> bool:
    parts = [p.lower() for p in Path(rel).parts]
    return any(p in _VENDOR_DIRS for p in parts)


# Path segments that usually mean prose / hand-written docs (not application source).
_DOC_PATH_SEGMENTS = frozenset(
    {"docs", "documentation", "guide", "guides", "manual", "man"}
)


def _looks_like_documentation(rel: str) -> bool:
    """README-style files and trees like ``docs/`` — excluded from inventory metrics."""
    norm = rel.replace("\\", "/").lower()
    parts = norm.split("/")
    if any(p in _DOC_PATH_SEGMENTS for p in parts):
        return True

    base = Path(rel).name.lower()
    stem = Path(rel).stem.lower()
    if stem == "readme" or base.startswith("readme."):
        return True
    if base.startswith("contributing"):
        return True
    if stem == "changelog" or base.startswith("changelog"):
        return True
    if stem == "license" or base.startswith("license."):
        return True
    if base in ("copying", "copying.txt", "notice", "notice.txt"):
        return True
    if "code_of_conduct" in base:
        return True
    if base in ("authors", "contributors", "maintainers", "credits"):
        return True
    if base == "security.md" or base.startswith("security.policy"):
        return True
    return False


_TEST_PATH_HINTS = ("test", "tests", "__tests__", "spec", "fixtures", "mock", "mocks")


def _looks_like_test(rel: str) -> bool:
    lower = rel.lower()
    base = Path(rel).name.lower()
    if any(h in lower for h in _TEST_PATH_HINTS):
        return True
    if base.startswith("test_") or base.endswith("_test.py"):
        return True
    if ".test." in base or ".spec." in base:
        return True
    if base in {"conftest.py", "jest.setup.ts", "jest.setup.js"}:
        return True
    return False


_SOURCE_SUFFIXES = frozenset({
    ".py",
    ".pyi",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".vue",
    ".java",
    ".kt",
    ".kts",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".cs",
    ".swift",
    ".scala",
    ".cpp",
    ".cc",
    ".cxx",
    ".c",
    ".h",
    ".hpp",
    ".sql",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".r",
    ".dart",
    ".lua",
    ".ex",
    ".exs",
    ".erl",
    ".hs",
    ".ml",
    ".css",
    ".scss",
    ".sass",
    ".html",
})

_SUFFIX_TO_LANGUAGE = {
    ".py": "Python",
    ".pyi": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".vue": "Vue",
    ".java": "Java",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".swift": "Swift",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".h": "C/C++",
    ".hpp": "C++",
    ".c": "C",
    ".sql": "SQL",
    ".sh": "Shell",
    ".dart": "Dart",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
}

_SHORTSTAT_INS = re.compile(r"(\d+)\s+insertions?\(\+\)")
_SHORTSTAT_DEL = re.compile(r"(\d+)\s+deletions?\(-\)")


def _shortstat_total(line: str) -> int:
    ins = sum(int(m.group(1)) for m in _SHORTSTAT_INS.finditer(line))
    delete = sum(int(m.group(1)) for m in _SHORTSTAT_DEL.finditer(line))
    return ins + delete


def _numstat_block_total(block: str) -> int:
    tot = 0
    for line in block.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        if parts[0] == "-" and parts[1] == "-":
            continue
        try:
            a = int(parts[0])
            d = int(parts[1])
            tot += a + d
        except ValueError:
            continue
    return tot


def _numstat_delta_for_rev(worktree: Path, rev: str) -> int:
    """
    Add+delete line count for one commit (text files in numstat).

    ``-m`` expands merge commits against each parent; for normal commits Git leaves
    output unchanged.
    """
    sh = git_run(worktree, "show", "-m", "--numstat", "--format=", rev.strip())
    if sh.returncode != 0:
        return 0
    return _numstat_block_total(sh.stdout)


def _git_log_numstat_delta_by_sha(
    worktree: Path,
    *,
    merge_parents: bool,
    no_merges: bool,
) -> dict[str, int]:
    """
    Per-commit summed add+delete deltas from ``git log --numstat``.

    ``merge_parents=True`` passes ``-m`` so merges get real line totals (and may emit
    multiple numstat chunks per SHA, which we sum). Without it, merges often contribute
    ~0 lines in numstat even when ``--no-merges`` is not used—so \"incl. merges\" vs
    \"non-merge\" top-N lists looked identical.

    ``no_merges=True`` walks only non-merge commits (still one chunk per SHA).
    """
    sep = "\x1e"
    args: list[str] = ["log"]
    if merge_parents:
        args.append("-m")
    if no_merges:
        args.append("--no-merges")
    args.extend(["--numstat", f"--pretty=format:{sep}%H%n"])

    log = git_run(worktree, *args)
    by_sha: dict[str, int] = defaultdict(int)
    if log.returncode != 0:
        return by_sha

    for block in log.stdout.split(sep):
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        if not lines:
            continue
        sha = lines[0].strip()
        body = "\n".join(lines[1:])
        by_sha[sha] += _numstat_block_total(body)

    return by_sha


@dataclass(frozen=True, slots=True)
class RepositoryMetrics:
    """Deterministic snapshot of repo shape + recent git activity."""

    total_files: int
    source_files: int
    test_files: int
    total_lines: int
    application_lines: int
    test_lines: int
    primary_language: str

    cicd_detected: bool
    cicd_placeholders: tuple[str, ...]

    testing_tool_hints: tuple[str, ...]

    commits_total: int | None
    commits_last_180_days: int | None
    commits_last_365_days: int | None

    branch_refs_total: int | None
    tag_refs_total: int | None
    github_branches_total: int | None

    earliest_ten_delta_lines_including_merges: tuple[int, ...]
    earliest_ten_delta_lines_non_merge_only: tuple[int, ...]
    top_ten_delta_lines_including_merges: tuple[int, ...]
    top_ten_delta_lines_non_merge_only: tuple[int, ...]

    commit_subject_chars_mean: float | None
    commit_subject_chars_median: float | None
    commit_subject_chars_variance: float | None

    avg_lines_changed_per_commit_sample: float | None
    median_lines_changed_per_commit_sample: float | None

    churn_coefficient: float | None
    approximate_comment_ratio: float | None

    issues_open: int | None
    issues_closed: int | None
    issues_total: int | None

    redistributable_signals_score: int
    redistributable_signals: tuple[str, ...]

    authoring_anomaly_score: int
    authoring_anomaly_signals: tuple[str, ...]


def compute_repository_metrics(
    worktree: Path,
    *,
    github_owner: str | None = None,
    github_repo: str | None = None,
    github_token: str | None = None,
) -> RepositoryMetrics:
    worktree = worktree.resolve()
    top = git_toplevel(worktree)

    r = git_run(worktree, "ls-files")
    files: list[str] = []
    if r.returncode == 0:
        for rel in r.stdout.splitlines():
            rel = rel.strip()
            if not rel:
                continue
            if _looks_like_vendor(rel):
                continue
            if _looks_like_documentation(rel):
                continue
            p = (top / rel).resolve()
            try:
                p.relative_to(worktree)
            except ValueError:
                continue
            if p.is_file():
                files.append(rel)

    sf = tf = nf = 0
    lang_lines: dict[str, int] = {}
    app_lines = test_lines = total_lines = 0

    for rel in files:
        path = top / rel
        lc = logical_line_count(path)
        nf += 1
        suffix = Path(rel).suffix.lower()
        is_src = suffix in _SOURCE_SUFFIXES
        test = _looks_like_test(rel)

        if test:
            tf += 1
            test_lines += lc
        elif is_src:
            sf += 1
            app_lines += lc
            lang = _SUFFIX_TO_LANGUAGE.get(suffix, "Other")
            lang_lines[lang] = lang_lines.get(lang, 0) + lc
        total_lines += lc

    primary = (
        max(lang_lines.items(), key=lambda x: x[1])[0] if lang_lines else "Unknown"
    )

    cicd, cicd_hints = _scan_ci_signals(worktree)
    tooling = _infer_test_tooling(worktree)

    gm = _git_derived_metrics(worktree, total_lines=max(1, total_lines))
    comment_ratio = _comment_ratio_heuristic(top, files)

    redist = _score_redistributable(worktree)
    author = _score_authoring_patterns(worktree)

    issue_open = issue_closed = issue_total = None
    github_branches: int | None = None
    if github_owner and github_repo and github_token:
        issue_open, issue_closed, issue_total = _fetch_issue_counts_http(
            github_token, github_owner, github_repo
        )
        github_branches = _fetch_github_branch_count_http(
            github_token, github_owner, github_repo
        )

    return RepositoryMetrics(
        total_files=nf,
        source_files=sf,
        test_files=tf,
        total_lines=total_lines,
        application_lines=app_lines,
        test_lines=test_lines,
        primary_language=primary,
        cicd_detected=cicd,
        cicd_placeholders=cicd_hints,
        testing_tool_hints=tooling,
        commits_total=gm.get("commits_total"),
        commits_last_180_days=gm.get("commits_180d"),
        commits_last_365_days=gm.get("commits_365d"),
        branch_refs_total=gm.get("branch_refs"),
        tag_refs_total=gm.get("tag_refs"),
        github_branches_total=github_branches,
        earliest_ten_delta_lines_including_merges=gm.get("first10_any", ()),
        earliest_ten_delta_lines_non_merge_only=gm.get("first10_no_merge", ()),
        top_ten_delta_lines_including_merges=gm.get("top10_any", ()),
        top_ten_delta_lines_non_merge_only=gm.get("top10_no_merge", ()),
        commit_subject_chars_mean=gm.get("subj_mean"),
        commit_subject_chars_median=gm.get("subj_med"),
        commit_subject_chars_variance=gm.get("subj_var"),
        avg_lines_changed_per_commit_sample=gm.get("chg_mean"),
        median_lines_changed_per_commit_sample=gm.get("chg_med"),
        churn_coefficient=gm.get("churn"),
        approximate_comment_ratio=comment_ratio,
        issues_open=issue_open,
        issues_closed=issue_closed,
        issues_total=issue_total,
        redistributable_signals_score=redist["score"],
        redistributable_signals=tuple(redist["signals"]),
        authoring_anomaly_score=author["score"],
        authoring_anomaly_signals=tuple(author["signals"]),
    )


def _fetch_github_branch_count_http(
    token: str, owner: str, repo: str
) -> int | None:
    """
    Count branches the same way GitHub's branches list does (REST paginated ``/branches``).

    This is unrelated to ``refs/heads`` in a single-branch clone, which typically has one ref.
    """
    try:
        import httpx

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fontaine-repo-screening",
        }
        total = 0
        page = 1
        with httpx.Client(timeout=120.0, headers=headers) as c:
            while True:
                r = c.get(
                    f"https://api.github.com/repos/{owner}/{repo}/branches",
                    params={"per_page": 100, "page": page},
                )
                record_github_http_request()
                if r.status_code != 200:
                    return None
                batch = r.json()
                if not isinstance(batch, list):
                    return None
                total += len(batch)
                if len(batch) < 100:
                    break
                page += 1
                if page > 500:
                    break
        return total
    except Exception:
        return None


def _fetch_issue_counts_http(
    token: str, owner: str, repo: str
) -> tuple[int | None, int | None, int | None]:
    try:
        import httpx

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "fontaine-repo-screening",
        }

        def total(q: str) -> int:
            with httpx.Client(timeout=60.0, headers=headers) as c:
                r = c.get(
                    "https://api.github.com/search/issues",
                    params={"q": q, "per_page": 1},
                )
                record_github_http_request()
                if r.status_code != 200:
                    return 0
                return int(r.json().get("total_count", 0))

        rq = f"repo:{owner}/{repo}"
        op = total(f"{rq} is:issue is:open")
        cl = total(f"{rq} is:issue is:closed")
        return op, cl, op + cl
    except Exception:
        return None, None, None


def _scan_ci_signals(root: Path) -> tuple[bool, tuple[str, ...]]:
    hints: list[str] = []
    candidates = [
        root / ".github" / "workflows",
        root / ".gitlab-ci.yml",
        root / "azure-pipelines.yml",
        root / ".circleci",
        root / "Jenkinsfile",
        root / "buildkite.yml",
    ]
    for p in candidates:
        if p.is_dir():
            if any(p.glob("*.yml")) or any(p.glob("*.yaml")):
                hints.append(str(p.relative_to(root)))
        elif p.is_file():
            hints.append(p.name)
    if (root / "vercel.json").is_file():
        hints.append("vercel.json")
    return (len(hints) > 0), tuple(sorted(set(hints))[:12])


def _infer_test_tooling(root: Path) -> tuple[str, ...]:
    found: set[str] = set()
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            keys = " ".join(deps.keys()).lower()
            for name in ("jest", "mocha", "vitest", "ava", "tap", "cypress", "playwright"):
                if name in keys:
                    found.add(name)
        except (json.JSONDecodeError, OSError):
            pass
    for name in ("pytest.ini", "pyproject.toml", "tox.ini"):
        if (root / name).is_file():
            found.add("pytest-tooling")
            break
    if (root / "Cargo.toml").is_file():
        txt = (root / "Cargo.toml").read_text(encoding="utf-8", errors="ignore")
        if "[dev-dependencies]" in txt:
            found.add("rust-tests")
    if (root / "go.mod").is_file():
        found.add("go-test")
    return tuple(sorted(found))


def _git_derived_metrics(worktree: Path, *, total_lines: int) -> dict[str, object]:
    """
    Git statistics assume the worktree has **full history** for the analyzed branch when
    produced from a GitHub metrics clone (see :func:`fontaine.domain.gather.gather_repo_facts`).
    Shallow clones truncate ``rev-list``, earliest-commit deltas, and churn.
    Logs here use **full branch history** (no ``-n`` caps; churn is not limited to one year).
    """
    out: dict[str, object] = {}

    rc = git_run(worktree, "rev-list", "--count", "HEAD")
    out["commits_total"] = (
        int(rc.stdout.strip())
        if rc.returncode == 0 and rc.stdout.strip().isdigit()
        else None
    )

    r180 = git_run(
        worktree, "rev-list", "--count", "HEAD", "--since=180 days ago"
    )
    out["commits_180d"] = (
        int(r180.stdout.strip())
        if r180.returncode == 0 and r180.stdout.strip().isdigit()
        else None
    )
    r365 = git_run(
        worktree, "rev-list", "--count", "HEAD", "--since=365 days ago"
    )
    out["commits_365d"] = (
        int(r365.stdout.strip())
        if r365.returncode == 0 and r365.stdout.strip().isdigit()
        else None
    )

    # for-each-ref exits 0 with no output when there are no matching refs (unlike show-ref).
    heads = git_run(
        worktree,
        "for-each-ref",
        "--format=%(refname)",
        "refs/heads",
    )
    if heads.returncode == 0:
        out["branch_refs"] = len([ln for ln in heads.stdout.splitlines() if ln.strip()])
    else:
        out["branch_refs"] = None

    tags = git_run(
        worktree,
        "for-each-ref",
        "--format=%(refname)",
        "refs/tags",
    )
    if tags.returncode == 0:
        out["tag_refs"] = len([ln for ln in tags.stdout.splitlines() if ln.strip()])
    else:
        out["tag_refs"] = None

    # Chronologically *oldest* commits: ``rev-list --reverse HEAD`` lists old→new.
    # Do **not** use ``-n 10`` on rev-list: that limits the *walk* to the ``n``
    # commits nearest ``HEAD``, then ``--reverse`` only flips order—so you get
    # the **most recent** window, not the repository root era.
    first_any: list[int] = []
    earliest_any = git_run(worktree, "rev-list", "--reverse", "HEAD")
    if earliest_any.returncode == 0:
        for rev in earliest_any.stdout.splitlines():
            rev = rev.strip()
            if not rev:
                continue
            first_any.append(_numstat_delta_for_rev(worktree, rev))
            if len(first_any) >= 10:
                break
    out["first10_any"] = tuple(first_any[:10])

    first_nm: list[int] = []
    earliest_nm = git_run(worktree, "rev-list", "--reverse", "--no-merges", "HEAD")
    if earliest_nm.returncode == 0:
        for rev in earliest_nm.stdout.splitlines():
            rev = rev.strip()
            if not rev:
                continue
            first_nm.append(_numstat_delta_for_rev(worktree, rev))
            if len(first_nm) >= 10:
                break
    out["first10_no_merge"] = tuple(first_nm[:10])

    # Full history; ``-m`` so merge commits get non-trivial deltas (aggregated per SHA).
    deltas_any = _git_log_numstat_delta_by_sha(
        worktree, merge_parents=True, no_merges=False
    )
    sorted_any = sorted(deltas_any.values(), reverse=True)
    out["top10_any"] = tuple(sorted_any[:10])

    deltas_nm = _git_log_numstat_delta_by_sha(
        worktree, merge_parents=False, no_merges=True
    )
    sorted_nm = sorted(deltas_nm.values(), reverse=True)
    out["top10_no_merge"] = tuple(sorted_nm[:10])

    subj = git_run(worktree, "log", "--pretty=format:%s")
    lengths: list[int] = []
    if subj.returncode == 0:
        lengths = [len(line) for line in subj.stdout.splitlines() if line.strip()]
    if lengths:
        out["subj_mean"] = statistics.mean(lengths)
        out["subj_med"] = statistics.median(lengths)
        out["subj_var"] = statistics.pvariance(lengths) if len(lengths) > 1 else 0.0
    else:
        out["subj_mean"] = out["subj_med"] = out["subj_var"] = None

    # Per-commit change distribution (shortstat lines only), full history.
    statlog = git_run(
        worktree,
        "log",
        "--shortstat",
        "--pretty=oneline",
    )
    per_commit: list[int] = []
    if statlog.returncode == 0:
        for line in statlog.stdout.splitlines():
            if "insertion" in line or "deletion" in line:
                per_commit.append(_shortstat_total(line))
    if per_commit:
        out["chg_mean"] = statistics.mean(per_commit)
        out["chg_med"] = statistics.median(per_commit)
    else:
        out["chg_mean"] = out["chg_med"] = None

    ylog = git_run(
        worktree,
        "log",
        "--shortstat",
        "--pretty=format:",
    )
    activity = 0
    if ylog.returncode == 0:
        for line in ylog.stdout.splitlines():
            activity += _shortstat_total(line)
    tl = max(1, total_lines)
    out["churn"] = min(2.0, activity / (2.0 * float(tl)))

    return out


_COMMENTISH = re.compile(
    r"^\s*(#|//|/\*|\*|--)|^\s*/\*|\*/\s*$"
)


def _comment_ratio_heuristic(top: Path, rel_files: list[str]) -> float | None:
    """Comment-ish lines / all lines over **all** eligible tracked source files (no subsample)."""
    candidates = [
        f
        for f in rel_files
        if Path(f).suffix.lower()
        in {".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java"}
    ]
    if not candidates:
        return None
    num = den = 0
    for rel in candidates:
        p = top / rel
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            den += 1
            if _COMMENTISH.match(line.strip()) or line.strip().startswith("<!--"):
                num += 1
    if den == 0:
        return None
    return round(num / den, 4)


# README phrases often used when a project identifies as open source or names a license.
_OSS_README_KEYWORDS: tuple[str, ...] = (
    "open source",
    "opensource",
    "foss",
    "free and open source",
    "free software",
    "osi approved",
    "osi-approved",
    "copyleft",
    "permissive license",
    "mit license",
    "apache license",
    "apache software license",
    "apache-2",
    "apache 2",
    "apache license 2",
    "gnu general public license",
    "gnu lesser general public license",
    "gnu affero general public license",
    "gpl",
    "lgpl",
    "agpl",
    "mozilla public license",
    "bsd license",
    "bsd 2-clause",
    "bsd 3-clause",
    "isc license",
    "the unlicense",
    "unlicense",
    "zlib license",
    "boost software license",
    "eclipse public license",
    "eupl",
    "cc0",
    "creative commons zero",
    "creative commons cc0",
    "public domain dedication",
    "spdx",
    "spdx-license-identifier",
)

# (score id, glob patterns relative to repo root). First match per id counts once.
_OSS_SURFACE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("contributing", ("CONTRIBUTING*",)),
    ("code_of_conduct", ("CODE_OF_CONDUCT*", "CODE-OF-CONDUCT*")),
    (
        "security_policy",
        ("SECURITY.md", "SECURITY.rst", "SECURITY.txt"),
    ),
    ("notice", ("NOTICE", "NOTICE.txt")),
    ("attribution", ("AUTHORS*", "CREDITS", "CREDITS.md", "MAINTAINERS*")),
)

_MANIFEST_LICENSE_FILENAMES: tuple[str, ...] = (
    "package.json",
    "composer.json",
    "pyproject.toml",
    "Cargo.toml",
    "setup.cfg",
    "setup.py",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "go.mod",
)


def _readme_lower(root: Path) -> str | None:
    for name in ("README.md", "README.rst", "README.txt"):
        p = root / name
        if not p.is_file():
            continue
        try:
            return p.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            continue
    return None


def _oss_readme_keyword_hits(txt_lower: str) -> list[str]:
    return [kw for kw in _OSS_README_KEYWORDS if kw in txt_lower]


def _manifests_declaring_license(root: Path) -> list[str]:
    """Filenames whose contents plausibly declare a license (heuristic)."""
    found: list[str] = []
    for name in _MANIFEST_LICENSE_FILENAMES:
        p = root / name
        if not p.is_file():
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        low = txt.lower()
        if name in ("package.json", "composer.json"):
            try:
                data = json.loads(txt)
                if isinstance(data, dict) and data.get("license"):
                    found.append(name)
            except (json.JSONDecodeError, TypeError):
                pass
            continue
        if name == "go.mod":
            if "spdx-license-identifier" in low:
                found.append(name)
            continue
        if '"license"' in low or "license =" in low or "license=" in low:
            found.append(name)
    return found


def _oss_surface_categories(root: Path) -> list[str]:
    """Common OSS-adjacent root files (excluding LICENSE*, already scored separately)."""
    hit: list[str] = []
    for cat, patterns in _OSS_SURFACE_HINTS:
        if any(root.glob(pat) for pat in patterns):
            hit.append(cat)
            continue
    gh = root / ".github" / "SECURITY.md"
    if gh.is_file() and "security_policy" not in hit:
        hit.append("security_policy")
    return hit


# Exclusive rubric (max 100): pick one license tier, pick one readme tier, add governance surface.
# Does not stack LICENSE + manifest nor readme_present + readme OSS keywords — same fact twice.
_REDIST_LICENSE_FILE_PTS = 40
_REDIST_LICENSE_MANIFEST_PTS = 25
_REDIST_README_KEYWORD_PTS = 35
_REDIST_README_PRESENT_PTS = 20
_REDIST_SURFACE_PTS_EACH = 5
_REDIST_SURFACE_CAP = 25


def _score_redistributable(root: Path) -> dict[str, object]:
    score = 0
    signals: list[str] = []
    lic = list(root.glob("LICENSE*")) + list(root.glob("COPYING*"))
    manifests = _manifests_declaring_license(root)

    if lic:
        score += _REDIST_LICENSE_FILE_PTS
        signals.append(f"license_file:{lic[0].name}")
    elif manifests:
        score += _REDIST_LICENSE_MANIFEST_PTS
        signals.append(f"license_field:{','.join(manifests)}")

    readme_txt = _readme_lower(root)
    if readme_txt is not None:
        kw_hits = _oss_readme_keyword_hits(readme_txt)
        if kw_hits:
            score += _REDIST_README_KEYWORD_PTS
            preview = ",".join(kw_hits[:6])
            more = len(kw_hits) - 6
            sig = f"readme_oss_keywords:{preview}"
            if more > 0:
                sig += f",{more}_more"
            signals.append(sig)
        else:
            score += _REDIST_README_PRESENT_PTS
            signals.append("readme_present")

    surface = _oss_surface_categories(root)
    if surface:
        pts = min(
            _REDIST_SURFACE_CAP,
            _REDIST_SURFACE_PTS_EACH * len(surface),
        )
        score += pts
        signals.append(f"oss_surface:{','.join(surface)}")

    score = min(100, score)
    return {"score": score, "signals": signals}


_GENERIC_MSG = re.compile(
    r"^(update|fix|wip|test|chore|bump|merge|initial commit|sync)",
    re.I,
)


def _score_authoring_patterns(worktree: Path) -> dict[str, object]:
    score = 0
    signals: list[str] = []
    log = git_run(worktree, "log", "--pretty=format:%s")
    if log.returncode != 0:
        return {"score": 0, "signals": ["insufficient_git_history"]}
    msgs = [m.strip() for m in log.stdout.splitlines() if m.strip()]
    if not msgs:
        return {"score": 0, "signals": ["no_commit_messages"]}
    generic = sum(1 for m in msgs if _GENERIC_MSG.match(m))
    ratio = generic / len(msgs)
    if ratio >= 0.45:
        score += 35
        signals.append(f"generic_subjects:{ratio:.2f}")
    elif ratio >= 0.28:
        score += 18
        signals.append(f"generic_subjects:{ratio:.2f}")
    score = min(100, score)
    return {"score": score, "signals": signals}