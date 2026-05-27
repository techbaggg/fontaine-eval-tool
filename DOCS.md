# Fontaine — documentation

This guide is for anyone who wants to understand Fontaine, tune behavior, or run it without the bundled **`.local.env`** workflow. The **[README](README.md)** is a minimal install-and-run path only.

---

## About Fontaine

Fontaine combines GitHub REST and GraphQL data with a local git clone to summarize repo facts, apply first-stage PR filters, and—when configured—execute the same test workflows developers use (Jest, Vitest, Mocha, pytest, Maven/Surefire/Failsafe).

**Why the codename *Fontaine*?** In French, *fontaine* means **fountain**—a place where water wells up and spreads. Fontaine gathers many signals—metadata, PR and file evidence, test outcomes, and heuristics—and **pours them out in one place** so you can see the project at a glance. A *fountain of signals*, not a single score.

---

## What it does

- **Acquisition summary** — Stars, languages, default branch, merged PR counts (GitHub API), lines of code from a shallow or full clone as configured.
- **Optional `--metrics`** — Extra filesystem and git heuristics (full clone in GitHub mode when metrics are requested).
- **`--pr-analysis`** — Lists PRs via GraphQL and applies Fontaine’s screening rules (file counts, test heuristics, title/body checks, etc.). **Default GraphQL feed:** all PR states, **CREATED_AT** order. **Lookback (days > 0):** the bulk scan counts **merged PRs** with **`mergedAt`** inside the window—open PRs do not fill **`--pr-analysis-max-prs`**, so a shorter window usually yields fewer rows and less work. **`--pr-analysis-lookback-days 0`** disables merged-time filtering and allows open PRs in the sample again (they have no `mergedAt`). Use **`--pr-analysis-merged-only`** for **merged PRs only**, **UPDATED_AT** order (often better aligned with merge recency). Default window **730 days** unless you set **`--pr-analysis-lookback-days`** or **`PR_ANALYSIS_LOOKBACK_DAYS`** in **`.local.env`**.
- **`--pr-f2p`** — For PRs that pass screening, checks out base/head in your **`--repo-dir`** clone and runs the detected JS, Python, or Maven test runner in stages to classify F2P vs P2P tests.

Activity and timing are written to **`fontaine-activity.log`** by default (configurable with **`--activity-log`**). While a run is in progress, **numbered phases** are printed to **stderr** (for example `Starting phase 2/5: …`) so you can tell the process is still working; use **`--log-terminal`** if you also want the full activity log mirrored to stderr. GraphQL calls retry automatically on transient GitHub errors (e.g. 502/504).

---

## Supported languages and test runners

**`--pr-analysis` screening** uses a **broad** dominant-language whitelist on PR changed files (many languages; see `SUPPORTED_LANGUAGES_WHITELIST` in `fontaine/domain/pr_analysis.py`). **`--pr-f2p`** execution below covers **JavaScript/TypeScript** (Jest/Vitest/Mocha), **Python/pytest**, and **Maven** (Surefire/Failsafe JUnit XML); other languages never run tests through Fontaine today.

F2P/P2P runs when Fontaine selects a **JavaScript/TypeScript**, **Python**, or **Maven** workflow. The backend is chosen from repository layout and the PR’s changed paths (`choose_f2p_backend` in `fontaine/domain/f2p/detect.py`): one stack when only that stack is available, otherwise file extensions on changed and test paths, with **TypeScript** as the tie-breaker when still ambiguous. If no supported test stack is detected and path hints do not resolve it, **`--pr-f2p`** cannot run for that PR.

### JavaScript and TypeScript

- **Runners (one per `package.json` workspace):** **Vitest**, **Jest**, or **Mocha**. Precedence among declared frameworks is **Vitest → Jest → Mocha** (`detect_ts_runner_kind` in `fontaine/domain/f2p/ts_runner.py`: `dependencies` / `devDependencies`, then `scripts.test`, layout/config). Workspace roots and **`scripts.test`** that only invoke **turbo** / **nx** / **lerna** (without naming a runner in the string) still resolve to **Jest** when no other runner wins.
- **Package managers:** **npm**, **pnpm**, or **yarn** (lockfile-based detection).

### Python

- **Runner:** **pytest** only (not `unittest`). Detection uses `pytest.ini`, `setup.cfg` (`[tool:pytest]`), `tox.ini`, `pyproject.toml` (pytest tool/deps), `conftest.py`, conventional `test_*.py` / test-directory layouts, and `requirements*.txt` (etc.) listing pytest—see `pytest_ready_at` / `pytest_runner_available` in `fontaine/domain/f2p/detect.py` and `fontaine/domain/f2p/py_runner.py`.

