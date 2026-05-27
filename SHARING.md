# Fontaine - Sharing and Running as a bundled executable

This guide is for anyone who wants to bundle up Fontaine as a binary for sharing.

---

## Bundle via Pyinstaller

These are steps to bundle up this tool using [pyinstaller](https://pyinstaller.org/en/stable/).

* Be sure that the virtual environment for Fontaine is activated and fully configured.
* Be sure to be in the Fontaine repo root.

1. `pip install -U pyinstaller` (Install the Pyinstaller package.)
2. `PYTHONPATH=. pyinstaller --onefile --name fontaine fontaine/__main__.py` (A /dist direcotry containing your binary will be created under repo root.)

## Running the executable

Copy the executable to the target machine. In this example we'll save it to the user's ${HOMEDIR}.

1. `export FONTAINE_CONFIG_DIR="${HOMEDIR}"` (The HOMEDIR variable may not be defined, so specify the full path here.)
2. In the ${HOMEDIR} create a config file named `.local.env` with the following contents.

```
FONTAINE_FORMAT=human
FONTAINE_METRICS=1
FONTAINE_PR_ANALYSIS=1
FONTAINE_PR_F2P=1
FONTAINE_PR_F2P_LIMIT=150
FONTAINE_PR_ANALYSIS_LOOKBACK_DAYS=2190
```

3. Run the tool via `./fontaine --repo-dir ${TARGET_REPO} --verbose` (Replace ${TARGET_REPO} with the path to the cloned repo you wish to evaluate.)

Depending on the size and complexity of the repo, this step could take minutes to hours. Once it finishes, the following items will exist.
- A file named `fontaine-activity.log`
- A file named `fontained-detail.log`
- An executive summary echoed out to the console.

Save all of the console messages to a file, then deliver it along with the two log files for analysis.
