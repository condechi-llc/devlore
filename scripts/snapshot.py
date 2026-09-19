#!/usr/bin/env python3
"""One-shot KB from directories you do NOT want to instrument (devlore snapshot).

The normal `devlore add` flow assumes an ongoing relationship: it registers the
codebase AND wires capture hooks into it, so future sessions keep flowing in.
A snapshot is the opposite intent — compile what already happened, once, and leave
the project exactly as it was found. Nothing outside the new KB is written.

    devlore snapshot ~/Code/foo-kb --code ~/Code/repo-a --code ~/Code/repo-b

Without --yes this is a DRY RUN: every step reports what it would ingest and what
it would cost, and no LLM call is made. Re-run with --yes to execute.

Composition, not reimplementation: this drives `init_kb.py` and `add_codebase.py
--no-hooks` and then compiles once at the end. The per-directory work — historical
backfill of stored Claude/Codex sessions, markdown doc ingest — already lives in
add_codebase and is reused verbatim, so the snapshot path cannot drift from the
normal path.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# The shared modules live at ~/.devlore/lib/ and the launcher normally puts them on
# PYTHONPATH before running us. Normally is not always: `python3 snapshot.py` from a
# shell arrives with nothing, and then every child we spawn inherits that nothing and
# dies on `import utils`. Bootstrap the path ourselves so the orchestration works
# however it was started.
# Two layouts must both work: an installed KB (<kb>/scripts/ here, shared lib at
# ~/.devlore/lib/) and an unpacked dist run in place (scripts/ and lib/ siblings),
# which is how a first install runs before ~/.devlore/lib exists at all.
_LIB: Path | None = None
for _cand in (Path.home() / ".devlore" / "lib", SCRIPTS.parent / "lib", SCRIPTS):
    if (_cand / "utils.py").is_file():
        _LIB = _cand
        break
if _LIB is not None:
    sys.path.insert(0, str(_LIB))
from utils import devlore_python, devlore_child_env  # noqa: E402


def _run(argv: list[str], kb: Path | None) -> int:
    """Spawn a sibling script under the shared venv with a correct environment.

    Never inherit DEVLORE_KB_ROOT here: it names whatever KB the launcher resolved
    from the cwd, and the entire point of a snapshot is a KB that did not exist a
    moment ago. Every step is pinned to the new one explicitly.
    """
    env = devlore_child_env(kb) if kb is not None else devlore_child_env(SCRIPTS.parent)
    # devlore_child_env points PYTHONPATH at the INSTALLED lib. When we resolved the
    # shared modules somewhere else (an unpacked dist, before install), the children
    # must be told the same place or they fail the import we just succeeded at.
    if _LIB is not None and str(_LIB) not in env.get("PYTHONPATH", ""):
        env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    # We invoke every child by full path with an explicit --kb, which is the
    # UNAMBIGUOUS form. The marker means "the user typed a bare `devlore` and we
    # had to guess which KB they meant"; inherited here it makes kb_resolve try to
    # re-route an already-decided call back through the launcher.
    env.pop("DEVLORE_VIA_PATH_SYMLINK", None)
    if kb is None:
        # init_kb creates the KB; there is no KB root to name yet, and leaving a
        # stale inherited one would point the child at somebody else's KB.
        env.pop("DEVLORE_KB_ROOT", None)
    return subprocess.run([devlore_python(), *argv], env=env).returncode


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build a knowledge base from directories WITHOUT instrumenting them.")
    ap.add_argument("kb_dir", help="Directory for the new KB (created; must not be a non-empty dir).")
    ap.add_argument("--code", action="append", default=[], required=True, metavar="DIR",
                    help="Directory to snapshot (repeatable).")
    ap.add_argument("--full-recursive", action="store_true",
                    help="Scan the WHOLE tree for markdown docs, not just the root and "
                         "first-level dirs. Every accepted file is compiled at real cost.")
    ap.add_argument("--with-obsidian", action="store_true",
                    help="Also install the optional Obsidian layer in the new KB.")
    ap.add_argument("--yes", action="store_true",
                    help="Execute. Without this the run is a dry run: plan + cost estimate only.")
    args = ap.parse_args()

    kb = Path(args.kb_dir).expanduser().resolve()
    codes = [Path(c).expanduser().resolve() for c in args.code]

    missing = [c for c in codes if not c.is_dir()]
    if missing:
        sys.exit("error: not a directory: " + ", ".join(str(m) for m in missing))

    mode = "EXECUTE" if args.yes else "DRY RUN (nothing is spent; add --yes to execute)"
    print(f"devlore snapshot — {mode}")
    print(f"  knowledge base: {kb}")
    for c in codes:
        print(f"  snapshot of:    {c}")
    print("  capture hooks:  NOT installed — these projects are left untouched\n")

    if kb.exists() and any(kb.iterdir()):
        sys.exit(f"error: {kb} exists and is not empty — pick a new directory")

    # 1. The KB itself. No --code here: passing codebases to init_kb would wire
    #    hooks into them, which is precisely what a snapshot must not do. They are
    #    attached in step 2 with the opt-out recorded.
    init = [str(SCRIPTS / "init_kb.py"), str(kb)]
    if args.with_obsidian:
        init.append("--with-obsidian")
    if not args.yes:
        init.append("--dry-run")
    if (rc := _run(init, None)) != 0:
        sys.exit(f"error: init_kb failed (exit {rc}) — nothing else attempted")

    if not args.yes:
        # init_kb --dry-run creates nothing, so there is no KB for add_codebase to
        # attach to. Report honestly rather than emitting a misleading per-directory
        # estimate against a KB that does not exist.
        print("\nDry run: the KB was not created, so per-directory backfill estimates")
        print("were not computed.")
        print("\n--yes is UNATTENDED: it builds the KB and then ingests every")
        print("directory's conversations and markdown without stopping to ask. Each")
        print("one prints its own cost estimate as it goes, but nothing waits for")
        print("confirmation — the only way to stop it is to interrupt a running")
        print("compile. To see what a directory would cost before committing to the")
        print("whole run, add it on its own:")
        print("    devlore add <dir> --no-hooks          # prompts before spending")
        return

    # 2. Attach each directory: symlink, capture-roots, code-roots — then historical
    #    backfill and markdown docs. --no-hooks is what keeps the project untouched
    #    and records the opt-out so `devlore update` never retrofits it.
    for c in codes:
        print(f"\n{'=' * 60}\n{c.name}\n{'=' * 60}")
        # The new KB's OWN machinery, not ours. Passing `--kb` to this KB's copy
        # would be the ambiguous form kb_resolve exists to disambiguate: it would
        # announce a route to the owning KB and hand off, which is pointless when
        # we can simply run the owner's script. Post-init the new KB has a complete
        # scripts/ tree, so it operates on itself and no routing occurs.
        # --no-compile defers each directory's DOC compile so they all run in the
        # single pass below and can cite across each other. The per-conversation
        # compile inside backfill is NOT deferred and cannot be: the regression
        # check, the fabrication gate and quarantine all run on its output, so
        # moving it would disable the safety gate. Conversations therefore still
        # compile in ingest order, and the first directory still sees the smallest
        # wiki — that asymmetry is inherent, not something the final pass fixes.
        add = [str(kb / "scripts" / "add_codebase.py"), str(c),
               "--no-hooks", "--yes", "--no-compile"]
        if args.full_recursive:
            add.append("--full-recursive")
        if (rc := _run(add, kb)) != 0:
            sys.exit(f"error: adding {c} failed (exit {rc}) — "
                     f"the KB at {kb} keeps whatever completed before this point")

    # 3. One compile at the end for every directory's DOCS, so articles drawn from
    #    them can cite across all of them — the reason for snapshotting together.
    #    Until --no-compile existed this pass always found nothing to do, because
    #    each add had already compiled its own.
    print(f"\n{'=' * 60}\nCompiling\n{'=' * 60}")
    if (rc := _run([str(kb / "scripts" / "compile.py")], kb)) != 0:
        sys.exit(f"error: compile failed (exit {rc}) — daily logs are intact; "
                 f"re-run `devlore compile` against {kb}")

    print(f"\n✓ snapshot complete: {kb}")
    print(f"  devlore use {kb.name}   # make it the active KB")


if __name__ == "__main__":
    main()
