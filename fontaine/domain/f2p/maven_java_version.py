"""
Infer the Java language level a Maven build expects.

Primary signal: **effective POM** from ``mvn help:effective-pom`` (merged model; parents resolved
from repositories) — see :mod:`fontaine.domain.f2p.maven_effective_pom`.

Fallback: walk on-disk ``pom.xml`` files and ``<parent><relativePath>`` only.

Fontaine maps the major version to ``maven:3-eclipse-temurin-<N>`` Docker tags unless overridden.

If the **runtime** JVM must differ from the compiler release (e.g. Byte Buddy vs JDK 21), set
:envvar:`FONTAINE_F2P_MAVEN_JAVA_MAJOR` or :envvar:`FONTAINE_F2P_MAVEN_DOCKER_IMAGE`.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path


def _local_tag(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _local_tag(c.tag) == name:
            return c
    return None


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def _parse_java_major(text: str) -> int | None:
    """
    Map ``21``, ``21.0``, ``1.8``, ``8`` to a major Java version number (8 for Java 8).
    Returns ``None`` for unresolved Maven placeholders (``${...}``).
    """
    t = text.strip()
    if not t or "${" in t:
        return None
    if re.match(r"^1\.\d+$", t):
        try:
            return int(t.split(".", 1)[1])
        except ValueError:
            return None
    try:
        return int(float(t))
    except ValueError:
        return None


def _collect_properties(pom_root: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    props_el = _child(pom_root, "properties")
    if props_el is None:
        return out
    for el in props_el:
        name = _local_tag(el.tag)
        if not name or name.startswith("#"):
            continue
        val = _text(el)
        if val:
            out[name] = val
    return out


def _compiler_plugin_dict_from_plugin(plugin: ET.Element) -> dict[str, str]:
    aid = _child(plugin, "artifactId")
    if _text(aid) != "maven-compiler-plugin":
        return {}
    cfg = _child(plugin, "configuration")
    if cfg is None:
        return {}
    out: dict[str, str] = {}
    for key in ("release", "source", "target"):
        node = _child(cfg, key)
        v = _text(node)
        if v:
            out[key] = v
    return out


def _plugins_from_container(container: ET.Element | None) -> list[ET.Element]:
    if container is None:
        return []
    out: list[ET.Element] = []
    for plugin in container:
        if _local_tag(plugin.tag) == "plugin":
            out.append(plugin)
    return out


def _compiler_plugin_configuration(pom_root: ET.Element) -> dict[str, str]:
    """Merge ``maven-compiler-plugin`` ``release`` / ``source`` / ``target`` within one POM."""
    maps: list[dict[str, str]] = []
    build_el = _child(pom_root, "build")
    if build_el is not None:
        pman = _child(build_el, "pluginManagement")
        if pman is not None:
            plm = _child(pman, "plugins")
            if plm is not None:
                for plugin in _plugins_from_container(plm):
                    d = _compiler_plugin_dict_from_plugin(plugin)
                    if d:
                        maps.append(d)
        pl = _child(build_el, "plugins")
        if pl is not None:
            for plugin in _plugins_from_container(pl):
                d = _compiler_plugin_dict_from_plugin(plugin)
                if d:
                    maps.append(d)
    profiles_el = _child(pom_root, "profiles")
    if profiles_el is not None:
        for prof in profiles_el:
            if _local_tag(prof.tag) != "profile":
                continue
            b2 = _child(prof, "build")
            if b2 is None:
                continue
            plugins2 = _child(b2, "plugins")
            if plugins2 is None:
                continue
            for plugin in plugins2:
                if _local_tag(plugin.tag) != "plugin":
                    continue
                d = _compiler_plugin_dict_from_plugin(plugin)
                if d:
                    maps.append(d)
    return _merge_compiler_maps(maps)


def _merge_compiler_maps(maps: list[dict[str, str]]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for m in maps:
        merged.update(m)
    return merged


def _effective_java_major_from_maps(
    merged_props: dict[str, str],
    merged_compiler: dict[str, str],
) -> int | None:
    """Prefer compiler plugin config, then ``maven.compiler.*``, then ``java.version``."""
    if merged_compiler.get("release"):
        m = _parse_java_major(merged_compiler["release"])
        if m is not None:
            return m
    for key in ("target", "source"):
        if merged_compiler.get(key):
            m = _parse_java_major(merged_compiler[key])
            if m is not None:
                return m
    for prop_name in ("maven.compiler.release", "maven.compiler.target", "maven.compiler.source"):
        if merged_props.get(prop_name):
            m = _parse_java_major(merged_props[prop_name])
            if m is not None:
                return m
    if merged_props.get("java.version"):
        return _parse_java_major(merged_props["java.version"])
    return None


def iter_pom_chain(leaf_project_root: Path) -> list[Path]:
    """
    Ordered **leaf → parent → …** ``pom.xml`` paths by following ``<parent><relativePath>``.
    """
    leaf = (leaf_project_root / "pom.xml").resolve()
    if not leaf.is_file():
        return []

    chain: list[Path] = []
    seen: set[Path] = set()
    cur: Path | None = leaf

    for _ in range(12):
        if cur is None or cur in seen:
            break
        seen.add(cur)
        chain.append(cur)

        try:
            tree = ET.parse(cur)
            root_el = tree.getroot()
        except (ET.ParseError, OSError):
            break

        parent_el = _child(root_el, "parent")
        if parent_el is None:
            break

        rp_el = _child(parent_el, "relativePath")
        rp_raw = _text(rp_el)
        rp = rp_raw if rp_raw else "../pom.xml"

        parent_pom = (cur.parent / rp).resolve()
        if not parent_pom.is_file():
            break
        cur = parent_pom

    return chain


def infer_java_major_from_effective_pom_file(effective_pom_path: Path) -> int | None:
    """
    Parse one ``help:effective-pom`` output file (fully merged ``<project>`` tree).
    """
    try:
        tree = ET.parse(effective_pom_path)
        root_el = tree.getroot()
    except (ET.ParseError, OSError):
        return None
    props = _collect_properties(root_el)
    comp = _compiler_plugin_configuration(root_el)
    return _effective_java_major_from_maps(props, comp)


def infer_java_major_from_maven_project(project_root: Path) -> int | None:
    """
    Best-effort Java major version from on-disk POMs (child overrides parent for properties).

    Returns ``None`` if nothing usable was found (caller may fall back to a default image tag).
    """
    chain = iter_pom_chain(project_root)
    if not chain:
        return None

    merged_props: dict[str, str] = {}
    compiler_maps: list[dict[str, str]] = []

    # Root ancestor first, leaf last — later wins for duplicate keys in ``properties``.
    for pom_path in reversed(chain):
        try:
            tree = ET.parse(pom_path)
            root_el = tree.getroot()
        except (ET.ParseError, OSError):
            continue
        merged_props.update(_collect_properties(root_el))
        compiler_maps.append(_compiler_plugin_configuration(root_el))

    merged_compiler = _merge_compiler_maps(compiler_maps)
    return _effective_java_major_from_maps(merged_props, merged_compiler)
