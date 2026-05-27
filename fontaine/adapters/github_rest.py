"""
GitHub REST + GraphQL helpers for Fontaine.

Uses a personal access token (GITHUB_TOKEN or GH_TOKEN). Tokens are never logged.
Uses common GitHub REST + GraphQL patterns; GraphQL queries are intentionally minimal.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from fontaine.adapters.github_http_counter import record_github_http_request
from fontaine.domain.models import PullRequestSummary

GITHUB_API_BASE = "https://api.github.com"
GRAPHQL_URL = f"{GITHUB_API_BASE}/graphql"

DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GitHubApiError(RuntimeError):
    """Raised when the GitHub API returns an error response."""


_MAX_GRAPHQL_ATTEMPTS = 6
_GRAPHQL_BASE_DELAY_S = 1.5
# Gateway timeouts / overload — GitHub occasionally returns HTML error pages (504 Unicorn).
_TRANSIENT_HTTP_STATUS = frozenset({500, 502, 503, 504})


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _post_graphql(
    token: str,
    *,
    query: str,
    variables: dict[str, Any],
) -> httpx.Response:
    """
    POST to ``/graphql`` with retries on rate limits, gateway timeouts, and brief outages.

    Records one HTTP counter increment per request attempt (including retries).
    """
    headers = {
        **DEFAULT_HEADERS,
        "Authorization": f"Bearer {token}",
        "User-Agent": "fontaine-repo-screening",
    }
    payload = {"query": query, "variables": variables}
    delay = _GRAPHQL_BASE_DELAY_S
    last_exc: BaseException | None = None

    for attempt in range(_MAX_GRAPHQL_ATTEMPTS):
        try:
            with httpx.Client(timeout=120.0, headers=headers) as c:
                r = c.post(GRAPHQL_URL, json=payload)
        except (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.RemoteProtocolError,
        ) as e:
            last_exc = e
            if attempt >= _MAX_GRAPHQL_ATTEMPTS - 1:
                raise GitHubApiError(
                    f"GitHub GraphQL network error after {_MAX_GRAPHQL_ATTEMPTS} attempts: {e}"
                ) from e
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
            continue

        record_github_http_request()

        if r.status_code == 200:
            return r

        if r.status_code == 429:
            if attempt < _MAX_GRAPHQL_ATTEMPTS - 1:
                wait = _retry_after_seconds(r) or delay
                time.sleep(min(max(wait, 1.0), 120.0))
                delay = min(delay * 2, 60.0)
                continue
            return r

        if r.status_code in _TRANSIENT_HTTP_STATUS:
            if attempt < _MAX_GRAPHQL_ATTEMPTS - 1:
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            return r

        return r

    if last_exc:
        raise GitHubApiError(f"GitHub GraphQL failed: {last_exc}") from last_exc
    raise GitHubApiError("GitHub GraphQL failed after retries")


def resolve_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token or not token.strip():
        raise GitHubApiError(
            "GitHub token required for remote mode. Set GITHUB_TOKEN or GH_TOKEN "
            "(repo scope is sufficient for private repositories you can access)."
        )
    return token.strip()


def _client(token: str) -> httpx.Client:
    headers = {
        **DEFAULT_HEADERS,
        "Authorization": f"Bearer {token}",
        "User-Agent": "fontaine-repo-screening",
    }
    return httpx.Client(base_url=GITHUB_API_BASE, headers=headers, timeout=120.0)


def fetch_repository(token: str, owner: str, repo: str) -> dict[str, Any]:
    """GET /repos/{owner}/{repo}"""
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}")
        record_github_http_request()
        if r.status_code == 404:
            raise GitHubApiError(f"Repository not found: {owner}/{repo}")
        if r.status_code != 200:
            raise GitHubApiError(f"GitHub API error {r.status_code}: {r.text[:500]}")
        return r.json()


def fetch_repository_license_spdx(token: str, owner: str, repo: str) -> str | None:
    """
    GET /repos/{owner}/{repo}/license — detected SPDX id when GitHub has indexed a license.

    Returns ``None`` on 404 or missing license payload.
    """
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/license")
        record_github_http_request()
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    lic = data.get("license") or {}
    spdx = lic.get("spdx_id")
    if isinstance(spdx, str) and spdx.strip():
        return spdx.strip()
    key = lic.get("key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    return None


def fetch_languages(token: str, owner: str, repo: str) -> dict[str, int]:
    """GET /repos/{owner}/{repo}/languages — bytes per language."""
    with _client(token) as c:
        r = c.get(f"/repos/{owner}/{repo}/languages")
        record_github_http_request()
        if r.status_code != 200:
            raise GitHubApiError(f"Languages API error {r.status_code}: {r.text[:300]}")
        data = r.json()
        return {str(k): int(v) for k, v in data.items()}


def count_merged_pull_requests(
    token: str,
    owner: str,
    repo: str,
    *,
    base_branch: str | None,
) -> int:
    """
    Count merged PRs using Search API (aligned with GitHub UI `is:merged` + optional `base:`).

    ``base_branch`` — if set, append ``base:<branch>`` so we only count PRs merged into that branch.
    """
    repo_qual = f"repo:{owner}/{repo}"
    parts = [repo_qual, "is:pr", "is:merged"]
    if base_branch:
        parts.append(f"base:{base_branch}")
    q = " ".join(parts)
    with _client(token) as c:
        r = c.get("/search/issues", params={"q": q, "per_page": 1})
        record_github_http_request()
        if r.status_code != 200:
            raise GitHubApiError(f"Search API error {r.status_code}: {r.text[:500]}")
        payload = r.json()
        return int(payload.get("total_count", 0))


def count_all_pull_requests(token: str, owner: str, repo: str) -> int:
    """
    Count every pull request in the repository (open, closed, merged) via Search API.

    Query shape: ``repo:owner/name is:pr`` — repo-level totals, independent of PR-analysis lookback.
    """
    q = f"repo:{owner}/{repo} is:pr"
    with _client(token) as c:
        r = c.get("/search/issues", params={"q": q, "per_page": 1})
        record_github_http_request()
        if r.status_code != 200:
            raise GitHubApiError(f"Search API error {r.status_code}: {r.text[:500]}")
        payload = r.json()
        return int(payload.get("total_count", 0))


def count_merged_pull_requests_with_search_qualifiers(
    token: str,
    owner: str,
    repo: str,
    *,
    base_branch: str | None,
    extra_qualifiers: list[str],
) -> int:
    """
    ``total_count`` for merged PRs matching ``repo:… is:pr is:merged`` plus optional
    ``base:`` and extra tokens. Use **one** ``merged:`` term per search (e.g. a single
    ``merged:date..date`` range); multiple ``merged:`` qualifiers are not reliably ANDed.
    """
    repo_qual = f"repo:{owner}/{repo}"
    parts: list[str] = [repo_qual, "is:pr", "is:merged"]
    if base_branch:
        parts.append(f"base:{base_branch}")
    parts.extend(extra_qualifiers)
    q = " ".join(parts)
    with _client(token) as c:
        r = c.get("/search/issues", params={"q": q, "per_page": 1})
        record_github_http_request()
        if r.status_code != 200:
            raise GitHubApiError(f"Search API error {r.status_code}: {r.text[:500]}")
        payload = r.json()
        return int(payload.get("total_count", 0))


def fetch_merged_pr_year_matrix_counts(
    token: str,
    owner: str,
    repo: str,
    *,
    base_branch: str | None,
) -> tuple[list[int], int]:
    """
    Sixteen Search totals for a 4×4 year matrix (see reporting).

    GitHub issue search applies **at most one** ``merged:`` predicate reliably; do **not** use
    two tokens (e.g. ``merged:>=… merged:<…``) or each query can degenerate to the same count.

    Bucket 0: ``merged:<Y`` where ``Y`` is Jan 1 of the first single-year column (merges before
    that instant). Buckets 1–15: one **inclusive range** per calendar year,
    ``merged:YYYY-01-01..YYYY-12-31``.

    ``end_year`` is the current calendar year in UTC.
    """
    end_year = datetime.now(timezone.utc).year
    y_first_single = end_year - 14
    counts: list[int] = []

    counts.append(
        count_merged_pull_requests_with_search_qualifiers(
            token,
            owner,
            repo,
            base_branch=base_branch,
            extra_qualifiers=[f"merged:<{y_first_single}-01-01"],
        )
    )
    for y in range(y_first_single, end_year + 1):
        counts.append(
            count_merged_pull_requests_with_search_qualifiers(
                token,
                owner,
                repo,
                base_branch=base_branch,
                extra_qualifiers=[f"merged:{y}-01-01..{y}-12-31"],
            )
        )

    if len(counts) != 16:
        raise GitHubApiError(f"internal: expected 16 year buckets, got {len(counts)}")
    return counts, end_year


def fetch_repo_facts_remote(
    token: str,
    owner: str,
    repo: str,
    *,
    merged_to_default_only: bool,
) -> dict[str, Any]:
    """Return a plain dict suitable for building :class:`fontaine.domain.repo_facts.RepoFacts`.

    Merged PR counts use Search API (``/search/issues``): ``is:pr is:merged``. We always fetch
    totals merged into the default branch (``base:<default>``) and merged into **any** branch
    (no ``base:``). ``merged_pr_count`` follows ``merged_to_default_only``. All-PR count uses
    ``is:pr`` only (all states). Totals are unrelated to PR-analysis lookback.
    """
    body = fetch_repository(token, owner, repo)
    default_branch = body.get("default_branch") or "main"
    langs = fetch_languages(token, owner, repo)
    merged_into_default = count_merged_pull_requests(
        token, owner, repo, base_branch=default_branch
    )
    merged_any_branch = count_merged_pull_requests(
        token, owner, repo, base_branch=None
    )
    merged_primary = (
        merged_into_default if merged_to_default_only else merged_any_branch
    )
    all_pr_count = count_all_pull_requests(token, owner, repo)
    total_bytes = sum(langs.values()) if langs else 0
    return {
        "repo_name": body.get("name") or repo,
        "default_branch": default_branch,
        "merged_pr_count": merged_primary,
        "merged_pr_count_any_branch": merged_any_branch,
        "all_pr_count": all_pr_count,
        "language_bytes": langs,
        "language_total_bytes": total_bytes,
        "stars": int(body.get("stargazers_count") or 0),
        "repository_disk_size_kb": int(body.get("size") or 0),
        "fork": bool(body.get("fork")),
        "pushed_at": body.get("pushed_at"),
        "open_issues_count": int(body.get("open_issues_count") or 0),
    }


# GitHub ``PullRequestOrderField`` has no MERGED_AT; UPDATED_AT is the usual proxy for recency.
_MERGED_PRS_QUERY = """
query ($owner: String!, $name: String!, $cursor: String, $pageSize: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      states: MERGED
      first: $pageSize
      after: $cursor
      orderBy: { field: UPDATED_AT, direction: DESC }
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        number
        title
        mergedAt
        baseRefName
        headRefName
      }
    }
  }
}
"""

_SINGLE_PR_ANALYSIS_QUERY = """
query ($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      title
      body
      merged
      mergedAt
      baseRefOid
      headRefOid
      baseRefName
      headRefName
      author {
        login
        __typename
      }
      files(first: 100) {
        nodes {
          path
          additions
          deletions
        }
      }
    }
  }
}
"""

_PR_ANALYSIS_NODES = """
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        number
        title
        body
        mergedAt
        baseRefOid
        headRefOid
        baseRefName
        headRefName
        author {
          login
          __typename
        }
        files(first: 100) {
          nodes {
            path
            additions
            deletions
          }
        }
      }
