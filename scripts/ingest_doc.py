"""
Ingest hand-written doc file(s) into the knowledge flush path.

Appends each file's full content to today's daily log under a dated
"Doc Ingest: <name>" section, so compile.py turns it into wiki articles
(and supersedes any decisions it revises). Unlike the conversation flush,
this does NOT pre-summarize — the daily log is source material, and compile
does the extraction, so your careful docs reach the wiki at full fidelity.

Usage:
    uv run python scripts/ingest_doc.py <file.md | dir> [more ...]
                                        [--full-recursive] [--ignore-gitignore]

A directory argument is scanned for human-written markdown via
utils.collect_markdown_docs: git-aware (tracked + untracked-but-not-ignored),
vendored trees deny-listed at any depth, root + first-level subdirs by default
(--full-recursive for the whole tree), with a tripwire on directories that
contribute suspiciously many files. Explicitly named FILES bypass all filters.

--ignore-gitignore widens the git gate to gitignored markdown too — for
research notes and drafts kept out of the remote on purpose. The deny-list,
depth, size and tripwire gates still apply.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "daily"
SCRIPTS = ROOT / "scripts"


def source_project(path: Path) -> str:
    """Which registered codebase does this file belong to? ("" when none does.)

    A single daily receives markdown from every codebase a snapshot touches, so
    the compiler cannot tell them apart from the file alone — that is how one
    repo's slug came to claim 97 of 98 articles. Longest matching code-root wins,
    so a repo nested inside another is attributed to the nearer one.
    """
    try:
        names = [n.strip() for n in (SCRIPTS / "code-roots").read_text(encoding="utf-8").splitlines()
                 if n.strip() and not n.strip().startswith("#")]
    except OSError:
        return ""
    target = os.path.realpath(path)
    best, best_len = "", -1
    for name in names:
        root = ROOT / name
        try:
            r = os.path.realpath(root)
        except OSError:
            continue
        if (target == r or target.startswith(r + os.sep)) and len(r) > best_len:
            best, best_len = name, len(r)
    return best


def append_to_daily(content: str, section: str, project: str = "") -> Path:
    """Append a section to today's daily log (same format as flush.py)."""
    today = datetime.now(timezone.utc).astimezone()
    log_path = DAILY_DIR / f"{today.strftime('%Y-%m-%d')}.md"
    if not log_path.exists():
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"# Daily Log: {today.strftime('%Y-%m-%d')}\n\n## Sessions\n\n## Memory Maintenance\n\n",
            encoding="utf-8",
        )
    # Same body-tag convention flush.py uses: the source project goes in the
    # entry BODY, never the header, because compile splits on the header and
    # flush hashes it. compile reads this tag as authoritative for `project:`.
    try:
        from utils import redact_secrets, redaction_note
        content, _found = redact_secrets(content)
        if _found:
            print(f"  ⚠ {redaction_note(_found)}")
    except Exception as _e:
        print(f"  · redaction skipped ({_e})")
    tag = f"**Project:** {project}\n\n" if project else ""
    entry = f"### {section} ({today.strftime('%H:%M')})\n\n{tag}{content}\n\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)
    return log_path


def main() -> None:
    full_recursive = "--full-recursive" in sys.argv[1:]
    ignore_gitignore = "--ignore-gitignore" in sys.argv[1:]
    args = [a for a in sys.argv[1:]
            if a not in ("--", "", "--full-recursive", "--ignore-gitignore")]
    if not args:
        print("Usage: ingest_doc.py <file.md | dir> [more ...] "
              "[--full-recursive] [--ignore-gitignore]")
        sys.exit(1)

    # Expand args into a flat list of files (a directory -> filtered doc scan;
    # explicitly named files bypass all filters).
    from utils import collect_markdown_docs
    files: list[Path] = []
    for arg in args:
        # Tolerate a leading @ (from "/devlore @file.md" style references).
        raw = arg[1:] if arg.startswith("@") else arg
        p = Path(raw)
        if not p.is_absolute():
            p = Path.cwd() / p
        if p.is_dir():
            md, excluded = collect_markdown_docs(p, recursive=full_recursive,
                                                 ignore_gitignore=ignore_gitignore)
            for top, n in sorted(excluded.items()):
                print(f"SKIP (vendored-tree smell, {n} files): {arg}/{top}/ — "
                      f"pass that directory explicitly to ingest it")
            if not md:
                hints = ([] if full_recursive else ["--full-recursive scans deeper"]) \
                    + ([] if ignore_gitignore else ["--ignore-gitignore includes gitignored docs"])
                print(f"SKIP (no ingestable *.md in dir): {arg}"
                      + (f"  ({'; '.join(hints)})" if hints else ""))
            files.extend(md)
        elif p.exists():
            files.append(p)
        else:
            print(f"SKIP (not found): {arg}")

    ingested: list[str] = []
    for p in files:
        content = p.read_text(encoding="utf-8").strip()
        if not content:
            print(f"SKIP (empty): {p.name}")
            continue
        log_path = append_to_daily(content, f"Doc Ingest: {p.name}", source_project(p))
        ingested.append(p.name)
        print(f"Ingested {p.name} ({len(content)} chars) -> daily/{log_path.name}")
        from activity import emit
        emit("ingest", "doc", f"ingested {p.name} ({len(content)} chars)")

    if ingested:
        print(f"\n{len(ingested)} doc(s) added to the flush path. "
              f"Run compile.py to publish them to the wiki.")
    else:
        print("\nNothing ingested.")
        sys.exit(1)


if __name__ == "__main__":
    main()
