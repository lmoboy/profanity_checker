#!/usr/bin/env python3
"""Scrub profanity/slurs from git history by rewriting commits.

Strategy (safe, stdlib-only, no filter-repo install needed):
  1. Refuses on dirty tree or merge commits (keeps it simple/safe).
  2. Creates backup branch  backup/pre-scrub-<stamp>.
  3. Replays the branch oldest->newest on an orphan branch, censoring
     file contents + commit messages, preserving author/committer/date.
  4. Re-scans the rewritten history with script.py's engine to verify
     zero hits, then points the branch at the new history.
  5. NEVER pushes unless you pass --push (uses --force-with-lease).

Censoring: every match of the scanner regex is replaced with a neutral
word (default "test", override with --word), so history stays readable and
code keeps working ('nigger@nigger.nigger' becomes 'test@test.test',
'return "nigger"' becomes 'return "test"').

Typical use (dry-run first, always):
  python scrub.py --path /path/to/repo                # dry-run in temp copy
  python scrub.py --path /path/to/repo --apply        # rewrite for real
  python scrub.py --path /path/to/repo --apply --push # + force-with-lease push
  python scrub.py --clone https://github.com/u/r.git --work-dir /tmp/scrub
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import script as checker  # noqa: E402 — reuse the scanner engine


def sh(repo: str, *args: str, check: bool = True, input_text: str | None = None) -> str:
    p = subprocess.run(
        ["git", *args], cwd=repo, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
        input=input_text,
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()[-400:]}")
    return p.stdout


def ensure_clean(repo: str) -> None:
    out = sh(repo, "status", "--porcelain")
    if out.strip():
        raise SystemExit("Working tree dirty — commit/stash first. Refusing.")


def current_branch(repo: str) -> str:
    b = sh(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if b == "HEAD":
        raise SystemExit("Detached HEAD — checkout a branch first.")
    return b


def censor_text(text: str, severe_only: bool = False, word: str = "test") -> str:
    """Replace flagged terms with a neutral word (default 'test')."""
    if severe_only:
        for rx in checker.FUZZY_SEVERE_RES.values():
            text = rx.sub(word, text)
        text = checker.STRICT_SEVERE_RE.sub(word, text)
    else:
        for rx in checker.FUZZY_SEVERE_RES.values():
            text = rx.sub(word, text)
        text = checker.STRICT_ALL_RE.sub(word, text)
    return text


def is_binary(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(8192)
    except OSError:
        return True


def censor_worktree(repo: str, severe_only: bool, word: str = "test",
                    skip: tuple[str, ...] = (".git",)) -> list[str]:
    """Censor every text file in the worktree. Returns changed relpaths."""
    changed: list[str] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".git")]
        for fn in files:
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, repo)
            if os.path.islink(fp):
                continue
            try:
                if os.path.getsize(fp) > 2_000_000 or is_binary(fp):
                    continue
                with open(fp, "r", encoding="utf-8", errors="strict") as f:
                    orig = f.read()
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            new = censor_text(orig, severe_only, word)
            if new != orig:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(new)
                changed.append(rel)
    return changed


def get_metas(repo: str, branch: str) -> list[dict]:
    out = sh(repo, "rev-list", "--reverse", "--topo-order", branch, "--")
    shas = [s for s in out.splitlines() if s.strip()]
    merges = sh(repo, "rev-list", "--merges", branch, "--").split()
    if merges:
        raise SystemExit(
            f"Branch has {len(merges)} merge commit(s) — this tool handles "
            "linear history only. Use git-filter-repo for merges, or ask me."
        )
    US, RS = "\x1f", "\x1e"
    fmt = US.join(["%H", "%an", "%ae", "%ad", "%cn", "%ce", "%cd", "%s", "%b"]) + RS
    raw = sh(repo, "log", "--reverse", "--date=raw", f"--format={fmt}", branch, "--")
    metas = []
    for rec in raw.split(RS):
        if not rec.strip():
            continue
        p = rec.split(US)
        if len(p) < 9 or not re.fullmatch(r"[0-9a-f]{40}", p[0].strip()):
            continue
        metas.append({
            "sha": p[0].strip(), "an": p[1], "ae": p[2], "ad": p[3],
            "cn": p[4], "ce": p[5], "cd": p[6], "subj": p[7], "body": US.join(p[8:]),
        })
    if len(metas) != len(shas):
        raise RuntimeError(f"meta/{{{len(metas)}}}/sha/{{{len(shas)}}} count mismatch")
    return metas


def wipe_tree(repo: str) -> None:
    """Empty index + workdir (except .git). Safe: caller guarantees a clean
    tree at start and commits after every iteration, so nothing precious can
    be lost — everything is recoverable from the object db / backup branch."""
    sh(repo, "rm", "-qrf", ".", check=False)
    for entry in os.listdir(repo):
        if entry == ".git":
            continue
        fp = os.path.join(repo, entry)
        try:
            if os.path.islink(fp) or os.path.isfile(fp):
                os.remove(fp)
            elif os.path.isdir(fp):
                shutil.rmtree(fp, ignore_errors=True)
        except OSError:
            pass
    sh(repo, "rm", "-qrf", "--cached", ".", check=False)


def replay(repo: str, branch: str, severe_only: bool, word: str = "test") -> tuple[str, list[dict]]:
    """Replay branch censored onto orphan __scrub_tmp. Returns (new_head, report)."""
    metas = get_metas(repo, branch)
    sh(repo, "checkout", "-q", branch, "--")
    sh(repo, "checkout", "-q", "--orphan", "__scrub_tmp")
    report: list[dict] = []
    for i, m in enumerate(metas):
        wipe_tree(repo)
        sh(repo, "checkout", m["sha"], "--", ".")
        changed_files = censor_worktree(repo, severe_only, word)
        sh(repo, "add", "-A")
        full_msg = (m["subj"] + ("\n\n" + m["body"] if m["body"].strip() else "")).strip()
        new_msg = censor_text(full_msg, severe_only, word)
        msg_changed = new_msg != full_msg
        if not new_msg.strip():
            new_msg = word
            msg_changed = True
        env = dict(os.environ)
        env.update({
            "GIT_AUTHOR_NAME": m["an"], "GIT_AUTHOR_EMAIL": m["ae"],
            "GIT_AUTHOR_DATE": m["ad"], "GIT_COMMITTER_NAME": m["cn"],
            "GIT_COMMITTER_EMAIL": m["ce"], "GIT_COMMITTER_DATE": m["cd"],
        })
        p = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-q",
             "--allow-empty", "--allow-empty-message", "--no-verify", "-F", "-"],
            cwd=repo, input=new_msg, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
        )
        if p.returncode != 0:
            raise RuntimeError(f"commit replay failed at {m['sha'][:8]}: {p.stderr[-400:]}")
        new_sha = sh(repo, "rev-parse", "HEAD").strip()
        if changed_files or msg_changed:
            report.append({"old": m["sha"][:8], "new": new_sha[:8],
                           "files": changed_files, "msg_changed": msg_changed})
    return sh(repo, "rev-parse", "__scrub_tmp").strip(), report


def verify_clean(repo: str, rev: str, target=None) -> int:
    r = checker.analyze_repo(repo, "", target, rev=rev)
    return r["total_hits"]


def scrub_repo(repo: str, branch: str, severe_only: bool, apply: bool,
               word: str = "test") -> int:
    ensure_clean(repo)
    branch = branch or current_branch(repo)
    sh(repo, "rev-parse", "--verify", branch)
    before = verify_clean(repo, branch, None)
    print(f"Before: {before} total hits on {branch} (all authors).")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    if apply:
        backup = f"backup/pre-scrub-{stamp}"
        sh(repo, "branch", backup, branch)
        print(f"Backup branch created: {backup}")
        new_head, report = replay(repo, branch, severe_only, word)
        # NB: branch -f fails on the checked-out branch; reset instead.
        sh(repo, "checkout", "-q", branch)
        sh(repo, "reset", "--hard", "-q", new_head)
        sh(repo, "branch", "-D", "__scrub_tmp", check=False)
        after = verify_clean(repo, branch, None)
        print(f"Rewrote {len(report)} commit(s). After: {after} hits.")
        for e in report:
            print(f"  {e['old']} -> {e['new']} files={e['files'][:4]} "
                  f"{'(msg)' if e['msg_changed'] else ''}")
        if after != 0:
            print("WARNING: hits remain — likely allowlist/exclusion needed. "
                  "Check log, do NOT push yet.")
            return 1
        print(f"\nDone. Review with: git log --oneline {backup}..{branch} ; "
              f"git diff {backup}..{branch} --stat")
        print(f"Push with: git push --force-with-lease origin {branch}")
        return 0
    else:
        # dry-run in a temp clone so the original is untouched
        tmp = tempfile.mkdtemp(prefix="scrub_dryrun_")
        try:
            print(f"Dry-run: cloning to {tmp} …")
            p = subprocess.run(["git", "clone", "-q", repo, tmp + "/c"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if p.returncode != 0:
                raise RuntimeError(f"clone for dry-run failed: {p.stderr[-300:]}")
            c = tmp + "/c"
            new_head, report = replay(c, branch, severe_only, word)
            after = verify_clean(c, new_head, None)
            print(f"Dry-run would rewrite {len(report)} commit(s). "
                  f"Before={before} After={after}.")
            for e in report:
                print(f"  {e['old']} -> {e['new']} files={e['files'][:4]} "
                      f"{'(msg)' if e['msg_changed'] else ''}")
            print("No changes made to the original. Re-run with --apply.")
            return 0 if after == 0 else 2
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scrub slurs from git history.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--path", default=None, help="Local repo to scrub")
    g.add_argument("--clone", default=None, help="Clone URL to scrub in a temp dir")
    ap.add_argument("--branch", default=None)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--apply", action="store_true",
                    help="Really rewrite (default is dry-run)")
    ap.add_argument("--push", action="store_true",
                    help="Push with --force-with-lease after --apply")
    ap.add_argument("--severe-only", action="store_true",
                    help="Censor severe slurs only (default: all terms)")
    ap.add_argument("--word", default="test",
                    help="Replacement word (default: test)")
    a = ap.parse_args(argv)

    if a.clone:
        wd = a.work_dir or tempfile.mkdtemp(prefix="scrub_")
        dest = os.path.join(wd, "repo")
        if not os.path.isdir(os.path.join(dest, ".git")):
            print(f"Cloning into {dest} …")
            p = subprocess.run(["git", "clone", a.clone, dest],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if p.returncode != 0:
                print(f"Clone failed: {p.stderr[-500:]}", file=sys.stderr)
                return 1
        repo = dest
    else:
        assert a.path is not None
        repo = a.path
        if not os.path.isdir(os.path.join(repo, ".git")):
            print(f"{repo} is not a git repo.", file=sys.stderr)
            return 2

    if a.push and not a.apply:
        print("--push needs --apply.", file=sys.stderr)
        return 2

    rc = scrub_repo(repo, a.branch or "", a.severe_only, a.apply, a.word)
    if rc == 0 and a.push:
        br = a.branch or current_branch(repo)
        print(f"Pushing --force-with-lease origin {br} …")
        p = subprocess.run(["git", "push", "--force-with-lease", "origin", br],
                           cwd=repo, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
        print(p.stdout[-1000:] + p.stderr[-1000:])
        return p.returncode
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
