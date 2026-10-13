# profanity_checker

Scans a GitHub user's commit history for profanity and slurs. Every hit is
attributed to the person who wrote it, and the run ends with a summary graph.

```
$ python3 script.py Alice --path /tmp/readme-demo --no-color
Scanning local repo…

┌──────────────────────────────────────────────────────────────────┐
│                     Profanity scan — @Alice                      │
│                          verdict: HIGH                           │
├──────────────────────────────────────────────────────────────────┤
  repos scanned ...... 1
  your commits ....... 2
  flagged commits .... 1 (50.00%)
  total hits ......... 1  (severe: 0, profanity: 1)

  Per-repo flagged-commit rate (your commits only):
    readme-demo            ██████████████░░░░░░░░░░░░░░  50.00%  [1/2] HIGH

  Severity breakdown:
    severe    ░░░░░░░░░░░░░░░░░░░░░░░░░░░░     0 (  0.0%)
    profanity ████████████████████████████     1 (100.0%)

  Top flagged terms:
    shit                 ██████████████████████     1  (profanity)

  ⚠️  Some profanity found — see log for commit SHAs + snippets.
└──────────────────────────────────────────────────────────────────┘

--- Scan Results ---
repository | flagged_% | acceptability | hits | severe | your_commits | link
----------------------------------------------------------------------------------------------------
readme-demo | 50.00% | 50.00% | 1 | 0 | 2 | /tmp/readme-demo
----------------------------------------------------------------------------------------------------
Total hits: 1 | Repos: 1 | Your commits: 2

Log:  /tmp/ghscan_Alice_tmo2566j/logfile_20261013_111949.log
JSON: /tmp/ghscan_Alice_tmo2566j/results_20261013_111949.json
Done.
```

## What it does

`script.py` clones each of the user's repos with full history and walks
the local git log, so there is no 100-commit API cap. Only commits whose
author or committer matches the target count toward that user. Everything
else is reported as `skipped_other_authors`.

Only commit messages and added (`+`) lines are scanned. Lines the user
deleted, and context written by other people, are ignored.

Matching is word-boundary based: `cockpit`, `dictionary` and `Essex` do
not flag. Leet spellings (`sh1t`, `b1tch`) and stretched words (`shiiit`)
flag as obfuscated. Separator tricks (`n.i.g.g.e.r`, `f@gg0t`) flag as
obfuscated severe.

`scrub.py` rewrites a branch so flagged terms become `test`:

```
$ python3 scrub.py --path /tmp/readme-demo
Before: 1 total hits on master (all authors).
Dry-run: cloning to /tmp/scrub_dryrun_agh2z8cx …
Dry-run would rewrite 1 commit(s). Before=1 After=0.
  e556d99f -> 7b613167 files=['f.txt']
No changes made to the original. Re-run with --apply.
```

Dry-run is the default and touches nothing. `--apply` creates a
`backup/pre-scrub-*` branch first. Authors and dates are preserved.

## Tradeoffs

Do not use this as proof that a history is clean. It is a fixed word list
with pattern tricks, not a content moderator. Sarcasm, slurs in other
languages, and images or audio are out of scope.

Do not rewrite history you share with others without telling them. Anyone
who pulled the old history has to re-clone after a force-push.

Force-push does not delete the old commits from GitHub immediately. Old
SHAs can stay reachable by direct URL until GitHub's garbage collection
gets to them.

`scrub.py` refuses branches with merge commits. Histories with merges need
the `censor_filter.py` + `git filter-branch` path instead (see Usage).

## Install

Source only. Needs git and Python 3 (tested on 3.14.7).

```bash
git clone https://github.com/lmoboy/profanity_checker.git
cd profanity_checker
pip install -r requirements.txt   # optional: requests, python-dotenv, matplotlib
```

Without the optional packages the scripts still run on the standard
library. `requests` only adds nicer GitHub API handling, `matplotlib`
only adds PNG graph export.

## Usage

```bash
python3 script.py --help
python3 script.py octocat
python3 script.py octocat --include-forks --max-repos 20
python3 script.py Alice --path ./my-local-repo --no-color
python3 scrub.py --path ./my-local-repo
python3 scrub.py --path ./my-local-repo --apply
git push --force-with-lease origin main
```

`script.py` prompts for a username if none is given. A `GITHUB_TOKEN`
in a `.env` file raises API limits and unlocks private repos.

Histories with merges (where `scrub.py` refuses to run):

```bash
export FILTER_BRANCH_SQUELCH_WARNING=1
git filter-branch --force \
  --tree-filter 'python3 censor_filter.py --tree .' \
  --msg-filter 'python3 censor_filter.py --msg' \
  -- --all
```

## Verify

There is no test suite. The check that exists is a byte-compile of all
three scripts:

```bash
python3 -m py_compile script.py scrub.py censor_filter.py
```

It prints nothing on success.

## Configuration

One file, in the repo directory:

```
# .env file
GITHUB_TOKEN="YOUR_PERSONAL_ACCESS_TOKEN_HERE"
```

The token needs the `repo` scope for private repositories.

## Troubleshooting

`[Errno 2] No such file or directory: 'clone'` on every repo. The clone
helper called `clone` without the `git` prefix. Fixed in the current
`script.py`; pull and re-run.

`scrub.py` says the branch has merge commits and quits. That is
intentional. Use the `filter-branch` + `censor_filter.py` commands from
Usage, which preserve merge topology.

`git push --force-with-lease` rejected with `stale info` after a
`filter-branch` run. `filter-branch` rewrites the local `origin/main`
tracking ref, which breaks the lease check. Run `git fetch origin` first
so the lease compares against the real remote, then push again.

## Goals and alternatives

The goal is a small audit tool for personal repos: find your own bad
commits, rewrite them, show a graph. It is not a moderation service and
not a pre-commit hook.

Alternatives: `git-filter-repo` and BFG Repo-Cleaner rewrite history
faster and handle every topology, but you write your own replacement
rules. GitHub push protection blocks secrets going forward but does not
scan past commits for language.

## Roadmap

The original todo list for this repo, and where each item stands:

- [x] Filter commits by user instead of tagging every commit
- [x] Clean up the cloned repositories right after the checks
- [x] Log less (per-commit cap, `--quiet`)
- [x] Per-term counts and top-terms graph
- [ ] Microservice/website — deliberately not built; the CLI stays, and
  `--json-out` is the integration point if one is ever needed
