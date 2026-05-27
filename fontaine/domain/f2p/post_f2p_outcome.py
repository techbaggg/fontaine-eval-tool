"""
Post–F2P/P2P validation: a PR counts as a good candidate only when the three-stage
run completes and both at least one Fail→Pass and one Pass→Pass test is observed
(non-empty F2P and P2P test ID sets). Rejections line up with common ``empty_f2p`` /
``empty_p2p`` style labels when a bucket is empty.
"""

from __future__ import annotations

from typing import Literal

from fontaine.domain.pr_analysis import PrAnalysisLine


PostF2PBucket = Literal[
    "not_run",
    "runner_failed",
    "valid",
    "empty_f2p",
    "empty_p2p",
]


def post_f2p_bucket(line: PrAnalysisLine) -> PostF2PBucket:
    """
    Classify one PR line after optional TypeScript F2P enrichment.

    ``not_run``: never analyzed (e.g. beyond ``--pr-f2p-limit``).
    ``runner_failed``: clone/install/run error (``f2p_error`` set).
    ``valid``: at least one F2P and one P2P test ID.
    ``empty_f2p`` / ``empty_p2p``: analyzed but the corresponding bucket is empty.
    """
    if line.f2p_error:
        return "runner_failed"
    if line.f2p_count is None and line.p2p_count is None:
        return "not_run"
    fc = line.f2p_count or 0
    pc = line.p2p_count or 0
    if fc > 0 and pc > 0:
        return "valid"
    if fc == 0:
        return "empty_f2p"
    return "empty_p2p"


def is_validated_f2p_candidate(line: PrAnalysisLine) -> bool:
    """``True`` when the PR has both non-zero F2P and P2P counts and no runner error."""
    return post_f2p_bucket(line) == "valid"
