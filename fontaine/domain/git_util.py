"""Small git helpers shared by gather and repository metrics."""

from __future__ import annotations

import subprocess
from pathlib import Path

# Full-line ``#`` comments (filename suffix, lower-case).
_HASH_COMMENT_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".rb",
        ".r",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".properties",
        ".dockerignore",
    }
)

# ``//`` or ``/* … */``-style line comments (suffix lower-case).
_C_STYLE_COMMENT_SUFFIXES = frozenset(
    {
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".go",
        ".rs",
        ".swift",
        ".kt",
        ".kts",
        ".cs",
        ".php",
        ".scala",
        ".dart",
        ".lua",  # also ``--`` handled below
        ".cpp",
        ".cc",
        ".cxx",
        ".c",
        ".h",
        ".hpp",
        ".vue",
    }
)

_CSS_LIKE_SUFFIXES = frozenset({".css", ".scss", ".sass"})


def git_run(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def git_toplevel(cwd: Path) -> Path:
    r = git_run(cwd, "rev-parse", "--show-toplevel")
    if r.returncode != 0 or not r.stdout.strip():
        return cwd.resolve()
    return Path(r.stdout.strip()).resolve()


def git_default_branch(cwd: Path) -> str:
    r = git_run(cwd, "symbolic-ref", "refs/remotes/origin/HEAD")
    if r.returncode == 0 and r.stdout.strip():
        ref = r.stdout.strip()
        prefix = "refs/remotes/origin/"
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    r2 = git_run(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if r2.returncode == 0:
        b = r2.stdout.strip()
        if b and b != "HEAD":
            return b
    return "main"


def tracked_files(worktree: Path) -> list[str]:
    """Paths relative to repo root (git ls-files)."""
    top = git_toplevel(worktree)
    r = git_run(worktree, "ls-files", "-z")
    if r.returncode != 0:
        return []
    paths: list[str] = []
    for rel in r.stdout.split("\0"):
        if not rel:
            continue
        path = (top / rel).resolve()
        try:
            path.relative_to(worktree.resolve())
        except ValueError:
            continue
        paths.append(rel)
    return paths


def line_count_for_file(path: Path) -> int:
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if b"\0" in data[:8192]:
        return 0
    if not data:
        return 0
    n = data.count(b"\n")
    if not data.endswith(b"\n"):
        n += 1
    return n


def _line_is_heuristic_comment_only(stripped: str, suffix: str, filename: str) -> bool:
    """True if *stripped* line is treated as comment-only for metrics (best-effort)."""
    fl = filename.lower()
    if fl == "dockerfile" or fl.startswith("dockerfile."):
        return stripped.startswith("#")

    if suffix == ".md":
        return stripped.startswith("<!--")

    if suffix in _HASH_COMMENT_SUFFIXES:
        return stripped.startswith("#")

    if suffix in _C_STYLE_COMMENT_SUFFIXES:
        if stripped.startswith("//"):
            return True
        if stripped.startswith("/*"):
            return True
        if stripped.endswith("*/") and "/*" in stripped:
            return True
        return False

    if suffix == ".lua":
        return stripped.startswith("--")

    if suffix == ".sql":
        return stripped.startswith("--")

    if suffix in {".hs", ".erl"}:
        return stripped.startswith("--")

    if suffix in {".ex", ".exs"}:
        return stripped.startswith("#")

    if suffix in _CSS_LIKE_SUFFIXES:
        if stripped.startswith("//"):
            return True
        if stripped.startswith("/*"):
            return True
        if stripped.endswith("*/") and "/*" in stripped:
            return True
        return False

    if suffix in {".html", ".htm", ".xml"}:
        return stripped.startswith("<!--")

    return False


def logical_line_count(path: Path) -> int:
    """
    Count lines that contribute to LoC-style metrics: drop blank lines and lines that are
    *only* comments (by suffix-specific heuristics). Docstrings and inline comments are
    still counted; multi-line comment bodies are only skipped when each line matches the
    heuristic (no full parser).
    """
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if b"\0" in data[:8192]:
        return 0
    if not data:
        return 0

    suffix = path.suffix.lower()
    filename = path.name
    text = data.decode("utf-8", errors="ignore")
    n = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _line_is_heuristic_comment_only(stripped, suffix, filename):
            continue
        n += 1
    return n
