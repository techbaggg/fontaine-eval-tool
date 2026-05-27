"""Maven Surefire/Failsafe JUnit XML for PR F2P (no Gradle in this module)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from fontaine.domain.f2p.docker_maven import (
    docker_cli_available,
    docker_maven_argv,
    ensure_maven_docker_image_ready,
    maven_docker_enabled,
    resolve_maven_docker_image,
)
from fontaine.domain.f2p.git_workspace import path_exists_at
from fontaine.domain.f2p.junit_xml import parse_junit_xml
from fontaine.domain.f2p.maven_effective_pom import (
    skip_effective_pom_resolution,
    try_java_major_from_effective_pom,
)
from fontaine.domain.f2p.maven_java_version import infer_java_major_from_maven_project
from fontaine.domain.f2p.models import StageResult
from fontaine.run_log import progress

_SKIP_WALK_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "venv",
        ".venv",
        "__pycache__",
        ".npm",
        ".yarn",
    }
)


def find_maven_project_root(repo: Path, *, hint: Path | None = None) -> Path:
    """
    Directory containing ``pom.xml`` for Maven goals (default ``test``; override with ``FONTAINE_F2P_MAVEN_GOALS``).

    ``hint``: explicit module root (monorepo merge mode — currently unused for Maven F2P).
    """
    repo = repo.resolve()
    if hint is not None:
        h = hint.resolve()
        try:
            h.relative_to(repo)
        except ValueError:
            return repo
        if (h / "pom.xml").is_file():
            return h
        return h

    if (repo / "pom.xml").is_file():
        return repo

    try:
        for child in sorted(repo.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                if (child / "pom.xml").is_file():
                    return child
    except OSError:
        pass

    return repo


def maven_layout_present(repo: Path) -> bool:
    """True when ``find_maven_project_root`` yields a directory that contains ``pom.xml``."""
    root = find_maven_project_root(repo.resolve())
    return (root / "pom.xml").is_file()


def _mvn_command(project_root: Path) -> list[str] | None:
    """Prefer Maven Wrapper, then ``mvn`` on PATH."""
    root = project_root.resolve()
    mvnw = root / "mvnw"
    if mvnw.is_file():
        if sys.platform == "win32":
            cmd_path = root / "mvnw.cmd"
            if cmd_path.is_file():
                return [str(cmd_path)]
        import os as _os

        if _os.access(mvnw, _os.X_OK):
            return [str(mvnw)]
        return ["sh", str(mvnw)]

    exe = shutil.which("mvn")
    if exe:
        return [exe]
    # Some Windows setups only expose ``mvn.cmd``; ``which('mvn')`` still usually resolves it,
    # but check explicitly when needed.
    if sys.platform == "win32":
        cmd = shutil.which("mvn.cmd")
        if cmd:
            return [cmd]
    return None


def mvn_executable_missing_message(
    repo_root: Path,
    project_root: Path,
    *,
    head_sha: str | None = None,
) -> str:
    """
    User-facing explanation when ``mvn``/``mvnw`` cannot be invoked (PATH, wrapper placement).
    """
    base = (
        "Neither Apache Maven (`mvn` on PATH) nor a Maven Wrapper (`./mvnw` in this project) "
        "is available here. Fontaine PR F2P runs `mvn` at the **merge-base** checkout first "
        "(stage 1), then with PR patches — install JDK + Maven on the runner or commit "
        "`mvnw` + `.mvn/wrapper` at merge base."
    )
    if not head_sha:
        return base
    root = repo_root.resolve()
    proj = project_root.resolve()
    try:
        rel = proj.relative_to(root)
        mvnw_git = f"{rel.as_posix()}/mvnw" if rel.parts else "mvnw"
    except ValueError:
        mvnw_git = "mvnw"
    if not (proj / "mvnw").is_file() and path_exists_at(root, mvnw_git, head_sha):
        return (
            base
            + " This repo has `mvnw` on the PR branch but **not** at the merge-base revision; "
            "stage 1 needs Maven or `mvnw` at base too (rebase the default branch, or install Maven on PATH)."
        )
    return base


def maven_runner_available(repo: Path) -> bool:
    """
    True when a ``pom.xml`` exists and Maven can run via host ``mvn``/``mvnw``, or via Docker when
    host Maven is missing (:envvar:`FONTAINE_F2P_MAVEN_NO_DOCKER` opts out).
    """
    root = find_maven_project_root(repo.resolve())
    if not (root / "pom.xml").is_file():
        return False
    if _mvn_command(root) is not None:
        return True
    return docker_cli_available() and maven_docker_enabled()


def collect_maven_junit_xml_paths(project_root: Path, *, max_files: int = 800) -> list[Path]:
    """
    Collect JUnit XML under ``**/target/surefire-reports/*.xml`` and ``**/target/failsafe-reports/*.xml``.

    Surefire uses ``target/surefire-reports``; Failsafe uses ``target/failsafe-reports``
    (e.g. ``mvn verify``). Nested multi-module builds are included.
    """
    root = project_root.resolve()
    out: list[Path] = []
    _report_dirs = frozenset({"surefire-reports", "failsafe-reports"})
    try:
        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            parts = Path(dirpath).parts
            if any(p in _SKIP_WALK_NAMES for p in parts):
                dirnames[:] = []
                continue
            base = Path(dirpath)
            if base.name in _report_dirs and base.parent.name == "target":
                for fn in filenames:
                    if fn.endswith(".xml"):
                        out.append(base / fn)
                        if len(out) >= max_files:
                            return sorted(out)
            _prune_hidden(dirnames)
    except OSError:
        pass
    return sorted(out)


def _prune_hidden(dirnames: list[str]) -> None:
    dirnames[:] = [d for d in dirnames if not d.startswith(".")]


def _pom_xml_stat_identity(proj: Path) -> tuple[int, int] | None:
    """``(st_mtime_ns, st_size)`` for ``proj/pom.xml``, or ``None`` if unreadable."""
    try:
        st = (proj / "pom.xml").stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


# ``help:effective-pom`` is expensive; PR F2P calls ``run_maven_tests`` three times per PR with the
# same checkout layout — reuse the resolved Java major when ``pom.xml`` identity is unchanged.
_effective_pom_java_major_cache: dict[tuple[str, str, tuple[int, int], str], int | None] = {}


def _try_java_major_from_effective_pom_if_needed(
    proj: Path,
    repo_root: Path,
    *,
    local_mvn: list[str] | None,
    effective_timeout: int,
    pull_budget: int,
    bootstrap_image: str,
) -> int | None:
    """
    Run ``mvn help:effective-pom`` only when Fontaine will run ``mvn`` in **Docker** and the JDK
    image tag is not already fixed by env.

    With **host** ``mvn``/``mvnw``, effective POM is skipped: it can take many minutes on large
    projects (dependency resolution) and does not affect the host ``mvn`` invocation.
    """
    if local_mvn is not None:
        return None
    if skip_effective_pom_resolution():
        return None
    if (os.environ.get("FONTAINE_F2P_MAVEN_DOCKER_IMAGE") or "").strip():
        return None
    if (os.environ.get("FONTAINE_F2P_MAVEN_JAVA_MAJOR") or "").strip().isdigit():
        return None

    pom_id = _pom_xml_stat_identity(proj)
    if pom_id is None:
        return None
    cache_key = (
        str(proj.resolve()),
        str(repo_root.resolve()),
        pom_id,
        bootstrap_image,
    )
    if cache_key in _effective_pom_java_major_cache:
        return _effective_pom_java_major_cache[cache_key]

    result = try_java_major_from_effective_pom(
        proj,
        repo_root,
        local_mvn_cmd=local_mvn,
        timeout_sec=effective_timeout,
        pull_timeout_sec=pull_budget,
        bootstrap_image=bootstrap_image,
    )
    _effective_pom_java_major_cache[cache_key] = result
    return result


def merge_maven_junit_stage_results(parts: list[StageResult]) -> StageResult:
    """
    Merge parsed JUnit XML from Maven Surefire and Failsafe (multiple modules / files).

    **Per test id**, if it appears in more than one bucket across files, status follows the same
    rule as :func:`fontaine.domain.f2p.orchestrator._stage_to_map`: **SKIPPED** over **FAILED** over
    **PASSED**. Duplicate ids within one bucket are collapsed.
    """
    merged = StageResult()
    passed_ids: set[str] = set()
    failed_ids: set[str] = set()
    skipped_ids: set[str] = set()
    for st in parts:
        if st.error:
            return StageResult(error=st.error)
        passed_ids.update(st.passed)
        failed_ids.update(st.failed)
        skipped_ids.update(st.skipped)
        if st.exit_code not in (None, 0):
            merged.exit_code = st.exit_code
        if st.warning:
            merged.warning = (
                f"{merged.warning}; {st.warning}".strip("; ")
                if merged.warning
                else st.warning
            )
    failed_final = failed_ids - skipped_ids
    passed_final = passed_ids - skipped_ids - failed_final
    merged.skipped = sorted(skipped_ids)
    merged.failed = sorted(failed_final)
    merged.passed = sorted(passed_final)
    return merged


def parse_maven_junit_reports(project_root: Path) -> StageResult:
    """Parse all Surefire and Failsafe JUnit XML under the Maven project tree."""
    paths = collect_maven_junit_xml_paths(project_root)
    if not paths:
        return StageResult(
            error=(
                f"No Maven JUnit XML under {project_root} "
                "(expected **/target/surefire-reports/*.xml or **/target/failsafe-reports/*.xml)"
            )
        )

    chunks: list[StageResult] = []
    for p in paths:
        st = parse_junit_xml(p, project_root=project_root)
        if st.error:
            try:
                disp = p.relative_to(project_root)
            except ValueError:
                disp = p
            return StageResult(error=f"{st.error} (file {disp})")
        chunks.append(st)
    return merge_maven_junit_stage_results(chunks)


def run_maven_tests(
    repo_root: Path,
    *,
    test_timeout: int,
    maven_project_root: Path | None = None,
    f2p_head_sha: str | None = None,
) -> tuple[StageResult, str | None]:
    """
    Run ``mvn`` at the current working tree (default goal ``test``; override ``FONTAINE_F2P_MAVEN_GOALS``);
    aggregate Surefire and Failsafe JUnit XML.

    ``target/`` is usually gitignored, so git checkout does not remove old classes or JUnit XML.
    Unless ``clean`` is already in ``FONTAINE_F2P_MAVEN_GOALS``, Fontaine prepends the ``clean``
    phase so each run does not reuse stale ``target/`` output between PR F2P stages.

    Returns ``(StageResult, runner_kind)`` where ``runner_kind`` is ``"maven"`` when the Maven
    project was recognized and ``mvn`` ran (even when tests fail — failures appear in
    ``StageResult.failed``), or ``None`` when Maven could not be invoked.
    """
    repo_root = repo_root.resolve()
    proj = find_maven_project_root(repo_root, hint=maven_project_root)
    if not (proj / "pom.xml").is_file():
        return StageResult(error="Maven project not detected (no pom.xml at expected root)"), None

    local_mvn = _mvn_command(proj)
    pull_budget = min(900, max(test_timeout * 2, 300))
    effective_timeout = min(420, max(120, test_timeout // 2))

    static_java = infer_java_major_from_maven_project(proj)
    preliminary_image = resolve_maven_docker_image(proj, discovered_java_major=static_java)

    effective_java_major = _try_java_major_from_effective_pom_if_needed(
        proj,
        repo_root,
        local_mvn=local_mvn,
        effective_timeout=effective_timeout,
        pull_budget=pull_budget,
        bootstrap_image=preliminary_image,
    )
    resolved_docker_image = resolve_maven_docker_image(
        proj,
        discovered_java_major=effective_java_major if effective_java_major is not None else static_java,
    )
    display_java_major = effective_java_major or static_java

    will_use_docker = (
        not local_mvn and docker_cli_available() and maven_docker_enabled()
    )
    if will_use_docker:
        pull_err = ensure_maven_docker_image_ready(
            image=resolved_docker_image,
            pull_timeout=pull_budget,
        )
        if pull_err:
            return StageResult(error=pull_err), None

    goals_raw = (os.environ.get("FONTAINE_F2P_MAVEN_GOALS") or "test").strip() or "test"
    goal_tokens = [t for t in goals_raw.split() if t] or ["test"]
    if "clean" not in goal_tokens:
        goal_tokens = ["clean", *goal_tokens]
    extra_raw = (os.environ.get("FONTAINE_F2P_MAVEN_ARGS") or "").strip()
    extra_list = extra_raw.split() if extra_raw else []

    env = {**os.environ, "CI": "true"}

    if local_mvn:
        cmd = [*local_mvn, "-B", *goal_tokens, *extra_list]
        run_cwd: Path | None = proj
    elif will_use_docker:
        if display_java_major is not None:
            src = "effective POM" if effective_java_major is not None else "local POM scan"
            progress(
                "[PR F2P] host Maven missing; Java %s (%s) — Docker image %s …",
                display_java_major,
                src,
                resolved_docker_image,
            )
        else:
            progress(
                "[PR F2P] host Maven missing; running `mvn` in Docker (%s) …",
                resolved_docker_image,
            )
        cmd = docker_maven_argv(
            repo_root,
            proj,
            image=resolved_docker_image,
            goals=" ".join(goal_tokens),
            extra_tokens=extra_list,
        )
        run_cwd = repo_root
    else:
        detail = mvn_executable_missing_message(repo_root, proj, head_sha=f2p_head_sha)
        if maven_docker_enabled() and not docker_cli_available():
            detail += (
                " `docker` was not found on PATH — Fontaine would pull and run "
                f"{resolved_docker_image} automatically when Docker is available."
            )
        elif not maven_docker_enabled():
            detail += (
                " Docker fallback for Maven is disabled — clear FONTAINE_F2P_MAVEN_NO_DOCKER, "
                "remove or change FONTAINE_F2P_MAVEN_USE_DOCKER=0/false/off, or set "
                "FONTAINE_F2P_MAVEN_USE_DOCKER=1 to allow it."
            )
        return StageResult(error=detail), None

    try:
        r = subprocess.run(
            cmd,
            cwd=run_cwd,
            capture_output=True,
            text=True,
            timeout=test_timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return StageResult(error=f"mvn timed out after {test_timeout}s"), "maven"
    except OSError as e:
        return StageResult(error=f"mvn failed to start: {e}"), None

    tail_out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()

    out = parse_maven_junit_reports(proj)
    out.exit_code = r.returncode

    if out.error:
        if not tail_out:
            return out, "maven"
        return (
            StageResult(
                error=f"{out.error}\nLast output:\n{tail_out[-3500:]}",
                exit_code=r.returncode,
            ),
            "maven",
        )

    if not out.passed and not out.failed and not out.skipped:
        if r.returncode != 0:
            return (
                StageResult(
                    error=(
                        "mvn reported no parseable tests (Surefire/Failsafe reports empty?) "
                        f"(exit {r.returncode}):\n{tail_out[-3500:]}"
                    ),
                    exit_code=r.returncode,
                ),
                "maven",
            )
        out.warning = out.warning or "mvn exited 0 but Surefire/Failsafe reported zero tests"

    return out, "maven"