### Maven (Java)

- **Runner:** **Maven** with **Surefire** / **Failsafe** JUnit XML under `target/surefire-reports/` and `target/failsafe-reports/` (e.g. `mvn verify`). Unless `FONTAINE_F2P_MAVEN_GOALS` already lists `clean`, Fontaine prepends `clean` so gitignored `target/` (classes and old XML) is not reused across PR F2P stages. Detection uses `pom.xml` at the repo root or a single immediate subdirectory, plus `mvn` or `./mvnw` on `PATH`, or **Docker** when the host has no Maven—Fontaine can pull and run a **`maven:3-eclipse-temurin-*`** image (Java level from `help:effective-pom`, env overrides, or defaults)—see `fontaine/domain/f2p/maven_runner.py` and `fontaine/domain/f2p/docker_maven.py`.

### Polyglot / monorepos

If JS/TS, pytest, and/or Maven signals appear, Fontaine uses **path heuristics**—not a manual flag—to pick one backend per PR. **`--pr-f2p-merge-packages`** can run per-package TypeScript or Python passes in monorepos (Maven F2P v1 targets a single `pom.xml` tree).

---

## Prerequisites

- **Python** 3.10 or newer (virtual environment recommended; see the **[README](README.md)**).
- **Git** on `PATH`.
- **`GITHUB_TOKEN`** or **`GH_TOKEN`** when using GitHub API features.

