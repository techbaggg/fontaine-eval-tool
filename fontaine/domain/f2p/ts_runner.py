"""
TypeScript/JavaScript test execution (Jest, Vitest, Mocha, or asyncjs).

Selects one runner per ``package.json`` via :func:`detect_ts_runner_kind`. Runs the
project's ``npm test`` (or overridden script) when declared; otherwise invokes Jest /
Mocha via ``npx`` / ``pnpm exec`` / ``yarn run`` so repos like paigo-backend (runner in
devDependencies plus ``test/**/*.spec.js``, no ``scripts.test``) still execute.
Vitest already uses ``npx vitest`` and does not rely on ``scripts.test``.

**asyncjs** (e.g. Ace editor): ``devDependencies`` includes ``asyncjs`` and
``scripts.test`` typically runs ``node src/test/all.js``. Output lines look like
``[k/n] Suite: case OK`` — parsed by :func:`parse_asyncjs_console_output`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fontaine.domain.f2p.errors import summarize_dependency_install_log
from fontaine.domain.f2p.jest_parse import parse_jest_json_output_file
from fontaine.domain.f2p.models import StageResult


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def detect_package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    return "npm"


def read_package_json(root: Path) -> dict | None:
    p = root / "package.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


def _git_work_tree_root(start: Path) -> Path | None:
    """Return ``git rev-parse --show-toplevel`` for ``start``, or None."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start.resolve()),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode != 0:
            return None
        line = (r.stdout or "").strip()
        return Path(line).resolve() if line else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _is_workspace_root(repo: Path, pkg: dict) -> bool:
    """Root ``package.json`` drives deps for npm/yarn/pnpm workspaces."""
    ws = pkg.get("workspaces")
    if ws:
        return True
    if (repo / "pnpm-workspace.yaml").is_file():
        return True
    if (repo / "lerna.json").is_file():
        return True
    if (repo / "turbo.json").is_file() or (repo / "turbo.jsonc").is_file():
        return True
    if (repo / "nx.json").is_file():
        return True
    return False


_RUNNER_DEP_KEYS = frozenset(
    {
        "jest",
        "vitest",
        "mocha",
        "asyncjs",
        "ts-jest",
        "@types/jest",
        "@vitejs/plugin-react",
        "@types/mocha",
    }
)

_JS_SPEC_GLOBS = (
    "*.spec.js",
    "*.spec.ts",
    "*.spec.tsx",
    "*.spec.mjs",
    "*.spec.cjs",
    "*.test.js",
    "*.test.ts",
    "*.test.tsx",
    "*.test.mjs",
    "*.test.cjs",
)

_JS_TEST_DIRS = ("test", "tests", "__tests__")

_MAX_JS_LAYOUT_SCAN = 4000


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _deps_only_js_allowed() -> bool:
    """Allow inference from package.json deps alone (no conventional test/config layout)."""
    return _truthy_env("FONTAINE_F2P_JS_DEPS_ONLY")


def _runner_deps_present(pkg: dict) -> bool:
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    return any(k in deps for k in _RUNNER_DEP_KEYS)


def _read_text_small(path: Path, max_bytes: int = 96_000) -> str | None:
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()[:max_bytes]
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def _jest_config_present(root: Path) -> bool:
    try:
        for p in root.glob("jest.config.*"):
            if p.is_file():
                return True
    except OSError:
        pass
    return False


def _vitest_layout_signal(root: Path) -> bool:
    try:
        for p in root.glob("vitest.config.*"):
            if p.is_file():
                return True
    except OSError:
        pass
    for name in ("vite.config.ts", "vite.config.js", "vite.config.mts", "vite.config.mjs"):
        p = root / name
        if p.is_file():
            t = _read_text_small(p)
            if t and "vitest" in t.lower():
                return True
    return False


def _mocha_rc_present(root: Path) -> bool:
    for name in (
        ".mocharc.json",
        ".mocharc.js",
        ".mocharc.cjs",
        ".mocharc.yaml",
        ".mocharc.yml",
        "mocha.opts",
    ):
        if (root / name).is_file():
            return True
    return False


