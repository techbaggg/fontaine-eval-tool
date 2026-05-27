"""Apply F2P runs to a PR analysis report."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from fontaine.domain.f2p.errors import summarize_f2p_error_message
from fontaine.domain.f2p.orchestrator import analyze_pr_f2p
from fontaine.domain.pr_analysis import (
    PrAnalysisLine,
    PrAnalysisReport,
    sample_indices_for_f2p,
)
from fontaine.run_log import progress, timed_phase


def _apply_shared_npm_cache(cache_dir: Path | None) -> None:
    """Point npm at a persistent cache directory (shared across worker clones)."""
    if cache_dir is None:
        return
    root = cache_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["npm_config_cache"] = str(root)


def _git_clone_local(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    r = subprocess.run(
        ["git", "clone", "--local", str(src.resolve()), str(dst.resolve())],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "")[:800]
        raise RuntimeError(f"git clone failed: {msg}".strip())


def _run_one_f2p(
    batch_position: int,
    line: PrAnalysisLine,
    row: dict[str, Any],
    repo_path: Path,
    *,
    result_index: int,
    batch_total: int,
    test_timeout: int,
    merge_affected_packages: bool,
) -> tuple[int, PrAnalysisLine]:
    with timed_phase(
        "f2p.pr",
        pr=line.number,
        idx=batch_position + 1,
        total=batch_total,
    ):
        title_short = line.title.replace("\r", " ").replace("\n", " ").strip()[:100]
        if line.complexity_score is not None:
            progress(
                "F2P/P2P: PR #%s (%s/%s) — %s — complexity_score=%.2f",
                line.number,
                batch_position + 1,
                batch_total,
                title_short,
                line.complexity_score,
            )
        else:
            progress(
                "F2P/P2P: PR #%s (%s/%s) — %s",
                line.number,
                batch_position + 1,
                batch_total,
                title_short,
            )
        base = str(row.get("baseRefOid") or "")
        head = str(row.get("headRefOid") or "")
        nodes = row.get("files_nodes") or []

        out = analyze_pr_f2p(
            repo_path,
            base_sha=base,
            head_sha=head,
            files_nodes=nodes if isinstance(nodes, list) else [],
            test_timeout=test_timeout,
            merge_affected_packages=merge_affected_packages,
        )
        if out.error:
            progress(
                "F2P/P2P: PR #%s finished with error — %s",
                line.number,
                summarize_f2p_error_message(out.error, max_len=140),
            )
            pl = PrAnalysisLine(
                number=line.number,
                title=line.title,
                complexity_score=line.complexity_score,
                f2p_count=None,
                p2p_count=None,
                f2p_error=summarize_f2p_error_message(out.error, max_len=400),
                f2p_diagnostic=None,
            )
        else:
            progress(
                "F2P/P2P: PR #%s finished — F2P=%s P2P=%s",
                line.number,
                len(out.f2p_tests),
                len(out.p2p_tests),
            )
            pl = PrAnalysisLine(
                number=line.number,
                title=line.title,
                complexity_score=line.complexity_score,
                f2p_count=len(out.f2p_tests),
                p2p_count=len(out.p2p_tests),
                f2p_error=None,
                f2p_diagnostic=out.diagnostic,
            )
    return result_index, pl


def _worker_loop(
    bundle: list[tuple[int, PrAnalysisLine, dict[str, Any], int]],
    clone: Path,
    *,
    batch_total: int,
    test_timeout: int,
    merge_affected_packages: bool,
) -> dict[int, PrAnalysisLine]:
    out: dict[int, PrAnalysisLine] = {}
    for batch_position, line, row, result_index in bundle:
        ri, pl = _run_one_f2p(
            batch_position,
            line,
            row,
            clone,
            result_index=result_index,
            batch_total=batch_total,
            test_timeout=test_timeout,
            merge_affected_packages=merge_affected_packages,
        )
        out[ri] = pl
    return out


def enrich_report_with_ts_f2p(
    report: PrAnalysisReport,
    accepted_rows: tuple[dict[str, Any], ...],
    repo_path: Path,
    *,
    limit: int,
    test_timeout: int,
    merge_affected_packages: bool = False,
    workers: int = 1,
    npm_cache_dir: Path | None = None,
) -> PrAnalysisReport:
    """
    Rebuild ``passed`` lines with F2P/P2P counts where analysis succeeds.

    ``accepted_rows`` must align index-for-index with ``report.passed`` (same order).

    ``limit`` is the target number of PRs to run. When fewer PRs passed screening than
    ``limit``, all are run in screening order. Otherwise a **random sample** of size
    ``limit`` is drawn with a bell curve over :func:`~fontaine.domain.pr_analysis.pr_complexity_score`
    (medium score preferred; score blends GraphQL file stats with a patch cyclomatic proxy
    when available). Set ``FONTAINE_F2P_SAMPLE_SEED`` for reproducibility.

    With ``workers`` > 1, uses ``workers`` isolated git clones (from ``repo_path`` via
    ``git clone --local``) so PRs run in parallel; ``npm_cache_dir`` enables a shared npm
    cache directory across clones.
    """
    if len(accepted_rows) != len(report.passed):
        return dataclasses.replace(report, f2p_limit=limit)

    _apply_shared_npm_cache(npm_cache_dir)

    picked = sample_indices_for_f2p(accepted_rows, limit)
    if len(picked) < len(accepted_rows):
        progress(
            "F2P/P2P: sampling %s of %s passed PR(s) (bell curve on complexity); "
            "--pr-f2p-limit=%s",
            len(picked),
            len(accepted_rows),
            limit,
        )

    tasks = [
        (
            bp,
            report.passed[orig_i],
            accepted_rows[orig_i],
            orig_i,
        )
        for bp, orig_i in enumerate(picked)
    ]

    results: dict[int, PrAnalysisLine] = {}

    eff_w = min(max(1, workers), len(tasks)) if tasks else 1

    with timed_phase(
        "f2p.batch",
        pr_count=len(tasks),
        workers=eff_w,
        merge=int(bool(merge_affected_packages)),
    ):
        if eff_w <= 1 or len(tasks) <= 1:
            root = repo_path.resolve()
            for batch_position, line, row, result_index in tasks:
                ri, pl = _run_one_f2p(
                    batch_position,
                    line,
                    row,
                    root,
                    result_index=result_index,
                    batch_total=len(tasks),
                    test_timeout=test_timeout,
                    merge_affected_packages=merge_affected_packages,
                )
                results[ri] = pl
        else:
            bundles: list[list[tuple[int, PrAnalysisLine, dict[str, Any], int]]] = [
                [] for _ in range(eff_w)
            ]
            for j, t in enumerate(tasks):
                bundles[j % eff_w].append(t)

            tmp_roots: list[Path] = []
            clones: list[Path] = []
            try:
                src = repo_path.resolve()
                for w in range(eff_w):
                    td = Path(tempfile.mkdtemp(prefix="fontaine-f2p-w"))
                    tmp_roots.append(td)
                    dest = td / "repo"
                    progress(
                        "F2P/P2P: preparing isolated clone %s/%s at %s",
                        w + 1,
                        eff_w,
                        dest,
                    )
                    _git_clone_local(src, dest)
                    clones.append(dest)

                with ThreadPoolExecutor(max_workers=eff_w) as ex:
                    futs = [
                        ex.submit(
                            _worker_loop,
                            bundles[w],
                            clones[w],
                            batch_total=len(tasks),
                            test_timeout=test_timeout,
                            merge_affected_packages=merge_affected_packages,
                        )
                        for w in range(eff_w)
                        if bundles[w]
                    ]
                    for f in as_completed(futs):
                        results.update(f.result())
            finally:
                for td in tmp_roots:
                    shutil.rmtree(td, ignore_errors=True)

    new_lines: list[PrAnalysisLine] = []
    for i in range(len(report.passed)):
        if i in results:
            new_lines.append(results[i])
        else:
            new_lines.append(report.passed[i])

    return PrAnalysisReport(
        total_analyzed=report.total_analyzed,
        considered_for_filters=report.considered_for_filters,
        passed_first_filters=report.passed_first_filters,
        rejection_breakdown=report.rejection_breakdown,
        passed=tuple(new_lines),
        first_stage_rejected=report.first_stage_rejected,
        pr_target_mode=report.pr_target_mode,
        f2p_limit=limit,
        pr_analysis_lookback_days=report.pr_analysis_lookback_days,
    )
