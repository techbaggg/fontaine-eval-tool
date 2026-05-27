"""
Discover monorepo / workspace package roots from changed file paths.

Heuristic: first path segment may be a project directory; if none match, fall back to
repo root or direct children that look like projects.
"""

from __future__ import annotations

from pathlib import Path

# Workspace / project markers (language-agnostic).
PROJECT_MARKERS = (
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "Gemfile",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
)


def _is_project_dir(path: Path) -> bool:
    return any((path / marker).exists() for marker in PROJECT_MARKERS)


def _extract_package_from_path(file_path: str, repo_path: Path) -> Path | None:
    parts = file_path.replace("\\", "/").split("/")
    if len(parts) < 2:
        return None
    candidate = repo_path / parts[0]
    if candidate.is_dir() and _is_project_dir(candidate):
        return candidate
    return None


def get_affected_packages(repo_path: Path, changed_files: list[str]) -> list[Path]:
    """
    Return sorted unique package directories that should run their own install + tests.

    ``changed_files`` are repo-relative POSIX paths (e.g. from PR file nodes).
    Callers building a per-package merge list should pass **changed test paths**
    only so incidental non-test edits do not reroute runs to unrelated subtrees.
    """
    root = repo_path.resolve()
    packages: set[Path] = set()
    for f in changed_files:
        if not f.strip():
            continue
        pkg = _extract_package_from_path(f, root)
        if pkg is not None:
            packages.add(pkg.resolve())

    if not packages:
        if _is_project_dir(root):
            return [root]
        for sub in sorted(root.iterdir()):
            if sub.is_dir() and _is_project_dir(sub):
                packages.add(sub.resolve())

    if not packages:
        return [root]

    return sorted(packages, key=lambda p: str(p).lower())
