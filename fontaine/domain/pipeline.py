from __future__ import annotations

from abc import ABC, abstractmethod

from fontaine.config import ScreeningConfig
from fontaine.domain.models import RepoRollup
from fontaine.adapters.github import GitHubClient
from fontaine.reporting import JsonReporter


class Stage(ABC):
    name: str

    @abstractmethod
    def run(self, ctx: RepoRollup) -> None:
        ...


class ContessaPipeline:
    """Orchestrates stages; 'Contessa' = playful nod to the character."""

    def __init__(
        self,
        *,
        config: ScreeningConfig,
        github: GitHubClient,
        reporter: JsonReporter,
        stages: list[Stage] | None = None,
    ) -> None:
        self._config = config
        self._github = github
        self._reporter = reporter
        self._stages = stages or [
            InventoryStage(github),
            PullRequestSliceStage(github, config),
            HeuristicSignalsStage(),
        ]

    def run(self) -> RepoRollup:
        ctx = RepoRollup(owner=self._config.owner, name=self._config.repo, default_branch="")
        for stage in self._stages:
            stage.run(ctx)
        self._reporter.write(ctx)
        return ctx


class InventoryStage(Stage):
    name = "inventory"

    def __init__(self, github: GitHubClient) -> None:
        self._github = github

    def run(self, ctx: RepoRollup) -> None:
        meta = self._github.fetch_repo_meta(ctx.owner, ctx.name)
        ctx.default_branch = meta.default_branch
        ctx.signals["stars"] = meta.stars


class PullRequestSliceStage(Stage):
    name = "pr_slice"

    def __init__(self, github: GitHubClient, config: ScreeningConfig) -> None:
        self._github = github
        self._config = config

    def run(self, ctx: RepoRollup) -> None:
        prs = self._github.list_merged_prs(
            ctx.owner,
            ctx.name,
            default_branch=ctx.default_branch,
            only_default_base=self._config.merged_to_default_only,
            merge_not_before=self._config.merge_not_before,
        )
        ctx.prs = prs


class HeuristicSignalsStage(Stage):
    name = "heuristics"

    def run(self, ctx: RepoRollup) -> None:
        # Stub: plug deterministic checks here (tests dir, CI files, etc.)
        ctx.signals["merged_pr_count"] = len(ctx.prs)
