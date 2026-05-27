"""Choose JS runner (Jest/Vitest/Mocha) vs pytest vs Maven from repository layout and PR file paths."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Literal

from fontaine.domain.f2p.maven_runner import maven_layout_present, maven_runner_available
from fontaine.domain.f2p.ts_runner import detect_ts_runner_kind, find_js_project_root

F2PBackend = Literal["typescript", "python", "maven"]


def _typescript_runner_available(repo: Path) -> bool:
    """
    True if some workspace under ``repo`` yields a detectable Jest/Vitest/Mocha setup.

    Tries :func:`find_js_project_root` first, then the repository root if that directory
    fails detection (common when a monorepo child is chosen but runner deps live at root).
    """
    r = repo.resolve()
    preferred = find_js_project_root(r)
    seen: set[Path] = set()
    for cand in (preferred, r):
        c = cand.resolve()
        if c in seen:
            continue
        seen.add(c)
        if detect_ts_runner_kind(c) is not None:
            return True
    return False


def _read_text(path: Path, max_bytes: int = 400_000) -> str | None:
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()[:max_bytes]
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def _pytest_signals_in_pyproject(text: str) -> bool:
    if "[tool.pytest" in text:
        return True
    # Loose match: pytest listed as dependency / optional extra (common patterns).
    lines = text.lower().splitlines()
    for ln in lines:
        if "pytest" in ln and any(
            x in ln for x in ("dependencies", "optional-dependencies", "requires", "dev")
        ):
            return True
    return False


# Skip giant / external trees when scanning for test modules (rglob is expensive).
_SKIP_PATH_PARTS = frozenset(
    {
        ".git",
        "node_modules",
        "venv",
        ".venv",
        "__pycache__",
        "dist",
        "build",
        ".tox",
        ".eggs",
        "site-packages",
        ".npm",
        ".yarn",
    }
)


def _path_has_skip_part(path: Path) -> bool:
    return any(p in _SKIP_PATH_PARTS for p in path.parts)


# Cap walks when no match (avoid scanning huge trees forever).
_MAX_TEST_FILE_SCAN = 8000


def _repo_has_pytest_style_tests(root: Path) -> bool:
    """
    True if the tree contains pytest-style modules (``test_*.py``) outside ignored dirs.

    Covers monorepo layouts like ``packages/foo/tests/test_x.py`` where only one-level
    child scans miss the package.
    """
    root = root.resolve()
    n = 0
    try:
        for p in root.rglob("test_*.py"):
            n += 1
            if n > _MAX_TEST_FILE_SCAN:
                return False
            if _path_has_skip_part(p):
                continue
            return True
    except OSError:
        pass
    return False


def _repo_has_nested_conftest(root: Path) -> bool:
    root = root.resolve()
    n = 0
    try:
        for p in root.rglob("conftest.py"):
            n += 1
            if n > _MAX_TEST_FILE_SCAN:
                return False
            if _path_has_skip_part(p):
                continue
            return True
    except OSError:
        pass
    return False


_TEST_DIR_NAMES = frozenset({"tests", "test", "testing", "legacy_tests"})


def _pytest_under_named_test_dirs(root: Path) -> bool:
    """``tests/``, ``test/``, ``testing/``, … with nested ``test_*.py`` (not only top-level)."""
    for name in _TEST_DIR_NAMES:
        d = root / name
        if not d.is_dir():
            continue
        try:
            for p in d.rglob("test_*.py"):
                if not _path_has_skip_part(p):
                    return True
        except OSError:
            pass
    return False


def pytest_ready_at(root: Path) -> bool:
    """True if ``root`` looks like a pytest-driven Python project."""
    if (root / "pytest.ini").is_file():
        return True
    cfg = root / "setup.cfg"
    if cfg.is_file():
        t = _read_text(cfg)
        if t and "[tool:pytest]" in t:
            return True
    tox = root / "tox.ini"
    if tox.is_file():
        t = _read_text(tox)
        if t and ("pytest" in t.lower() or "tool:pytest" in t):
            return True
    pp = root / "pyproject.toml"
    if pp.is_file():
        t = _read_text(pp)
        if t and _pytest_signals_in_pyproject(t):
            return True
    # Explicit layout hints without committed config (still common).
    if (root / "conftest.py").is_file():
        return True
    if _pytest_under_named_test_dirs(root):
        return True
    if _repo_has_nested_conftest(root):
        return True
    if _repo_has_pytest_style_tests(root):
        return True
    return False


def _line_declares_pytest_requirement(line: str) -> bool:
    """True when a requirements.txt line lists pytest (after stripping comments)."""
    s = line.split("#", 1)[0].strip()
    if not s:
        return False
    low = s.lower().split(";")[0].strip()
    tok = low.split("[")[0].strip()
    # PEP 508 name or obvious pytest plugins (pytest-xdist, …).
    return tok == "pytest" or tok.startswith("pytest-")


def _pytest_listed_in_requirements_files(root: Path) -> bool:
    """Detect ``pytest`` pinned in ``requirements*.txt`` (no pyproject needed)."""
    fixed = (
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "dev-requirements.txt",
    )
    for name in fixed:
        t = _read_text(root / name)
        if not t:
            continue
        for raw in t.splitlines():
            if _line_declares_pytest_requirement(raw):
                return True
    try:
        for p in sorted(root.glob("requirements*.txt")):
            if not p.is_file():
                continue
            t = _read_text(p)
            if not t:
                continue
            for raw in t.splitlines():
                if _line_declares_pytest_requirement(raw):
                    return True
    except OSError:
        pass
    return False


def _python_test_tree_without_pytest_ini(root: Path) -> bool:
    """Layout that suggests pytest-style tests when config lives only in requirements."""
    if _repo_has_pytest_style_tests(root):
        return True
    if _pytest_under_named_test_dirs(root):
        return True
    if _repo_has_nested_conftest(root):
        return True
    return False


def _paths_suggest_js_tests(paths: Iterable[str]) -> bool:
    """
    Heuristic when repo-level TS detection fails: PR touches obvious JS/TS tests.

    Used so we still pick the TypeScript backend when :func:`detect_ts_runner_kind` fails
    on a mis-identified workspace subdirectory but file paths are clearly test files.
    """
    for raw in paths:
        if not raw:
            continue
        name = Path(raw.replace("\\", "/")).name.lower()
        suf = Path(raw).suffix.lower()
        if suf not in (
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
            ".vue",
            ".svelte",
        ):
            continue
        if ".spec." in name or ".test." in name:
            return True
        if name.startswith("test.") or name.endswith(".test.js") or name.endswith(".test.ts"):
            return True
    return False


def _paths_suggest_python_tests(paths: Iterable[str]) -> bool:
    """
    Heuristic when repo layout signals are missing: PR touches obvious Python tests.

    Lets F2P select pytest when the merge only lists paths like ``qa/test_x.py`` or
    ``src/foo_test.py`` without a conventional ``tests/`` tree at repo root.
    """
    for raw in paths:
        if not raw:
            continue
        suf = Path(raw).suffix.lower()
        if suf != ".py":
            continue
        base = Path(raw.replace("\\", "/")).name.lower()
        if base.startswith("test_"):
            return True
        if base.endswith("_test.py"):
            return True
    return False


def pytest_runner_available(repo: Path) -> bool:
    """Whether pytest can be selected for ``repo`` (checks root then shallow children)."""
    root = repo.resolve()
    if pytest_ready_at(root):
        return True
    if _pytest_listed_in_requirements_files(root) and _python_test_tree_without_pytest_ini(
        root
    ):
        return True
    try:
        for child in sorted(root.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                if pytest_ready_at(child):
                    return True
                if _pytest_listed_in_requirements_files(child) and _python_test_tree_without_pytest_ini(
                    child
                ):
                    return True
    except OSError:
        pass
    return False


def choose_f2p_backend(
    repo: Path,
    changed_paths: list[str],
    test_paths: list[str],
) -> F2PBackend | None:
    """
    Pick TypeScript vs Python vs Maven runner without CLI switches.

    This is **only** the coarse language/backend choice. It does not distinguish Jest vs
    Vitest vs Mocha — that happens per ``package.json`` in ``detect_ts_runner_kind``.
    Uses repo capabilities plus PR path extensions; breaks ties with test-path hints,
    then prefers TypeScript when still ambiguous among **available** stacks (historical default).
    """
    repo_r = repo.resolve()
    ts_ok = _typescript_runner_available(repo_r)
    py_ok = pytest_runner_available(repo_r)
    mv_ok = maven_runner_available(repo_r)

    n_capabilities = int(ts_ok) + int(py_ok) + int(mv_ok)
    if n_capabilities == 1:
        if ts_ok:
            return "typescript"
        if py_ok:
            return "python"
        return "maven"

    if n_capabilities == 0:
        merged = [*changed_paths, *test_paths]
        if _paths_suggest_python_tests(merged):
            return "python"
        if _paths_suggest_js_tests(merged):
            return "typescript"

    py_ext = frozenset({".py", ".pyi"})
    js_ext = frozenset(
        {
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
            ".vue",
            ".svelte",
        }
    )
    # JVM sources that imply a Maven-oriented PR when they dominate path counts.
    java_ext = frozenset({".java", ".kt", ".kts"})

    def count_exts(paths: list[str]) -> tuple[int, int, int]:
        py_n = 0
        js_n = 0
        java_n = 0
        for p in paths:
            suf = Path(p).suffix.lower()
            if suf in py_ext:
                py_n += 1
            elif suf in js_ext:
                js_n += 1
            elif suf in java_ext:
                java_n += 1
        return py_n, js_n, java_n

    def dominant_backend(triple: tuple[int, int, int]) -> F2PBackend | None:
        py_n, js_n, java_n = triple
        m = max(py_n, js_n, java_n)
        if m == 0:
            return None
        if sum(1 for x in (py_n, js_n, java_n) if x == m) != 1:
            return None
        if py_n == m:
            return "python"
        if js_n == m:
            return "typescript"
        return "maven"

    def extension_pick(triple: tuple[int, int, int]) -> F2PBackend | None:
        """
        Pick backend from path extensions.

        When Maven is runnable (``mv_ok``), Java counts participate like Python/JS, but a path
        winner is only returned if that stack is available (``py_ok`` / ``ts_ok``); otherwise we
        fall back to another runnable backend or Maven. If counts give **no** unique winner (all
        zeros, or a three-way tie), return ``None`` so callers try other path lists instead of
        defaulting to TypeScript/Maven. When Maven is **not** runnable, JVM file counts are ignored
        for this step so ``.java`` cannot outvote an available Python or JS stack;
        ``maven_layout_present`` below still routes JVM + ``pom.xml`` PRs to Maven for actionable
        errors.
        """
        py_n, js_n, java_n = triple
        if mv_ok:
            w = dominant_backend(triple)
            if w is None:
                return None
            if w == "python" and py_ok:
                return "python"
            if w == "typescript" and ts_ok:
                return "typescript"
            if w == "maven":
                return "maven"
            # Counts favored py/ts but that runner is not available at repo level.
            if w == "python" and not py_ok:
                return "typescript" if ts_ok else "maven"
            if w == "typescript" and not ts_ok:
                return "python" if py_ok else "maven"

        w2 = dominant_backend((py_n, js_n, 0))
        if w2 == "python" and py_ok:
            return "python"
        if w2 == "typescript" and ts_ok:
            return "typescript"
        if py_n > js_n and py_ok:
            return "python"
        if js_n > py_n and ts_ok:
            return "typescript"
        if py_n == 0 and js_n == 0:
            return None
        if py_ok and not ts_ok:
            return "python"
        if ts_ok and not py_ok:
            return "typescript"
        if ts_ok:
            return "typescript"
        if py_ok:
            return "python"
        return None

    for paths in (changed_paths, test_paths):
        w = extension_pick(count_exts(paths))
        if w:
            return w

    test_set = frozenset(x.replace("\\", "/") for x in test_paths)
    non_test = [p for p in changed_paths if p.replace("\\", "/") not in test_set]
    w = extension_pick(count_exts(non_test))
    if w:
        return w

    # JVM-dominant PR on a repo with pom.xml: still Maven so the orchestrator surfaces PATH
    # Maven/wrapper or Docker-based Maven hints instead of "no runner detected".
    if maven_layout_present(repo_r):
        for paths in (changed_paths, test_paths, non_test):
            if dominant_backend(count_exts(paths)) == "maven":
                return "maven"

    if n_capabilities == 0:
        return None
    if ts_ok:
        return "typescript"
    if py_ok:
        return "python"
    if mv_ok:
        return "maven"
    return None