"""

# Order enums must be literal in the document: passing them as variables can trigger
# GitHub validation bugs (PullRequestOrderField vs IssueOrderField mismatch).
#
# No ``states:`` filter — default connection lists all PRs (open + closed). Explicit
# ``[OPEN, CLOSED]`` changed totals/pagination vs GitHub.
_PRS_ANALYSIS_QUERY_DEFAULT_LISTING = (
    """
query ($owner: String!, $name: String!, $cursor: String, $pageSize: Int!) {
  repository(owner: $owner, name: $name) {
    primaryLanguage { name }
    pullRequests(
      first: $pageSize
      after: $cursor
      orderBy: { field: CREATED_AT, direction: DESC }
    ) {
"""
    + _PR_ANALYSIS_NODES
    + """
    }
  }
}
"""
)

_PRS_ANALYSIS_QUERY_MERGED_ONLY = (
    """
query ($owner: String!, $name: String!, $cursor: String, $pageSize: Int!) {
  repository(owner: $owner, name: $name) {
    primaryLanguage { name }
    pullRequests(
      states: MERGED
      first: $pageSize
      after: $cursor
      orderBy: { field: UPDATED_AT, direction: DESC }
    ) {
"""
    + _PR_ANALYSIS_NODES
    + """
    }
  }
}
"""
)


def fetch_prs_analysis_page(
    token: str,
    owner: str,
    repo: str,
    *,
    cursor: str | None,
    page_size: int,
    merged_only: bool,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """
    One page of PRs for screening.

    * ``merged_only=True``: merged PRs only (``states: MERGED``), ``UPDATED_AT`` descending.
    * ``merged_only=False``: **default PR connection** (no ``states`` filter), ``CREATED_AT``
      descending (bulk listing).

    Returns ``(rows, primary_language_name, next_cursor)``.
    """
    variables: dict[str, Any] = {
        "owner": owner,
        "name": repo,
        "cursor": cursor,
        "pageSize": page_size,
    }
    query_text = _PRS_ANALYSIS_QUERY_MERGED_ONLY if merged_only else _PRS_ANALYSIS_QUERY_DEFAULT_LISTING

    r = _post_graphql(token, query=query_text, variables=variables)
    if r.status_code != 200:
        raise GitHubApiError(f"GraphQL error {r.status_code}: {r.text[:500]}")
    data = r.json()
    if "errors" in data:
        msgs = "; ".join(str(e.get("message", e)) for e in data.get("errors", []))
        raise GitHubApiError(f"GraphQL errors: {msgs}")

    repo_data = data.get("data", {}).get("repository") or {}
    prim = repo_data.get("primaryLanguage") or {}
    primary_language = prim.get("name")
    primary_language_str = str(primary_language) if primary_language else None

    conn = repo_data.get("pullRequests") or {}
    nodes = conn.get("nodes") or []
    page_info = conn.get("pageInfo") or {}
    # Only stop when GitHub explicitly says hasNextPage=false. Using ``not .get()``
    # treats a missing key like false and was truncating pagination (~one page / 25 rows).
    has_next = page_info.get("hasNextPage")
    end_cursor = page_info.get("endCursor")
    if has_next is True:
        next_cursor: str | None = end_cursor if end_cursor else None
    elif has_next is False:
        next_cursor = None
    else:
        # Ambiguous (missing hasNextPage): keep going only with a cursor and a full page.
        next_cursor = (
            end_cursor
            if end_cursor and len(nodes) >= page_size
            else None
        )

    rows: list[dict[str, Any]] = []
    for node in nodes:
        files_wrap = node.get("files") or {}
        file_nodes = files_wrap.get("nodes") or []
        rows.append(
            {
                "number": int(node["number"]),
                "title": str(node.get("title") or ""),
                "body": str(node.get("body") or ""),
                "mergedAt": node.get("mergedAt"),
                "baseRefOid": str(node.get("baseRefOid") or ""),
                "headRefOid": str(node.get("headRefOid") or ""),
                "baseRefName": str(node.get("baseRefName") or ""),
                "headRefName": str(node.get("headRefName") or ""),
                "author": node.get("author") or {},
                "files_nodes": [
                    {
                        "path": str(x.get("path") or ""),
                        "additions": int(x.get("additions") or 0),
                        "deletions": int(x.get("deletions") or 0),
                    }
                    for x in file_nodes
                    if x.get("path")
                ],
            }
        )
    return rows, primary_language_str, next_cursor


def fetch_merged_prs_analysis_page(
    token: str,
    owner: str,
    repo: str,
    *,
    cursor: str | None,
    page_size: int,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Backward-compatible alias for merged-only screening pages."""
    return fetch_prs_analysis_page(
        token,
        owner,
        repo,
        cursor=cursor,
        page_size=page_size,
        merged_only=True,
    )