For **`--pr-f2p`**, you also need the **toolchains for the repository you analyze** on the host (not Fontaine’s `pip install`), or **Docker** for Maven when configured—see **[Toolchains for F2P](#toolchains-for-f2p)** below.

---

## Requirements

### Fontaine itself (Python)

- **Python** 3.10 or newer.
- **Virtual environment (recommended):** Create a venv in the Fontaine checkout, activate it, and install dependencies there so **httpx** and other packages from **`requirements.txt`** stay off your system interpreter. Step-by-step commands are in **[README → Install](README.md#install)**.
- **Git** on `PATH` when Fontaine invokes `git`.

Node, npm, pnpm, Yarn, and the **analyzed repository’s** Python test stack are **not** pip packages; install them on the host for **`--pr-f2p`** (see **[Toolchains for F2P](#toolchains-for-f2p)**).

### GitHub API access (remote mode)

- **Personal access token:** `GITHUB_TOKEN` or `GH_TOKEN` (REST + GraphQL to `api.github.com`; never logged by Fontaine).

### Enabling test runs (`--pr-f2p`)

- **JavaScript/TypeScript:** **Node.js** and **npm** (bundled with Node), plus **pnpm** or **Yarn** if the repo uses them.
- **Python:** **Python** on `PATH` (override with `FONTAINE_F2P_PYTHON`) and a pytest-oriented project.
- **Maven/Java:** **JDK**, **Maven** (`mvn` or `./mvnw`), or **Docker** when Fontaine falls back to a Maven container image (see **`FONTAINE_F2P_MAVEN_*`** env vars in CLI `--help` for `--pr-f2p`).

---

## Toolchains for F2P

Host installs only (**not** Fontaine’s `requirements.txt`). Needed when **`--pr-f2p`** is enabled. Install like any other dev tools—**not** via Fontaine’s pip environment.

- **Node & npm** — [nodejs.org](https://nodejs.org) or your OS package manager; **pnpm** / **Yarn** as needed ([pnpm](https://pnpm.io/installation), [Yarn](https://yarnpkg.com/getting-started/install)).
- **Python / pytest** — interpreter and deps for the **analyzed** repo (Fontaine may create `.venv` in the clone unless skipped via env vars).
- **Maven / Java** — JDK and Maven (or `mvnw` committed in the repo). Without local Maven, **Docker** can run `mvn` in an official Maven image when enabled.
- **Git** — [git-scm.com](https://git-scm.com) or OS packages.

---

## Where to clone repositories you analyze

**Do not** clone candidate or target repositories **inside** your Fontaine (`helix-fontaine`) checkout. Keep Fontaine’s tree dedicated to the tool; put every repository you evaluate **next to** Fontaine (or anywhere else outside that checkout). Fontaine only needs a path via **`--repo-dir`**.

**Suggested layout:** a **`candidates`** directory **alongside** your Fontaine clone:

```text
~/work/
├── helix-fontaine/          # Fontaine tool only (venv lives here)
└── candidates/
    ├── org-repo-one/
    └── org-repo-two/
```

Pass **`--repo-dir`** pointing at the analyzed clone (for example **`~/work/candidates/org-repo-one`**).

---

## Quickstart: using `.local.env`

The **[README](README.md)** flow copies **`.local.env.example`** to **`.local.env`** and runs Fontaine with **`--repo-dir`**. Details:

1. Activate your Fontaine virtual environment (see **[README → Install](README.md#install)**).
2. Copy **`.local.env.example`** to **`.local.env`** and edit values for your target GitHub repo (owner, repo, format, metrics, PR analysis, F2P, limit, etc.), if the defaults are not what you want.
3. Export **`GITHUB_TOKEN`** (or **`GH_TOKEN`**).
4. From the Fontaine repository root, run Fontaine with **`--repo-dir`** pointing at your analyzed clone.

If you omit owner or repo in **`.local.env`**, Fontaine can infer them from **`git remote`** on that clone when it points at github.com.

```bash
cd /path/to/fontaine/checkout
source .venv/bin/activate          # Windows: .venv\Scripts\activate
export GITHUB_TOKEN=ghp_…          # or GH_TOKEN
cp .local.env.example .local.env
# edit .local.env if needed, then:
PYTHONPATH=. python -m fontaine --repo-dir /path/to/analyzed/clone
```

**CLI precedence:** flags override **`.local.env`** when both apply (for example **`--pr-f2p-limit`**, **`--pr-analysis-lookback-days`**, **`--no-pr-f2p`**, **`--format json`**).

Supported keys and lookup order: **[Local defaults](#local-defaults)** below.

---

## Quickstart: explicit CLI flags (no `.local.env`)

Use your Fontaine virtual environment. Set the token, then pass behavior entirely on the command line—no config file required.

### Token

```bash
cd /path/to/fontaine/checkout
source .venv/bin/activate          # Windows: .venv\Scripts\activate
export GITHUB_TOKEN=ghp_…          # or GH_TOKEN
```

### GitHub metadata only

```bash
cd /path/to/fontaine/checkout
source .venv/bin/activate          # Windows: .venv\Scripts\activate
PYTHONPATH=. python -m fontaine --owner your-org --repo your-repo --format human
```

### PR screening

```bash
cd /path/to/fontaine/checkout
source .venv/bin/activate          # Windows: .venv\Scripts\activate
PYTHONPATH=. python -m fontaine \
  --owner your-org \
  --repo your-repo \
  --format human \
  --pr-analysis
```

To scan merged PRs further back than the default **730-day** window, pass **`--pr-analysis-lookback-days N`** or set **`PR_ANALYSIS_LOOKBACK_DAYS=N`** in **`.local.env`** (use **`0`** for no merged-time limit). To **exclude open PRs** from the GraphQL sample and use **merged PRs, `UPDATED_AT` order**, add **`--pr-analysis-merged-only`** (or **`PR_ANALYSIS_MERGED_ONLY=1`** in **`.local.env`**).

### PR screening + F2P

```bash
cd /path/to/fontaine/checkout
source .venv/bin/activate          # Windows: .venv\Scripts\activate
PYTHONPATH=. python -m fontaine \
  --owner your-org \
  --repo your-repo \
  --format human \
  --metrics \
  --pr-analysis \
  --pr-f2p \
  --repo-dir /path/to/local/clone \
  --pr-f2p-limit 30
```

**Rules:** **`--pr-f2p`** needs **`--pr-analysis`**, **`--repo-dir`**, **`--owner`**, and **`--repo`** when they are not supplied via **`.local.env`** or inferred from **`git remote`** on **`--repo-dir`**. Without **`--pr-analysis`**, use **either** **`--repo-dir` alone** (local summary) **or** **`--owner`** + **`--repo`** (remote metadata)—not both unless you are in PR-analysis + F2P mode as above.

---

## Output and format options

- Default format is **JSON** to stdout. Use **`--format json --out report.json`** (or **`--out`** with defaults) to write a file.
- **`--format human --verbose`** writes the long narrative to **`fontaine-detail.txt`** (override with **`--human-detail`**).

---

## Local defaults

Configuration in **`.local.env`** lives **only** next to the Fontaine tool, **not** inside the repository you pass as `--repo-dir`.

**Lookup order** (later paths override earlier):

1. `<fontaine-checkout>/.local.env`
2. `<fontaine-checkout>/fontaine/.local.env`
3. If **`FONTAINE_CONFIG_DIR`** is set: `$FONTAINE_CONFIG_DIR/.local.env` then `$FONTAINE_CONFIG_DIR/fontaine/.local.env` (optional when the package is installed away from your config)

`<fontaine-checkout>` is the parent of the `fontaine` Python package (your git clone root in development).

**Supported keys** (also with a `FONTAINE_` prefix, e.g. `FONTAINE_OWNER`):

| Key | Meaning |
|-----|---------|
| `OWNER` / `REPO` | GitHub coordinates for `--pr-analysis` / `--pr-f2p` |
| `FORMAT` | `human` or `json` |
| `METRICS` | `1` / `0`, `true` / `false`, etc. |
| `PR_ANALYSIS` | Enable PR listing + screening |
| `PR_F2P` | Enable F2P/P2P after screening |
| `PR_F2P_LIMIT` | Cap passed PRs for F2P (same as `--pr-f2p-limit`; integer ≥ 1) |
| `PR_ANALYSIS_LOOKBACK_DAYS` | Merged-time window for PR analysis (same as `--pr-analysis-lookback-days`; integer ≥ 0; default **730** when unset in the file and on the CLI) |
| `PR_ANALYSIS_MERGED_ONLY` | Same as `--pr-analysis-merged-only`: merged-only GraphQL feed (`1` / `true` / …); use `--no-pr-analysis-merged-only` on the CLI to force the default all-states listing |

If `OWNER` or `REPO` is omitted, Fontaine uses **`git remote get-url`** on the **`--repo-dir`** clone for **github.com**. Override the remote name with **`FONTAINE_GIT_REMOTE`** if needed.

**Precedence:** CLI always wins over the file (`--metrics` / `--no-metrics`, `--pr-analysis` / `--no-pr-analysis`, `--pr-f2p` / `--no-pr-f2p`, `--pr-analysis-merged-only` / `--no-pr-analysis-merged-only`, `--pr-analysis-lookback-days`, `--pr-f2p-limit`, etc.).

The fastest path using **`.local.env.example`**: **[README → Run](README.md#run)**.

---

## Useful CLI flags (short list)

| Flag | Purpose |
|------|--------|
| `--repo-dir` | Local git tree (required for `--pr-f2p`) |
| `--owner` / `--repo` | GitHub coordinates |
| `--format human` | Readable summary (default format is `json`) |
| `--metrics` | Extended repository metrics |
| `--pr-analysis` | GraphQL PR listing + screening |
| `--pr-analysis-merged-only` / `--no-pr-analysis-merged-only` | Merged PRs only (`states=MERGED`, `UPDATED_AT` desc); default off — omit flag for open+closed listing (`CREATED_AT` desc). **`.local.env`:** `PR_ANALYSIS_MERGED_ONLY` |
| `--pr-analysis-max-prs` | Cap PRs scanned (default 500) |
| `--pr-analysis-lookback-days` | Merged PRs only if `mergedAt` within last N days (default **730**; **`0`** = no limit; **`.local.env`**: `PR_ANALYSIS_LOOKBACK_DAYS`) |
| `--pr-f2p` | Run F2P/P2P after screening |
| `--pr-f2p-limit` | Max passing PRs to run F2P on (default 30) |
| `--pr-f2p-timeout` | Per-stage timeout seconds (default 600) |
| `--pr-f2p-workers` | Parallel workers with isolated clones (default 1) |
| `--pr-f2p-merge-packages` | Monorepo: per-package runs from changed test paths |
| `--npm-cache-dir` | Sets `npm_config_cache` for installs |
| `--api-clone-depth` | Shallow depth for LOC-only clones without `--metrics` (default 1) |

Run **`python -m fontaine --help`** for the full list.

---

## F2P-related environment variables

Examples (see **`--help`** on `--pr-f2p` for the full set):

- **JavaScript:** `FONTAINE_F2P_SKIP_INSTALL`, `FONTAINE_F2P_NPM_SCRIPT`, `FONTAINE_F2P_JEST_RUN_IN_BAND`, `FONTAINE_F2P_MOCHA_ARGS`
- **Python:** `FONTAINE_F2P_PYTHON`, `FONTAINE_F2P_SKIP_PIP_INSTALL`, `FONTAINE_F2P_SKIP_VENV`, `FONTAINE_F2P_PYTEST_ARGS`
- **Sampling:** `FONTAINE_F2P_SAMPLE_SEED` when `--pr-f2p-limit` subsamples passes

---

## Logs

- **`fontaine-activity.log`** — Progress and phase timings (`TIMING` lines).
- **`--activity-log -`** — Activity to stderr only.
- **`--log-terminal`** — Mirror activity to stderr as well as the log file.

---

## License

See the repository license file if present.
