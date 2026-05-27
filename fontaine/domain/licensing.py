"""
Repository license discovery (GitHub license API + root LICENSE / manifests).

Categories are heuristic labels for diligence summaries, not legal advice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_LICENSE_ROOT_FILES = (
    "LICENSE",
    "LICENSE.txt",
    "LICENSE.md",
    "LICENSE.markdown",
    "LICENCE",
    "LICENCE.txt",
    "LICENCE.md",
    "COPYING",
    "COPYING.txt",
)

_TEXT_HINTS: list[tuple[str, str]] = [
    (r"\bGNU\s+AFFERO\s+GENERAL\s+PUBLIC\s+LICENSE\b", "AGPL-copyleft"),
    (r"\bGNU\s+GENERAL\s+PUBLIC\s+LICENSE\b", "GPL-copyleft"),
    (r"\bGNU\s+LESSER\s+GENERAL\s+PUBLIC\s+LICENSE\b", "LGPL-copyleft"),
    (r"\bMozilla\s+Public\s+License\b", "MPL-copyleft"),
    (r"\bApache\s+License\b", "Apache"),
    (r"\bApache\s+License\s*,\s*Version\s+2", "Apache"),
    (r"\bMIT\s+License\b", "MIT"),
    (r"\bISC\s+License\b", "ISC"),
    (r"\bBSD\s+[23][ -]Clause\b", "BSD-family"),
    (r"\bEclipse\s+Public\s+License\b", "EPL-copyleft"),
    (r"\bCOMMON\s+DEVELOPMENT\s+AND\s+DISTRIBUTION\s+LICENSE\b", "CDDL-copyleft"),
]


def _norm_spdx_fragment(s: str) -> str:
    return s.strip().upper().replace(" ", "-")


def classify_spdx_or_string(raw: str) -> str | None:
    """Map SPDX id or npm-style license string to a coarse tag."""
    if not raw:
        return None
    u = _norm_spdx_fragment(raw)
    if "AGPL" in u:
        return "AGPL-copyleft"
    if u.startswith("GPL-") or u == "GPL":
        return "GPL-copyleft"
    if "LGPL" in u or u.startswith("LGPL"):
        return "LGPL-copyleft"
    if "MPL" in u:
        return "MPL-copyleft"
    if "CDDL" in u:
        return "CDDL-copyleft"
    if "EPL" in u:
        return "EPL-copyleft"
    if "Apache" in u:
        return "Apache"
    if "MIT" in u:
        return "MIT"
    if "BSD" in u:
        return "BSD-family"
    if "ISC" in u:
        return "ISC"
    if "UNLICENSE" in u or u == "UNLICENSE":
        return "public-domain-ish"
    if "CC0" in u:
        return "CC0/public-domain-ish"
    if u in ("PROPRIETARY", "COMMERCIAL"):
        return "proprietary-ish"
    return None


def _hints_from_license_text(body: str) -> list[str]:
    found: list[str] = []
    sample = body[:48000]
    seen: set[str] = set()
    for pattern, tag in _TEXT_HINTS:
        if re.search(pattern, sample, flags=re.IGNORECASE):
            if tag not in seen:
                seen.add(tag)
                found.append(tag)
    return found


def _parse_package_json_license(root: Path) -> str | None:
    p = root / "package.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    lic = data.get("license")
    if isinstance(lic, str):
        return lic.strip() or None
    if isinstance(lic, dict):
        return str(lic.get("type") or lic.get("name") or "").strip() or None
    return None


def _parse_pyproject_license(root: Path) -> str | None:
    path = root / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        import tomllib
    except ImportError:
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    proj = data.get("project") or {}
    lic = proj.get("license")
    if isinstance(lic, str):
        return lic.strip() or None
    if isinstance(lic, dict):
        return str(lic.get("text") or lic.get("file") or "").strip() or None
    return None


def _parse_cargo_license(root: Path) -> str | None:
    path = root / "Cargo.toml"
    if not path.is_file():
        return None
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'^\s*license\s*=\s*"([^"]+)"', txt, flags=re.MULTILINE)
    return m.group(1).strip() if m else None


def scan_repo_filesystem(root: Path) -> tuple[list[str], list[str]]:
    """Return (source labels used, coarse license tags)."""
    root = root.resolve()
    sources: list[str] = []
    tags: list[str] = []

    for name in _LICENSE_ROOT_FILES:
        p = root / name
        if not p.is_file():
            continue
        sources.append(name)
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in body.splitlines()[:60]:
            if "SPDX-License-Identifier:" in line:
                sid = line.split(":", 1)[1].strip()
                ct = classify_spdx_or_string(sid)
                if ct:
                    tags.append(ct)
                break
        tags.extend(_hints_from_license_text(body))

    for label, parser in (
        ("package.json", _parse_package_json_license),
        ("pyproject.toml", _parse_pyproject_license),
        ("Cargo.toml", _parse_cargo_license),
    ):
        raw = parser(root)
        if raw:
            sources.append(label)
            ct = classify_spdx_or_string(raw)
            if ct:
                tags.append(ct)

    # Dedupe preserve order
    seen: set[str] = set()
    uniq_tags: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            uniq_tags.append(t)

    return sources, uniq_tags


def _copyleft_signal(tags: tuple[str, ...]) -> str:
    pool = frozenset(tags)
    strong = frozenset({"GPL-copyleft", "AGPL-copyleft"})
    weak_copyleft = frozenset(
        {"LGPL-copyleft", "MPL-copyleft", "EPL-copyleft", "CDDL-copyleft"}
    )
    permissive_like = frozenset(
        {
            "MIT",
            "Apache",
            "BSD-family",
            "ISC",
            "public-domain-ish",
            "CC0/public-domain-ish",
        }
    )
    has_strong = bool(pool & strong)
    has_weak = bool(pool & weak_copyleft)
    has_perm = bool(pool & permissive_like)

    if has_strong and (has_weak or has_perm):
        return "mixed"
    if has_strong:
        return "strong"
    if has_weak:
        return "weak"
    return "none"


@dataclass(frozen=True, slots=True)
class LicenseInfo:
    """Aggregated licensing signals."""

    tags: tuple[str, ...]
    """Coarse tags (Apache, MIT, GPL-copyleft, …)."""
    copyleft_signal: str
    """``none`` | ``weak`` | ``strong`` | ``mixed`` — heuristic only."""
    sources: tuple[str, ...]
    """Where data came from (``github_api``, ``LICENSE``, ``package.json``, …)."""
    github_spdx: str | None
    """Raw SPDX id from GitHub license API when present."""


def build_license_info(
    *,
    github_spdx: str | None,
    filesystem_root: Path | None,
) -> LicenseInfo:
    tags: list[str] = []
    sources: list[str] = []

    if github_spdx:
        sources.append("github_api")
        ct = classify_spdx_or_string(github_spdx)
        if ct:
            tags.append(ct)
        else:
            tags.append(f"GitHub-declared:{github_spdx}")

    if filesystem_root is not None and filesystem_root.is_dir():
        fs_sources, fs_tags = scan_repo_filesystem(filesystem_root)
        sources.extend(fs_sources)
        tags.extend(fs_tags)

    seen: set[str] = set()
    uniq: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            uniq.append(t)

    if not uniq:
        uniq = ["unknown"]

    cs = _copyleft_signal(tuple(uniq))

    return LicenseInfo(
        tags=tuple(uniq[:16]),
        copyleft_signal=cs,
        sources=tuple(sorted(set(sources))) if sources else tuple(),
        github_spdx=github_spdx,
    )


def license_summary_line(li: LicenseInfo) -> str:
    tag_str = ", ".join(li.tags[:10])
    gh = f"; GitHub SPDX={li.github_spdx}" if li.github_spdx else ""
    return f"{tag_str} (copyleft signal: {li.copyleft_signal}){gh}"
