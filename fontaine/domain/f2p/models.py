from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class StageResult:
    """Pass/fail/skip test IDs from one test run."""

    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    error: str | None = None
    exit_code: int | None = None
    """Non-fatal hint (e.g. Jest suites that failed to load — undercount vs CI)."""
    warning: str | None = None


@dataclass(slots=True)
class F2PRunOutcome:
    """Per-PR F2P/P2P classification after three-stage runs."""

    f2p_tests: list[str] = field(default_factory=list)
    p2p_tests: list[str] = field(default_factory=list)
    error: str | None = None
    diagnostic: str | None = None
    """Explains undercounts (e.g. Jest load failures); does not mean analysis failed."""
