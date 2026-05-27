# Fontaine

Fontaine screens GitHub repositories and optionally runs fail-to-pass (F2P) / pass-to-pass (P2P) test analysis on merged pull requests. It combines GitHub REST and GraphQL data with a local git clone to summarize repo facts, apply first-stage PR filters, and execute the same test workflows developers use (Jest, Vitest, Mocha, pytest, Maven).

This guide is for people who want to get up and running as quickly as possible without the need to read through detailed documentation.

For more details on what Fontaine does, advanced configurations, other ways to run it, and tooling requirements for test runs, see **[DOCS.md](DOCS.md)**.

---

## Install

After you have cloned the Fontaine repository locally, from the Fontaine repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
export PYTHONPATH=.
python -m fontaine --help
```
---

## Run

1. **Activate** the virtual environment (`source .venv/bin/activate` on macOS/Linux).
2. **Copy** the included **`.local.env.example`** file to **`.local.env`** in this repository root (same directory as this README). It has been configured with commonly-used parameters.

Fontaine loads behavior from **`.local.env`** (see **[DOCS.md — Local defaults](DOCS.md#local-defaults)**). Edit that file when you need different options.

3. **Export** a GitHub token that has **READ** access to the target repo into your shell environment (Fontaine reads **`GITHUB_TOKEN`** or **`GH_TOKEN`**). If you have not yet provisioned a Personal Access Token (PAT), then you will need to do so first. It may be quickest to [provision a classic PAT](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-personal-access-token-classic) for this.

   ```bash
   export GITHUB_TOKEN=ghp_your_token_here
   ```

4. **Clone** the repository you want to analyze somewhere **outside** this Fontaine checkout, then point Fontaine at it.

Assuming you have already set up your environment as described in the **Install** section above, here is the full set of commands to run to analyze your first repo.

   ```bash
   cd /path/to/helix-fontaine
   source .venv/bin/activate
   export GITHUB_TOKEN=ghp_your_token_here
   PYTHONPATH=. python -m fontaine --repo-dir /path/to/analyzed/repo/clone --verbose
   ```
---
## Findings

   Fontaine will begin analyzing the repo, and if it discovers any candidate PRs to test, will run F2P and P2P tests against them before outputting a summary of its findings.

   While the repo is being analyzed, quick update messages will be echoed to the console. At the same time, more detailed messages will be written to log files in the current directory.
   
  Runtime typically spans from 3 minutes to >1 hour depending on how many PRs run their test suites.

### Key Files
- fontaine-activity.log
- fontaine-detail.log

### Sample console output

#### Real-time updates
```
Starting phase 1/5: GitHub repository metadata — typical duration ~5–30 s
Phase 1/5 completed in 3s
Starting phase 2/5: Git clone for lines-of-code — typical duration ~2–20 min (full history for metrics)
Phase 2/5 completed in 10s
Starting phase 3/5: License signals — typical duration ~2–15 s
Phase 3/5 completed in 0.5s
Starting phase 4/5: PR screening (GraphQL list + first-stage filters) — typical duration ~1–15 min
Phase 4/5 completed in 47s
Starting phase 5/5: F2P/P2P test runs on local clone — typical duration ~5 min–hours
Phase 5/5 completed in 55s
```

#### Summary of findings
```
==========================================================
FONTAINE — SUMMARY
==========================================================
Repository    : impacket
Facts source  : github_api
Default branch: master
Stars         : 15,690
Languages     : Python 100.0% (primary); Dockerfile 0.0%
Merged PRs    : 682  (merged into master) | 712  (merged into any branch)
Lines of code : 189,625

Licensing (heuristic; not legal advice):
  Tags       : GitHub-declared:NOASSERTION, Apache
  Copyleft   : none
  GitHub SPDX: NOASSERTION
  Sources    : LICENSE, github_api

Inventory & activity:
  Files         : 354 total; 240 source / 106 test
  Primary lang  : Python
  CI/CD         : yes
  Test tooling  : pytest-tooling
  Issues        : 169 open / 882 closed (total 1051)
  Commits (180d): 66
  Signals       : redistributable 100/100; authoring pattern 18/100

Merged PRs by merge year (GitHub Search, mergedAt UTC):
  +--------------+-------------+-------------+------------++------------------+
  | <=2011 (0%)  | 2012 (0%)   | 2013 (0%)   | 2014 (0%)  || Row total (0%)   |
  |            0 |           0 |           0 |          0 ||                0 |
  +--------------+-------------+-------------+------------++------------------+
  | 2015 (1%)    | 2016 (6%)   | 2017 (4%)   | 2018 (8%)  || Row total (20%)  |
  |           10 |          41 |          25 |         57 ||              133 |
  +--------------+-------------+-------------+------------++------------------+
  | 2019 (8%)    | 2020 (13%)  | 2021 (8%)   | 2022 (8%)  || Row total (37%)  |
  |           52 |          91 |          56 |         54 ||              253 |
  +--------------+-------------+-------------+------------++------------------+
  | 2023 (10%)   | 2024 (11%)  | 2025 (15%)  | 2026 (7%)  || Row total (43%)  |
  |           70 |          76 |         100 |         50 ||              296 |
  +--------------+-------------+-------------+------------++------------------+

  PR screening & sample (lookback 100 days):
  +---------------------------------------+--------+
  | PRs in analysis sample                | 43     |
  | Passed first-stage filters            |  2     |
  | Strong candidates (F2P+P2P non-empty) |  2  ✅ |
  +---------------------------------------+--------+

  First-stage rejections (by reason):
  +----------------------------------------------------------------------+----+
  | not enough test files                                                | 29 |
  | difficulty not high enough (at most five non-asset files, need six+) | 12 |
  +----------------------------------------------------------------------+----+

  Complexity distribution — histogram (F2P batch — PRs selected for testing):

    ~201–422   (lowest)  | ######################################## 1
    ~422–644             |  0
    ~644–865             |  0
    ~865–1086            |  0
    ~1086–1308 (highest) | ######################################## 1

  F2P/P2P execution detail:
  +----------------------------------------+--------------------------+
  | Attempted (runner invoked)             |                        2 |
  | Completed (tests ran)                  |                        2 |
  | Failed (runner/setup/checkout/install) |                        0 |
  | Passed screening, not in F2P batch     |                        0 |
  | Outcome: strong (valid F2P+P2P)        |                        2 |
  | Outcome: empty_f2p                     |                        0 |
  | Outcome: empty_p2p                     |                        0 |
  | Outcome: runner_failed                 |                        0 |
  | Outcome: not_run                       |                        0 |
  | Strong estimate                        | 100.0% · 4.7% (2/2×2/43) |
  | F2P vs pass-filter pool                |             2/2 (100.0%) |
  | F2P vs screening sample                |              2/43 (4.7%) |
  +----------------------------------------+--------------------------+

Performance:
  Wall time               : 2 mins 6 secs
  GitHub API HTTP requests: 42
==========================================================
```
