# Changelog

All notable changes to **Fontaine** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Work before **0.1.0** was not recorded here. This release is a **summary of the product as it already exists** at the first versioned checkpoint—not a commit-by-commit history.

## [Unreleased]

### Added

### Changed

### Fixed

### Removed

---

## [0.1.0] - 2026-05-13

### Added

- **GitHub + local git workflow** — Uses GitHub REST and GraphQL together with a **local clone** of the analyzed repository (`--repo-dir`) for metrics, PR workflows, and F2P checkouts.
- **Repository acquisition summary** — Stars, languages, default branch, merged PR counts, lines of code, and related inventory signals (files, tests, CI, issues, recent commits, heuristic licensing hints — *not legal advice*).
- **PR analysis (`--pr-analysis`)** — GraphQL PR listing with configurable lookback, ordering, and merged-only options; first-stage screening using file/test heuristics and PR metadata as documented in `DOCS.md`.
- **F2P / P2P test classification (`--pr-f2p`)** — For screened PRs, checks out base and head and runs the project’s test workflow to separate fail-to-pass vs pass-to-pass signals.
- **Supported test stacks** — **JS/TS:** Vitest, Jest, or Mocha with **npm**, **pnpm**, or **yarn**. **Python:** **pytest**-oriented layouts. **Java:** **Maven** with Surefire/Failsafe JUnit XML, including optional **Docker**-based Maven when the host has no Maven.
- **Monorepos** — Backend selection from layout and changed paths; optional **`--pr-f2p-merge-packages`** for per-package TypeScript or Python passes where applicable.
- **Operator experience** — Phased progress on stderr, activity logging (`fontaine-activity.log` and related detail logs per docs), **`.local.env`** / **`.local.env.example`** defaults, and automatic GraphQL retries on transient GitHub errors.
- **Documentation** — `README.md` for a minimal install/run path and `DOCS.md` for flags, behavior, and host toolchain requirements.

### Notes

- **0.x series** — Minor releases may add or adjust behavior and heuristics; treat output as an engineering aid, not a single authoritative score or legal determination of licensing.
