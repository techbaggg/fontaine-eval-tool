"""Load optional `.local.env` from the Fontaine checkout and derive GitHub coords from ``--repo-dir``."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_ENV_FILENAMES = (".local.env",)


def _parse_bool(raw: str) -> bool | None:
    s = raw.strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def _canonical_key(key: str) -> str | None:
    k = key.strip().upper().replace("-", "_")
    aliases = {
        "FONTAINE_OWNER": "owner",
        "OWNER": "owner",
        "FONTAINE_REPO": "repo",
        "REPO": "repo",
        "FONTAINE_FORMAT": "format",
        "FORMAT": "format",
        "FONTAINE_METRICS": "metrics",
        "METRICS": "metrics",
        "FONTAINE_PR_ANALYSIS": "pr_analysis",
        "PR_ANALYSIS": "pr_analysis",
        "FONTAINE_PR_F2P": "pr_f2p",
        "PR_F2P": "pr_f2p",
        "FONTAINE_PR_F2P_LIMIT": "pr_f2p_limit",
        "PR_F2P_LIMIT": "pr_f2p_limit",
        "FONTAINE_PR_ANALYSIS_LOOKBACK_DAYS": "pr_analysis_lookback_days",
        "PR_ANALYSIS_LOOKBACK_DAYS": "pr_analysis_lookback_days",
        "FONTAINE_PR_ANALYSIS_MERGED_ONLY": "pr_analysis_merged_only",
        "PR_ANALYSIS_MERGED_ONLY": "pr_analysis_merged_only",
    }
    return aliases.get(k)


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        key, _, val = s.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        ck = _canonical_key(key)
        if ck:
            out[ck] = val
    return out


def _fontaine_config_base_dirs() -> list[Path]:
    """
    Directories that may contain ``.local.env`` for Fontaine itself.

    Order (later directories override earlier when merging): parent of the installed
    ``fontaine`` package (your git checkout root in development), then
    :envvar:`FONTAINE_CONFIG_DIR` if set (optional override when the package is not on disk
    next to your config).
    """
    seen: set[Path] = set()
    ordered: list[Path] = []
    # Normal layout: …/helix-fontaine/fontaine/env_config.py → checkout root is parent.parent
    pkg_parent = Path(__file__).resolve().parent.parent
    if pkg_parent not in seen:
        seen.add(pkg_parent)
        ordered.append(pkg_parent)
    override = (os.environ.get("FONTAINE_CONFIG_DIR") or "").strip()
    if override:
        p = Path(override).expanduser().resolve()
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def load_fontaine_env_overlays() -> dict[str, str | bool | int]:
    """
    Merge ``.local.env`` then ``fontaine/.local.env`` from each Fontaine config base directory.

    Later directories (see :func:`_fontaine_config_base_dirs`) and later filenames override.
    """
    merged: dict[str, str] = {}
    for base in _fontaine_config_base_dirs():
        for name in _ENV_FILENAMES:
            merged.update(_parse_env_file(base / name))
        merged.update(_parse_env_file(base / "fontaine" / ".local.env"))

    out: dict[str, str | bool | int] = {}
    for k, v in merged.items():
        if k in ("metrics", "pr_analysis", "pr_f2p", "pr_analysis_merged_only"):
            b = _parse_bool(str(v))
            if b is not None:
                out[k] = b
        elif k == "format":
            low = str(v).strip().lower()
            if low in ("human", "json"):
                out[k] = low
        elif k in ("owner", "repo"):
            s = str(v).strip()
            if s:
                out[k] = s
        elif k == "pr_f2p_limit":
            try:
                n = int(str(v).strip())
                if n >= 1:
                    out[k] = n
            except ValueError:
                pass
        elif k == "pr_analysis_lookback_days":
            try:
                n = int(str(v).strip())
                if n >= 0:
                    out[k] = n
            except ValueError:
                pass
    return out


_GITHUB_HTTPS = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$",
    re.I,
)
_GITHUB_SSH = re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", re.I)
_GITHUB_SSH_ALT = re.compile(r"^ssh://git@github\.com/([^/]+)/([^/]+?)(?:\.git)?$", re.I)


def github_owner_repo_from_git(repo_dir: Path) -> tuple[str, str] | None:
    """
    Parse ``origin`` (or :envvar:`FONTAINE_GIT_REMOTE`) URL for github.com ``owner/repo``.

    Uses the **analyzed** clone (``--repo-dir``), not the Fontaine checkout.
    """
    env_override = (os.environ.get("FONTAINE_GIT_REMOTE") or "").strip()
    remotes = [env_override] if env_override else ["origin"]
    for name in remotes:
        if not name:
            continue
        r = subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "get-url", name],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0:
            continue
        url = (r.stdout or "").strip()
        if not url:
            continue
        for rx in (_GITHUB_HTTPS, _GITHUB_SSH, _GITHUB_SSH_ALT):
            m = rx.match(url)
            if m:
                owner, repo = m.group(1), m.group(2)
                if repo.endswith(".git"):
                    repo = repo[: -4]
                return owner, repo
    return None


def build_arg_defaults(analyzed_repo_dir: Path | None) -> dict[str, object]:
    """
    Defaults from Fontaine's ``.local.env`` plus optional ``owner``/``repo`` from the
    analyzed repository's git remote when those keys are unset.
    """
    d: dict[str, str | bool | int] = dict(load_fontaine_env_overlays())

    if analyzed_repo_dir is not None:
        root = analyzed_repo_dir.expanduser().resolve()
        if root.is_dir():
            gh = github_owner_repo_from_git(root)
            if gh:
                if not d.get("owner"):
                    d["owner"] = gh[0]
                if not d.get("repo"):
                    d["repo"] = gh[1]

    out: dict[str, object] = {}
    if "owner" in d and d["owner"]:
        out["owner"] = str(d["owner"])
    if "repo" in d and d["repo"]:
        out["repo"] = str(d["repo"])
    if "format" in d:
        out["format"] = d["format"]
    for key in ("metrics", "pr_analysis", "pr_f2p", "pr_analysis_merged_only"):
        if key in d:
            out[key] = bool(d[key])
    if "pr_f2p_limit" in d:
        try:
            lim = int(d["pr_f2p_limit"])
            if lim >= 1:
                out["pr_f2p_limit"] = lim
        except (TypeError, ValueError):
            pass
    if "pr_analysis_lookback_days" in d:
        try:
            lb = int(d["pr_analysis_lookback_days"])
            if lb >= 0:
                out["pr_analysis_lookback_days"] = lb
        except (TypeError, ValueError):
            pass
    return out


# CLI flags that set a given argparse ``dest`` (honor CLI over ``.local.env``).
_DEST_TO_FLAGS: dict[str, tuple[str, ...]] = {
    "format": ("--format",),
    "owner": ("--owner",),
    "repo": ("--repo",),
    "metrics": ("--metrics", "--no-metrics"),
    "pr_analysis": ("--pr-analysis", "--no-pr-analysis"),
    "pr_f2p": ("--pr-f2p", "--no-pr-f2p"),
    "pr_f2p_limit": ("--pr-f2p-limit",),
    "pr_analysis_lookback_days": ("--pr-analysis-lookback-days",),
    "pr_analysis_merged_only": (
        "--pr-analysis-merged-only",
        "--no-pr-analysis-merged-only",
    ),
}


def argv_explicitly_sets_dest(argv: list[str], dest: str) -> bool:
    flags = _DEST_TO_FLAGS.get(dest)
    if not flags:
        return False
    for tok in argv:
        for f in flags:
            if tok == f or tok.startswith(f + "="):
                return True
    return False


def apply_local_env_to_namespace(args: object, argv: list[str]) -> None:
    """
    Merge :func:`build_arg_defaults` into ``args`` after :meth:`argparse.ArgumentParser.parse_args`.

    Reads ``.local.env`` only from the Fontaine installation / checkout (see
    :func:`_fontaine_config_base_dirs`). Uses ``args.repo_dir`` only to fill GitHub
    ``owner``/``repo`` from ``git remote`` when those keys are missing from the file.

    Argparse ``default=`` on ``BooleanOptionalAction`` overrides parser ``set_defaults``;
    merging here fixes that. Skips keys the user set explicitly on the command line.
    """
    repo_dir = getattr(args, "repo_dir", None)
    analyzed = Path(repo_dir) if repo_dir is not None else None
    defaults = build_arg_defaults(analyzed)
    if not defaults:
        return
    for key, val in defaults.items():
        if argv_explicitly_sets_dest(argv, key):
            continue
        setattr(args, key, val)
