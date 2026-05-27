"""
Docker images for running Maven + JDK when the host has Docker but no local ``mvn``/JDK.

**Image selection** — :func:`resolve_maven_docker_image`:

1. :envvar:`FONTAINE_F2P_MAVEN_DOCKER_IMAGE` (full name).
2. Else :envvar:`FONTAINE_F2P_MAVEN_JAVA_MAJOR` → ``maven:3-eclipse-temurin-<N>``.
3. Else Java major from ``mvn help:effective-pom`` when run (see :mod:`fontaine.domain.f2p.maven_effective_pom`).
4. Else static scan of on-disk POMs (:mod:`fontaine.domain.f2p.maven_java_version`).
5. Else ``maven:3-eclipse-temurin-21``.

Set :envvar:`FONTAINE_F2P_MAVEN_SKIP_EFFECTIVE_POM=1` to skip the Maven network step and use only
local POM text.

Tags follow `Docker Library maven <https://hub.docker.com/_/maven>`_ (multi-arch).

``MAVEN_OPTS`` / ``FONTAINE_F2P_MAVEN_OPTS`` are forwarded into the container (e.g.\
``-Dnet.bytebuddy.experimental=true`` for Byte Buddy on newer JDKs).

Fontaine only **orchestrates** pull/run; ``docker`` must be on ``PATH``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fontaine.domain.f2p.env_truthy import env_truthy
from fontaine.domain.f2p.maven_java_version import infer_java_major_from_maven_project
from fontaine.run_log import progress

_DOCKER_IMAGE_PREFIX = "maven:3-eclipse-temurin"
_FALLBACK_JAVA_MAJOR = 21

# Default Docker image for ``mvn help:effective-pom`` when host Maven is missing; callers may pass
# another ``maven:3-eclipse-temurin-<N>`` as ``bootstrap_image`` to align with the test-run image.
EFFECTIVE_POM_BOOTSTRAP_IMAGE = f"{_DOCKER_IMAGE_PREFIX}-{_FALLBACK_JAVA_MAJOR}"

_pulled_or_verified: set[str] = set()


def docker_cli_available() -> bool:
    return bool(_which_docker())


def _which_docker() -> str | None:
    import shutil

    return shutil.which("docker")


def resolve_maven_docker_image(
    project_root: Path,
    *,
    discovered_java_major: int | None = None,
) -> str:
    """
    Resolve ``maven:3-eclipse-temurin-<N>`` using env, optional **discovered** major (from
    ``help:effective-pom``), then static on-disk POM scan.

    Call with the same ``Path`` as :func:`fontaine.domain.f2p.maven_runner.find_maven_project_root`.
    """
    explicit = (os.environ.get("FONTAINE_F2P_MAVEN_DOCKER_IMAGE") or "").strip()
    if explicit:
        return explicit
    env_major = (os.environ.get("FONTAINE_F2P_MAVEN_JAVA_MAJOR") or "").strip()
    if env_major.isdigit():
        return f"{_DOCKER_IMAGE_PREFIX}-{env_major}"
    major = discovered_java_major
    if major is None:
        major = infer_java_major_from_maven_project(project_root)
    if major is not None:
        return f"{_DOCKER_IMAGE_PREFIX}-{major}"
    return f"{_DOCKER_IMAGE_PREFIX}-{_FALLBACK_JAVA_MAJOR}"


def maven_docker_enabled() -> bool:
    """
    Whether Fontaine may use Docker to run ``mvn`` when the host has no ``mvn``/``mvnw``.

    **Default: True** — if ``docker`` is on ``PATH`` and the JDK/Maven image is missing, Fontaine
    runs ``docker pull`` then ``docker run … mvn``.

    Set :envvar:`FONTAINE_F2P_MAVEN_NO_DOCKER` to opt out, or set
    :envvar:`FONTAINE_F2P_MAVEN_USE_DOCKER` to ``0``/``false`` (legacy) to disable the Docker path.
    """
    if env_truthy("FONTAINE_F2P_MAVEN_NO_DOCKER", default=False):
        return False
    raw = os.environ.get("FONTAINE_F2P_MAVEN_USE_DOCKER")
    if raw is not None and raw.strip().lower() in ("0", "false", "no", "off"):
        return False
    return True


def force_docker_pull_maven_image() -> bool:
    """If true, always ``docker pull`` the Maven image (before runs), subject to cache below."""
    return env_truthy("FONTAINE_F2P_DOCKER_PULL_MAVEN", default=False)


def _docker_image_present_locally(image: str) -> bool:
    exe = _which_docker()
    if not exe:
        return False
    try:
        r = subprocess.run(
            [exe, "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=45,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_maven_docker_image_ready(
    *,
    image: str | None = None,
    pull_timeout: int = 600,
) -> str | None:
    """
    Pull the Maven/JDK image when **needed** (missing locally) or when
    :func:`force_docker_pull_maven_image` is true.

    Returns an error string on failure, or ``None`` on success / nothing to do.
    """
    img = (image or "").strip() or f"{_DOCKER_IMAGE_PREFIX}-{_FALLBACK_JAVA_MAJOR}"
    if img in _pulled_or_verified and not force_docker_pull_maven_image():
        return None

    exe = _which_docker()
    if not exe:
        if maven_docker_enabled() or force_docker_pull_maven_image():
            return "Docker CLI not found on PATH (install Docker or add `docker` to PATH)"
        return None

    need_pull = force_docker_pull_maven_image() or not _docker_image_present_locally(img)
    if not need_pull:
        _pulled_or_verified.add(img)
        return None

    progress("[PR F2P] docker pull %s (JDK + Maven toolkit) …", img)
    try:
        r = subprocess.run(
            [exe, "pull", img],
            capture_output=True,
            text=True,
            timeout=max(120, pull_timeout),
            env={**os.environ},
        )
    except subprocess.TimeoutExpired:
        return f"docker pull {img} timed out after {pull_timeout}s"
    except OSError as e:
        return f"docker pull failed: {e}"

    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip()[-1800:]
        return f"docker pull {img} failed (exit {r.returncode}): {tail}"

    _pulled_or_verified.add(img)
    return None


def docker_mvn_argv(
    repo_root: Path,
    maven_project_root: Path,
    *,
    image: str,
    mvn_argv: list[str],
) -> list[str]:
    """``docker run … mvn <mvn_argv…>`` with the repo mounted read-write at ``/workspace``."""
    rr = repo_root.resolve()
    pr = maven_project_root.resolve()
    mount = "/workspace"
    exe = _which_docker() or "docker"
    try:
        rel = pr.relative_to(rr)
        workdir = f"{mount}/{rel.as_posix()}" if rel.parts else mount
        volume = f"{str(rr)}:{mount}"
    except ValueError:
        volume = f"{str(pr)}:{mount}"
        workdir = mount

    argv = [
        exe,
        "run",
        "--rm",
        "-v",
        volume,
        "-w",
        workdir,
        "-e",
        "CI=true",
    ]
    mopts = (os.environ.get("FONTAINE_F2P_MAVEN_OPTS") or os.environ.get("MAVEN_OPTS") or "").strip()
    if mopts:
        argv.extend(["-e", f"MAVEN_OPTS={mopts}"])
    argv.extend([image, "mvn", *mvn_argv])
    return argv


def docker_maven_argv(
    repo_root: Path,
    maven_project_root: Path,
    *,
    image: str,
    goals: str,
    extra_tokens: list[str],
) -> list[str]:
    """``docker run … mvn -B <goals…>`` with the repo mounted read-write at ``/workspace``."""
    goal_tokens = [t for t in goals.strip().split() if t] or ["test"]
    return docker_mvn_argv(
        repo_root,
        maven_project_root,
        image=image,
        mvn_argv=["-B", *goal_tokens, *extra_tokens],
    )
