"""Three-stage F2P/P2P orchestration (Jest/Vitest/Mocha, pytest, or Maven); see :func:`analyze_pr_f2p`."""

from __future__ import annotations

from pathlib import Path

from fontaine.domain.f2p.classify import classify_f2p_p2p
from fontaine.domain.f2p.detect import choose_f2p_backend
from fontaine.domain.f2p.git_workspace import (
    apply_full_patch,
    apply_test_files_from_head,
    checkout_clean,
    ensure_commit,
    has_new_test_file,
)
from fontaine.domain.f2p.maven_runner import run_maven_tests
from fontaine.domain.f2p.models import F2PRunOutcome, StageResult
from fontaine.domain.f2p.packages import get_affected_packages
from fontaine.domain.f2p.py_runner import run_python_tests
from fontaine.domain.f2p.ts_runner import run_typescript_tests
from fontaine.domain.pr_analysis import _is_data_file_path, _is_test_path
from fontaine.run_log import progress, timed_phase


def _test_paths_from_nodes(nodes: list[dict]) -> list[str]:
    out: list[str] = []
    for n in nodes:
        p = str(n.get("path") or "")
        if not p or _is_data_file_path(p):
            continue
        if _is_test_path(p):
            out.append(p)
    return out


def _changed_paths_from_nodes(nodes: list[dict]) -> list[str]:
    """Non-asset paths from PR file nodes (for monorepo package discovery)."""
    out: list[str] = []
    for n in nodes:
        p = str(n.get("path") or "")
        if not p or _is_data_file_path(p):
            continue
        out.append(p.replace("\\", "/"))
    return out


def _package_display_prefix(repo: Path, pkg: Path) -> str:
    repo_r = repo.resolve()
    pkg_r = pkg.resolve()
    if pkg_r == repo_r:
        return ""
    try:
        return pkg_r.relative_to(repo_r).as_posix()
    except ValueError:
        return ""


def _merge_prefixed_stages(parts: list[tuple[str, StageResult]]) -> StageResult:
    """Stable ids: ``[relative/pkg] assertion name`` (empty prefix at repo root)."""
    merged = StageResult()
    warns: list[str] = []
    for rel, st in parts:
        if st.error:
            return StageResult(error=st.error)
        prefix = f"[{rel}] " if rel else ""
        merged.passed.extend(prefix + t for t in st.passed)
        merged.failed.extend(prefix + t for t in st.failed)
        merged.skipped.extend(prefix + t for t in st.skipped)
        if st.exit_code not in (None, 0):
            merged.exit_code = st.exit_code
        if st.warning:
            warns.append(f"{prefix}{st.warning}".strip())
    if warns:
        merged.warning = "; ".join(warns)
    return merged


def _run_typescript_tests_merged_packages(
    repo: Path,
    packages: list[Path],
    *,
    test_timeout: int,
    skip_install: bool = False,
) -> tuple[StageResult | None, str | None, str | None]:
    """
    Run install+tests per affected package and merge with stable prefixes.

    Packages without a detected JS test workspace (Jest/Vitest/Mocha), or runs that error, are skipped
    (try each discovered package; merge successes).

    ``skip_install``: pass True for F2P stages 2–3 so each package installs only on stage 1.
    """
    chunks: list[tuple[str, StageResult]] = []
    last_kind: str | None = None
    for pkg in packages:
        rel = _package_display_prefix(repo, pkg)
        st, kind = run_typescript_tests(
            repo,
            test_timeout=test_timeout,
            js_project_root=pkg,
            skip_install=skip_install,
        )
        if kind is None:
            continue
        last_kind = kind
        if st.error:
            continue
        chunks.append((rel, st))

    if not chunks:
        return (
            None,
            None,
            "No successful JS test run (Jest/Vitest/Mocha) in affected packages (merge mode)",
        )

    return _merge_prefixed_stages(chunks), last_kind, None