def fetch_pull_request_patch(
    token: str,
    owner: str,
    repo: str,
    *,
    base_sha: str,
    head_sha: str,
) -> str | None:
    """
    Raw unified diff from ``GET /repos/{owner}/{repo}/compare/{base}...{head}``.

    Returns ``None`` on HTTP error or empty body.
    """
    if not base_sha or not head_sha:
        return None
    compare = f"{base_sha}...{head_sha}"
    headers = {
        **DEFAULT_HEADERS,
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.diff",
        "User-Agent": "fontaine-repo-screening",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/compare/{compare}"
    try:
        with httpx.Client(timeout=120.0, headers=headers) as c:
            r = c.get(url)
        record_github_http_request()
        if r.status_code != 200:
            return None
        text = r.text or ""
        return text if text.strip() else None
    except httpx.HTTPError:
        return None


def fetch_merged_pull_request_row(
    token: str,
    owner: str,
    repo: str,
    *,
    number: int,
) -> dict[str, Any]:
    """
    One merged PR by number, same row shape as :func:`fetch_merged_prs_analysis_page`.

    Raises :class:`GitHubApiError` if the PR does not exist, is not merged, or GraphQL fails.
    """
    variables: dict[str, Any] = {
        "owner": owner,
        "name": repo,
        "number": int(number),
    }
    r = _post_graphql(token, query=_SINGLE_PR_ANALYSIS_QUERY, variables=variables)
    if r.status_code != 200:
        raise GitHubApiError(f"GraphQL error {r.status_code}: {r.text[:500]}")
    data = r.json()
    if "errors" in data:
        msgs = "; ".join(str(e.get("message", e)) for e in data.get("errors", []))
        raise GitHubApiError(f"GraphQL errors: {msgs}")

    repo_data = data.get("data", {}).get("repository") or {}
    node = repo_data.get("pullRequest")
    if not node:
        raise GitHubApiError(f"Pull request #{number} not found in {owner}/{repo}")
    if not node.get("merged"):
        raise GitHubApiError(
            f"Pull request #{number} is not merged — F2P/P2P target requires a merged PR"
        )

    files_wrap = node.get("files") or {}
    file_nodes = files_wrap.get("nodes") or []
    return {
        "number": int(node["number"]),
        "title": str(node.get("title") or ""),
        "body": str(node.get("body") or ""),
        "mergedAt": node.get("mergedAt"),
        "baseRefOid": str(node.get("baseRefOid") or ""),
        "headRefOid": str(node.get("headRefOid") or ""),
        "baseRefName": str(node.get("baseRefName") or ""),
        "headRefName": str(node.get("headRefName") or ""),
        "author": node.get("author") or {},
        "files_nodes": [
            {
                "path": str(x.get("path") or ""),
                "additions": int(x.get("additions") or 0),
                "deletions": int(x.get("deletions") or 0),
            }
            for x in file_nodes
            if x.get("path")
        ],
    }


def list_merged_pull_summaries(
    token: str,
    owner: str,
    repo: str,
    *,
    default_branch: str,
    only_default_base: bool,
    merge_not_before: datetime | None,
    page_size: int = 50,
    max_pages: int = 20,
) -> list[PullRequestSummary]:
    """
    GraphQL pagination of merged PRs — for pipeline / future screening stages.
    Narrow field selection; no per-file bodies or linked issue bodies in this helper.
    """
    out: list[PullRequestSummary] = []
    cursor: str | None = None
    for _ in range(max_pages):
        variables: dict[str, Any] = {
            "owner": owner,
            "name": repo,
            "cursor": cursor,
            "pageSize": page_size,
        }
        r = _post_graphql(token, query=_MERGED_PRS_QUERY, variables=variables)
        if r.status_code != 200:
            raise GitHubApiError(f"GraphQL error {r.status_code}: {r.text[:500]}")
        data = r.json()
        if "errors" in data:
            msgs = "; ".join(
                str(e.get("message", e)) for e in data.get("errors", [])
            )
            raise GitHubApiError(f"GraphQL errors: {msgs}")
        repo_data = data.get("data", {}).get("repository")
        if not repo_data:
            break
        conn = repo_data.get("pullRequests") or {}
        nodes = conn.get("nodes") or []
        page_info = conn.get("pageInfo") or {}
        for node in nodes:
            base_ref = node.get("baseRefName") or ""
            if only_default_base and base_ref != default_branch:
                continue
            merged_raw = node.get("mergedAt")
            merged_at: datetime | None = None
            if merged_raw:
                merged_at = datetime.fromisoformat(
                    merged_raw.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            if merge_not_before is not None and merged_at is not None:
                if merged_at < merge_not_before.astimezone(timezone.utc):
                    continue
            out.append(
                PullRequestSummary(
                    number=int(node["number"]),
                    title=str(node.get("title") or ""),
                    merged_at=merged_at,
                    base_ref=base_ref,
                    head_ref=str(node.get("headRefName") or ""),
                    is_merged_to_default=(base_ref == default_branch),
                )
            )
        if page_info.get("hasNextPage") is False:
            break
        nxt = page_info.get("endCursor")
        if not nxt:
            break
        cursor = nxt
        if not nodes:
            break
    return out
