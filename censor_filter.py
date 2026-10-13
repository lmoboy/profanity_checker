#!/usr/bin/env python3
"""Helpers for `git filter-branch` tree/msg filters. Shares scrub.py rules:
censor flagged terms -> neutral word, skip binaries/symlinks/.git.
Usage (run with cwd = checked-out tree, as filter-branch does):
  python censor_filter.py --tree .     # censor files under DIR
  python censor_filter.py --msg        # stdin message -> stdout censored
  --word WORD --severe-only optional.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrub import censor_text, is_binary  # noqa: E402


def censor_tree(root: str, word: str, severe_only: bool) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
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


def censor_message(word: str, severe_only: bool) -> None:
    data = sys.stdin.read()
    sys.stdout.write(censor_text(data, severe_only, word))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", default=None)
    ap.add_argument("--msg", action="store_true")
    ap.add_argument("--word", default="test")
    ap.add_argument("--severe-only", action="store_true")
    a = ap.parse_args(argv)
    if a.tree is not None:
        censor_tree(a.tree, a.word, a.severe_only)
    elif a.msg:
        censor_message(a.word, a.severe_only)
    else:
        ap.error("need --tree DIR or --msg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
