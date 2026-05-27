from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import wcwidth

from fontaine.domain.models import RepoRollup
from fontaine.run_log import format_duration_human

from fontaine.domain.f2p.errors import summarize_f2p_error_message
from fontaine.domain.f2p.post_f2p_outcome import post_f2p_bucket
from fontaine.domain.pr_analysis import (
    PrAnalysisReport,
    first_stage_reject_label,
    lookback_cutoff_utc,
)
from fontaine.domain.repo_facts import RepoFacts
from fontaine.domain.repository_metrics import RepositoryMetrics

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_text_len(s: str) -> int:
    """
    Terminal display width (ANSI stripped): ASCII matches ``len``; wide emoji/CJK count as 2
    so ASCII box columns stay aligned with the right border.
    """
    plain = _ANSI_ESCAPE_RE.sub("", s)
    w = wcwidth.wcswidth(plain)
    if isinstance(w, int) and w >= 0:
        return w
    return len(plain)


def _pad_visible_right(s: str, width: int) -> str:
    n = _visible_text_len(s)
    if n >= width:
        return s
    return " " * (width - n) + s


def _languages_value_human(language_bytes: dict[str, int]) -> str:
    """
    Semicolon-separated language shares (for ``Languages:`` lines); not prefixed with a label.
    """
    if not language_bytes:
        return "(no data from API)"
    total = sum(language_bytes.values())
    if total <= 0:
        return "(no data from API)"
    ordered = sorted(language_bytes.items(), key=lambda x: (-x[1], x[0].lower()))
    parts: list[str] = []
    for i, (name, b) in enumerate(ordered):
        pct = 100.0 * b / total
        if i == 0:
            parts.append(f"{name} {pct:.1f}% (primary)")
        else:
            parts.append(f"{name} {pct:.1f}%")
    return "; ".join(parts)


def _kv_lines(indent: str, rows: list[tuple[str, str]]) -> list[str]:
    """Align ``label: value`` so values line up after the colon within this block only."""
    if not rows:
        return []
    w = max(len(label) for label, _ in rows)
    return [f"{indent}{label:<{w}}: {value}" for label, value in rows]


def _merged_at_lower_bound_phrase_utc(cutoff: datetime) -> str:
    """Calendar date in UTC, e.g. ``Jan 4, 2026 UTC`` (matches :func:`lookback_cutoff_utc`)."""
    d = cutoff.date()
    months = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )
    return f"{months[d.month - 1]} {d.day}, {d.year} UTC"


F2P_STRONG_CANDIDATES_LABEL = "Strong candidates (F2P+P2P non-empty)"


def _merged_time_lookback_days_display(pa: PrAnalysisReport) -> str:
    """
    PR-analysis merged-time window: day count plus ``mergedAt`` cutoff (same rule as
    screening: ``now (UTC) − N days``).
    """
    lb = pa.pr_analysis_lookback_days
    if lb is None:
        return "n/a"
    if lb <= 0:
        return "0 days (no merged-time filter)"
    cutoff = lookback_cutoff_utc(lb)
    if cutoff is None:
        return f"{lb} days"
    when = _merged_at_lower_bound_phrase_utc(cutoff)
    return f"{lb} days (mergedAt >= {when})"


def _ascii_box_table(
    rows: list[tuple[str, str]],
    *,
    indent: str = "  ",
    yellow_row_label: str | None = None,
) -> list[str]:
    """Simple two-column ASCII box (``label | value``). Optionally yellow-highlight one row."""
    if not rows:
        return []
    shown = list(rows)
    lw = max(len(a) for a, _ in shown)
    rw = max(_visible_text_len(b) for _, b in shown)
    bar = indent + "+" + "-" * (lw + 2) + "+" + "-" * (rw + 2) + "+"
    out: list[str] = [bar]
    for label, val in shown:
        cell = _pad_visible_right(val, rw)
        raw = f"| {label:<{lw}} | {cell} |"
        if yellow_row_label and label == yellow_row_label and _summary_color_enabled():
            # Yellow border/label and closing pipe; cell may include its own SGR resets.
            raw = f"\033[93m| {label:<{lw}} | \033[0m{cell}\033[93m |\033[0m"
        out.append(indent + raw)
    out.append(bar)
    return out


def _pr_screening_sample_section_title(pa: PrAnalysisReport) -> str:
    """Heading line for the PR screening & sample box."""
    if pa.pr_target_mode:
        return f"PR screening & sample (direct targets, {pa.total_analyzed} PR(s)):"
    lb = pa.pr_analysis_lookback_days
    if lb is not None and lb > 0:
        return f"PR screening & sample (lookback {lb} days):"
    if lb == 0:
        return "PR screening & sample (lookback off):"
    return "PR screening & sample:"


@dataclass(frozen=True, slots=True)
class _F2pRateMetrics:
    valid: int
    attempted: int
    n_pass: int
    n_scan: int
    pct_strong_pass: float
    pct_strong_fetch: float
    pct_sample_of_pass: float
    pct_sample_of_fetch: float


