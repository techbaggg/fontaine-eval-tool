"""Git helpers for three-stage F2P runs (checkout, apply patches, fetch SHAs)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(repo: Path, args: list[str], *, timeout: int = 120, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_bytes.decode() if input_bytes else None,
    )


def git_cat_file_ok(repo: Path, sha: str) -> bool:
    if len(sha) < 7:
        return False
    r = _run(repo, ["cat-file", "-t", sha], timeout=30)
    return r.returncode == 0


def git_fetch(repo: Path, ref: str, *, timeout: int = 180) -> bool:
    r = _run(repo, ["fetch", "-q", "origin", ref], timeout=timeout)
    return r.returncode == 0


def ensure_commit(repo: Path, sha: str) -> str | None:
    if git_cat_file_ok(repo, sha):
        return None
    git_fetch(repo, sha)
    if git_cat_file_ok(repo, sha):
        return None
    return f"Git object not available locally and fetch failed: {sha[:12]}"


def checkout_clean(repo: Path, sha: str) -> str | None:
    err = ensure_commit(repo, sha)
    if err:
        return err

    _run(repo, ["reset", "--hard"], timeout=60)
    _run(repo, ["clean", "-fd"], timeout=60)
    r = _run(repo, ["checkout", "--force", sha], timeout=120)
    if r.returncode != 0:
        return (r.stderr or r.stdout or "git checkout failed")[:800]
    _run(repo, ["clean", "-fd"], timeout=60)
    return None


def git_diff_patch(repo: Path, base_sha: str, head_sha: str) -> str | None:
    r = _run(
        repo,
        ["diff", "--binary", f"{base_sha}..{head_sha}"],
        timeout=180,
    )
    if r.returncode != 0:
        return None
    return r.stdout or ""


def apply_full_patch(repo: Path, base_sha: str, head_sha: str) -> str | None:
    patch = git_diff_patch(repo, base_sha, head_sha)
    if patch is None:
        return "git diff failed"
    if not patch.strip():
        return None

    r = subprocess.run(
        ["git", "-C", str(repo), "apply", "--verbose", "--reject", "--whitespace=nowarn", "-"],
        input=patch,
        capture_output=True,
        text=True,
        timeout=300,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[:600]
        return detail or f"git apply failed ({r.returncode})"
    return None


def list_deleted_paths(repo: Path, base_sha: str, head_sha: str) -> set[str]:
    r = _run(repo, ["diff", "--name-only", "--diff-filter=D", f"{base_sha}..{head_sha}"], timeout=120)
    if r.returncode != 0:
        return set()
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}


def apply_test_files_from_head(
    repo: Path,
    paths: list[str],
    head_sha: str,
    base_sha: str,
) -> str | None:
    deleted = list_deleted_paths(repo, base_sha, head_sha)
    to_apply = [p for p in paths if p not in deleted]
    if not to_apply:
        return None

    failed = 0
    for p in to_apply:
        r = _run(repo, ["checkout", head_sha, "--", p], timeout=60)
        if r.returncode != 0:
            failed += 1
    if failed == len(to_apply):
        return f"Could not checkout any test files from {head_sha[:12]} ({failed} paths)"
    return None


def path_exists_at(repo: Path, path: str, sha: str) -> bool:
    r = _run(repo, ["cat-file", "-e", f"{sha}:{path}"], timeout=30)
    return r.returncode == 0


def has_new_test_file(repo: Path, base_sha: str, head_sha: str, test_paths: list[str]) -> bool:
    for p in test_paths:
        if not path_exists_at(repo, p, base_sha) and path_exists_at(repo, p, head_sha):
            return True
    return False
