"""Shared JUnit XML parsing for pytest and Maven Surefire/Failsafe reports."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from fontaine.domain.f2p.models import StageResult


def parse_junit_xml(path: Path, *, project_root: Path) -> StageResult:
    """Parse a single JUnit XML file (pytest ``--junitxml`` or Maven Surefire/Failsafe output)."""
    _ = project_root  # API parity with pytest call sites (path display).
    if not path.is_file():
        return StageResult(error=f"JUnit XML missing: {path}")
    try:
        blob = path.read_bytes()
    except OSError as e:
        return StageResult(error=f"JUnit XML unreadable: {e}")
    if not blob.strip():
        return StageResult(
            error=(
                "JUnit XML file is empty — test runner exited before JUnit XML was written "
                "(collection/import failure, config error, or crash)."
            ),
        )
    try:
        tree = ET.ElementTree(ET.fromstring(blob))
        root_el = tree.getroot()
    except ET.ParseError as e:
        return StageResult(error=f"Invalid JUnit XML: {e}")

    passed: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []

    root_tag = root_el.tag.split("}")[-1]
    suites: list[ET.Element] = []
    if root_tag == "testsuites":
        suites.extend(root_el.findall(".//testsuite"))
        if not suites:
            suites = [root_el]
    elif root_tag == "testsuite":
        suites = [root_el]
    else:
        suites.extend(root_el.findall(".//testsuite"))

    for suite in suites:
        for case in suite.findall("testcase"):
            cls = (case.attrib.get("classname") or "").strip()
            name = (case.attrib.get("name") or "").strip()
            file_a = (case.attrib.get("file") or "").strip()
            if cls and name:
                tid = f"{cls}::{name}"
            elif file_a and name:
                tid = f"{file_a}::{name}"
            elif name:
                tid = name
            else:
                continue

            if case.find("skipped") is not None:
                skipped.append(tid)
            elif case.find("failure") is not None or case.find("error") is not None:
                failed.append(tid)
            else:
                passed.append(tid)

    return StageResult(passed=passed, failed=failed, skipped=skipped)