def _generic_js_spec_files_under_test_dirs(root: Path) -> bool:
    """True if conventional dirs contain *.spec.* / *.test.* (skips node_modules)."""
    n = 0
    for sub in _JS_TEST_DIRS:
        d = root / sub
        if not d.is_dir():
            continue
        try:
            for pattern in _JS_SPEC_GLOBS:
                for p in d.rglob(pattern):
                    n += 1
                    if n > _MAX_JS_LAYOUT_SCAN:
                        return False
                    if "node_modules" in p.parts:
                        continue
                    return True
        except OSError:
            pass
    return False


def _js_runner_layout_hint(root: Path) -> bool:
    """
    Repo signals that JS tests likely live here (configs or common tree layout).

    Used when ``scripts.test`` is absent so we only infer Jest/Vitest/Mocha from deps
    together with layout — unless :envvar:`FONTAINE_F2P_JS_DEPS_ONLY` relaxes that rule.
    """
    root = root.resolve()
    if _jest_config_present(root):
        return True
    if _vitest_layout_signal(root):
        return True
    if _mocha_rc_present(root):
        return True
    return _generic_js_spec_files_under_test_dirs(root)


def named_npm_script_present(root: Path, script_name: str) -> bool:
    """Whether ``package.json`` defines a non-empty named script (default: ``test``)."""
    pkg = read_package_json(root)
    if not pkg:
        return False
    return bool(str((pkg.get("scripts") or {}).get(script_name) or "").strip())


def effective_test_script_name() -> str:
    return (os.environ.get("FONTAINE_F2P_NPM_SCRIPT") or "").strip() or "test"


def _should_run_tests_from_repo_root(repo: Path, pkg: dict) -> bool:
    """Stay at repo root when tests are wired here (including workspace roots)."""
    scripts = pkg.get("scripts") or {}
    has_test = bool(str(scripts.get("test") or "").strip())
    has_runner_dep = _runner_deps_present(pkg)

    if has_test:
        if has_runner_dep:
            return True
        test_s = (scripts.get("test") or "").lower()
        if any(
            x in test_s
            for x in ("jest", "vitest", "mocha", "turbo", "nx run", "lerna")
        ):
            return True
        if _is_workspace_root(repo, pkg):
            return True
        return False

    if not has_runner_dep:
        return False

    if _is_workspace_root(repo, pkg):
        return True
    return _js_runner_layout_hint(repo) or _deps_only_js_allowed()


def _child_package_usable_for_js_f2p(cand: Path, pkg: dict) -> bool:
    """Nested ``package.json`` usable without ``scripts.test`` when deps + layout match."""
    if not _runner_deps_present(pkg):
        return False
    has_test = bool(str((pkg.get("scripts") or {}).get("test") or "").strip())
    if has_test:
        return True
    if _is_workspace_root(cand, pkg):
        return True
    return _js_runner_layout_hint(cand) or _deps_only_js_allowed()


