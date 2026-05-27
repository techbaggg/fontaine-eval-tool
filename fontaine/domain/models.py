from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Health(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass(slots=True)
class PullRequestSummary:
    number: int
    title: str
    merged_at: datetime | None
    base_ref: str
    head_ref: str
    is_merged_to_default: bool


@dataclass(slots=True)
class RepoRollup:
    owner: str
    name: str
    default_branch: str
    prs: list[PullRequestSummary] = field(default_factory=list)
    signals: dict[str, object] = field(default_factory=dict)
    health: Health = Health.GREEN
    notes: list[str] = field(default_factory=list)