def _run_python_tests_merged_packages(
    repo: Path,
    packages: list[Path],
    *,
    test_timeout: int,
) -> tuple[StageResult | None, str | None, str | None]:
    """
    One pytest pass per affected package directory, merged with stable prefixes.

    Skips packages where ``run_python_tests`` cannot run (attempt each package root; do not
    require a prior ``pytest_ready_at`` gate).
    """
    chunks: list[tuple[str, StageResult]] = []
    last_kind: str | None = None
    for pkg in packages:
        rel = _package_display_prefix(repo, pkg)
        st, kind = run_python_tests(
            repo,
            test_timeout=test_timeout,
            py_project_root=pkg,
        )
        if kind is None:
            continue
        last_kind = kind
        if st.error:
            continue
        chunks.append((rel, st))

    if not chunks:
        return (
            None,
            None,
            "No successful pytest run in affected packages (merge mode)",
        )

    return _merge_prefixed_stages(chunks), last_kind, None


def _stage_to_map(stage: StageResult) -> dict[str, str]:
    """
    Map test id → status for :func:`classify_f2p_p2p`.

    **Precedence when the same id appears in more than one list** (duplicate XML ids, merged
    Surefire/Failsafe, etc.): assignments run in order **passed**, then **failed**, then
    **skipped**, so **SKIPPED** wins over **FAILED** over **PASSED**. This matches
    :func:`merge_maven_junit_stage_results` in :mod:`fontaine.domain.f2p.maven_runner`.
    """
    m: dict[str, str] = {}
    for t in stage.passed:
        m[t] = "PASSED"
    for t in stage.failed:
        m[t] = "FAILED"
    for t in stage.skipped:
        m[t] = "SKIPPED"
    return m


def analyze_pr_f2p_typescript(
    repo: Path,
    *,
    base_sha: str,
    head_sha: str,
    files_nodes: list[dict],
    test_timeout: int = 600,
    merge_affected_packages: bool = False,
) -> F2PRunOutcome:
    """
    Run base / before / after stages and classify F2P vs P2P tests.

    ``repo`` must be a git clone with ``origin`` fetchable for missing SHAs.

    When ``merge_affected_packages`` is True, discover package roots from **changed test
    paths only**, run install+tests once per package, and
    merge stage results with ``[relative/path] `` prefixes so ids stay stable across
    stages (multi-package runs).

    JS dependencies are installed on **stage 1 only** (per package in merge mode); stages
    2–3 reuse ``node_modules`` so users need not prep the clone or set skip-install env vars.
    """
    test_paths = _test_paths_from_nodes(files_nodes)
    if not test_paths:
        return F2PRunOutcome(error="No changed test paths in PR (by Fontaine heuristics)")

    if not base_sha or not head_sha:
        return F2PRunOutcome(error="Missing base or head SHA")

    repo = repo.resolve()

    for sha in (base_sha, head_sha):
        msg = ensure_commit(repo, sha)
        if msg:
            return F2PRunOutcome(error=msg)

    packages: list[Path] | None = None
    if merge_affected_packages:
        packages = get_affected_packages(repo, test_paths)

    merged = bool(merge_affected_packages and packages)

    err = checkout_clean(repo, base_sha)
    if err:
        return F2PRunOutcome(error=f"Checkout base: {err}")

    progress("[PR F2P] Stage 1/3: running tests at merge base …%s", base_sha[:8])

    with timed_phase("f2p.ts.stage1_base", merge=int(merged)):
        if merge_affected_packages and packages:
            base_stage, _, merge_err = _run_typescript_tests_merged_packages(
                repo,
                packages,
                test_timeout=test_timeout,
                skip_install=False,
            )
            if merge_err:
                return F2PRunOutcome(error=merge_err)
            assert base_stage is not None
        else:
            base_stage, runner_kind = run_typescript_tests(
                repo, test_timeout=test_timeout, skip_install=False
            )
            if runner_kind is None or base_stage.error:
                return F2PRunOutcome(
                    error=base_stage.error or "TypeScript test runner not detected or failed"
                )

    has_new = has_new_test_file(repo, base_sha, head_sha, test_paths)

    err = checkout_clean(repo, base_sha)
    if err:
        return F2PRunOutcome(error=f"Reset for before stage: {err}")

    terr = apply_test_files_from_head(repo, test_paths, head_sha, base_sha)
    if terr:
        return F2PRunOutcome(error=f"Apply test files (before): {terr}")

    progress("[PR F2P] Stage 2/3: running tests with test patch from head only")

    with timed_phase("f2p.ts.stage2_before", merge=int(merged)):
        if merge_affected_packages and packages:
            before_stage, _, merge_err = _run_typescript_tests_merged_packages(
                repo,
                packages,
                test_timeout=test_timeout,
                skip_install=True,
            )
            if merge_err:
                return F2PRunOutcome(error=f"Tests (before stage): {merge_err}")
            assert before_stage is not None
        else:
            before_stage, _ = run_typescript_tests(
                repo, test_timeout=test_timeout, skip_install=True
            )
            if before_stage.error:
                return F2PRunOutcome(error=f"Tests (before stage): {before_stage.error}")

    err = checkout_clean(repo, base_sha)
    if err:
        return F2PRunOutcome(error=f"Reset for after stage: {err}")

    perr = apply_full_patch(repo, base_sha, head_sha)
    if perr:
        return F2PRunOutcome(error=f"Apply full patch (after): {perr}")

    progress("[PR F2P] Stage 3/3: running tests with full PR patch")

    with timed_phase("f2p.ts.stage3_after", merge=int(merged)):
        if merge_affected_packages and packages:
            after_stage, _, merge_err = _run_typescript_tests_merged_packages(
                repo,
                packages,
                test_timeout=test_timeout,
                skip_install=True,
            )
            if merge_err:
                return F2PRunOutcome(error=f"Tests (after stage): {merge_err}")
            assert after_stage is not None
        else:
            after_stage, _ = run_typescript_tests(
                repo, test_timeout=test_timeout, skip_install=True
            )
            if after_stage.error:
                return F2PRunOutcome(error=f"Tests (after stage): {after_stage.error}")

    with timed_phase("f2p.ts.classify"):
        buckets = classify_f2p_p2p(
            _stage_to_map(base_stage),
            _stage_to_map(before_stage),
            _stage_to_map(after_stage),
            has_new_test_file=has_new,
        )
    diag_parts: list[str] = []
    for st in (base_stage, before_stage, after_stage):
        if st.warning:
            diag_parts.append(st.warning)
    diagnostic: str | None = None
    if diag_parts:
        seen: set[str] = set()
        uniq: list[str] = []
        for w in diag_parts:
            if w not in seen:
                seen.add(w)
                uniq.append(w)
        diagnostic = " | ".join(uniq)

    return F2PRunOutcome(
        f2p_tests=buckets["FAIL_TO_PASS"],
        p2p_tests=buckets["PASS_TO_PASS"],
        diagnostic=diagnostic,
    )