def _compute_f2p_rate_metrics(
    pa: PrAnalysisReport,
    *,
    valid: int,
    attempted: int,
) -> _F2pRateMetrics | None:
    """Shared strong-rate and sample-fraction math for F2P summary tables (one place to fix)."""
    if attempted <= 0:
        return None
    n_pass = pa.passed_first_filters
    n_scan = pa.total_analyzed
    rate_given_passed = valid / attempted
    pct_strong_pass = 100.0 * rate_given_passed
    frac_pass = (n_pass / n_scan) if n_scan > 0 else 0.0
    pct_strong_fetch = 100.0 * rate_given_passed * frac_pass
    pct_sample_of_pass = (100.0 * attempted / n_pass) if n_pass > 0 else 0.0
    pct_sample_of_fetch = (100.0 * attempted / n_scan) if n_scan > 0 else 0.0
    return _F2pRateMetrics(
        valid=valid,
        attempted=attempted,
        n_pass=n_pass,
        n_scan=n_scan,
        pct_strong_pass=pct_strong_pass,
        pct_strong_fetch=pct_strong_fetch,
        pct_sample_of_pass=pct_sample_of_pass,
        pct_sample_of_fetch=pct_sample_of_fetch,
    )


def _f2p_rate_compact_table_rows(
    pa: PrAnalysisReport,
    *,
    valid: int,
    attempted: int,
) -> list[tuple[str, str]]:
    """Compact rate strings for the F2P/P2P execution detail table."""
    m = _compute_f2p_rate_metrics(pa, valid=valid, attempted=attempted)
    if m is None:
        return [("Strong estimate", "n/a (no attempts)")]
    return [
        (
            "Strong estimate",
            f"{m.pct_strong_pass:.1f}% · {m.pct_strong_fetch:.1f}% "
            f"({m.valid}/{m.attempted}×{m.n_pass}/{m.n_scan})",
        ),
        (
            "F2P vs pass-filter pool",
            f"{m.attempted}/{m.n_pass} ({m.pct_sample_of_pass:.1f}%)",
        ),
        (
            "F2P vs screening sample",
            f"{m.attempted}/{m.n_scan} ({m.pct_sample_of_fetch:.1f}%)",
        ),
    ]


def _strong_estimate_value_yellow_extrapolation(plain: str) -> str:
    """Yellow only the second percentage (extrapolation to all fetched PRs)."""
    if not _summary_color_enabled():
        return plain
    if plain.startswith("n/a"):
        return plain
    m = re.match(r"^(\d+\.\d+% · )(\d+\.\d+%)( \(.*\))$", plain)
    if not m:
        return plain
    return f"{m.group(1)}\033[93m{m.group(2)}\033[0m{m.group(3)}"


def _f2p_complexity_distribution_lines(
    pa: PrAnalysisReport,
    *,
    indent: str = "  ",
) -> list[str]:
    """Histogram of first-stage complexity scores for PRs that entered the F2P batch (attempted)."""
    if not _pr_analysis_any_f2p_touch(pa):
        return []
    attempted_rows = [ln for ln in pa.passed if post_f2p_bucket(ln) != "not_run"]
    scores: list[float] = []
    for ln in attempted_rows:
        if ln.complexity_score is not None:
            scores.append(ln.complexity_score)
    if not scores:
        return []
    n_pr = len(scores)
    mn, mx = min(scores), max(scores)
    n_bins = 5
    if mx <= mn:
        if n_pr == 1:
            no_spread = f"The single PR has complexity ~{mn:.0f} (no spread to bin)."
        else:
            no_spread = (
                f"All {n_pr} PRs share the same complexity score (~{mn:.0f}); "
                "no spread to bin."
            )
        return [
            "",
            f"{indent}Complexity distribution — histogram (F2P batch — PRs selected for testing):",
            "",
            f"{indent}  {no_spread}",
        ]
    counts = [0] * n_bins
    for s in scores:
        if s >= mx:
            i = n_bins - 1
        else:
            t = (s - mn) / (mx - mn)
            i = min(n_bins - 1, int(t * n_bins))
        counts[i] += 1
    max_c = max(counts) if counts else 1
    bar_max = 40
    cores: list[str] = []
    for i in range(n_bins):
        wbin = (mx - mn) / n_bins
        lo = mn + i * wbin
        hi = mn + (i + 1) * wbin if i < n_bins - 1 else mx
        cores.append(f"~{lo:.0f}–{hi:.0f}")
    core_w = max(len(c) for c in cores)
    built: list[str] = []
    for i in range(n_bins):
        core = cores[i]
        if i == 0:
            built.append(f"{core:<{core_w}} (lowest)")
        elif i == n_bins - 1:
            built.append(f"{core:<{core_w}} (highest)")
        else:
            built.append(f"{core:<{core_w}}")
    label_w = max(len(s) for s in built)
    labels = [s + " " * (label_w - len(s)) for s in built]
    lines = [
        "",
        f"{indent}Complexity distribution — histogram (F2P batch — PRs selected for testing):",
        "",
    ]
    for i in range(n_bins):
        lab = labels[i]
        c = counts[i]
        hashes = int(bar_max * c / max_c) if max_c else 0
        bar = "#" * hashes
        lines.append(f"{indent}  {lab:<{label_w}} | {bar} {c}")
    return lines


