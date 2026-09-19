"""devlore add — opt a codebase into this knowledge base.

The product's magic moment: point it at any repo you've been working on with
Claude Code or Codex and it (1) wires live capture for every future session, (2) finds
your PAST conversations for that codebase and offers to distill them into the
wiki (cost-gated batch backfill), (3) offers to ingest the repo's markdown docs,
then (4) briefs you on the knowledge that came out.

Usage:
    devlore add <codebase-path>                # interactive (the normal way)
    devlore add <codebase-path> --yes          # accept all defaults
    devlore add <codebase-path> --no-backfill --no-docs   # wiring only
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

KB = Path(__file__).resolve().parent.parent
SCRIPTS = KB / "scripts"


def _ask(prompt: str, default_yes: bool = True, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        ans = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default_yes
    if not ans:
        return default_yes
    return ans.startswith("y")


def _slug(path: Path) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", path.name.lower()).strip("-") or "code"


def _append_line(file: Path, line: str) -> bool:
    text = file.read_text(encoding="utf-8") if file.exists() else ""
    if line in text.splitlines():
        return False
    with open(file, "a", encoding="utf-8") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(line + "\n")
    return True


def wire(codebase: Path, no_hooks: bool = False) -> str:
    """Symlink + capture-roots + code-roots + (optionally) hooks. Idempotent.

    `no_hooks=True` performs every KB-LOCAL step and writes nothing whatsoever into
    the codebase. That is the snapshot case: ingest what already happened, leave the
    project alone. The opt-out is recorded in <kb>/scripts/no-hook-roots so a later
    `devlore update` does not retrofit the hooks this deliberately skipped.
    """
    from init_kb import link_name, merge_codebase_hooks, add_no_hook_root
    name = link_name(KB, codebase)
    link = KB / name
    if link.is_symlink() and link.resolve() == codebase.resolve():
        print(f"  · symlink already in place ({name})")
    elif link.exists() or link.is_symlink():
        sys.exit(f"error: {link} already exists and is not a symlink to this codebase —\n"
                 f"       remove it or rename the codebase directory, then re-run")
    else:
        link.symlink_to(codebase)
        print(f"  ✓ symlink: {name} → {codebase}")
    if _append_line(SCRIPTS / "capture-roots", f"{codebase}/"):
        print("  ✓ capture opt-in (sessions in this codebase flow into the KB)")
    else:
        print("  · already opted in (capture-roots)")
    if _append_line(SCRIPTS / "code-roots", name):
        print("  ✓ code root registered (staleness + symbol verification will scan it)")
    from utils import git_exclude
    if git_exclude(KB, name, add=True):
        print(f"  ✓ code link kept out of git ({name} → .git/info/exclude)")
    if no_hooks:
        add_no_hook_root(KB, codebase)
        print(f"  · no capture hooks written into {codebase.name} (--no-hooks) — "
              f"nothing outside the KB was modified")
        print(f"  ✓ recorded in scripts/no-hook-roots (devlore update will not retrofit them)")
    else:
        note = merge_codebase_hooks(codebase, KB, dry=False)
        print(f"  ✓ capture hooks for Claude Code + Codex in {codebase.name}: {note}")
    return name


def backfill(codebase: Path, assume_yes: bool) -> None:
    """Discover THIS codebase's past conversations and offer the gated backfill.

    Scoped to `codebase` (via ingest_all_context.py --root) so adding a repo
    backfills only its own history — not every other capture root in the KB.
    The standalone `devlore backfill` stays global for a deliberate full sweep."""
    print("\nLooking for PAST Claude Code and Codex conversations in this codebase to distill…")
    plan = subprocess.run(
        [sys.executable, str(SCRIPTS / "ingest_all_context.py"), "--root", str(codebase)],
        capture_output=True, text=True, cwd=str(KB))
    out = plan.stdout.strip()
    if "No candidate conversations" in out:
        print("  · none found (no transcripts for this codebase — capture starts with your next session)")
        return
    # The plan subprocess prints its own dry-run footer; misleading inside this flow.
    print("\n".join(l for l in out.splitlines() if not l.startswith("DRY RUN")))
    print("\nThe estimate above is a CEILING (compile parts are cheaper while the wiki is small).\n"
          "Note: a session still running right now is skipped (it's captured live from here on).")
    if not _ask("Run the backfill now?", default_yes=True, assume_yes=assume_yes):
        print("  · skipped — run `devlore backfill` any time")
        return
    subprocess.run(
        [sys.executable, str(SCRIPTS / "ingest_all_context.py"), "--yes", "--root", str(codebase)],
        cwd=str(KB))


def ingest_docs(codebase: Path, assume_yes: bool, full_recursive: bool = False,
                ignore_gitignore: bool = False, no_compile: bool = False) -> bool:
    """Offer the repo's human-written markdown docs.

    Candidates come from `git ls-files` (tracked + untracked-but-not-ignored)
    filtered through the vendored-tree deny-list and the per-directory tripwire
    (see utils.collect_markdown_docs). Default depth is root + first-level
    dirs; --full-recursive scans the whole tree; --ignore-gitignore also offers
    gitignored markdown. Every accepted file is compiled by the LLM — real cost
    — so the list is previewed and gated.
    """
    from utils import collect_markdown_docs
    candidates, excluded = collect_markdown_docs(codebase, recursive=full_recursive,
                                                 ignore_gitignore=ignore_gitignore)
    for top, n in sorted(excluded.items()):
        print(f"\n  ⚠ skipping {top}/ — {n} markdown files there looks like a vendored or"
              f"\n    generated tree; if it's really your writing, ingest it explicitly:"
              f"\n    devlore docs {codebase / top}")
    if not candidates:
        print("\nNo markdown docs found in the codebase to ingest."
              + ("" if full_recursive else "\n(only the root and first-level dirs are "
                 "scanned by default — `--full-recursive` scans the whole tree)")
              + ("" if ignore_gitignore else "\n(gitignored docs are skipped — "
                 "`--ignore-gitignore` offers those too)"))
        return False
    total_kb = sum(p.stat().st_size for p in candidates) / 1024
    scope = "whole tree" if full_recursive else "root + first-level dirs"
    gitness = "gitignored included" if ignore_gitignore else "git-aware"
    print(f"\nFound {len(candidates)} markdown doc(s) (~{total_kb:.0f} KB; {scope}, "
          f"{gitness}, vendored trees filtered):")
    for p in candidates[:12]:
        print(f"  - {p.relative_to(codebase)}")
    if len(candidates) > 12:
        print(f"  … and {len(candidates) - 12} more")
    if not _ask("Ingest these into the knowledge base?", default_yes=True, assume_yes=assume_yes):
        print("  · skipped — `devlore docs <file-or-dir>` any time")
        return False
    subprocess.run([sys.executable, str(SCRIPTS / "ingest_doc.py"),
                    *[str(p) for p in candidates]], cwd=str(KB))
    if no_compile:
        # `devlore snapshot` defers this so every directory's docs compile in ONE
        # pass at the end and can cite across each other — which is the whole point
        # of snapshotting them together. Only the DOC compile is deferrable: the
        # per-conversation compile inside backfill is load-bearing for the safety
        # gate (regression check + fabrication check + quarantine all run on its
        # output), so it cannot move.
        print("\n· compile deferred to the end of the snapshot (--no-compile)")
        return True
    print("\nCompiling into wiki articles (this is the LLM step — it can take a few minutes)…")
    subprocess.run([sys.executable, str(SCRIPTS / "compile.py")], cwd=str(KB))
    return True


def compile_pending() -> None:
    """Compile every pending/changed daily log into wiki articles NOW.

    Backfill already compiles the conversations it ingests, and doc-ingest
    compiles what it ingests — but live-captured daily entries (and any other
    uncompiled dailies) otherwise wait for the evening auto-compile or a manual
    `devlore compile`. `--compile-pending` flushes that whole backlog at add
    time. compile.py is incremental: already-compiled dailies are skipped, so
    this only does (and bills for) genuinely pending work."""
    print("\nCompiling all pending daily logs into wiki articles "
          "(LLM step — can take a few minutes; already-compiled dailies are skipped)…")
    subprocess.run([sys.executable, str(SCRIPTS / "compile.py")], cwd=str(KB))


def briefing(before_count: int) -> None:
    arts = list((KB / "knowledge").glob("*/*.md"))
    arts = [a for a in arts if a.parent.name in ("concepts", "connections", "qa", "mocs")]
    created = len(arts) - before_count
    qdir = KB / "quarantine"
    quarantined = sorted(qdir.glob("*.md")) if qdir.exists() else []
    print("\n" + "═" * 60)
    print(f"  Knowledge base: {len(arts)} article(s)"
          + (f"  (+{created} from this run)" if created > 0 else ""))
    print(f"  Catalog:        {KB / 'knowledge' / 'index.md'}")
    print(f"  History:        knowledge/log.md + git log (auto-committed)")
    if quarantined:
        print(f"  ⚠ Quarantined:  {len(quarantined)} conversation(s) failed a safety gate and were")
        print(f"                  rolled back — review quarantine/*.md, then `devlore backfill`")
        print(f"                  to retry (quarantined sessions are re-discovered automatically).")
    print("═" * 60)
    if arts:
        print(f"""
