from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from fontaine.adapters import github_rest
from fontaine.domain.models import PullRequestSummary


@dataclass(frozen=True, slots=True)
class RepoMeta:
    default_branch: str
    stars: int


class GitHubClient:
    """Thin facade over :mod:`fontaine.adapters.github_rest` for pipeline-style callers."""

    def __init__(self, token: str) -> None:
        self._token = token

    @classmethod
    def from_env(cls) -> GitHubClient:
        return cls(github_rest.resolve_github_token())

    def fetch_repo_meta(self, owner: str, name: str) -> RepoMeta:
        body = github_rest.fetch_repository(self._token, owner, name)
        return RepoMeta(
            default_branch=str(body.get("default_branch") or "main"),
            stars=int(body.get("stargazers_count") or 0),
        )

    def list_merged_prs(
        self,
        owner: str,
        name: str,
        *,
        default_branch: str,
        only_default_base: bool,
        merge_not_before: datetime | None,
    ) -> list[PullRequestSummary]:
        return github_rest.list_merged_pull_summaries(
            self._token,
            owner,
            name,
            default_branch=default_branch,
            only_default_base=only_default_base,
            merge_not_before=merge_not_before,
        )
