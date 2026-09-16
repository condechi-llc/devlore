"""
Opt a directory into the knowledge base in one command.

Capturing a directory needs two things:
  1. it must be listed in scripts/capture-roots (the gate), and
  2. the capture hooks must fire for sessions there — which, for a directory
     OUTSIDE the KB's own git project, means wiring the hooks into that
     project's own Claude Code and Codex hook config.

This script does both. Directories INSIDE the KB project are already covered
by its own hooks, so for those only step 1 is needed.

Usage:
    uv run python scripts/optin.py <directory> [--exact]

    <directory>   path to opt in (symlinks are resolved to their real path,
                  because the local agents use project roots for hook detection)
    --exact       capture ONLY that exact directory, not its subdirectories
                  (default is subtree: the directory and everything under it)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # the KB root
CAPTURE_ROOTS = ROOT / "scripts" / "capture-roots"

def add_to_capture_roots(real: Path, exact: bool) -> bool:
    """Append the path to capture-roots if not already present. Returns True if added."""
    entry = str(real) if exact else str(real) + "/"
    existing = CAPTURE_ROOTS.read_text(encoding="utf-8") if CAPTURE_ROOTS.exists() else ""
    present = {ln.strip().rstrip("/") for ln in existing.splitlines()
              if ln.strip() and not ln.strip().startswith("#")}
    if str(real) in present:
        return False
    with open(CAPTURE_ROOTS, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(entry + "\n")
    return True


def project_root_of(real: Path) -> Path:
    """The directory local coding agents treat as the project root."""
    try:
        out = subprocess.run(
            ["git", "-C", str(real), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return real


def wire_hooks(proj: Path) -> str:
    """Merge devlore hooks into Claude Code and Codex hook configs."""
    from init_kb import merge_codebase_hooks
    return merge_codebase_hooks(proj, ROOT, dry=False)


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--exact"]
    exact = "--exact" in sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    real = Path(args[0]).expanduser().resolve()
    if not real.is_dir():
        print(f"Error: not a directory: {real}")
        sys.exit(1)
    if not str(real).startswith(str(Path.home())):
        print(f"Error: path must be inside your home directory: {real}")
        sys.exit(1)
    import re as _re
    if not _re.match(r'^[A-Za-z0-9_\-/.~ ]+$', str(real)):
        print(f"Error: path contains characters not safe for command embedding: {real}")
        sys.exit(1)

    added = add_to_capture_roots(real, exact)
    print(f"{'++ added to' if added else '== already in'} capture-roots: "
          f"{real}{'' if exact else '/'}  ({'exact' if exact else 'subtree'})")

    inside_kb = real == ROOT or str(real).startswith(str(ROOT) + "/")
    if inside_kb:
        print("== inside the KB project: covered by its own hooks (no wiring needed)")
    else:
        try:
            from init_kb import is_no_hook_root
            opted_out = is_no_hook_root(ROOT, real)
        except Exception:
            opted_out = False
        if opted_out:
            # Registered content-only. Honour that here too: opting in from a second
            # entry point should not quietly reverse a deliberate opt-out.
            print("== content-only root (scripts/no-hook-roots): hooks NOT wired.\n"
                  "   Remove the entry from that file first if you want live capture here.")
        else:
            proj = project_root_of(real)
            print(wire_hooks(proj))

    print("\nDone. Start a NEW session in that directory for it to take effect.")


if __name__ == "__main__":
    main()
