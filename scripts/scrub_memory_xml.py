#!/usr/bin/env python3
"""Scrub XML-prompt-prefix garbage from MEMORY.md files (cleanup for #583).

Before PR #588, `host/container_runner.py:1083` wrote the host auto-write
fallback entry by slicing the raw XML prompt prefix:

    [YYYY-MM-DD] [auto] Task: <context timezone="UTC" /> <messages> <message
    sender="..." time="May DD, YYYY. Result: success.

These lines (a) pollute MEMORY.md for human readers and (b) feed XML noise
back into the next agent turn, which #586 identified as a trigger for the
introspection-OOM loop.  PR #588 fixed the writer; this script cleans up the
historic garbage that the writer already produced.

Usage:
    python -m scripts.scrub_memory_xml [--dry-run] [--no-backup] [PATH ...]

Default behaviour (no PATH args): scan every MEMORY.md under
`groups/**`.  PATH args may be files or glob patterns.

For each matching file:
  1. Read the file.
  2. Remove lines matching the #583 garbage pattern.
  3. If anything changed:
       - write a backup `<file>.bak.<unix-ts>` (unless --no-backup)
       - overwrite the original with the cleaned content
  4. Print a one-line summary: PATH: N lines removed.

Exit codes:
  0 — success (may have done nothing)
  2 — at least one input path could not be read
"""
from __future__ import annotations

import argparse
import glob
import re
import sys
import time
from pathlib import Path

# Matches one line of the form:
#   [YYYY-MM-DD] [auto] Task: <context ...
# Anchored at start-of-line, consumes through the next newline.  The trailing
# newline is included so the file does not gain a blank line after each
# scrubbed entry.
_GARBAGE_LINE_RE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2}\]\s+\[auto\]\s+Task:\s+<context[^\n]*\n",
    flags=re.MULTILINE,
)

# Default directory scanned when no PATH args are supplied.
_DEFAULT_ROOTS = ["groups/**/MEMORY.md"]


def scrub_text(src: str) -> tuple[str, int]:
    """Return (cleaned_text, lines_removed)."""
    cleaned, n = _GARBAGE_LINE_RE.subn("", src)
    return cleaned, n


def _resolve_paths(args: list[str]) -> list[Path]:
    if not args:
        # Repo-root-relative default: groups/*/MEMORY.md
        repo_root = Path(__file__).resolve().parent.parent
        out: list[Path] = []
        for pattern in _DEFAULT_ROOTS:
            out.extend(Path(p) for p in glob.glob(str(repo_root / pattern), recursive=True))
        return out

    resolved: list[Path] = []
    for arg in args:
        if any(c in arg for c in "*?["):
            resolved.extend(Path(p) for p in glob.glob(arg, recursive=True))
        else:
            resolved.append(Path(arg))
    return resolved


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        help="MEMORY.md paths or glob patterns. Default: groups/**/MEMORY.md",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip writing `<file>.bak.<ts>` backups.",
    )
    ns = parser.parse_args(argv)

    paths = _resolve_paths(ns.paths)
    if not paths:
        print("No MEMORY.md files matched.", file=sys.stderr)
        return 0

    rc = 0
    total_removed = 0
    for p in paths:
        try:
            src = p.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"{p}: read failed: {exc}", file=sys.stderr)
            rc = 2
            continue

        cleaned, removed = scrub_text(src)
        total_removed += removed
        if removed == 0:
            print(f"{p}: 0 lines removed (clean)")
            continue

        if ns.dry_run:
            print(f"{p}: would remove {removed} lines (dry-run)")
            continue

        if not ns.no_backup:
            backup = p.with_suffix(p.suffix + f".bak.{int(time.time())}")
            try:
                backup.write_text(src, encoding="utf-8", newline="")
            except OSError as exc:
                print(f"{p}: backup write failed: {exc}", file=sys.stderr)
                rc = 2
                continue

        try:
            p.write_text(cleaned, encoding="utf-8", newline="")
        except OSError as exc:
            print(f"{p}: write failed: {exc}", file=sys.stderr)
            rc = 2
            continue

        print(f"{p}: {removed} lines removed")

    if total_removed:
        print(f"Total: {total_removed} lines removed across {len(paths)} files.")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
