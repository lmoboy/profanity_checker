# 🔍 GitHub Profanity & Slur Checker

Scan a GitHub user's **full commit history** for profanity vs severe slurs —
attributed to *that user only* — with a terminal graph at the end.

## ✨ What it does (v2)

* **True history scan:** clones each repo (full history, all branches) and
  enumerates commits locally via `git log --all`. No 100-commit API cap.
* **User attribution:** only commits where the target matches
  author/committer name or email are scored. Others are counted as
  `skipped_other_authors`, never blamed on the user.
* **Added-lines-only:** scans commit messages + lines the user *added*
  (`+` lines). Deletions and context from other authors are ignored.
* **Robust matching:**
  * word-boundary regex — `cockpit`, `dictionary`, `peacock`,
    `thisisashitstring` do **not** flag;
  * leet decoding — `sh1t`, `b1tch`, `@sshole`, `shiiit` flag as obfuscated;
  * separator-tolerant fuzzy for severe slurs —
    `n.i.g.g.e.r`, `f@gg0t` flag as obfuscated severe.
* **Sane scoring:** `flagged% = flagged_commits / your_commits * 100`,
  `acceptability = 100 - flagged%` (0 if any severe hit).
  Verdicts: `CLEAN / LOW / MODERATE / HIGH / SEVERE / NO-DATA`.
* **Low clutter:** max 5 log lines per commit, single-line progress
  (no screen-clearing box), clones deleted after each repo unless `--keep`.
* **Graph + artifacts:** ASCII bar graph in-terminal, optional PNG
  (`--png-out`, needs `matplotlib`), JSON results (`--json-out`), full log.

Word-list hygiene: `sex`, `hitler`/`hittler` (not slurs, massive FP) and
`niggle` (legit English word) were removed. `retard`/`retarded` are
classified severe.

## ✔ Original todo — all fulfilled

- [x] Filter the commits by user instead of tagging every commit → author/committer attribution (`skipped_other_authors`)
- [x] Cleanup the repositories right after the checks → clones deleted unless `--keep`
- [x] Log way less for reduced clutter → max 5 lines per commit, single-line progress, `--quiet`
- [x] Count tags for same word instead of multiple across same commit → per-term counters + top-terms graph
- [ ] Turn this into a microservice to create a website → not built (CLI only by decision); `--json-out` is the integration point if ever needed

## 🚀 Setup

Needs **Python 3.10+** and **git**. Zero *required* pip deps (stdlib only).

```bash
pip install -r requirements.txt   # optional: requests, python-dotenv, matplotlib
```

GitHub token (for private repos + higher rate limits). Create `.env`:

```
GITHUB_TOKEN="YOUR_PERSONAL_ACCESS_TOKEN_HERE"
```

Token needs `repo` scope for private repos.

## 💻 How to run

```bash
python script.py octocat
python script.py octocat --include-forks --max-repos 20 --keep
python script.py --path ./my-local-repo --help
python script.py Alice --path /tmp/demo --no-color
```

Flags: `--max-repos N`, `--include-forks`, `--include-archived`,
`--exclude '*-fork'` (repeatable), `--work-dir`, `--log-dir`,
`--keep`, `--json-out out.json`, `--png-out graph.png`,
`--no-graph`, `--no-color`, `--quiet`, `--path LOCAL_REPO`.

## Example output

```
┌──────────────────────────────────────────────────────────────────┐
│                    Profanity scan — @octocat                     │
│                        verdict: MODERATE                         │
├──────────────────────────────────────────────────────────────────┤
  repos scanned ...... 2
  your commits ....... 13
  flagged commits .... 1 (7.69%)
  total hits ......... 3  (severe: 0, profanity: 3)

  Per-repo flagged-commit rate (your commits only):
    my-repo                ████░░░░░░░░░░░░░░░░░░░░░░░░  12.50%  [1/8] MODERATE
    clean-repo             ░░░░░░░░░░░░░░░░░░░░░░░░░░░░   0.00%  [0/5] CLEAN

  Severity breakdown:
    severe    ░░░░░░░░░░░░░░░░░░░░░░░░░░░░     0 (  0.0%)
    profanity ████████████████████████████     3 (100.0%)

  Top flagged terms:
    shit                 ██████████████████████     3  (profanity)

  ⚠️  Some profanity found — see log for commit SHAs + snippets.
└──────────────────────────────────────────────────────────────────┘

--- Scan Results ---
repository | flagged_% | acceptability | hits | severe | your_commits | link
----------------------------------------------------------------------------------------------------
my-repo | 12.50% | 87.50% | 3 | 0 | 8 | https://github.com/octocat/my-repo
----------------------------------------------------------------------------------------------------
Total hits: 3 | Repos: 2 | Your commits: 13

Log:  /tmp/ghscan_octocat_xxx/logfile_20261013_102749.log
JSON: /tmp/ghscan_octocat_xxx/results_20261013_102749.json
Done.
```

## 🧹 Scrubbing history (`scrub.py`)

Rewrites branch history so flagged terms become a neutral word
(default `test`, override with `--word dev`):

```bash
python scrub.py --path /path/to/repo                    # dry-run (safe, default)
python scrub.py --path /path/to/repo --apply            # rewrite + backup branch
python scrub.py --path /path/to/repo --apply --push     # + force-with-lease push
python scrub.py --path /path/to/repo --severe-only      # censor severe slurs only
```

Linear history only, refuses on dirty trees/merges, keeps authors/dates,
verifies zero hits before telling you to push. Undo via `backup/pre-scrub-*`.

## ⚙️ Customising lists

Edit the sets at the top of `script.py` (lowercase):

```python
PROFANITY = {"asshole", ...}
SEVERE_SLURS = {"nigger", "chink", ...}
```

Regexes, fuzzy patterns, and the graph rebuild automatically. Longest-first
alternation prevents `cock` shadowing `cock-sucker`.
