"""Python/pytest execution with JUnit XML for structured pass/fail lists."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fontaine.domain.f2p.detect import pytest_ready_at
from fontaine.domain.f2p.env_truthy import env_truthy
from fontaine.domain.f2p.junit_xml import parse_junit_xml
from fontaine.domain.f2p.models import StageResult

logger = logging.getLogger(__name__)


def _which_py() -> list[str]:
    env = (os.environ.get("FONTAINE_F2P_PYTHON") or "").strip()
    if env:
        return [env]
    for cand in ("python3", "python"):
        exe = shutil.which(cand)
        if exe:
            return [exe]
    return ["python3"]


def find_python_project_root(repo: Path, *, hint: Path | None = None) -> Path:
    """
    Pick directory for ``pytest`` + ``pip install``.

    ``hint``: explicit package dir (monorepo merge mode).
    """
    repo = repo.resolve()
    if hint is not None:
        h = hint.resolve()
        try:
            h.relative_to(repo)
        except ValueError:
            return repo
        if pytest_ready_at(h):
            return h
        return h

    if pytest_ready_at(repo):
        return repo

    try:
        for child in sorted(repo.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                if pytest_ready_at(child):
                    return child
    except OSError:
        pass

    return repo


def _venv_python_executable(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _read_pyproject_text(root: Path, *, max_bytes: int = 400_000) -> str:
    p = root / "pyproject.toml"
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:max_bytes]
    except OSError:
        return ""


def _is_poetry_project(root: Path) -> bool:
    return "[tool.poetry]" in _read_pyproject_text(root)


def _poetry_declared_optional_groups(pyproject_text: str) -> set[str]:
    """Names from ``[tool.poetry.group.<name>.dependencies]`` headers."""
    found: set[str] = set()
    for m in re.finditer(
        r"\[tool\.poetry\.group\.([a-zA-Z0-9_-]+)(?:\.dependencies)?\s*\]",
        pyproject_text,
    ):
        found.add(m.group(1))
    return found


def _poetry_extra_groups_env() -> list[str]:
    raw = (os.environ.get("FONTAINE_F2P_POETRY_GROUPS") or "test").strip()
    return [p.strip() for p in raw.split(",") if p.strip()]


def _poetry_cli_prefix() -> list[str] | None:
    """
    Resolve how to invoke Poetry: ``poetry`` on PATH, else ``sys.executable -m poetry``
    (requires the ``poetry`` package, declared in Fontaine's ``requirements.txt``).
    """
    exe = shutil.which("poetry")
    if exe:
        return [exe]
    try:
        r = subprocess.run(
            [sys.executable, "-m", "poetry", "--version"],
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ},
        )
        if r.returncode == 0:
            return [sys.executable, "-m", "poetry"]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _try_poetry_install_python(project_root: Path, *, timeout: int) -> tuple[Path | None, str | None]:
    """
    When ``pyproject.toml`` is Poetry-based and Poetry is available (see
    :func:`_poetry_cli_prefix`), run ``poetry install --with …`` so dependency **groups**
    (e.g. ``test``) are present — ``pip install -e .`` alone omits those.

    Disabled by ``FONTAINE_F2P_USE_POETRY=0``. On failure (non-zero exit, missing env,
    subprocess timeout, or ``OSError`` launching Poetry/pip), returns ``(None, None)``
    so callers fall back to the generic venv + pip path.
    """
    if not env_truthy("FONTAINE_F2P_USE_POETRY", default=True):
        return None, None
    if not _is_poetry_project(project_root):
        return None, None
    poetry_prefix = _poetry_cli_prefix()
    if not poetry_prefix:
        return None, None

    ppt = _read_pyproject_text(project_root)
    declared = _poetry_declared_optional_groups(ppt)
    requested = _poetry_extra_groups_env()
    with_groups = [g for g in requested if g in declared]
    if requested and not with_groups:
        with_groups = requested

    budget = min(600, timeout)
    cmd = [*poetry_prefix, "install", "--no-interaction", "-q"]
    if with_groups:
        cmd.extend(["--with", ",".join(with_groups)])

    env = {**os.environ}
    try:
        r = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=budget,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if r.returncode != 0 and with_groups:
        try:
            r = subprocess.run(
                [*poetry_prefix, "install", "--no-interaction", "-q"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=budget,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None, None
    if r.returncode != 0:
        return None, None

    try:
        info = subprocess.run(
            [*poetry_prefix, "env", "info", "-p"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=45,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if info.returncode != 0:
        return None, None
    line = (info.stdout or "").strip()
    if not line:
        return None, None
    py = _venv_python_executable(Path(line))
    if not py.is_file():
        return None, None

    skip_pip = (os.environ.get("FONTAINE_F2P_SKIP_PIP_INSTALL") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if skip_pip:
        return py, None

    try:
        pr = subprocess.run(
            [str(py), "-m", "pip", "install", "-q", "pytest"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=min(300, timeout),
            env={**os.environ},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if pr.returncode != 0:
        tail = (pr.stderr or pr.stdout or "")[:600]
        return None, f"pip install pytest failed in Poetry env: {tail}"

    return py, None


def _ensure_venv(project_root: Path, *, timeout: int) -> tuple[Path | None, str | None]:
    """Return venv python or None + error."""
    venv = project_root / ".venv"
    py_exe = _venv_python_executable(venv)
    base_cmd = _which_py()

    skip = (os.environ.get("FONTAINE_F2P_SKIP_VENV") or "").strip().lower()
    use_system = skip in ("1", "true", "yes", "on")

    if use_system:
        exe = shutil.which(base_cmd[0])
        if not exe:
            return None, f"{base_cmd[0]} not found on PATH"
        return Path(exe), None

    py_poetry, err_poetry = _try_poetry_install_python(project_root, timeout=timeout)
    if err_poetry:
        logger.warning("%s", err_poetry)
    if py_poetry is not None:
        return py_poetry, None

    if not py_exe.is_file():
        r = subprocess.run(
            [*base_cmd, "-m", "venv", str(venv)],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=min(120, timeout),
            env={**os.environ},
        )
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "")[:800]
            return None, f"python -m venv failed: {tail}"

    pip = subprocess.run(
        [str(py_exe), "-m", "pip", "install", "-q", "--upgrade", "pip"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=min(180, timeout),
        env={**os.environ},
    )
    if pip.returncode != 0:
        tail = (pip.stderr or pip.stdout or "")[:600]
        return None, f"pip upgrade failed: {tail}"

    skip_install = (os.environ.get("FONTAINE_F2P_SKIP_PIP_INSTALL") or "").strip().lower()
    if skip_install in ("1", "true", "yes", "on"):
        return py_exe, None

    # Editable install when declared.
    install_cmd: list[str] | None = None
    if (project_root / "pyproject.toml").is_file():
        install_cmd = [str(py_exe), "-m", "pip", "install", "-q", "-e", "."]
    elif (project_root / "setup.py").is_file():
        install_cmd = [str(py_exe), "-m", "pip", "install", "-q", "-e", "."]

    if install_cmd:
        ir = subprocess.run(
            install_cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=min(600, timeout),
            env={**os.environ},
        )
        if ir.returncode != 0:
            msg = (ir.stderr or ir.stdout or "")[:800]
            return None, f"pip install -e . failed: {msg}"

    pr = subprocess.run(
        [str(py_exe), "-m", "pip", "install", "-q", "pytest"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=min(300, timeout),
        env={**os.environ},
    )
    if pr.returncode != 0:
        tail = (pr.stderr or pr.stdout or "")[:600]
        return None, f"pip install pytest failed: {tail}"

    return py_exe, None


def run_python_tests(
    repo_root: Path,
    *,
    test_timeout: int,
    py_project_root: Path | None = None,
) -> tuple[StageResult, str | None]:
    """
    Run pytest at the current working tree state; return ``(StageResult, "pytest" or None)``.
    """
    repo_root = repo_root.resolve()
    proj = find_python_project_root(repo_root, hint=py_project_root)
    if not pytest_ready_at(proj):
        return StageResult(error="pytest project not detected at expected root"), None

    py_exe, verr = _ensure_venv(proj, timeout=test_timeout)
    if verr or py_exe is None:
        return StageResult(error=verr or "venv setup failed"), None

    fd, junit_path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    junit = Path(junit_path)
    env = {**os.environ, "CI": "true", "PYTHONWARNINGS": "ignore"}

    cmd = [
        str(py_exe),
        "-m",
        "pytest",
        "--tb=no",
        "-q",
        "--junitxml",
        str(junit),
    ]
    addopts = (os.environ.get("FONTAINE_F2P_PYTEST_ARGS") or "").strip()
    if addopts:
        cmd.extend(addopts.split())

    try:
        r = subprocess.run(
            cmd,
            cwd=proj,
            capture_output=True,
            text=True,
            timeout=test_timeout,
            env=env,
        )
        tail_out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
        try:
            junit_empty = not junit.read_bytes().strip()
        except OSError:
            junit_empty = True
        if junit_empty:
            return (
                StageResult(
                    error=(
                        "pytest wrote empty JUnit XML (report not flushed — typically import/"
                        "collection failure, --strict-config, or pytest exiting before junitxml). "
                        f"exit={r.returncode}. Captured output:\n{tail_out[-3500:]}"
                    ),
                    exit_code=r.returncode,
                ),
                "pytest",
            )

        out = parse_junit_xml(junit, project_root=proj)
        out.exit_code = r.returncode
        if out.error:
            return out, "pytest"

        if not out.passed and not out.failed and not out.skipped:
            tail = (r.stdout or "") + "\n" + (r.stderr or "")
            if r.returncode != 0:
                return (
                    StageResult(
                        error=f"pytest produced no parseable tests (exit {r.returncode}): "
                        f"{tail[-1400:]}",
                        exit_code=r.returncode,
                    ),
                    "pytest",
                )
            out.warning = out.warning or "pytest ran but reported zero tests"
        return out, "pytest"
    finally:
        try:
            junit.unlink()
        except OSError:
            pass
