"""Process-wide counter for outbound HTTP requests to the GitHub API (REST + GraphQL)."""

from __future__ import annotations

_count = 0


def reset_github_http_request_count() -> None:
    global _count
    _count = 0


def record_github_http_request(n: int = 1) -> None:
    global _count
    _count += n


def github_http_request_count() -> int:
    return _count
