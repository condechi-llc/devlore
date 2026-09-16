"""Which knowledge base OWNS a codebase path? (devlore add/remove routing)

install.sh puts ONE KB's launcher on PATH, so a bare `devlore add .` always
landed in whichever KB installed the symlink — wrong the moment a second KB
exists. When the launcher is invoked THROUGH that symlink it exports
DEVLORE_VIA_PATH_SYMLINK=1, and add/remove resolve the owning KB here instead:
the most-specific match of the target path against each registered KB's
affinity roots (the KB directory itself, its capture roots, its code-root
symlink targets), asking when ambiguous. Invoking a launcher by full path
(<kb>/scripts/devlore add ...) still means "THIS kb", so scripted flows and
single-KB setups behave exactly as before.
"""

from __future__ import annotations

import os
import subprocess
import shutil
import sys
from pathlib import Path

from utils import kb_dirs_registry  # noqa: E402 — ~/.devlore/kb-dirs (migrates legacy)
VIA_SYMLINK_ENV = "DEVLORE_VIA_PATH_SYMLINK"

# A worktree target belongs to the project it is a worktree of.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
try:
    from capture_gate import resolve_worktree
except ImportError:  # standalone copy without hooks/ — degrade to identity
    def resolve_worktree(cwd: str) -> str:
        return os.path.normpath(cwd)


def registered_kbs(self_kb: Path) -> list[Path]:
    """KB roots from the registry (~/.devlore/kb-dirs) that still look like
    KBs, plus self."""
    kbs: list[Path] = []
    try:
        lines = kb_dirs_registry().read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = Path(line)
        if (p / "scripts").is_dir() and p not in kbs:
            kbs.append(p)
    if self_kb not in kbs:
        kbs.append(self_kb)
    return kbs


def _real(p) -> str:
    """Canonical comparison form: prefix matching is only sound when both sides
    have symlinks resolved (e.g. macOS /tmp and /var → /private/...)."""
    return os.path.realpath(str(p))


def affinity_roots(kb: Path) -> list[str]:
    """Paths that mark a codebase as belonging to this KB: the KB directory
    itself, every capture root, every code-root symlink target."""
    roots = [_real(kb)]
    try:
        for line in (kb / "scripts" / "capture-roots").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                roots.append(_real(line))
    except OSError:
        pass
    try:
        for line in (kb / "scripts" / "code-roots").read_text(encoding="utf-8").splitlines():
            name = line.strip()
            if not name or name.startswith("#"):
                continue
            link = kb / name
            if link.is_symlink():
                roots.append(_real(link))
    except OSError:
        pass
    return roots


def candidates(target: Path, self_kb: Path) -> list[tuple[Path, str]]:
    """(kb, its best-matching root) for every KB claiming the target,
    most-specific (longest root) first."""
    t = _real(resolve_worktree(str(target)))
    best: dict[Path, str] = {}
    for kb in registered_kbs(self_kb):
        for root in affinity_roots(kb):
            if t == root or t.startswith(root + os.sep):
                if len(root) > len(best.get(kb, "")):
                    best[kb] = root
    return sorted(best.items(), key=lambda kv: len(kv[1]), reverse=True)


def _choose(options: list[Path], target: Path, reason: str) -> Path:
    print(f"{reason} {target}:")
    for i, kb in enumerate(options, 1):
        print(f"  {i}. {kb}")
    if not sys.stdin.isatty():
        sys.exit("error: cannot prompt (no tty) — re-run with --kb <kb-path> to choose.")
    while True:
        try:
            ans = input(f"Which knowledge base? [1-{len(options)}] ").strip()
        except EOFError:
            sys.exit("error: no choice made — re-run with --kb <kb-path>.")
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1]


def resolve_or_redispatch(command: str, target: Path, self_kb: Path,
                          forward_args: list[str], kb_override: str | None,
                          require_owner: bool) -> None:
    """Decide the owning KB for `target`; re-exec that KB's launcher when it
    isn't self_kb (exits with the child's code). Returning means self_kb
    handles the command itself.

    require_owner: True for `add` (a target no KB claims must be assigned
    explicitly), False for `remove` (an unowned target is a harmless local
    no-op)."""
    if kb_override:
        owner = Path(kb_override).expanduser().resolve()
        from kb_registry import looks_like_kb
        if not looks_like_kb(owner):
            sys.exit(f"error: --kb {owner} does not look like a devlore KB")
    else:
        # Full-path invocation = explicit KB choice; bare `devlore` on PATH is
        # the only ambiguous form (the launcher exports the marker).
        if os.environ.get(VIA_SYMLINK_ENV) != "1":
            return
        kbs = registered_kbs(self_kb)
        if len(kbs) == 1:
            return  # single-KB setup: nothing to disambiguate
        cands = candidates(target, self_kb)
        if len(cands) == 1 or (len(cands) > 1 and len(cands[0][1]) > len(cands[1][1])):
            owner = cands[0][0]
        elif cands:
            tied = [kb for kb, root in cands if len(root) == len(cands[0][1])]
            owner = _choose(tied, target, "Multiple knowledge bases claim")
        elif require_owner:
            owner = _choose(kbs, target, "No knowledge base captures")
        else:
            return
    if owner.resolve() == self_kb.resolve():
        return
    print(f"→ routing to the owning KB: {owner}\n")
    env = dict(os.environ)
    env.pop(VIA_SYMLINK_ENV, None)  # the owner's launcher is invoked by full path
    # v0.9.27+: there is no per-KB launcher to exec. Route through the global one
    # and name the destination with --kb, which is exactly what it expects.
    launcher = Path.home() / ".devlore" / "bin" / "devlore"
    if not launcher.exists():
        found = shutil.which("devlore")
        if not found:
            sys.exit("error: cannot find the devlore launcher (~/.devlore/bin/devlore)")
        launcher = Path(found)
    rc = subprocess.run([str(launcher), command, str(target),
                         "--kb", str(owner), *forward_args], env=env).returncode
    sys.exit(rc)
