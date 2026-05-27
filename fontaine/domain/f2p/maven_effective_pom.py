"""
Discover Java release level via ``mvn help:effective-pom``.

Unlike scanning local XML only, this invokes Maven so **parent POMs from repositories** participate
in the merged model (Spring Boot parent, BOM ``pluginManagement``, ``java.version``, etc.).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fontaine.domain.f2p.docker_maven import (
    EFFECTIVE_POM_BOOTSTRAP_IMAGE,
    docker_cli_available,
    docker_mvn_argv,
    ensure_maven_docker_image_ready,
    maven_docker_enabled,
)
from fontaine.domain.f2p.env_truthy import env_truthy
from fontaine.domain.f2p.maven_java_version import infer_java_major_from_effective_pom_file
from fontaine.run_log import progress

_EFFECTIVE_POM_FILENAME = ".fontaine-effective-pom.xml"


def skip_effective_pom_resolution() -> bool:
    """Skip ``mvn help:effective-pom`` (offline / speed); fall back to static POM scan only."""
    return env_truthy("FONTAINE_F2P_MAVEN_SKIP_EFFECTIVE_POM", default=False)


def try_java_major_from_effective_pom(
    maven_project_root: Path,
    repo_root: Path,
    *,
    local_mvn_cmd: list[str] | None,
    timeout_sec: int,
    bootstrap_image: str = EFFECTIVE_POM_BOOTSTRAP_IMAGE,
    pull_timeout_sec: int = 600,
) -> int | None:
    """
    Run ``mvn help:effective-pom`` on the host or in Docker and parse ``java.version`` / compiler
    settings from the merged output.

    ``bootstrap_image`` is the Docker image used when host Maven is unavailable (defaults to
    :data:`EFFECTIVE_POM_BOOTSTRAP_IMAGE`). Callers often pass a tag already inferred from static
    POM/env so the effective-POM step aligns with the image later used for ``mvn`` tests.

    Returns ``None`` if skipped, unsupported in this environment, or Maven failed.
    """
    if skip_effective_pom_resolution():
        return None

    rr = repo_root.resolve()
    out_host = rr / _EFFECTIVE_POM_FILENAME
    out_container = "/workspace/.fontaine-effective-pom.xml"

    budget = max(60, min(timeout_sec, 420))

    try:
        if out_host.exists():
            try:
                out_host.unlink()
            except OSError:
                pass

        if local_mvn_cmd:
            progress("[PR F2P] resolving Java version via host ``mvn help:effective-pom`` …")
            try:
                r = subprocess.run(
                    [
                        *local_mvn_cmd,
                        "-B",
                        "-ntp",
                        "help:effective-pom",
                        f"-Doutput={out_host}",
                    ],
                    cwd=maven_project_root,
                    capture_output=True,
                    text=True,
                    timeout=budget,
                    env={**os.environ, "CI": "true"},
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if r.returncode != 0 or not out_host.is_file() or out_host.stat().st_size == 0:
                return None
            return infer_java_major_from_effective_pom_file(out_host)

        if not docker_cli_available() or not maven_docker_enabled():
            return None

        pull_err = ensure_maven_docker_image_ready(
            image=bootstrap_image,
            pull_timeout=pull_timeout_sec,
        )
        if pull_err:
            return None

        progress(
            "[PR F2P] resolving Java version via ``mvn help:effective-pom`` in Docker (%s) …",
            bootstrap_image,
        )
        cmd = docker_mvn_argv(
            rr,
            maven_project_root,
            image=bootstrap_image,
            mvn_argv=[
                "-B",
                "-ntp",
                "help:effective-pom",
                f"-Doutput={out_container}",
            ],
        )
        try:
            r = subprocess.run(
                cmd,
                cwd=rr,
                capture_output=True,
                text=True,
                timeout=budget,
                env={**os.environ, "CI": "true"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0 or not out_host.is_file() or out_host.stat().st_size == 0:
            return None
        return infer_java_major_from_effective_pom_file(out_host)
    finally:
        try:
            if out_host.exists():
                out_host.unlink()
        except OSError:
            pass
