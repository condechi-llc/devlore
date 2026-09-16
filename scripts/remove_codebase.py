"""devlore remove — stop capturing a codebase (the inverse of `devlore add`).

`devlore add` wires two INDEPENDENT links, and the removal modes map onto them:

  capture link   capture-roots entry + hooks in the codebase's
                 .claude/settings.local.json and .codex/hooks.json. NEW conversations flow into the
                 KB through this. Always removed by this command — the
                 confidential-work case needs nothing more.
  code link      symlink + code-roots entry. verify/staleness keep checking
                 the EXISTING articles against the code as it evolves. Kept by
                 default (recommended); removed with --full, after which the
                 knowledge about this codebase is a frozen, unverified
                 snapshot.

Knowledge is NEVER touched: every article and daily stays, and conversations
already captured remain. `devlore add` re-wires the codebase at any time.

Usage:
    devlore remove <codebase-path>           # interactive
    devlore remove <codebase-path> --yes     # stop capture, keep code link
    devlore remove <codebase-path> --full    # also disconnect the code link
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

KB = Path(__file__).resolve().parent.parent
SCRIPTS = KB / "scripts"
CLAUDE_HOOK_EVENTS = ("SessionStart", "PreCompact", "SessionEnd", "Stop")
CODEX_HOOK_EVENTS = ("SessionStart", "PreCompact", "Stop")


def _ask(prompt: str, default_yes: bool = True, assume: bool | None = None) -> bool:
    if assume is not None:
        return assume
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        ans = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default_yes
    return default_yes if not ans else ans.startswith("y")


def _strip_lines(file: Path, wanted: set[str]) -> int:
    """Remove exact lines from a roots file. Returns how many were removed."""
    if not file.exists():
        return 0
    lines = file.read_text(encoding="utf-8").splitlines()
    kept = [l for l in lines if l.strip() not in wanted]
    removed = len(lines) - len(kept)
    if removed:
        file.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return removed


def _unwire_hook_file(settings_path: Path, events: tuple[str, ...], label: str) -> None:
    if not settings_path.exists():
        print(f"  · no {label} hook config in the codebase (no hooks to remove)")
        return
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print(f"  ⚠ {settings_path} unreadable — remove the KB hooks manually")
        return
    # Identify this KB's hooks by (KB path + hooks/), not by a command prefix.
    # The prefix this used — `uv run --directory <KB> ` — is only v0.9.24's spelling;
    # v0.9.25 switched to `python3 <KB>/hooks/x.py` and v0.9.27 to the shared venv,
    # so `startswith` stopped matching anything and `devlore remove` silently left
    # every hook behind. Same defect as the one that made registration stack
    # generations, in the opposite direction.
    def _ours(cmd: str) -> bool:
        return str(KB) in cmd and "hooks/" in cmd

    hooks = settings.get("hooks", {})
    removed = []
    for ev in events:
        groups = hooks.get(ev, [])
        for g in groups:
            before = len(g.get("hooks", []))
            g["hooks"] = [h for h in g.get("hooks", [])
                          if not _ours(str(h.get("command", "")))]
            if len(g["hooks"]) < before:
                removed.append(ev)
        hooks[ev] = [g for g in groups if g.get("hooks")]
        if not hooks.get(ev):
            hooks.pop(ev, None)
    if not hooks:
        settings.pop("hooks", None)
    if removed:
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        print(f"  ✓ {label} hooks removed: " + ", ".join(removed))
    else:
        print(f"  · no {label} hooks of this KB in the codebase")


def unwire_capture(codebase: Path) -> None:
    """Remove the capture link: capture-roots entry + our hooks in the codebase."""
    n = _strip_lines(SCRIPTS / "capture-roots", {f"{codebase}/", str(codebase)})
    print(f"  {'✓ capture opt-out (capture-roots)' if n else '· not in capture-roots'}")
    _unwire_hook_file(codebase / ".claude" / "settings.local.json",
                      CLAUDE_HOOK_EVENTS, "Claude Code")
    _unwire_hook_file(codebase / ".codex" / "hooks.json",
                      CODEX_HOOK_EVENTS, "Codex")


def unwire_code(codebase: Path) -> None:
    """Remove the code link: the KB-root symlink + its code-roots entry."""
    names = [p.name for p in KB.iterdir()
             if p.is_symlink() and p.resolve() == codebase.resolve()]
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from utils import git_exclude
    for name in names:
        (KB / name).unlink()
        n = _strip_lines(SCRIPTS / "code-roots", {name})
        git_exclude(KB, name, add=False)
        print(f"  ✓ code link removed (symlink {name}"
              + (" + code-roots entry)" if n else "; no code-roots entry)"))
    if not names:
        print("  · no code link found for this codebase")
        return
    print("    ⚠ existing articles about this code are now a frozen snapshot —\n"
          "      Tier-1/Tier-2 verification can no longer check them against the code.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Stop capturing a codebase (knowledge is kept).")
    ap.add_argument("codebase", help="Path to the codebase (e.g. `.` from inside it).")
    ap.add_argument("--yes", action="store_true",
                    help="No prompts: stop capture, keep the code link (recommended).")
    ap.add_argument("--full", action="store_true",
                    help="Also disconnect the code link — existing articles become a "
                         "frozen snapshot that verify/staleness can no longer check.")
    ap.add_argument("--kb", help="Operate on this KB instead of resolving the owner "
                                 "(bare `devlore` on PATH routes to the owning KB).")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from utils import resolve_invocation_path
    codebase = resolve_invocation_path(args.codebase)

    from kb_resolve import resolve_or_redispatch
    fwd = [f for f, on in [("--yes", args.yes), ("--full", args.full)] if on]
    resolve_or_redispatch("remove", codebase, KB, fwd, args.kb, require_owner=False)

    print(f"Removing {codebase} from capture for the KB at {KB}\n")

    unwire_capture(codebase)

    if args.full:
        drop_code = True
    elif args.yes:
        drop_code = False
    else:
        print("\nThe code link is still in place: verify/staleness keep checking the\n"
              "EXISTING articles about this code as it evolves (recommended).\n"
              "Disconnecting it freezes that knowledge as an unverified snapshot.")
        drop_code = _ask("Disconnect the code link too?", default_yes=False)
    if drop_code:
        unwire_code(codebase)
    else:
        print("  · code link kept — existing articles stay verified against this code\n"
              "    (devlore remove --full to disconnect that too)")

    subprocess.run(["git", "-C", str(KB), "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", str(KB), "commit", "-q", "-m",
                    f"remove: capture off for {codebase.name}"
                    + (" (+ code link)" if drop_code else "")], capture_output=True)

    print(f"""
Your knowledge is untouched: every article and daily stays in the KB, and
conversations already captured remain — only NEW sessions stop flowing in.
Re-wire any time:  devlore add {codebase}""")


if __name__ == "__main__":
    main()