Try it:
  devlore ask "What are the key decisions in this project?"

From here, it compounds on its own: every Claude Code or Codex session you run inside
the codebase is captured and compiled automatically.

Optional: open  {KB}  as an Obsidian vault for the rendered
experience (graph view, clickable wikilinks, the devlore side panel).""")
    else:
        print("""
The knowledge base is wired but still empty — live capture is on for every
future Claude Code or Codex session in this codebase. To seed it now:
  devlore backfill            # retry past conversations
  devlore docs <file-or-dir>  # ingest existing markdown docs""")


def main() -> None:
    ap = argparse.ArgumentParser(description="Opt a codebase into this knowledge base.")
    ap.add_argument("codebase", help="Path to the codebase (e.g. `.` from inside it).")
    ap.add_argument("--yes", action="store_true", help="Accept all prompts.")
    ap.add_argument("--no-backfill", action="store_true")
    ap.add_argument("--no-docs", action="store_true")
    ap.add_argument("--no-hooks", action="store_true",
                    help="Register the codebase WITHOUT writing capture hooks into it. "
                         "Nothing outside the KB is modified: no .claude/settings.local.json, "
                         "no .codex/hooks.json. Historical backfill and doc ingest still run — "
                         "this is the one-time-snapshot mode. Recorded in scripts/no-hook-roots "
                         "so `devlore update` never retrofits them.")
    ap.add_argument("--full-recursive", action="store_true",
                    help="Scan the WHOLE tree for markdown docs, not just the root and "
                         "first-level dirs. The git/deny-list/tripwire filters still "
                         "apply, but review the preview — every accepted file is "
                         "compiled at real LLM cost.")
    ap.add_argument("--ignore-gitignore", action="store_true",
                    help="Also offer GITIGNORED markdown docs (research notes, drafts "
                         "kept out of the remote on purpose). The deny-list, depth and "
                         "tripwire filters still apply — review the preview.")
    ap.add_argument("--no-compile", action="store_true",
                    help="Ingest docs but do not compile them yet. `devlore snapshot` "
                         "uses this so every directory's docs compile together at the "
                         "end. The per-conversation compile inside backfill is NOT "
                         "affected — it gates quarantine and cannot be deferred.")
    ap.add_argument("--compile-pending", action="store_true",
                    help="After wiring/backfill/docs, compile ALL pending daily logs into "
                         "wiki articles now (incremental — skips already-compiled dailies), "
                         "instead of waiting for the evening auto-compile or `devlore compile`.")
    ap.add_argument("--kb", help="Operate on this KB instead of resolving the owner "
                                 "(bare `devlore` on PATH routes to the owning KB).")
    args = ap.parse_args()

    from utils import resolve_invocation_path
    codebase = resolve_invocation_path(args.codebase)
    if not codebase.is_dir():
        sys.exit(f"error: not a directory: {codebase}")

    from kb_resolve import resolve_or_redispatch
    # EVERY flag must be listed here: resolve_or_redispatch re-execs the owning
    # KB's launcher with exactly these args, so a flag omitted from this list is
    # silently dropped when the target belongs to another KB. --no-hooks is the
    # dangerous one — losing it writes capture hooks into a codebase the user
    # explicitly asked to leave untouched.
    fwd = [f for f, on in [("--yes", args.yes), ("--no-backfill", args.no_backfill),
                           ("--no-docs", args.no_docs), ("--no-hooks", args.no_hooks),
                           ("--full-recursive", args.full_recursive),
                           ("--ignore-gitignore", args.ignore_gitignore),
                           ("--compile-pending", args.compile_pending)] if on]
    resolve_or_redispatch("add", codebase, KB, fwd, args.kb, require_owner=True)

    if str(codebase) == "/" or not str(codebase).startswith(str(Path.home())):
        sys.exit(f"error: codebase must be inside your home directory: {codebase}")
    if codebase == KB or str(codebase).startswith(str(KB) + "/"):
        sys.exit("error: that's the knowledge base itself — point at a project codebase")
    if not re.compile(r'^[A-Za-z0-9_\-/.~ ]+$').match(str(codebase)):
        sys.exit(f"error: codebase path contains characters not safe for shell command embedding: {codebase}")


    before = len([a for a in (KB / "knowledge").glob("*/*.md")
                  if a.parent.name in ("concepts", "connections", "qa", "mocs")])

    print(f"Adding {codebase} to the knowledge base at {KB}\n")
    wire(codebase, no_hooks=args.no_hooks)
    if not args.no_backfill:
        backfill(codebase, args.yes)
    if not args.no_docs:
        ingest_docs(codebase, args.yes, args.full_recursive,
                    ignore_gitignore=args.ignore_gitignore, no_compile=args.no_compile)
    if args.compile_pending:
        compile_pending()
    briefing(before)


if __name__ == "__main__":
    main()
