from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ScreeningConfig:
    owner: str
    repo: str
    merged_to_default_only: bool = True
    # v1.0 optional: ignore merges before this (UTC)
    merge_not_before: datetime | None = None
