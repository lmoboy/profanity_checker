#!/usr/bin/env python3
"""
GitHub Profanity & Slur Checker — robust rewrite.

What it does:
  * Enumerates a GitHub user's repos (paginated, rate-limit aware).
  * Clones each repo (full history, all branches) into a temp dir.
  * Enumerates commits LOCALLY via `git log` (no 100-commit API cap).
  * Attributes commits to the target user (author/committer name+email).
  * Scans ONLY the commit message + ADDED lines (lines starting with '+'),
    so deletions / other authors' context don't get blamed on the user.
  * Detects with word-boundaries + leet-normalisation + separator-tolerant
    fuzzy matching for severe slurs (catches n.i.g.g.e.r, f@gg0t, shiiit).
  * Scores, logs with context, cleans up clones, prints an ASCII graph.

Dependencies: stdlib only. Optional: matplotlib (PNG graph), requests,
python-dotenv (.env loading). All optionals degrade gracefully.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import fnmatch
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Optional deps (graceful fallback)
# ---------------------------------------------------------------------------

try:
    import requests  # type: ignore
    _HAS_REQUESTS = True
except Exception:
    _HAS_REQUESTS = False

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

try:
    import matplotlib  # type: ignore
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore
    _HAS_MPL = True
except Exception:
    _HAS_MPL = False

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

log = logging.getLogger("profanity_checker")

# ---------------------------------------------------------------------------
# Word lists (curated, lowercase canonical forms)
# ---------------------------------------------------------------------------
# NOTE on removals vs the original script:
#   - "sex" removed: not a slur/profanity, catastrophic false positives
#     (Essex, sextant, Middlesex, sexpression...).
#   - "hitler"/"hittler" removed: not slurs (historical name), huge FP risk.
#   - "niggle" removed: legitimate English word ("a small complaint").
#   - "nig"/"nigg" style fragments kept only as full tokens with boundaries,
#     leet variants (nigg3r etc.) are covered by normalisation — no need to
#     enumerate every spelling.
#   - "retard"/"retarded" moved to SEVERE (ableist slur).

PROFANITY = {
    "arsehole",
    "arsewad",
    "asshole",
    "asswipe",
    "bastard",
    "bitch",
    "bitcher",
    "bitchers",
    "bitches",
    "bitchy",
    "cock",
    "cock-sucker",
    "cock-suckers",
    "cock-sucking",
    "cocksuck",
    "cocksucked",
    "cocksucker",
    "cocksuckers",
    "cocksucking",
    "cocksucks",
    "cunt",
    "cunts",
    "dick",
    "dildo",
    "dildos",
    "shit",
    "shitty",
}

SEVERE_SLURS = {
    "nigger",
    "niggers",
    "nigga",
    "niggas",
    "niggaz",
    "niggah",
    "niglet",
    "niglets",
    "chink",
    "chinks",
    "kike",
    "kikes",
    "wetback",
    "wetbacks",
    "spic",
    "spics",
    "gook",
    "gooks",
    "faggot",
    "faggots",
    "retard",
    "retards",
    "retarded",
}

ALL_TERMS = PROFANITY | SEVERE_SLURS
SEVERE_LOWER = {s.lower() for s in SEVERE_SLURS}

# ---------------------------------------------------------------------------
# Detection engine
# ---------------------------------------------------------------------------

# leet -> canonical (for normalisation)
_LEET_DECODE = str.maketrans({
    "@": "a", "4": "a",
    "8": "b",
    "3": "e",
    "6": "g", "9": "g",
    "1": "i", "!": "i", "|": "i",
    "0": "o",
    "$": "s", "5": "s",
    "7": "t",
    "+": "t",
})

# canonical char -> regex class covering common leet spellings (for fuzzy)
_LEET_CLASS = {
    "a": "[aA@4]",
    "b": "[bB8]",
    "c": "[cC\\(]",
    "e": "[eE3]",
    "g": "[gG69]",
    "i": "[iI1!|]",
    "l": "[lL1|]",
    "o": "[oO0]",
    "s": "[sS$5]",
    "t": "[tT7+]",
    "z": "[zZ2]",
}


def _leet_class(ch: str) -> str:
    return _LEET_CLASS.get(ch.lower(), f"[{re.escape(ch)}{re.escape(ch.upper())}]")


def _term_to_strict(term: str) -> str:
    """Escape a term, allowing hyphens to match hyphen/underscore/space runs."""
    parts = re.escape(term).replace(r"\-", r"[-_\s]+")
    # also allow optional plural-adjacent matching? No — enumerate plurals
    # explicitly to keep FP low. Just return the term itself.
    return parts


def _build_strict_regex(terms: set[str]) -> re.Pattern:
    ordered = sorted(terms, key=len, reverse=True)  # longest first!
    alt = "|".join(_term_to_strict(t) for t in ordered)
    # lookarounds (not \b) so leet chars like @ $ behave sanely
    return re.compile(r"(?<![A-Za-z0-9_])(?:" + alt + r")(?![A-Za-z0-9_])", re.IGNORECASE)


STRICT_PROFANITY_RE = _build_strict_regex(PROFANITY)
STRICT_SEVERE_RE = _build_strict_regex(SEVERE_SLURS)
STRICT_ALL_RE = _build_strict_regex(ALL_TERMS)


def _build_fuzzy_severe(term: str) -> re.Pattern:
    """Separator-tolerant + leet-tolerant pattern for ONE severe term.

    Matches e.g. 'n.i.g.g.e.r', 'n i g g e r', 'n*gger', 'f@gg0t'.
    Bounded to <=3 non-alphanumerics between letters to limit FP.
    """
    letters = [c for c in term.lower() if c.isalnum()]
    inner = r"[\W_]{0,3}".join(_leet_class(c) for c in letters)
    return re.compile(r"(?<![A-Za-z0-9_])" + inner + r"(?![A-Za-z0-9_])", re.IGNORECASE)


FUZZY_SEVERE_RES: dict[str, re.Pattern] = {t: _build_fuzzy_severe(t) for t in SEVERE_SLURS}


def normalize_spaced(text: str) -> str:
    """Lowercase + leet-decode + non-alnum→space + collapse repeats/spaces."""
    t = text.lower().translate(_LEET_DECODE)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"(.)\1{2,}", r"\1", t)  # shiiit -> shit, fuuuuck -> fuck
    t = re.sub(r"\s+", " ", t)
    return t


def spans_overlap(a_start: int, a_end: int, spans: list[tuple[int, int]]) -> bool:
    return any(a_start < e and s < a_end for s, e in spans)


def scan_text(text: str) -> list[dict]:
    """Scan a blob of text. Returns list of findings.

    Each finding: {matched, canonical, severity, obfuscated, snippet}
    Robustness:
      1. strict word-boundary match (no substring FP like 'cockpit',
         'dictionary', 'Essex', 'thisisashitstring').
      2. leet-normalised rescan for obfuscations (sh1t, b1tch...).
      3. separator-tolerant fuzzy rescan for SEVERE only (n.i.g.g.e.r).
    """
    findings: list[dict] = []
    claimed_spans: list[tuple[int, int]] = []

    def _record(matched: str, start: int, end: int, obfuscated: bool):
        canon = matched.lower().translate(_LEET_DECODE)
        # map normalised form back to canonical term where possible
        canon_term = canon
        # find closest known term (exact lower match preferred)
        low = matched.lower()
        if low in ALL_TERMS:
            canon_term = low
        else:
            # try stripped non-alnum version
            stripped = re.sub(r"[^a-z0-9]", "", low).translate(_LEET_DECODE)
            stripped = re.sub(r"(.)\1{2,}", r"\1", stripped)
            if stripped in ALL_TERMS:
                canon_term = stripped
        severity = "severe" if canon_term in SEVERE_LOWER else "profanity"
        # snippet: 60 chars around match
        s0 = max(0, start - 60)
        s1 = min(len(text), end + 60)
        snippet = text[s0:s1].replace("\n", "\\n")
        if len(snippet) > 160:
            snippet = snippet[:160] + "…"
        findings.append({
            "matched": matched,
            "canonical": canon_term,
            "severity": severity,
            "obfuscated": obfuscated,
            "snippet": snippet,
        })
        claimed_spans.append((start, end))

    # 1. strict direct matches (both sets, single pass, longest-first)
    for m in STRICT_ALL_RE.finditer(text):
        _record(m.group(0), m.start(), m.end(), obfuscated=False)

    # 2. separator-tolerant fuzzy for SEVERE (catches n.i.g.g.e.r, f@gg0t).
    #    Runs BEFORE the normalised scan so severe obfuscations are claimed
    #    here with their original snippet (and aren't double-counted below).
    for term, rx in FUZZY_SEVERE_RES.items():
        for m in rx.finditer(text):
            if spans_overlap(m.start(), m.end(), claimed_spans):
                continue
            _record(m.group(0), m.start(), m.end(), obfuscated=True)

    # 3. leet-normalised scan for PROFANITY only (sh1t, b1tch, shiiit).
    #    Severe is intentionally excluded here — fuzzy above already covers
    #    leet+separators for slurs, and running both would double-count
    #    (e.g. 'f@gg0t' found once via fuzzy AND once via normalisation).
    #    Count reconciliation: normalised text preserves token counts, so the
    #    normalised total per term is authoritative; only the *excess* over
    #    strict is added as obfuscated.
    norm = normalize_spaced(text)
    _sep_key = lambda s: re.sub(r"[-_\s]+", "", s.lower())
    norm_counts: collections.Counter = collections.Counter(
        _sep_key(m.group(0)) for m in STRICT_PROFANITY_RE.finditer(norm)
    )
    strict_counts: collections.Counter = collections.Counter(
        _sep_key(f["canonical"]) for f in findings if not f["obfuscated"]
        and f["severity"] == "profanity"
    )
    for canon, total in norm_counts.items():
        excess = total - strict_counts.get(canon, 0)
        for _ in range(max(0, excess)):
            findings.append({
                "matched": canon,
                "canonical": canon,
                "severity": "profanity",
                "obfuscated": True,
                "snippet": f"(normalised leet) …{canon}…",
            })

    return findings


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def run_git(repo_path: str, args: list[str], timeout: int = 60,
            max_bytes: int = 4_000_000) -> str:
    try:
        proc = subprocess.run(
            ["git"] + args, cwd=repo_path,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
    except FileNotFoundError:
        raise SystemExit("git binary not found. Install git first.")
    except subprocess.TimeoutExpired:
        log.warning("git %s timed out in %s", " ".join(args[:3]), repo_path)
        return ""
    out = proc.stdout[:max_bytes]
    try:
        return out.decode("utf-8", errors="replace")
    except Exception:
        return out.decode("latin1", errors="replace")


def ensure_git_ok() -> None:
    try:
        subprocess.run(["git", "--version"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
    except Exception:
        raise SystemExit("git is required but not installed/found in PATH.")


def inject_token(clone_url: str, token: str | None) -> str:
    if not token or not clone_url.startswith("https://"):
        return clone_url
    if "@" in clone_url.split("://", 1)[1].split("/", 1)[0]:
        return clone_url  # credentials already present
    rest = clone_url[len("https://"):]
    return f"https://x-access-token:{token}@{rest}"


def clone_repo(clone_url: str, dest_path: str, token: str | None = None) -> None:
    if os.path.isdir(os.path.join(dest_path, ".git")):
        run_git(dest_path, ["fetch", "--all", "--prune", "--tags"], timeout=120)
        return
    url = inject_token(clone_url, token)
    # Full history, all branches. --no-checkout keeps workdir small; diffs
    # still work because objects are present.
    cmd = ["git", "clone", "--no-checkout", url, dest_path]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, timeout=600, check=False)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"git clone failed for {dest_path}: {err}")


_RS = "\x1e"
_US = "\x1f"


def list_commits(repo_path: str, rev: str = "--all") -> list[dict]:
    """Non-merge commits under <rev> (default: all branches), newest first."""
    fmt = f"%H{_US}%an{_US}%ae{_US}%cn{_US}%ce{_US}%s{_US}%b{_RS}"
    out = run_git(repo_path, ["log", rev, "--no-merges",
                              f"--format={fmt}"], timeout=120)
    commits = []
    for record in out.split(_RS):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split(_US)
        if len(parts) < 7:
            continue
        sha, an, ae, cn, ce, subj, body = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], _US.join(parts[6:])
        if not re.fullmatch(r"[0-9a-f]{40}", sha.strip()):
            continue
        commits.append({
            "sha": sha.strip(), "author_name": an.strip(),
            "author_email": ae.strip(), "committer_name": cn.strip(),
            "committer_email": ce.strip(), "subject": subj.strip(),
            "body": body.strip(),
        })
    return commits


def commit_belongs_to_user(c: dict, target: str | None) -> bool:
    """True if commit author/committer matches target user (or no filter)."""
    if not target:
        return True
    t = target.strip().lower().lstrip("@")
    if not t:
        return True
    hay = " ".join([c.get("author_name", ""), c.get("author_email", ""),
                    c.get("committer_name", ""), c.get("committer_email", "")]).lower()
    if "@" in t:  # target given as email
        return t in hay
    # username: match name, email local part, or noreply `user@` fragment
    if t in hay:
        return True
    # also match email local-part exactly (e.g. 'octocat' vs 'octocat@x')
    for email in (c.get("author_email", ""), c.get("committer_email", "")):
        if email.lower().split("@")[0] == t:
            return True
    return False


def added_lines_with_files(repo_path: str, sha: str) -> list[tuple[str, str]]:
    """[(file, added_line_text)] for a commit. Message excluded (handled sep)."""
    out = run_git(repo_path, ["show", "--pretty=format:", "--unified=0",
                              "--no-color", "--no-ext-diff", sha, "--"],
                  timeout=60)
    results: list[tuple[str, str]] = []
    current_file = "unknown"
    for line in out.splitlines():
        if line.startswith("diff --git"):
            m = re.search(r"b/(.+)$", line)
            if m:
                current_file = m.group(1).strip()
        elif line.startswith("+++ "):
            current_file = line[4:].strip()
            if current_file.startswith("b/"):
                current_file = current_file[2:]
        elif line.startswith("Binary files ") and "differ" in line:
            continue
        elif line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            if content.strip() == "":
                continue
            if len(content) > 2000:
                content = content[:2000]
            results.append((current_file, content))
        # context (' ') and removals ('-') intentionally ignored:
        # we only blame the user for lines they ADDED.
    return results


# ---------------------------------------------------------------------------
# GitHub API (requests if present, else urllib). Paginated + rate-limit aware.
# ---------------------------------------------------------------------------

def _api_headers() -> dict:
    h = {"Accept": "application/vnd.github+json",
         "User-Agent": "profanity-checker/2.0"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def api_get_json(url: str, params: dict | None = None) -> tuple[object, dict]:
    if _HAS_REQUESTS:
        assert requests is not None
        for attempt in range(4):
            r = requests.get(url, headers=_api_headers(), params=params, timeout=20)
            if r.status_code == 403 and "rate limit" in r.text.lower() and attempt < 3:
                wait = int(r.headers.get("Retry-After", 60))
                log.warning("Rate limited, sleeping %ss …", wait)
                time.sleep(min(wait, 120))
                continue
            if r.status_code == 404:
                raise SystemExit(f"GitHub API 404 for {url} — user/repo not found?")
            r.raise_for_status()
            return r.json(), dict(r.headers)
        raise RuntimeError("GitHub API rate limit retries exhausted")
    # urllib fallback (stdlib only)
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    req = urllib.request.Request(url + q, headers=_api_headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # nosec - https
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body), dict(resp.headers)
    except Exception as e:
        raise SystemExit(f"GitHub API request failed for {url}: {e}")


def list_user_repos(username: str, include_forks: bool = False,
                    include_archived: bool = False,
                    max_repos: int | None = None) -> list[dict]:
    repos: list[dict] = []
    page = 1
    while True:
        data, _ = api_get_json(f"{GITHUB_API}/users/{username}/repos",
                               {"page": page, "per_page": 100,
                                "type": "owner", "sort": "full_name"})
        if not isinstance(data, list) or not data:
            break
        for r in data:
            if not isinstance(r, dict):
                continue
            if r.get("fork") and not include_forks:
                continue
            if r.get("archived") and not include_archived:
                continue
            repos.append(r)
            if max_repos and len(repos) >= max_repos:
                return repos
        if len(data) < 100:
            break
        page += 1
        if page > 100:  # sanity cap: 10k repos
            break
    return repos


# ---------------------------------------------------------------------------
# Repo analysis
# ---------------------------------------------------------------------------

def analyze_repo(repo_path: str, repo_url: str, target_user: str | None,
                 progress_cb=None, rev: str = "--all") -> dict:
    repo_name = os.path.basename(os.path.abspath(repo_path))
    try:
        commits = list_commits(repo_path, rev)
    except Exception as e:
        log.error("Could not list commits in %s: %s", repo_name, e)
        commits = []

    total_in_repo = len(commits)
    mine = [c for c in commits if commit_belongs_to_user(c, target_user)]
    skipped = total_in_repo - len(mine)

    flagged_commits = 0
    total_hits = 0
    severe_hits = 0
    profanity_hits = 0
    per_term: collections.Counter = collections.Counter()
    per_file: collections.Counter = collections.Counter()
    details: list[dict] = []

    for i, c in enumerate(mine):
        sha = c["sha"]
        if progress_cb:
            progress_cb(i + 1, len(mine), sha[:8])
        # 1) commit message (subject+body) — the user wrote it
        msg = (c["subject"] + "\n" + c["body"]).strip()
        msg_hits = scan_text(msg) if msg else []
        msg_enriched: list[dict] = []
        for h in msg_hits:
            h = dict(h)
            h.update({"sha": sha, "file": "<commit-message>",
                      "author": f"{c['author_name']} <{c['author_email']}>"})
            details.append(h)
            msg_enriched.append(h)
        # 2) added code lines
        line_hits: list[dict] = []
        try:
            for fname, content in added_lines_with_files(repo_path, sha):
                for h in scan_text(content):
                    h = dict(h)
                    h.update({"sha": sha, "file": fname,
                              "author": f"{c['author_name']} <{c['author_email']}>"})
                    line_hits.append(h)
                    details.append(h)
                    per_file[fname] += 1
        except Exception as e:
            log.warning("Failed diff for %s@%s: %s", repo_name, sha[:8], e)
        all_hits = msg_enriched + line_hits
        if all_hits:
            flagged_commits += 1
            total_hits += len(all_hits)
            for h in all_hits:
                per_term[h["canonical"]] += 1
                if h["severity"] == "severe":
                    severe_hits += 1
                else:
                    profanity_hits += 1
            for h in all_hits[:5]:  # log max 5 per commit (less clutter)
                lvl = logging.WARNING if h["severity"] == "severe" else logging.INFO
                log.log(lvl, "[%s] %s@%s %s:'%s' :: %s",
                        h["severity"].upper(), repo_name, sha[:8],
                        h["file"], h["matched"], h["snippet"][:140])

    user_commits = len(mine)
    rate = (flagged_commits / user_commits * 100.0) if user_commits else 0.0
    if severe_hits > 0:
        acceptability = 0.0
    else:
        acceptability = max(0.0, 100.0 - rate)

    if severe_hits > 0:
        verdict = "SEVERE"
    elif user_commits == 0:
        verdict = "NO-DATA"
    elif flagged_commits == 0:
        verdict = "CLEAN"
    elif rate < 5:
        verdict = "LOW"
    elif rate < 20:
        verdict = "MODERATE"
    else:
        verdict = "HIGH"

    return {
        "repo": repo_name,
        "repo_url": repo_url,
        "total_commits_in_repo": total_in_repo,
        "user_commits": user_commits,
        "skipped_other_authors": skipped,
        "flagged_commits": flagged_commits,
        "profanity_rate_percent": round(rate, 2),
        "acceptability": round(acceptability, 2),
        "verdict": verdict,
        "total_hits": total_hits,
        "severe_hits": severe_hits,
        "profanity_hits": profanity_hits,
        "per_term": dict(per_term.most_common(25)),
        "per_file": dict(per_file.most_common(15)),
        "findings": details[:500],  # cap JSON size
    }


# ---------------------------------------------------------------------------
# Progress / graph rendering
# ---------------------------------------------------------------------------

def _supports_color(no_color: bool) -> bool:
    return (not no_color and sys.stdout.isatty()
            and os.environ.get("NO_COLOR") is None
            and os.environ.get("TERM", "") != "dumb")


def _color(text: str, code: str, use_color: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if use_color else text


_VERDICT_COLOR = {"CLEAN": "32", "LOW": "32", "MODERATE": "33",
                  "HIGH": "31", "SEVERE": "31;1", "NO-DATA": "90"}


def bar(percent: float, width: int = 28) -> str:
    filled = int(round(max(0.0, min(100.0, percent)) / 100 * width))
    return "█" * filled + "░" * (width - filled)


def render_graph(results: list[dict], target: str, use_color: bool = True) -> str:
    L: list[str] = []
    total_user = sum(r["user_commits"] for r in results)
    total_flag = sum(r["flagged_commits"] for r in results)
    total_hits = sum(r["total_hits"] for r in results)
    total_sev = sum(r["severe_hits"] for r in results)
    overall_rate = (total_flag / total_user * 100.0) if total_user else 0.0

    if total_sev > 0:
        overall = "SEVERE"
    elif total_user == 0:
        overall = "NO-DATA"
    elif total_flag == 0:
        overall = "CLEAN"
    elif overall_rate < 5:
        overall = "LOW"
    elif overall_rate < 20:
        overall = "MODERATE"
    else:
        overall = "HIGH"

    W = 68
    L.append("┌" + "─" * (W - 2) + "┐")
    title = f"Profanity scan — @{target}" if target else "Profanity scan (local)"
    L.append("│" + title.center(W - 2) + "│")
    L.append("│" + f"verdict: {overall}".center(W - 2) + "│")
    L.append("├" + "─" * (W - 2) + "┤")

    L.append(f"  repos scanned ...... {len(results)}")
    L.append(f"  your commits ....... {total_user}")
    L.append(f"  flagged commits .... {total_flag} ({overall_rate:.2f}%)")
    L.append(f"  total hits ......... {total_hits}  (severe: {total_sev}, profanity: {total_hits - total_sev})")
    L.append("")
    L.append("  Per-repo flagged-commit rate (your commits only):")
    if not results:
        L.append("    (no repos)")
    for r in sorted(results, key=lambda x: -x["profanity_rate_percent"])[:30]:
        label = r["repo"][:22].ljust(22)
        b = bar(r["profanity_rate_percent"])
        v = _color(r["verdict"], _VERDICT_COLOR.get(r["verdict"], "0"), use_color)
        L.append(f"    {label} {b} {r['profanity_rate_percent']:6.2f}%  "
                 f"[{r['flagged_commits']}/{r['user_commits']}] {v}")
    if len(results) > 30:
        L.append(f"    … and {len(results) - 30} more (see JSON log)")

    L.append("")
    L.append("  Severity breakdown:")
    if total_hits:
        sev_pct = total_sev / total_hits * 100
        prof_pct = 100 - sev_pct
        L.append(f"    severe    {bar(sev_pct)} {total_sev:5d} ({sev_pct:5.1f}%)")
        L.append(f"    profanity {bar(prof_pct)} {total_hits - total_sev:5d} ({prof_pct:5.1f}%)")
    else:
        L.append("    (no hits — nothing to break down)")

    # top terms across all repos
    agg: collections.Counter = collections.Counter()
    for r in results:
        agg.update(r.get("per_term", {}))
    L.append("")
    L.append("  Top flagged terms:")
    if agg:
        top_max = max(agg.values())
        for term, cnt in agg.most_common(10):
            sev = "severe" if term in SEVERE_LOWER else "profanity"
            w = 22
            filled = int(round(cnt / top_max * w)) if top_max else 0
            L.append(f"    {term[:20].ljust(20)} {'█' * filled + '░' * (w - filled)} {cnt:5d}  ({sev})")
    else:
        L.append("    (none — clean)")

    L.append("")
    if overall == "CLEAN":
        L.append("  ✅ No slurs/profanity attributed to this user in scanned history.")
    elif overall == "SEVERE":
        L.append("  🚨 SEVERE slur(s) found — review the log immediately.")
    elif overall == "NO-DATA":
        L.append("  ⚠️  No commits attributed to this user were found.")
    else:
        L.append("  ⚠️  Some profanity found — see log for commit SHAs + snippets.")
    L.append("└" + "─" * (W - 2) + "┘")
    return "\n".join(L)


def try_save_png(results: list[dict], target: str, out_path: str) -> str | None:
    if not _HAS_MPL:
        return None
    if not results:
        return None
    try:
        names = [r["repo"][:18] for r in results[:25]]
        rates = [r["profanity_rate_percent"] for r in results[:25]]
        colors = ["#d62728" if r["severe_hits"] else
                  ("#2ca02c" if r["flagged_commits"] == 0 else "#ff7f0e")
                  for r in results[:25]]
        fig_h = max(3, 0.45 * len(names) + 1.5)
        plt.figure(figsize=(9, fig_h))
        plt.barh(names[::-1], rates[::-1], color=colors[::-1])
        plt.xlabel("Flagged commits % (your commits only)")
        plt.title(f"Profanity rate — @{target}" if target else "Profanity rate")
        plt.tight_layout()
        plt.savefig(out_path, dpi=140)
        plt.close()
        return out_path
    except Exception as e:
        log.warning("PNG export failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def setup_logging(log_path: str, quiet: bool) -> None:
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING if quiet else logging.INFO)
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    log.addHandler(sh)


def print_progress(msg: str, quiet: bool) -> None:
    if quiet:
        return
    sys.stdout.write("\r" + msg[:120].ljust(120))
    sys.stdout.flush()


def scan_github_user(username: str, work_dir: str, keep: bool = False,
                     include_forks: bool = False, include_archived: bool = False,
                     max_repos: int | None = None, quiet: bool = False,
                     exclude: list[str] | None = None) -> list[dict]:
    repos = list_user_repos(username, include_forks, include_archived, max_repos)
    log.info("Found %d repos for @%s (forks=%s archived=%s)",
             len(repos), username, include_forks, include_archived)
    results: list[dict] = []
    for idx, repo in enumerate(repos, 1):
        name = repo.get("name", "?")
        if exclude and any(fnmatch.fnmatch(name, pat) for pat in exclude):
            log.info("Skipping %s (excluded)", name)
            continue
        api_url = repo.get("url", "")
        clone_url = repo.get("clone_url", "")
        web_url = repo.get("html_url", api_url.replace("api.github.com/repos", "github.com"))
        local_path = os.path.join(work_dir, name)
        print_progress(f"[{idx}/{len(repos)}] cloning {name}…", quiet)
        try:
            clone_repo(clone_url, local_path, GITHUB_TOKEN)
        except Exception as e:
            log.error("Skipping %s (clone failed): %s", name, e)
            continue

        def cb(i, n, sha, _repo=name, _idx=idx, _tot=len(repos)):
            print_progress(f"[{_idx}/{_tot}] {_repo}: commit {i}/{n} ({sha})", quiet)

        log.info("--- %s (%d/%d) ---", name, idx, len(repos))
        r = analyze_repo(local_path, web_url, username, progress_cb=cb)
        results.append(r)
        log.info("%s: %d your commits, %d flagged (%.2f%%), %d hits",
                 name, r["user_commits"], r["flagged_commits"],
                 r["profanity_rate_percent"], r["total_hits"])
        if not keep:
            shutil.rmtree(local_path, ignore_errors=True)
    if not quiet:
        sys.stdout.write("\n")
    return results


def scan_local_repo(path: str, target_user: str | None) -> dict:
    if not os.path.isdir(os.path.join(path, ".git")):
        raise SystemExit(f"{path} is not a git repo (no .git/).")
    url = os.path.abspath(path)
    try:
        out = run_git(path, ["remote", "get-url", "origin"], timeout=10).strip()
        if out:
            url = out.strip()
    except Exception:
        pass
    return analyze_repo(path, url, target_user)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scan git history for profanity/slurs attributed to a user.")
    p.add_argument("username", nargs="?", default=None,
                   help="GitHub username to scan (prompts if omitted).")
    p.add_argument("--path", default=None,
                   help="Scan a local git repo instead of a GitHub user.")
    p.add_argument("--max-repos", type=int, default=None)
    p.add_argument("--include-forks", action="store_true")
    p.add_argument("--include-archived", action="store_true")
    p.add_argument("--exclude", action="append", default=[],
                   help="Glob to exclude repo names (repeatable).")
    p.add_argument("--work-dir", default=None)
    p.add_argument("--log-dir", default=None)
    p.add_argument("--keep", action="store_true",
                   help="Keep cloned repos (default: delete after scan).")
    p.add_argument("--json-out", default=None)
    p.add_argument("--png-out", default=None)
    p.add_argument("--no-graph", action="store_true")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    ensure_git_ok()
    args = build_parser().parse_args(argv)

    target = (args.username or "").strip() or None
    if not target and not args.path:
        try:
            target = input("GitHub username to scan: ").strip() or None
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
    if not target and not args.path:
        print("Need a username or --path.", file=sys.stderr)
        return 2

    work_dir = args.work_dir or tempfile.mkdtemp(prefix=f"ghscan_{(target or 'local')}_")
    os.makedirs(work_dir, exist_ok=True)
    log_dir = args.log_dir or work_dir
    os.makedirs(log_dir, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"logfile_{stamp}.log")
    json_path = args.json_out or os.path.join(log_dir, f"results_{stamp}.json")

    setup_logging(log_path, args.quiet)
    log.info("Target: %s | work_dir=%s", target or f"local:{args.path}", work_dir)

    use_color = _supports_color(args.no_color)
    results: list[dict] = []
    try:
        if args.path:
            print("Scanning local repo…")
            results = [scan_local_repo(args.path, target)]
        else:
            assert target is not None
            results = scan_github_user(
                target, work_dir, keep=args.keep,
                include_forks=args.include_forks,
                include_archived=args.include_archived,
                max_repos=args.max_repos, quiet=args.quiet,
                exclude=args.exclude)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except SystemExit as e:
        raise
    except Exception as e:
        log.exception("Scan failed: %s", e)
        print(f"Scan failed: {e}", file=sys.stderr)
        return 1

    # persist JSON (microservice-friendly)
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"target": target, "results": results,
                       "generated_utc": stamp}, f, indent=2)
    except Exception as e:
        log.warning("Could not write JSON: %s", e)
        json_path = "(write failed)"

    if not args.no_graph and not args.quiet:
        print()
        print(render_graph(results, target or "", use_color=use_color))

    png_saved = None
    if args.png_out or (not args.no_graph and _HAS_MPL):
        png_path = args.png_out or os.path.join(log_dir, f"graph_{stamp}.png")
        png_saved = try_save_png(results, target or "local", png_path)

    # compact table (backward-compat with v1 output)
    print("\n--- Scan Results ---")
    print("repository | flagged_% | acceptability | hits | severe | your_commits | link")
    print("-" * 100)
    for r in results:
        print(f"{r['repo']} | {r['profanity_rate_percent']:.2f}% | "
              f"{r['acceptability']:.2f}% | {r['total_hits']} | {r['severe_hits']} | "
              f"{r['user_commits']} | {r['repo_url']}")
    print("-" * 100)
    print(f"Total hits: {sum(r['total_hits'] for r in results)} | "
          f"Repos: {len(results)} | "
          f"Your commits: {sum(r['user_commits'] for r in results)}")
    print(f"\nLog:  {os.path.abspath(log_path)}")
    print(f"JSON: {os.path.abspath(json_path)}")
    if png_saved:
        print(f"PNG:  {os.path.abspath(png_saved)}")
    elif args.png_out:
        print("(PNG requested but matplotlib not installed — skipped. pip install matplotlib)")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
