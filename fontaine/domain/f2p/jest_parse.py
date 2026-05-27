"""Parse Jest ``--json`` aggregate output files."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fontaine.domain.f2p.models import StageResult


def _node_version_text() -> str:
    try:
        r = subprocess.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            return (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _jest_undercount_warning(data: dict) -> str | None:
    """When Jest skips most suites (load/runtime errors), F2P/P2P counts stay low."""
    n_rt = int(data.get("numRuntimeErrorTestSuites") or 0)
    n_pass = int(data.get("numPassedTests") or 0)
    n_total = int(data.get("numTotalTests") or 0)
    if n_rt <= 0:
        return None
    msg = (
        f"{n_rt} suite(s) failed to load; Jest ran {n_pass}/{n_total} tests. "
        "Align Node with the project (often 18.x–20.x LTS for Nest/ts-jest)."
    )
    nv = _node_version_text()
    if nv:
        msg += f" Current: {nv}."
    return msg


def parse_jest_json_output_file(json_path: Path, project_root: Path | None = None) -> StageResult:
    if not json_path.is_file():
        return StageResult(error=f"Jest JSON file missing: {json_path}")

    try:
        data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        return StageResult(error=f"Invalid Jest JSON: {e}")

    passed: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []

    for test_file in data.get("testResults", []) or []:
        assertion_results = test_file.get("assertionResults") or []
        for assertion in assertion_results:
            full_name = assertion.get("fullName")
            if not full_name:
                ancestors = assertion.get("ancestorTitles", []) or []
                title = assertion.get("title", "")
                full_name = (
                    " ".join(ancestors + [title]) if ancestors else title
                )
            status = assertion.get("status", "")
            if status == "passed":
                passed.append(full_name)
            elif status == "failed":
                failed.append(full_name)
            elif status in ("pending", "skipped", "todo"):
                skipped.append(full_name)

        ar = test_file.get("assertionResults") or []
        if test_file.get("status") == "failed" and len(ar) == 0:
            raw_name = test_file.get("name") or ""
            name = raw_name
            if project_root and raw_name:
                pr = str(project_root.resolve()).rstrip("/") + "/"
                if raw_name.startswith(pr):
                    name = raw_name[len(pr) :]
            failed.append(f"{name}::(suite failed)" if name else "(suite failed)")

    warn = _jest_undercount_warning(data)
    return StageResult(
        passed=passed,
        failed=failed,
        skipped=skipped,
        warning=warn,
    )