def analyze_pr_f2p_python(
    repo: Path,
    *,
    base_sha: str,
    head_sha: str,
    files_nodes: list[dict],
    test_timeout: int = 600,
    merge_affected_packages: bool = False,
) -> F2PRunOutcome:
    """Three-stage F2P/P2P using pytest + JUnit XML (see :func:`analyze_pr_f2p_typescript`)."""
    test_paths = _test_paths_from_nodes(files_nodes)
    if not test_paths:
        return F2PRunOutcome(error="No changed test paths in PR (by Fontaine heuristics)")

    if not base_sha or not head_sha:
        return F2PRunOutcome(error="Missing base or head SHA")

    repo = repo.resolve()

    for sha in (base_sha, head_sha):
        msg = ensure_commit(repo, sha)
        if msg:
            return F2PRunOutcome(error=msg)

    packages: list[Path] | None = None
    if merge_affected_packages:
        packages = get_affected_packages(repo, test_paths)

    merged = bool(merge_affected_packages and packages)

    err = checkout_clean(repo, base_sha)
    if err:
        return F2PRunOutcome(error=f"Checkout base: {err}")

    progress("[PR F2P] Stage 1/3: pytest at merge base …%s", base_sha[:8])

    with timed_phase("f2p.py.stage1_base", merge=int(merged)):
        if merge_affected_packages and packages:
            base_stage, _, merge_err = _run_python_tests_merged_packages(
                repo,
                packages,
                test_timeout=test_timeout,
            )
            if merge_err:
                return F2PRunOutcome(error=merge_err)
            assert base_stage is not None
        else:
            base_stage, runner_kind = run_python_tests(repo, test_timeout=test_timeout)
            if runner_kind is None or base_stage.error:
                return F2PRunOutcome(
                    error=base_stage.error or "pytest not detected or failed at merge base"
                )

    has_new = has_new_test_file(repo, base_sha, head_sha, test_paths)

    err = checkout_clean(repo, base_sha)
    if err:
        return F2PRunOutcome(error=f"Reset for before stage: {err}")

    terr = apply_test_files_from_head(repo, test_paths, head_sha, base_sha)
    if terr:
        return F2PRunOutcome(error=f"Apply test files (before): {terr}")

    progress("[PR F2P] Stage 2/3: pytest with test patch from head only")

    with timed_phase("f2p.py.stage2_before", merge=int(merged)):
        if merge_affected_packages and packages:
            before_stage, _, merge_err = _run_python_tests_merged_packages(
                repo,
                packages,
                test_timeout=test_timeout,
            )
            if merge_err:
                return F2PRunOutcome(error=f"Tests (before stage): {merge_err}")
            assert before_stage is not None
        else:
            before_stage, _ = run_python_tests(repo, test_timeout=test_timeout)
            if before_stage.error:
                return F2PRunOutcome(error=f"Tests (before stage): {before_stage.error}")

    err = checkout_clean(repo, base_sha)
    if err:
        return F2PRunOutcome(error=f"Reset for after stage: {err}")

    perr = apply_full_patch(repo, base_sha, head_sha)
    if perr:
        return F2PRunOutcome(error=f"Apply full patch (after): {perr}")

    progress("[PR F2P] Stage 3/3: pytest with full PR patch")

    with timed_phase("f2p.py.stage3_after", merge=int(merged)):
        if merge_affected_packages and packages:
            after_stage, _, merge_err = _run_python_tests_merged_packages(
                repo,
                packages,
                test_timeout=test_timeout,
            )
            if merge_err:
                return F2PRunOutcome(error=f"Tests (after stage): {merge_err}")
            assert after_stage is not None
        else:
            after_stage, _ = run_python_tests(repo, test_timeout=test_timeout)
            if after_stage.error:
                return F2PRunOutcome(error=f"Tests (after stage): {after_stage.error}")

    with timed_phase("f2p.py.classify"):
        buckets = classify_f2p_p2p(
            _stage_to_map(base_stage),
            _stage_to_map(before_stage),
            _stage_to_map(after_stage),
            has_new_test_file=has_new,
        )
    diag_parts: list[str] = []
    for st in (base_stage, before_stage, after_stage):
        if st.warning:
            diag_parts.append(st.warning)
    diagnostic: str | None = None
    if diag_parts:
        seen: set[str] = set()
        uniq: list[str] = []
        for w in diag_parts:
            if w not in seen:
                seen.add(w)
                uniq.append(w)
        diagnostic = " | ".join(uniq)

    return F2PRunOutcome(
        f2p_tests=buckets["FAIL_TO_PASS"],
        p2p_tests=buckets["PASS_TO_PASS"],
        diagnostic=diagnostic,
    )