def _pr_analysis_acquisition_ascii_blocks(pa: PrAnalysisReport) -> list[str]:
    """PR screening box, rejection box, optional complexity + F2P execution detail (stdout summary)."""
    indent = "  "
    lines: list[str] = []
    lines.append("")
    lines.append(f"{indent}{_pr_screening_sample_section_title(pa)}")

    n_scan = pa.total_analyzed
    n_pass = pa.passed_first_filters
    f2p_touch = _pr_analysis_any_f2p_touch(pa)
    buckets: dict[str, int] = {
        "valid": 0,
        "empty_f2p": 0,
        "empty_p2p": 0,
        "runner_failed": 0,
        "not_run": 0,
    }
    strong_suffix = ""
    if f2p_touch:
        for row in pa.passed:
            buckets[post_f2p_bucket(row)] += 1
        if buckets["valid"] > 0:
            if _summary_color_enabled():
                strong_suffix = "  \033[32m✅\033[0m"
            else:
                strong_suffix = "  ✅"

    nums_for_w = [n_scan, n_pass]
    if f2p_touch:
        nums_for_w.append(buckets["valid"])
    num_w = max(len(str(x)) for x in nums_for_w)
    sw = _visible_text_len(strong_suffix) if strong_suffix else 0
    # Reserve suffix width on every row so digits line up; border uses full column width.
    rw_sample = num_w + sw

    def _pr_screening_val_cell(n: int, *, suffix: str) -> str:
        lp = rw_sample - num_w - _visible_text_len(suffix)
        if lp < 0:
            lp = 0
        return " " * lp + str(n).rjust(num_w) + suffix

    sample_rows: list[tuple[str, str]] = [
        ("PRs in analysis sample", _pr_screening_val_cell(n_scan, suffix=" " * sw)),
        ("Passed first-stage filters", _pr_screening_val_cell(n_pass, suffix=" " * sw)),
    ]
    if f2p_touch:
        sample_rows.append(
            (
                F2P_STRONG_CANDIDATES_LABEL,
                _pr_screening_val_cell(buckets["valid"], suffix=strong_suffix),
            ),
        )
    lines.extend(
        _ascii_box_table(
            sample_rows,
            indent=indent,
            yellow_row_label=F2P_STRONG_CANDIDATES_LABEL if f2p_touch else None,
        )
    )

    if pa.rejection_breakdown:
        lines.append("")
        lines.append(f"{indent}First-stage rejections (by reason):")
        rej_rows = [
            (first_stage_reject_label(reason), str(n))
            for reason, n in pa.rejection_breakdown
        ]
        lines.extend(_ascii_box_table(rej_rows, indent=indent))

    if f2p_touch:
        attempted, completed, failed_run, not_in_batch = _f2p_execution_counts(pa)
        lines.extend(_f2p_complexity_distribution_lines(pa, indent=indent))
        detail_rows: list[tuple[str, str]] = [
            ("Attempted (runner invoked)", str(attempted)),
            ("Completed (tests ran)", str(completed)),
            (
                "Failed (runner/setup/checkout/install)",
                str(failed_run),
            ),
            ("Passed screening, not in F2P batch", str(not_in_batch)),
            ("Outcome: strong (valid F2P+P2P)", str(buckets["valid"])),
            ("Outcome: empty_f2p", str(buckets["empty_f2p"])),
            ("Outcome: empty_p2p", str(buckets["empty_p2p"])),
            ("Outcome: runner_failed", str(buckets["runner_failed"])),
            ("Outcome: not_run", str(buckets["not_run"])),
        ]
        rate_plain = _f2p_rate_compact_table_rows(
            pa, valid=buckets["valid"], attempted=attempted
        )
        detail_rows.extend(
            [
                (
                    lab,
                    _strong_estimate_value_yellow_extrapolation(val)
                    if lab == "Strong estimate"
                    else val,
                )
                for lab, val in rate_plain
            ]
        )
        lines.append("")
        lines.append(f"{indent}F2P/P2P execution detail:")
        lines.extend(_ascii_box_table(detail_rows, indent=indent))

    return lines


class JsonReporter:
    def __init__(self, destination: Path) -> None:
        self._destination = destination

    def write(self, rollup: RepoRollup) -> None:
        payload = asdict(rollup)  # adjust if you use enums / datetimes
        text = json.dumps(payload, indent=2, default=str)
        if self._destination.name == "-" and not self._destination.is_absolute():
            sys.stdout.write(text + "\n")
        else:
            self._destination.write_text(text, encoding="utf-8")

class OutputFormat(StrEnum):
    JSON = "json"
    HUMAN = "human"


def write_report(
    facts: RepoFacts,
    *,
    format: OutputFormat,
    destination: Path,
    verbose: bool = False,
    human_detail_path: Path | None = None,
) -> None:
    """Echo a short acquisition summary to stdout; optional verbose human narrative to file."""
    summary_text = "\n".join(_acquisition_summary_lines(facts)) + "\n"
    sys.stdout.write(summary_text)

    if format is OutputFormat.JSON:
        _write_json(facts, destination)
        return

    if verbose:
        detail = human_detail_path or Path("fontaine-detail.txt")
        verbose_lines = _human_verbose_lines(facts)
        block = "\n".join(verbose_lines) + "\n"
        if detail.name == "-" and not detail.is_absolute():
            sys.stderr.write(block)
        else:
            detail.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            detail.write_text(block, encoding="utf-8")


def _pad_visible_left(s: str, width: int) -> str:
    n = _visible_text_len(s)
    if n >= width:
        return s
    return s + " " * (width - n)


def _summary_color_enabled() -> bool:
    if os.environ.get("NO_COLOR", "").strip():
        return False
    return sys.stdout.isatty()