def find_js_project_root(repo: Path) -> Path:
    """Pick the JS workspace directory for install + test execution (workspace-aware)."""
    if (repo / "package.json").is_file():
        pkg = read_package_json(repo)
        if pkg and _should_run_tests_from_repo_root(repo, pkg):
            return repo

    for sub in ("web", "app", "apps", "packages", "frontend", "client", "api"):
        cand = repo / sub
        if cand.is_dir() and (cand / "package.json").is_file():
            pkg = read_package_json(cand)
            if pkg and _child_package_usable_for_js_f2p(cand, pkg):
                return cand

    for child in sorted(repo.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir() and (child / "package.json").is_file():
            pkg = read_package_json(child)
            if pkg and _child_package_usable_for_js_f2p(child, pkg):
                return child

    return repo


def detect_ts_runner_kind(root: Path) -> str | None:
    """
    Pick a single JS/TS test runner for this ``package.json`` workspace.

    If several frameworks are declared (e.g. both Jest and Mocha in devDependencies),
    precedence is **Vitest → Jest → Mocha** — there is no per-file or per-test routing
    within one package.

    Without ``scripts.test``, a runner is selected when it appears in dependencies and
    either the tree shows conventional config/tests, the package is a workspace root, or
    :envvar:`FONTAINE_F2P_JS_DEPS_ONLY` is set.
    """
    pkg = read_package_json(root)
    if not pkg:
        return None
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    scripts = pkg.get("scripts") or {}
    test_s = (scripts.get("test") or "").lower()
    has_test = bool(str(scripts.get("test") or "").strip())

    def pick_from_deps() -> str | None:
        if "vitest" in deps:
            return "vitest"
        if "jest" in deps or "ts-jest" in deps or "@types/jest" in deps:
            return "jest"
        if "mocha" in deps or "@types/mocha" in deps:
            return "mocha"
        if "asyncjs" in deps:
            return "asyncjs"
        return None

    if has_test:
        k = pick_from_deps()
        if k is not None:
            return k
        if "vitest" in test_s:
            return "vitest"
        if "jest" in test_s:
            return "jest"
        if "mocha" in test_s:
            return "mocha"
        if _is_workspace_root(root, pkg):
            # test_s and deps already checked above; last-resort default for workspace roots.
            return "jest"
        # Orchestrators (aligned with ``_should_run_tests_from_repo_root``): ``scripts.test``
        # may invoke turbo/nx/lerna without spelling jest/vitest/mocha in the string.
        # ``pick_from_deps()`` was already None at the top of this branch (lines 299–301).
        if any(x in test_s for x in ("turbo", "nx run", "lerna")):
            return "jest"
        if _js_runner_layout_hint(root):
            return "jest"
        return None

    k = pick_from_deps()
    if k is None:
        return None
    if _is_workspace_root(root, pkg):
        return k
    if _js_runner_layout_hint(root) or _deps_only_js_allowed():
        return k
    return None


def _npm_install_cwd_and_workspace_args(js_root: Path) -> tuple[Path, list[str]]:
    """
    npm workspaces require ``npm install`` / ``npm ci`` from the **workspace root**.
    When Fontaine's JS directory is a nested package (an ancestor ``package.json`` defines
    ``workspaces``), return that ancestor and ``-w <package.json name>``.

    Single-package repos return ``(js_root, [])``. Only ancestors **inside the same git
    repository** are considered (never walks above ``git rev-parse --show-toplevel``).
    """
    js_root = js_root.resolve()
    child_pkg = read_package_json(js_root)
    pkg_name = str((child_pkg or {}).get("name") or "").strip()
    git_root = _git_work_tree_root(js_root)

    if child_pkg and child_pkg.get("workspaces"):
        return js_root, []

    cur = js_root.parent
    anchor = Path(js_root.anchor)
    depth = 0
    while cur != cur.parent and cur != anchor and depth < 24:
        depth += 1
        if git_root is not None:
            try:
                cur.relative_to(git_root)
            except ValueError:
                break
        rp = read_package_json(cur)
        if rp and rp.get("workspaces"):
            if pkg_name:
                return cur, ["-w", pkg_name]
            return cur, []
        cur = cur.parent
    return js_root, []


def _npm_install_env() -> dict[str, str]:
    """
    Environment for npm installs.

    Many shells set ``NODE_ENV=production``, which makes npm **omit devDependencies**
    — fatal for test runners. Unless ``FONTAINE_F2P_PRESERVE_NODE_ENV=1``, force
    ``NODE_ENV=development`` for the install subprocess when the parent had production.

    A Python virtualenv does **not** provide Node/npm; Fontaine uses ``PATH`` and expects
    a system or nvm/Homebrew ``node``/``npm`` like any other subprocess.
    """
    env = {**os.environ, "CI": "true"}
    if (os.environ.get("FONTAINE_F2P_PRESERVE_NODE_ENV") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return env
    if env.get("NODE_ENV", "").strip().lower() == "production":
        env = {**env, "NODE_ENV": "development"}
    return env


def _npm_run_install_with_retries(
    npm: str,
    install_cwd: Path,
    *,
    ws_args: list[str],
    timeout: int,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    """
    ``npm ci`` when ``package-lock.json`` exists, then fall back to ``npm install``.
    Adds ``--include-workspace-root`` only when ``install_cwd``/package.json declares
    ``workspaces`` (avoids spurious npm messaging on single-package repos).
    """
    install_cwd = install_cwd.resolve()
    pkg = read_package_json(install_cwd)
    monorepo_workspaces = bool(pkg and pkg.get("workspaces"))
    lock_exists = (install_cwd / "package-lock.json").is_file()

    def build_cmd(verb: str, *, include_ws_root: bool) -> list[str]:
        flags = ["--legacy-peer-deps", "--ignore-scripts"]
        if include_ws_root and monorepo_workspaces:
            flags.append("--include-workspace-root")
        flags.extend(ws_args)
        return [npm, verb, *flags]

    last: subprocess.CompletedProcess

    if lock_exists:
        last = subprocess.run(
            build_cmd("ci", include_ws_root=monorepo_workspaces),
            cwd=install_cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if last.returncode == 0:
            return last

    last = subprocess.run(
        build_cmd("install", include_ws_root=monorepo_workspaces),
        cwd=install_cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if last.returncode == 0:
        return last

    if monorepo_workspaces and ws_args:
        last = subprocess.run(
            build_cmd("install", include_ws_root=False),
            cwd=install_cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if last.returncode == 0:
            return last

    return last


def install_dependencies(root: Path, *, timeout: int = 300) -> str | None:
    """
    Install JS deps before tests. npm uses ``--legacy-peer-deps`` and
    ``--ignore-scripts``, with workspace-aware cwd/flags when applicable.

    Workspace installs use ``--include-workspace-root`` only if ``package.json`` at the
    install root defines ``workspaces``. Ancestor walks never leave the git work tree.

    Retries: ``npm ci`` → ``npm install`` → install without ``--include-workspace-root``
    (monorepos only). ``NODE_ENV=production`` in the shell is overridden to ``development``
    for installs unless ``FONTAINE_F2P_PRESERVE_NODE_ENV=1``.

    Call this once per JS workspace per PR analysis (stage 1); stages 2–3 reuse
    ``node_modules``. Set ``FONTAINE_F2P_SKIP_INSTALL=1`` to skip every install when
    dependencies are already present (power-user override).
    """
    skip = (os.environ.get("FONTAINE_F2P_SKIP_INSTALL") or "").strip().lower()
    if skip in ("1", "true", "yes", "on"):
        return None

    pm = detect_package_manager(root)
    env = _npm_install_env()
    if pm == "pnpm":
        exe = _which("pnpm")
        if not exe:
            return "pnpm not found on PATH"
        r = subprocess.run(
            [exe, "install", "--frozen-lockfile"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    elif pm == "yarn":
        exe = _which("yarn")
        if not exe:
            return "yarn not found on PATH"
        r = subprocess.run(
            [exe, "install", "--frozen-lockfile"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    else:
        npm = _which("npm")
        if not npm:
            return "npm not found on PATH"
        install_cwd, ws_args = _npm_install_cwd_and_workspace_args(root)
        r = _npm_run_install_with_retries(
            npm,
            install_cwd,
            ws_args=ws_args,
            timeout=timeout,
            env=env,
        )
    if r.returncode != 0:
        combined = (r.stderr or "") + "\n" + (r.stdout or "")
        msg = summarize_dependency_install_log(combined)
        return msg or f"install exit {r.returncode}"
    return None


def _jest_extra_cli_flags() -> list[str]:
    """Optional flags from env (e.g. ``FONTAINE_F2P_JEST_RUN_IN_BAND=1``)."""
    flags: list[str] = []
    rib = (os.environ.get("FONTAINE_F2P_JEST_RUN_IN_BAND") or "").strip().lower()
    if rib in ("1", "true", "yes", "on"):
        flags.append("--runInBand")
    return flags


def run_jest_stage(root: Path, *, timeout: int, json_out: Path) -> StageResult:
    pm = detect_package_manager(root)
    env = {**os.environ, "CI": "true"}
    json_out.parent.mkdir(parents=True, exist_ok=True)

    extra = _jest_extra_cli_flags()
    npm_script = effective_test_script_name()
    use_script = named_npm_script_present(root, npm_script)

    jest_tail = [
        *extra,
        "--json",
        f"--outputFile={json_out}",
        "--watchAll=false",
        "--passWithNoTests",
    ]

    if use_script:
        if pm == "yarn":
            cmd = ["yarn", npm_script, "--", *jest_tail]
        elif pm == "pnpm":
            cmd = ["pnpm", "run", npm_script, "--", *jest_tail]
        else:
            cmd = ["npm", "run", npm_script, "--", *jest_tail]
    elif pm == "yarn":
        cmd = ["yarn", "run", "jest", "--", *jest_tail]
    elif pm == "pnpm":
        cmd = ["pnpm", "exec", "jest", *jest_tail]
    else:
        npx = _which("npx")
        if not npx:
            return StageResult(error="npx not found on PATH (needed to run jest without npm test)")
        cmd = [npx, "jest", *jest_tail]

    r = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    out = parse_jest_json_output_file(json_out, project_root=root)
    out.exit_code = r.returncode
    if out.error:
        return out
    if not out.passed and not out.failed and not out.skipped:
        tail = (r.stdout or "") + "\n" + (r.stderr or "")
        if r.returncode != 0:
            return StageResult(
                error=f"Jest produced no parseable tests (exit {r.returncode}): {tail[-1200:]}",
                exit_code=r.returncode,
            )
    return out


def parse_vitest_json(path: Path) -> StageResult:
    """Vitest JSON reporter aggregate file (shape similar to Jest in many versions)."""
    if not path.is_file():
        return StageResult(error=f"Vitest JSON missing: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        return StageResult(error=f"Invalid Vitest JSON: {e}")

    passed: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("title") or ""
            if item.get("result", {}).get("state") == "pass":
                passed.append(str(name))
            elif item.get("result", {}).get("state") == "fail":
                failed.append(str(name))
        return StageResult(passed=passed, failed=failed, skipped=skipped)

    for tr in raw.get("testResults", []) or []:
        for a in tr.get("assertionResults", []) or []:
            fn = a.get("fullName") or a.get("title") or ""
            st = (a.get("status") or "").lower()
            if st == "passed":
                passed.append(str(fn))
            elif st == "failed":
                failed.append(str(fn))
            else:
                skipped.append(str(fn))
    return StageResult(passed=passed, failed=failed, skipped=skipped)


def run_vitest_stage(root: Path, *, timeout: int, json_out: Path) -> StageResult:
    pm = detect_package_manager(root)
    env = {**os.environ, "CI": "true"}
    json_out.parent.mkdir(parents=True, exist_ok=True)

    npx = _which("npx")
    if not npx:
        return StageResult(error="npx not found on PATH")

    cmd = [
        npx,
        "vitest",
        "run",
        "--reporter=json",
        f"--outputFile={json_out}",
    ]

    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout, env=env)
    st = parse_vitest_json(json_out)
    st.exit_code = r.returncode
    if st.error:
        return st
    if not st.passed and not st.failed and not st.skipped and r.returncode != 0:
        tail = (r.stdout or "") + (r.stderr or "")
        return StageResult(
            error=f"Vitest failed or produced no tests: {tail[-800:]}",
            exit_code=r.returncode,
        )
    return st


_ASYNCJS_LINE_RE = re.compile(
    r"^\[(\d+)/(\d+)\]\s+(.+)\s+(OK|FAIL)\s*$",
    re.MULTILINE,
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def parse_asyncjs_console_output(text: str) -> StageResult:
    """
    Parse asyncjs runner console lines (see ace ``src/test/asyncjs/test.js`` ``report()``).

    Example:: ``[12/150] mode/text_test: test unicode OK``
    """
    blob = _ANSI_ESCAPE_RE.sub("", text or "")
    passed: list[str] = []
    failed: list[str] = []
    for m in _ASYNCJS_LINE_RE.finditer(blob):
        name = (m.group(3) or "").strip()
        if not name:
            continue
        if m.group(4) == "OK":
            passed.append(name)
        else:
            failed.append(name)
    if not passed and not failed:
        return StageResult(
            error="asyncjs output contained no [n/m] … OK|FAIL lines "
            "(need asyncjs-style test reporter output)",
        )
    return StageResult(passed=passed, failed=failed, skipped=[])


def run_asyncjs_stage(root: Path, *, timeout: int) -> StageResult:
    """Run ``npm|pnpm|yarn test`` for asyncjs harness (custom ``node …`` script)."""
    pm = detect_package_manager(root)
    env = {**os.environ, "CI": "true"}
    npm_script = effective_test_script_name()

    if pm == "yarn":
        cmd = ["yarn", npm_script]
    elif pm == "pnpm":
        cmd = ["pnpm", "run", npm_script]
    else:
        npm = _which("npm")
        if not npm:
            return StageResult(error="npm not found on PATH")
        cmd = ["npm", "run", npm_script]

    r = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    combined = (r.stdout or "") + "\n" + (r.stderr or "")
    out = parse_asyncjs_console_output(combined)
    out.exit_code = r.returncode
    if out.error:
        return out
    if not out.passed and not out.failed and r.returncode != 0:
        tail = combined[-1400:]
        return StageResult(
            error=f"asyncjs exited {r.returncode} with no parseable tests: {tail}",
            exit_code=r.returncode,
        )
    return out


def parse_mocha_json_report(text: str) -> StageResult:
    """Parse Mocha's built-in ``--reporter json`` output (usually on stdout)."""
    blob = (text or "").strip()
    start = blob.find("{")
    if start < 0:
        return StageResult(error="Mocha JSON reporter produced no JSON object")
    try:
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(blob[start:])
    except json.JSONDecodeError as e:
        return StageResult(error=f"Invalid Mocha JSON: {e}")

    passed: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []

    tests = data.get("tests")
    if isinstance(tests, list):
        for t in tests:
            if not isinstance(t, dict):
                continue
            fn = str(t.get("fullTitle") or t.get("title") or "").strip()
            if not fn:
                continue
            if t.get("pending"):
                skipped.append(fn)
                continue
            err = t.get("err")
            has_err = bool(isinstance(err, dict) and err) or (
                isinstance(err, str) and bool(err.strip())
            )
            if has_err:
                failed.append(fn)
            else:
                passed.append(fn)
        return StageResult(passed=passed, failed=failed, skipped=skipped)

    for item in data.get("passes", []) or []:
        if isinstance(item, dict):
            fn = str(item.get("fullTitle") or item.get("title") or "").strip()
            if fn:
                passed.append(fn)
    for item in data.get("failures", []) or []:
        if isinstance(item, dict):
            fn = str(item.get("fullTitle") or item.get("title") or "").strip()
            if fn:
                failed.append(fn)
    for item in data.get("pending", []) or []:
        if isinstance(item, dict):
            fn = str(item.get("fullTitle") or item.get("title") or "").strip()
            if fn:
                skipped.append(fn)

    if passed or failed or skipped:
        return StageResult(passed=passed, failed=failed, skipped=skipped)

    return StageResult(error="Mocha JSON contained no tests")


def run_mocha_stage(root: Path, *, timeout: int) -> StageResult:
    pm = detect_package_manager(root)
    env = {**os.environ, "CI": "true"}
    npm_script = effective_test_script_name()
    raw_extra = (os.environ.get("FONTAINE_F2P_MOCHA_ARGS") or "").strip()
    extra = raw_extra.split() if raw_extra else []

    reporter_args = ["--reporter", "json", *extra]
    use_script = named_npm_script_present(root, npm_script)

    if use_script:
        if pm == "yarn":
            cmd = ["yarn", npm_script, "--", *reporter_args]
        elif pm == "pnpm":
            cmd = ["pnpm", "run", npm_script, "--", *reporter_args]
        else:
            cmd = ["npm", "run", npm_script, "--", *reporter_args]
    elif pm == "yarn":
        cmd = ["yarn", "run", "mocha", "--", *reporter_args]
    elif pm == "pnpm":
        cmd = ["pnpm", "exec", "mocha", *reporter_args]
    else:
        npx = _which("npx")
        if not npx:
            return StageResult(error="npx not found on PATH (needed to run mocha without npm test)")
        cmd = [npx, "mocha", *reporter_args]

    r = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    combined = (r.stdout or "") + "\n" + (r.stderr or "")
    out = parse_mocha_json_report(combined)
    out.exit_code = r.returncode
    if out.error:
        return out
    if not out.passed and not out.failed and not out.skipped:
        tail = combined[-1400:]
        if r.returncode != 0:
            return StageResult(
                error=(
                    f"Mocha produced no parseable tests (exit {r.returncode}): {tail}"
                ),
                exit_code=r.returncode,
            )
        out.warning = out.warning or "mocha ran but reported zero tests"
    return out


def run_typescript_tests(
    repo_root: Path,
    *,
    test_timeout: int,
    js_project_root: Path | None = None,
    skip_install: bool = False,
) -> tuple[StageResult, str | None]:
    """
    Run tests at current working tree state.

    ``js_project_root``: when set (e.g. monorepo package dir), resolve the JS workspace
    from this path instead of auto-detecting from ``repo_root`` alone.

    ``skip_install``: when True, do not invoke ``install_dependencies`` (used for F2P
    stages 2–3 after stage 1 populated ``node_modules``). For stage 1, always pass
    ``False`` unless you know deps are present. ``FONTAINE_F2P_SKIP_INSTALL`` is handled
    inside :func:`install_dependencies` when that helper runs.

    Returns ``(StageResult, runner_kind or None on failure to detect)``.
    """
    repo_root = repo_root.resolve()
    if js_project_root is not None:
        anchor = js_project_root.resolve()
        try:
            anchor.relative_to(repo_root)
        except ValueError:
            return StageResult(error="js_project_root must be inside repo_root"), None
        # Use the explicit package directory as-is (do not re-walk into a child app).
        # Merge-packages mode passes repo root or workspace folders; find_js_project_root
        # would otherwise pick the first nested package and miss the full suite.
        if (anchor / "package.json").is_file():
            js_root = anchor
        else:
            js_root = find_js_project_root(anchor)
    else:
        js_root = find_js_project_root(repo_root)
    kind = detect_ts_runner_kind(js_root)
    if kind is None and js_root != repo_root:
        kind = detect_ts_runner_kind(repo_root)
    if kind is None:
        return StageResult(
            error=(
                "No supported JS test runner detected (need Jest/Vitest/Mocha/asyncjs in "
                "dependencies with scripts.test and/or conventional config/tests, "
                "or workspace root / FONTAINE_F2P_JS_DEPS_ONLY)"
            ),
        ), None

    pm = detect_package_manager(js_root)
    if not skip_install:
        inst = install_dependencies(js_root, timeout=min(300, test_timeout))
        if inst:
            return StageResult(error=f"{pm} install failed: {inst}"), kind

    if kind == "asyncjs":
        return run_asyncjs_stage(js_root, timeout=test_timeout), kind

    if kind == "mocha":
        return run_mocha_stage(js_root, timeout=test_timeout), kind

    fd, json_name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    json_path = Path(json_name)
    try:
        if kind == "jest":
            return run_jest_stage(js_root, timeout=test_timeout, json_out=json_path), kind
        return run_vitest_stage(js_root, timeout=test_timeout, json_out=json_path), kind
    finally:
        try:
            json_path.unlink()
        except OSError:
            pass