def analyze_pr_f2p_maven(
    repo: Path,
    *,
    base_sha: str,
    head_sha: str,
    files_nodes: list[dict],
    test_timeout: int = 600,
    merge_affected_packages: bool = False,
) -> F2PRunOutcome:
    """Three-stage F2P/P2P using ``mvn`` + Surefire/Failsafe JUnit XML (single ``pom.xml`` tree)."""
    _ = merge_affected_packages  # v1: single-module Maven only (ignore merge-packages).

    test_paths = _test_paths_from_nodes(files_nodes)
    if not test_paths:
        return F2PRunOutcome(error="No changed test paths in PR (by Fontaine heuristics)")

    if not base_sha or not head_sha:
        return F2PRunOutcome(error="Missing base or head SHA")

    repo = repo.resolve()

    for sha in (base_sha, head_sha):
        msg = ensure_commit(repo, sha)
        if msg:
            return F2PRunOutcome(error=msg)

    err = checkout_clean(repo, base_sha)
    if err:
        return F2PRunOutcome(error=f"Checkout base: {err}")

    progress("[PR F2P] Stage 1/3: running Maven tests at merge base …%s", base_sha[:8])

    with timed_phase("f2p.maven.stage1_base", merge=0):
        base_stage, runner_kind = run_maven_tests(
            repo, test_timeout=test_timeout, f2p_head_sha=head_sha
        )
        if runner_kind is None or base_stage.error:
            return F2PRunOutcome(
                error=base_stage.error or "Maven test run not detected or failed at merge base"
            )

    has_new = has_new_test_file(repo, base_sha, head_sha, test_paths)

    err = checkout_clean(repo, base_sha)
    if err:
        return F2PRunOutcome(error=f"Reset for before stage: {err}")

    terr = apply_test_files_from_head(repo, test_paths, head_sha, base_sha)
    if terr:
        return F2PRunOutcome(error=f"Apply test files (before): {terr}")

    progress("[PR F2P] Stage 2/3: Maven tests with test patch from head only")

    with timed_phase("f2p.maven.stage2_before", merge=0):
        before_stage, _ = run_maven_tests(repo, test_timeout=test_timeout, f2p_head_sha=head_sha)
        if before_stage.error:
            return F2PRunOutcome(error=f"Tests (before stage): {before_stage.error}")

    err = checkout_clean(repo, base_sha)
    if err:
        return F2PRunOutcome(error=f"Reset for after stage: {err}")

    perr = apply_full_patch(repo, base_sha, head_sha)
    if perr:
        return F2PRunOutcome(error=f"Apply full patch (after): {perr}")

    progress("[PR F2P] Stage 3/3: Maven tests with full PR patch")

    with timed_phase("f2p.maven.stage3_after", merge=0):
        after_stage, _ = run_maven_tests(repo, test_timeout=test_timeout, f2p_head_sha=head_sha)
        if after_stage.error:
            return F2PRunOutcome(error=f"Tests (after stage): {after_stage.error}")

    with timed_phase("f2p.maven.classify"):
        buckets = classify_f2p_p2p(
            _stage_to_map(base_stage),
            _stage_to_map(before_stage),
            _stage_to_map(after_stage),
            has_new_test_file=has_new,
        )
    diag_parts: list[str] = []
    for st in (base_stage, before_stage, after_stage):
        if st.warning:
            diag_parts.append(st.warning)
    diagnostic: str | None = None
    if diag_parts:
        seen: set[str] = set()
        uniq: list[str] = []
        for w in diag_parts:
            if w not in seen:
                seen.add(w)
                uniq.append(w)
        diagnostic = " | ".join(uniq)

    return F2PRunOutcome(
        f2p_tests=buckets["FAIL_TO_PASS"],
        p2p_tests=buckets["PASS_TO_PASS"],
        diagnostic=diagnostic,
    )