def _merged_pr_year_pct_int(count: int, total: int) -> int:
    """``round(100 * count / total)`` for nonnegative integers, half-up at .5."""
    if total <= 0:
        return 0
    return (100 * count + total // 2) // total


def _merged_pr_year_pct_colored(count: int, total: int) -> str:
    """Bright yellow ``(n%)`` of merged PRs (same scope as matrix), or plain if color off."""
    if total <= 0:
        inner = "(—%)"
    else:
        inner = f"({_merged_pr_year_pct_int(count, total)}%)"
    if not _summary_color_enabled():
        return inner
    return f"\033[93m{inner}\033[0m"


def _merged_pr_year_matrix_labels(end_year: int) -> list[str]:
    """Row-major labels for 4×4 grid; aligns with :func:`fetch_merged_pr_year_matrix_counts`."""
    y_first = end_year - 14
    return [f"<={y_first - 1}"] + [str(y) for y in range(y_first, end_year + 1)]


def _merged_pr_year_matrix_summary_lines(facts: RepoFacts) -> list[str]:
    """ASCII 4×4 merged-PR counts by calendar year (GitHub Search)."""
    if (
        facts.facts_source != "github_api"
        or facts.merged_pr_year_matrix is None
        or facts.merged_pr_year_matrix_end_year is None
        or len(facts.merged_pr_year_matrix) != 16
    ):
        return []
    end_year = facts.merged_pr_year_matrix_end_year
    labels = _merged_pr_year_matrix_labels(end_year)
    counts = list(facts.merged_pr_year_matrix)
    total_merged = facts.merged_pr_count
    lines: list[str] = [
        "",
        "Merged PRs by merge year (GitHub Search, mergedAt UTC):",
    ]
    cols = 4
    row_sums = [sum(counts[r * cols + c] for c in range(cols)) for r in range(4)]
    # Header row: "YEAR (pct%)" with yellow percentage; width uses visible (non-ANSI) length.
    col_ws: list[int] = []
    for c in range(cols):
        mw = 0
        for r in range(4):
            i = r * cols + c
            header = f"{labels[i]} {_merged_pr_year_pct_colored(counts[i], total_merged)}"
            mw = max(mw, _visible_text_len(header), len(str(counts[i])))
        col_ws.append(max(mw + 1, 10))

    summary_w = 0
    for r in range(4):
        sum_header = f"Row total {_merged_pr_year_pct_colored(row_sums[r], total_merged)}"
        summary_w = max(
            summary_w,
            _visible_text_len(sum_header),
            len(str(row_sums[r])),
        )
    summary_w = max(summary_w + 1, 12)

    def hsep() -> str:
        segs = ["-" * (col_ws[c] + 2) for c in range(cols)]
        segs.append("-" * (summary_w + 2))
        # Double rule between the 4×4 grid and the row-total column (offset visual).
        return "+" + "+".join(segs[:cols]) + "++" + segs[cols] + "+"

    pad = "  "
    lines.append(pad + hsep())
    for r in range(4):
        lab_cells: list[str] = []
        num_cells: list[str] = []
        for c in range(cols):
            i = r * cols + c
            w = col_ws[c]
            header = f"{labels[i]} {_merged_pr_year_pct_colored(counts[i], total_merged)}"
            lab_cells.append(_pad_visible_left(header, w))
            num_cells.append(str(counts[i]).rjust(w))
        sum_header = f"Row total {_merged_pr_year_pct_colored(row_sums[r], total_merged)}"
        lab_cells.append(_pad_visible_left(sum_header, summary_w))
        num_cells.append(str(row_sums[r]).rjust(summary_w))
        lines.append(
            pad
            + "| "
            + " | ".join(lab_cells[:cols])
            + " || "
            + lab_cells[cols]
            + " |"
        )
        lines.append(
            pad
            + "| "
            + " | ".join(num_cells[:cols])
            + " || "
            + num_cells[cols]
            + " |"
        )
        lines.append(pad + hsep())
    return lines


def _acquisition_summary_lines(facts: RepoFacts) -> list[str]:
    """Minimal view for acquisition / diligence decisions + performance."""
    lines = [
        "",
        "=" * 58,
        "FONTAINE — SUMMARY",
        "=" * 58,
    ]
    main_rows: list[tuple[str, str]] = [
        ("Repository", facts.repo_name),
        ("Facts source", str(facts.facts_source)),
        ("Default branch", facts.default_branch),
    ]
    if facts.facts_source == "github_api":
        if facts.stars is not None:
            main_rows.append(("Stars", f"{facts.stars:,}"))
        if facts.language_bytes:
            main_rows.append(("Languages", _languages_value_human(facts.language_bytes)))
        if facts.merged_into_default_only and facts.merged_pr_count_any_branch is not None:
            main_rows.append(
                (
                    "Merged PRs",
                    f"{facts.merged_pr_count:,}  (merged into {facts.default_branch}) | "
                    f"{facts.merged_pr_count_any_branch:,}  (merged into any branch)",
                )
            )
        else:
            scope = (
                f"merged into {facts.default_branch}"
                if facts.merged_into_default_only
                else "all merge bases"
            )
            main_rows.append(("Merged PRs", f"{facts.merged_pr_count:,}  ({scope})"))
    main_rows.append(("Lines of code", f"{facts.lines_of_code:,}"))
    if facts.lines_of_code_note:
        main_rows.append(("Note", facts.lines_of_code_note))
    if facts.facts_source == "local":
        main_rows.append(("Merge commits", f"{facts.merged_pr_count}"))
    lines.extend(_kv_lines("", main_rows))

    if facts.license_info is not None:
        li = facts.license_info
        lic_rows: list[tuple[str, str]] = [
            ("Tags", ", ".join(li.tags)),
            ("Copyleft", li.copyleft_signal),
        ]
        if li.github_spdx:
            lic_rows.append(("GitHub SPDX", li.github_spdx))
        if li.sources:
            lic_rows.append(("Sources", ", ".join(li.sources)))
        lines.append("")
        lines.append("Licensing (heuristic; not legal advice):")
        lines.extend(_kv_lines("  ", lic_rows))

    if facts.repository_metrics is not None:
        m = facts.repository_metrics
        inv_rows: list[tuple[str, str]] = [
            (
                "Files",
                f"{m.total_files:,} total; {m.source_files:,} source / {m.test_files:,} test",
            ),
            ("Primary lang", m.primary_language),
            ("CI/CD", "yes" if m.cicd_detected else "no"),
        ]
        if m.testing_tool_hints:
            inv_rows.append(("Test tooling", ", ".join(m.testing_tool_hints)))
        if m.issues_total is not None:
            inv_rows.append(
                (
                    "Issues",
                    f"{m.issues_open if m.issues_open is not None else 'n/a'} open / "
                    f"{m.issues_closed if m.issues_closed is not None else 'n/a'} closed "
                    f"(total {m.issues_total})",
                )
            )
        if m.commits_last_180_days is not None:
            inv_rows.append(("Commits (180d)", f"{m.commits_last_180_days:,}"))
        inv_rows.append(
            (
                "Signals",
                f"redistributable {m.redistributable_signals_score}/100; "
                f"authoring pattern {m.authoring_anomaly_score}/100",
            )
        )
        lines.append("")
        lines.append("Inventory & activity:")
        lines.extend(_kv_lines("  ", inv_rows))

    lines.extend(_merged_pr_year_matrix_summary_lines(facts))

    if facts.pr_analysis is not None:
        pa = facts.pr_analysis
        lines.extend(_pr_analysis_acquisition_ascii_blocks(pa))

    perf_rows: list[tuple[str, str]] = [
        ("Wall time", format_duration_human(facts.elapsed_seconds)),
        ("GitHub API HTTP requests", str(facts.github_api_http_requests)),
    ]
    lines.extend(
        [
            "",
            "Performance:",
            *_kv_lines("  ", perf_rows),
            "=" * 58,
            "",
        ]
    )
    return lines


def _write_json(facts: RepoFacts, destination: Path) -> None:
    text = json.dumps(asdict(facts), indent=2, default=str)
    if destination.name == "-" and not destination.is_absolute():
        sys.stdout.write(text + "\n")
    else:
        destination.write_text(text, encoding="utf-8")
def _repository_metrics_primary_human(m: RepositoryMetrics) -> list[str]:
    ci = "yes" if m.cicd_detected else "no"
    lines = [
        "",
        "--- Repository metrics (deterministic) ---",
        "(excludes vendor + documentation trees/files; lines: non-blank, "
        "whole-line comments heuristic)",
    ]
    primary_rows: list[tuple[str, str]] = [
        ("Total files", str(m.total_files)),
        ("Source files", str(m.source_files)),
        ("Test files", str(m.test_files)),
        ("Total lines", f"{m.total_lines:,}"),
        ("Application lines", f"{m.application_lines:,}"),
        ("Test lines", f"{m.test_lines:,}"),
        ("Primary (by lines)", m.primary_language),
        ("CI/CD detected", ci),
    ]
    if m.cicd_placeholders:
        primary_rows.append(("CI/CD hints", ", ".join(m.cicd_placeholders)))
    if m.testing_tool_hints:
        primary_rows.append(("Test tooling hints", ", ".join(m.testing_tool_hints)))
    primary_rows.extend(
        [
            (
                "Open issues",
                str(m.issues_open if m.issues_open is not None else "n/a"),
            ),
            (
                "Closed issues",
                str(m.issues_closed if m.issues_closed is not None else "n/a"),
            ),
            (
                "Total issues",
                str(m.issues_total if m.issues_total is not None else "n/a"),
            ),
            (
                "Commits (analyzed branch)",
                str(m.commits_total if m.commits_total is not None else "n/a"),
            ),
            (
                "Commits (180d)",
                str(m.commits_last_180_days if m.commits_last_180_days is not None else "n/a"),
            ),
            (
                "Commits (365d)",
                str(m.commits_last_365_days if m.commits_last_365_days is not None else "n/a"),
            ),
            (
                "Branches (GitHub)",
                str(m.github_branches_total if m.github_branches_total is not None else "n/a"),
            ),
            (
                "Branch refs (local)",
                str(m.branch_refs_total if m.branch_refs_total is not None else "n/a"),
            ),
            (
                "Tag refs (local)",
                str(m.tag_refs_total if m.tag_refs_total is not None else "n/a"),
            ),
            (
                "Earliest 10 (incl. merges) Δlines",
                str(list(m.earliest_ten_delta_lines_including_merges)),
            ),
            (
                "Earliest 10 (non-merge only) Δlines",
                str(list(m.earliest_ten_delta_lines_non_merge_only)),
            ),
            (
                "Top 10 (incl. merges) by Δlines",
                str(list(m.top_ten_delta_lines_including_merges)),
            ),
            (
                "Top 10 (non-merge only) by Δlines",
                str(list(m.top_ten_delta_lines_non_merge_only)),
            ),
        ]
    )
    if (
        m.commit_subject_chars_mean is not None
        and m.commit_subject_chars_median is not None
        and m.commit_subject_chars_variance is not None
    ):
        primary_rows.append(
            (
                "Commit subject len (avg/med/var)",
                f"{m.commit_subject_chars_mean:.2f}/"
                f"{m.commit_subject_chars_median:.2f}/"
                f"{m.commit_subject_chars_variance:.2f}",
            )
        )
    if (
        m.avg_lines_changed_per_commit_sample is not None
        and m.median_lines_changed_per_commit_sample is not None
    ):
        primary_rows.append(
            (
                "Δlines/commit (avg/med, full-history shortstat)",
                f"{m.avg_lines_changed_per_commit_sample:.2f}/"
                f"{m.median_lines_changed_per_commit_sample:.2f}",
            )
        )
    lines.extend(_kv_lines("", primary_rows))
    return lines


def _repository_metrics_secondary_human(m: RepositoryMetrics) -> list[str]:
    secondary: list[str] = ["", "--- Secondary signals ---"]
    sec_rows: list[tuple[str, str]] = []
    if m.churn_coefficient is not None:
        sec_rows.append(
            (
                "Churn coefficient",
                f"{m.churn_coefficient:.4f}  (full-history Δlines vs scoped inventory)",
            )
        )
    if m.approximate_comment_ratio is not None:
        sec_rows.append(
            (
                "Comment ratio (heuristic, all eligible files)",
                f"{m.approximate_comment_ratio:.4f}",
            )
        )
    sec_rows.append(
        (
            f"Redistributable signals ({m.redistributable_signals_score}/100)",
            ", ".join(m.redistributable_signals),
        )
    )
    sec_rows.append(
        (
            f"Authoring-pattern score ({m.authoring_anomaly_score}/100)",
            ", ".join(m.authoring_anomaly_signals),
        )
    )
    secondary.extend(_kv_lines("", sec_rows))
    return secondary


def _pr_f2p_limit_display(pa: PrAnalysisReport) -> int:
    """CLI sample/run cap for messaging (default 30 when analysis did not record a limit)."""
    return pa.f2p_limit if pa.f2p_limit is not None else 30


def _pr_f2p_scheduled_batch_size(pa: PrAnalysisReport) -> int:
    """PRs actually scheduled for F2P/P2P: ``min(CLI cap, rows that passed first-stage filters)."""
    return min(_pr_f2p_limit_display(pa), len(pa.passed))


def _pr_analysis_any_f2p_touch(pa: PrAnalysisReport) -> bool:
    """True when ``--pr-f2p`` populated any line with counts, errors, or diagnostics."""
    return any(
        x.f2p_count is not None
        or x.p2p_count is not None
        or x.f2p_error is not None
        or x.f2p_diagnostic is not None
        for x in pa.passed
    )


def _f2p_execution_counts(pa: PrAnalysisReport) -> tuple[int, int, int, int]:
    """
    From ``passed`` rows after F2P enrichment, return:

    - **attempted**: PRs in the F2P/P2P batch (runner was invoked).
    - **completed**: Attempted with no ``f2p_error`` — test stages ran and counts were recorded.
    - **failed**: ``runner_failed`` (checkout, install, or test runner error).
    - **not_run**: Passed first-stage filters but not selected in this F2P batch (sample/cap).
    """
    attempted = 0
    completed = 0
    failed = 0
    not_run = 0
    for row in pa.passed:
        b = post_f2p_bucket(row)
        if b == "not_run":
            not_run += 1
        else:
            attempted += 1
            if b == "runner_failed":
                failed += 1
            else:
                completed += 1
    return attempted, completed, failed, not_run


def _f2p_rate_and_extrapolation_rows(
    pa: PrAnalysisReport,
    *,
    valid: int,
    attempted: int,
) -> list[tuple[str, str]]:
    """
    Key/value rows for F2P rate / sample lines (no indent; build one :func:`_kv_lines` block
    with execution/outcome rows so colons align within the F2P section).
    """
    m = _compute_f2p_rate_metrics(pa, valid=valid, attempted=attempted)
    if m is None:
        return [("F2P rates", "n/a (no attempts).")]

    return [
        (
            "Strong estimate",
            f"{m.pct_strong_pass:.1f}% of pass-filter PRs; "
            f"{m.pct_strong_fetch:.1f}% of all fetched ({m.valid}/{m.attempted} × {m.n_pass}/{m.n_scan}).",
        ),
        (
            "F2P sample (of pass-filter pool)",
            f"{m.attempted}/{m.n_pass} = {m.pct_sample_of_pass:.1f}% of all pass-filter PRs were run "
            "through F2P.",
        ),
        (
            "F2P sample (of full fetch)",
            f"{m.attempted}/{m.n_scan} = {m.pct_sample_of_fetch:.1f}% of all PRs fetched in this scan "
            "were run through F2P.",
        ),
    ]


def _f2p_p2p_results_human(pa: PrAnalysisReport) -> list[str]:
    """Post–F2P summary before ``--- Performance ---`` (non-empty F2P and P2P)."""
    if not _pr_analysis_any_f2p_touch(pa):
        return []
    attempted, completed, failed, not_in_batch = _f2p_execution_counts(pa)
    buckets: dict[str, int] = {
        "valid": 0,
        "empty_f2p": 0,
        "empty_p2p": 0,
        "runner_failed": 0,
        "not_run": 0,
    }
    good_pr_lines: list[tuple[int, str]] = []
    for row in pa.passed:
        b = post_f2p_bucket(row)
        buckets[b] += 1
        if b == "valid":
            good_pr_lines.append((row.number, row.title))

    n_good = buckets["valid"]
    lim_cli = _pr_f2p_limit_display(pa)
    scheduled = _pr_f2p_scheduled_batch_size(pa)
    n_analyzed = (
        buckets["valid"]
        + buckets["empty_f2p"]
        + buckets["empty_p2p"]
        + buckets["runner_failed"]
    )
    beyond_note = (
        f"outside the F2P sample of {scheduled} (--pr-f2p-limit={lim_cli})"
        if scheduled < lim_cli
        else f"past --pr-f2p-limit={lim_cli}"
    )
    batch_note = (
        f" Passed screening but not in this F2P batch: {not_in_batch}."
        if not_in_batch
        else ""
    )
    execution_v = (
        f"{attempted} PR(s) attempted · {completed} completed (test stages ran, "
        f"no runner error) · {failed} failed (runner/setup/checkout/install).{batch_note}"
    )
    summary_v = f"{n_good} PR(s) remain good candidates after F2P/P2P testing."
    detail_v = (
        f"good candidates (non-empty F2P and P2P): {buckets['valid']}; "
        f"empty_f2p: {buckets['empty_f2p']}; "
        f"empty_p2p: {buckets['empty_p2p']}; "
        f"runner/setup failed: {buckets['runner_failed']}; "
        f"not run ({beyond_note}): {buckets['not_run']}. "
        f"Runner executed on {n_analyzed} PR(s) (within the F2P batch of up to {scheduled})."
    )
    lines = [
        "",
        "--- F2P / P2P results ---",
        *_kv_lines("", [("Execution", execution_v)]),
        "",
        "Rule: keep a PR as a good candidate only when the three-stage run succeeds and "
        "at least one Fail→Pass test and one Pass→Pass test appear (non-empty F2P and "
        "P2P counts). Otherwise treat as empty_f2p or empty_p2p when the matching bucket "
        "has no tests.",
        "",
        *_kv_lines(
            "",
            [("Summary", summary_v), ("Detail", detail_v)]
            + _f2p_rate_and_extrapolation_rows(
                pa, valid=n_good, attempted=attempted
            ),
        ),
    ]
    if good_pr_lines:
        lines.append("")
        lines.append("PRs still good candidates after F2P/P2P (both gates non-empty):")
        for num, title in good_pr_lines:
            one = title.replace("\r", " ").replace("\n", " ").strip()
            if len(one) > 110:
                one = one[:107] + "..."
            lines.append(f"  - #{num} {one}")
    return lines


def _pr_analysis_human(pa: PrAnalysisReport) -> list[str]:
    lines = ["", "--- PR Analysis ---"]
    n_ok = pa.passed_first_filters
    if n_ok == 0:
        summary_v = (
            "No PRs in this sample passed first-stage screening — none are flagged as "
            "candidates for deeper review yet."
        )
    else:
        summary_v = (
            f"{n_ok} PR(s) may be good candidates — they passed the first-stage "
            "filters and are worth deeper checks (e.g. F2P or manual review)."
        )
    top_rows: list[tuple[str, str]] = [("Summary", summary_v)]
    if pa.pr_target_mode:
        top_rows.insert(
            0,
            (
                "Mode",
                "direct PR target(s) — bulk GraphQL listing and first-stage filters were "
                "skipped; F2P/P2P runs on the requested merged PR numbers only.",
            ),
        )
    top_rows.append(("Total PRs fetched (GraphQL cap)", str(pa.total_analyzed)))
    if pa.considered_for_filters != pa.total_analyzed:
        top_rows.append(
            (
                "Merged into default branch (when scoped)",
                str(pa.considered_for_filters),
            )
        )
    top_rows.extend(
        [
            ("Merged-time lookback", _merged_time_lookback_days_display(pa)),
            (
                "Filters",
                "bot, English title+body (~90% ASCII), ≥1 test file, ≤100 non-test "
                "files, >5 non-asset files (difficulty), ≤15 test files, ≤50 changed code files, "
                "≥1 Δline on non-test source (GraphQL approximates a patch-based minimum). "
                "Closing-issue checks are not applied here.",
            ),
        ]
    )
    if pa.rejection_breakdown:
        parts = [f"{k}={v}" for k, v in pa.rejection_breakdown]
        top_rows.append(
            ("First-stage rejections (counts by reason)", "; ".join(parts))
        )
    lines.extend(_kv_lines("", top_rows))
    if pa.first_stage_rejected:
        lines.append("")
        lines.append(
            f"First-stage rejected PRs ({len(pa.first_stage_rejected)}), fetch order — "
            "reason per PR:"
        )
        for r in pa.first_stage_rejected:
            title_one = r.title.replace("\r", " ").replace("\n", " ").strip()
            why = first_stage_reject_label(r.reason_code)
            lines.append(f"  - #{r.number} — {why}")
            lines.append(f"      {title_one}")
    any_f2p = _pr_analysis_any_f2p_touch(pa)
    if any_f2p:
        lines.append(
            "Per-PR Fail→Pass / Pass→Pass (--pr-f2p): JS tests (Jest/Vitest/Mocha) on your "
            "local clone."
        )
        attempted, completed, failed_run, not_in_batch = _f2p_execution_counts(pa)
        completed_rows = [
            x
            for x in pa.passed
            if post_f2p_bucket(x) in ("valid", "empty_f2p", "empty_p2p")
        ]
        sum_f2p = sum(x.f2p_count or 0 for x in completed_rows)
        sum_p2p = sum(x.p2p_count or 0 for x in completed_rows)
        lim_cli = _pr_f2p_limit_display(pa)
        scheduled = _pr_f2p_scheduled_batch_size(pa)
        beyond = (
            f"outside F2P sample of {scheduled} (--pr-f2p-limit caps at {lim_cli})"
            if scheduled < lim_cli
            else f"beyond cap {lim_cli}"
        )
        tail_batch = (
            f"; {not_in_batch} passed screening but not in this batch ({beyond})"
            if not_in_batch
            else ""
        )
        lines.extend(
            _kv_lines(
                "",
                [
                    (
                        "F2P/P2P execution",
                        f"{attempted} attempted · {completed} completed (tests ran) · "
                        f"{failed_run} failed{tail_batch}.",
                    ),
                    (
                        "Test counts among completed runs",
                        f"Σ F2P={sum_f2p}, Σ P2P={sum_p2p} "
                        f"(see --- F2P / P2P results --- for gate breakdown). "
                        f"--pr-f2p-limit={lim_cli}.",
                    ),
                ],
            )
        )
    else:
        lines.append(
            "Add --repo-dir and --pr-f2p to run tests and compute F2P/P2P on the first N "
            "filtered PRs."
        )
    for row in pa.passed:
        title_one_line = row.title.replace("\r", " ").replace("\n", " ").strip()
        tail = ""
        if row.f2p_error:
            err = summarize_f2p_error_message(row.f2p_error, max_len=160)
            tail = f"  [{err}]"
        elif row.f2p_count is not None or row.p2p_count is not None:
            f2p = row.f2p_count if row.f2p_count is not None else "—"
            p2p = row.p2p_count if row.p2p_count is not None else "—"
            tail = f"  [F2P: {f2p}, P2P: {p2p}]"
            if row.f2p_diagnostic:
                note = row.f2p_diagnostic.replace("\r", " ").replace("\n", " ").strip()
                if len(note) > 220:
                    note = note[:217] + "..."
                tail += f"\n      note: {note}"
        elif any_f2p:
            lim_row = _pr_f2p_limit_display(pa)
            sched_row = _pr_f2p_scheduled_batch_size(pa)
            cap_note = (
                f" (--pr-f2p-limit={lim_row})"
                if sched_row < lim_row
                else f" (--pr-f2p-limit={lim_row}; omit for default 30)"
            )
            tail = (
                "  [F2P/P2P: skipped — not among the first "
                f"{sched_row} PR(s) scheduled for analysis{cap_note}]"
            )
        lines.append(f"  - {title_one_line} (#{row.number}){tail}")
    return lines


def _human_verbose_lines(facts: RepoFacts) -> list[str]:
    """Full narrative report (PR lists, metrics sections, diagnostics)."""
    hdr_rows: list[tuple[str, str]] = [
        ("source", str(facts.facts_source)),
        ("repo", facts.repo_name),
        ("default branch", facts.default_branch),
    ]
    if facts.facts_source == "github_api":
        if facts.stars is not None:
            hdr_rows.append(("stars", str(facts.stars)))
        if facts.repository_disk_size_kb is not None:
            hdr_rows.append(("on-disk (kb)", str(facts.repository_disk_size_kb)))
        if facts.language_bytes:
            hdr_rows.append(("languages", _languages_value_human(facts.language_bytes)))
    hdr_rows.append(("lines of code", str(facts.lines_of_code)))
    if facts.lines_of_code_note:
        hdr_rows.append(("note", facts.lines_of_code_note))
    lines = _kv_lines("", hdr_rows)
    if facts.license_info is not None:
        li = facts.license_info
        lic_v_rows: list[tuple[str, str]] = [
            ("tags", ", ".join(li.tags)),
            ("copyleft signal", li.copyleft_signal),
        ]
        if li.github_spdx:
            lic_v_rows.append(("GitHub SPDX", li.github_spdx))
        if li.sources:
            lic_v_rows.append(("sources", ", ".join(li.sources)))
        lines.extend(
            [
                "",
                "--- Licensing (heuristic) ---",
                *_kv_lines("", lic_v_rows),
            ]
        )
    merge_rows: list[tuple[str, str]] = []
    if facts.facts_source == "local":
        merge_rows.append(
            (
                "merge commits",
                f"{facts.merged_pr_count}  "
                "(git log --first-parent --merges on default branch)",
            )
        )
    else:
        if facts.merged_into_default_only and facts.merged_pr_count_any_branch is not None:
            merge_rows.append(
                (
                    "merged PRs",
                    f"{facts.merged_pr_count}  (base:{facts.default_branch}) | "
                    f"{facts.merged_pr_count_any_branch}  (no base: / any branch) — "
                    "GitHub Search API",
                )
            )
        else:
            scope = (
                f"base:{facts.default_branch}"
                if facts.merged_into_default_only
                else "all merged bases"
            )
            merge_rows.append(
                (
                    "merged PRs",
                    f"{facts.merged_pr_count}  "
                    f"(GitHub Search API: is:merged, {scope}; source of truth for remote mode)",
                )
            )
    if facts.inferred_merged_pr_count is not None and facts.facts_source == "local":
        merge_rows.append(
            (
                "PR #s (guess)",
                f"{facts.inferred_merged_pr_count}  "
                "(from commit subjects; use GitHub mode for Search API count)",
            )
        )
    if facts.local_root is not None:
        merge_rows.append(("local root", str(facts.local_root)))
    if merge_rows:
        lines.extend(["", *_kv_lines("", merge_rows)])
    if facts.repository_metrics is not None:
        lines.extend(_repository_metrics_primary_human(facts.repository_metrics))
        lines.extend(_repository_metrics_secondary_human(facts.repository_metrics))
    if facts.pr_analysis is not None:
        lines.extend(_pr_analysis_human(facts.pr_analysis))
        lines.extend(_f2p_p2p_results_human(facts.pr_analysis))
    lines.extend(
        [
            "",
            "--- Performance ---",
            *_kv_lines(
                "",
                [
                    ("Elapsed (gather)", format_duration_human(facts.elapsed_seconds)),
                    ("GitHub API HTTP requests", str(facts.github_api_http_requests)),
                ],
            ),
        ]
    )
    return lines
