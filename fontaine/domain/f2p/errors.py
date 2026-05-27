"""Short, human-readable summaries of noisy tool output (installers, runners)."""

from __future__ import annotations

import re


# Match multi-segment Unix paths (including short final components like .../bin/node).
_ABS_PATH = re.compile(r"(?<![\w/])(/[A-Za-z0-9._+\-]+)+")


def _strip_stack_noise(line: str) -> bool:
    low = line.lower()
    if " at " in line and ("node_modules" in low or "anonymous" in low):
        return True
    if low.startswith("error: stack") or low.startswith("stack error"):
        return True
    return False


def _is_npm_workspace_help_noise(line: str) -> bool:
    """npm prints multi-line flag docs on some failures; skip for summaries."""
    low = line.lower()
    if "set to true to run the command in the context of" in low:
        return True
    if low.strip() in ("--include-workspace-root", "--workspaces", "--workspace"):
        return True
    if low.startswith("npm error --include-workspace-root"):
        return True
    return False


def _meaningful_install_line(line: str) -> bool:
    s = line.strip()
    low = s.lower()
    if _is_npm_workspace_help_noise(s):
        return False
    # Always skip; paths are useless and we summarize native failures separately.
    if "gyp ERR!" in s and "command" in low:
        return False
    if s.startswith("npm error ") or s.startswith("npm ERR!"):
        return True
    if s.startswith("yarn error ") or "error Command failed" in s:
        return True
    if "ERR_PNPM" in s or "ERR_PNPM_" in s:
        return True
    if "ERR!" in s and ("gyp" in low or "node-pre-gyp" in low or "node-gyp" in low):
        return True
    if "`make` failed" in s or ("make" in low and "exit code" in low):
        return True
    if "error: " in low and ("gyp" in low or "node-gyp" in low):
        return True
    return False


def collapse_paths_and_gyp(text: str) -> str:
    """
    Remove path-heavy noise (Homebrew Cellar, node-gyp command lines, long quoted paths).
    """
    s = text
    # Entire "gyp ERR! command …" lines are never helpful in a one-line summary.
    s = re.sub(
        r"gyp ERR!\s+command[^\n]*",
        "gyp ERR! native addon build failed",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"node-gyp[^\n]{0,240}",
        "node-gyp build step",
        s,
        flags=re.IGNORECASE,
        count=1,
    )
    # Common absolute prefixes (npm/gyp loves long Cellar paths).
    s = re.sub(r"/opt/homebrew(?:/[^\s\]`\"']+)+", "[brew]", s)
    s = re.sub(r"/usr/local(?:/[^\s\]`\"']+)+", "[usr-local]", s)
    s = re.sub(r"/Users/[^\s\]`\"']+", "[home]", s)
    # Any remaining absolute paths → placeholder (includes …/bin/node).
    s = _ABS_PATH.sub("[path]", s)
    # Collapse doubled placeholders
    s = re.sub(r"(\[path\]\s*)+", "[path] ", s)
    s = re.sub(r"(\[brew\]\s*)+", "[brew] ", s)
    return " ".join(s.split()).strip()


def shorten_paths(text: str, *, max_segment: int = 48) -> str:
    """Legacy hook: alias to :func:`collapse_paths_and_gyp`."""
    _ = max_segment
    return collapse_paths_and_gyp(text)


def _clip_line(s: str, cap: int = 180) -> str:
    s = s.strip()
    if len(s) <= cap:
        return s
    return s[: cap - 3].rstrip() + "..."


def summarize_dependency_install_log(raw: str, *, max_len: int = 280) -> str:
    """Turn npm/yarn/pnpm install stderr into one short line."""
    blob = (raw or "").replace("\r\n", "\n").strip()
    if not blob:
        return "(no installer output)"

    candidates: list[str] = []
    for line in blob.split("\n"):
        if _strip_stack_noise(line):
            continue
        if _is_npm_workspace_help_noise(line):
            continue
        if _meaningful_install_line(line):
            s = _clip_line(line.strip())
            if s not in candidates:
                candidates.append(s)

    if not candidates and "\n" not in blob and len(blob) > 120:
        for sep in ("npm ERR!", "npm error ", "gyp ERR!", "ERR_PNPM"):
            idx = blob.find(sep)
            if idx >= 0:
                snippet = blob[idx : idx + 360]
                candidates.append(_clip_line(snippet))
                break
        if not candidates:
            one = _clip_line(blob)
            if not _strip_stack_noise(one):
                candidates.append(one)

    if not candidates:
        for line in reversed(blob.split("\n")):
            s = line.strip()
            if not s or _strip_stack_noise(s):
                continue
            candidates.append(_clip_line(s))
            break

    out = "; ".join(candidates[-8:])
    out = collapse_paths_and_gyp(out)
    out = " ".join(out.split())
    if len(out) > max_len:
        out = out[: max_len - 3].rstrip() + "..."
    return out or "(install failed)"


def summarize_f2p_error_message(raw: str, *, max_len: int = 200) -> str:
    """One-line summary for human PR reports (install + other orchestration errors)."""
    text = (raw or "").strip()
    if not text:
        return "(unknown error)"
    low = text.lower()
    if "install failed" in low:
        if ":" in text:
            prefix, rest = text.split(":", 1)
            rest = rest.strip()
            if len(rest) > 80:
                tail = summarize_dependency_install_log(rest, max_len=max(max_len - len(prefix), 80))
                merged = f"{prefix.strip()}: {tail}".strip()
            else:
                merged = f"{prefix.strip()}: {rest}".strip()
            merged = collapse_paths_and_gyp(merged)
            if len(merged) > max_len:
                merged = merged[: max_len - 3].rstrip() + "..."
            # Single readable line for the usual node-gyp wall of text.
            if "gyp" in merged.lower() or "native addon" in merged.lower():
                canned = (
                    f"{prefix.strip()}: native addon build failed "
                    f"(node-gyp/make; install Xcode CLI tools or use Node with prebuilt deps)"
                )
                return canned if len(canned) <= max_len else (canned[: max_len - 3] + "...")
            return merged
    out = collapse_paths_and_gyp(text)
    if len(out) <= max_len:
        return out
    head = out.split("\n", 1)[0].strip()
    if len(head) > max_len:
        head = head[: max_len - 3].rstrip() + "..."
    return head
