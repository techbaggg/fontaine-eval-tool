"""Shared helpers for parsing boolean environment variables."""

from __future__ import annotations

import os


def env_truthy(name: str, *, default: bool = False) -> bool:
    """
    Interpret ``name`` as a boolean env var.

    Explicit false: ``0``, ``false``, ``no``, ``off``. Explicit true: ``1``, ``true``, ``yes``, ``on``.
    Unset, empty, whitespace-only, or any other value returns ``default`` (``""`` is not false).
    """
    v = (os.environ.get(name) or "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    if v in ("1", "true", "yes", "on"):
        return True
    return default