def analyze_pr_f2p(
    repo: Path,
    *,
    base_sha: str,
    head_sha: str,
    files_nodes: list[dict],
    test_timeout: int = 600,
    merge_affected_packages: bool = False,
) -> F2PRunOutcome:
    """
    Auto-select Jest/Vitest/Mocha vs pytest vs Maven from the repo and PR paths, then run three-stage F2P.

    No separate CLI flags per language — selection is heuristic (see :mod:`fontaine.domain.f2p.detect`).
    """
    repo_r = repo.resolve()
    changed = _changed_paths_from_nodes(files_nodes)
    test_paths = _test_paths_from_nodes(files_nodes)
    backend = choose_f2p_backend(repo_r, changed, test_paths)

    if backend is None:
        return F2PRunOutcome(
            error=(
                "Could not detect a supported test runner: need Jest, Vitest, or Mocha with an "
                "npm/yarn/pnpm test script, pytest (pytest.ini / [tool.pytest] / conftest.py / tests/), "
                "or Maven (pom.xml with Surefire/Failsafe JUnit XML; host `mvn`/`mvnw`, or Docker for "
                "containerized Maven when Docker is available and not disabled via FONTAINE_F2P_MAVEN_*)."
            )
        )

    progress("[PR F2P] auto-selected runner: %s", backend)

    if backend == "typescript":
        return analyze_pr_f2p_typescript(
            repo_r,
            base_sha=base_sha,
            head_sha=head_sha,
            files_nodes=files_nodes,
            test_timeout=test_timeout,
            merge_affected_packages=merge_affected_packages,
        )

    if backend == "maven":
        return analyze_pr_f2p_maven(
            repo_r,
            base_sha=base_sha,
            head_sha=head_sha,
            files_nodes=files_nodes,
            test_timeout=test_timeout,
            merge_affected_packages=merge_affected_packages,
        )

    return analyze_pr_f2p_python(
        repo_r,
        base_sha=base_sha,
        head_sha=head_sha,
        files_nodes=files_nodes,
        test_timeout=test_timeout,
        merge_affected_packages=merge_affected_packages,
    )
