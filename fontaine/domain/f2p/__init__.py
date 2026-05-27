"""Fail→Pass / Pass→Pass test analysis (Fontaine-native; Jest/Vitest/Mocha, pytest, or Maven, auto-selected)."""

from fontaine.domain.f2p.batch import enrich_report_with_ts_f2p
from fontaine.domain.f2p.orchestrator import analyze_pr_f2p, analyze_pr_f2p_typescript

__all__ = [
    "analyze_pr_f2p",
    "analyze_pr_f2p_typescript",
    "enrich_report_with_ts_f2p",
]
