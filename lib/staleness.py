"""Tier-2 deterministic staleness scan (PR D).

Flags compiled KB articles whose cited code has changed since the article's
knowledge *vintage*. Anchors to per-article `valid_as_of` + `code_baseline` (the
repo SHAs that were HEAD at that vintage) when present — so a file change counts
only if it postdates the baseline, not the compile date. Falls back to a coarse
`updated:`-date heuristic for unstamped articles. Pure git + grep, NO LLM.

Reused by hooks/session-start.py (the nudge) and (later) scripts/verify.py.
Preview:  uv run python scripts/staleness.py
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import code_repos  # noqa: E402 — scripts/code-roots drives the repo set

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = ROOT / "knowledge"
REPOS = code_repos()
CODE_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".py"}
LOG_WINDOW = "180 days ago"  # fallback heuristic only

_fileref = re.compile(r"`[^`\n]*?([\w.\-/]+\.(?:js|jsx|ts|tsx|mjs|py))(?::\d+)?[^`\n]*`")
_updated = re.compile(r"^updated:\s*(\S+)", re.M)
_valid = re.compile(r"^valid_as_of:\s*(\S+)", re.M)
_baseline = re.compile(r"^code_baseline:\s*\{([^}]*)\}", re.M)
# The key is anchored to a brace/comma/space boundary and escaped. Interpolated raw
# and unanchored (as this was), any key that is a SUFFIX of another matches inside it:
# a code root named `irty` already resolved to `dirty`'s value, and `pushed` — added
# to the baseline in v0.9.28 — extends that to `ushed`, `shed` and `ed`. Code-root
# names come from directory names, so this is reachable, not theoretical.
_kv = lambda blob, k: (re.search(rf"(?:^|[{{,\s]){re.escape(k)}:\s*([^\s,}}]+)", blob)
                       or [None, None])[1]


def _git(repo: Path, *args: str, timeout: int = 20) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def _repo_state(repo: Path):
    """(dirty: {basename: Path}, present: set[basename], changed_since(sha)->set,
    log_changed: {basename: iso_date})  — for one repo. Lazy per-baseline diff cache."""
    rp = repo.resolve()
    dirty: dict[str, Path] = {}
    for ln in _git(rp, "status", "--porcelain").splitlines():
        p = ln[3:].strip().strip('"')
        if Path(p).suffix in CODE_EXT:
            dirty[Path(p).name] = rp / p
    present = {Path(p).name for p in _git(rp, "ls-files").splitlines()
               if Path(p).suffix in CODE_EXT} | set(dirty)
    # fallback heuristic data: basename -> most-recent commit date
    log_changed: dict[str, str] = {}
    cur = None
    for ln in _git(rp, "log", f"--since={LOG_WINDOW}", "--name-only", "--pretty=format:@%cI").splitlines():
        if ln.startswith("@"):
            cur = ln[1:11]
        elif ln.strip() and Path(ln).suffix in CODE_EXT and cur:
            b = Path(ln).name
            if b not in log_changed or cur > log_changed[b]:
                log_changed[b] = cur

    cache: dict[str, set[str]] = {}

    def changed_since(sha: str) -> set[str]:
        if sha not in cache:
            out = _git(rp, "diff", "--name-only", f"{sha}..HEAD")
            cache[sha] = {Path(p).name for p in out.splitlines() if Path(p).suffix in CODE_EXT}
        return cache[sha]

    return dirty, present, changed_since, log_changed


def _mtime_date(p: Path) -> str:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).date().isoformat()
    except OSError:
        return "9999-99-99"


def stale_risk() -> list[tuple[str, list[str]]]:
    """[(article_stem, [reason, ...]), ...] for articles citing changed code."""
    if not KNOWLEDGE.exists():
        return []
    state = {}
    for name, repo in REPOS.items():
        rp = repo.resolve()
        if rp.exists() and (rp / ".git").exists():
            state[name] = _repo_state(rp)
    if not state:
        return []

    out: list[tuple[str, list[str]]] = []
    for sub in ("concepts", "connections", "qa", "mocs"):
        d = KNOWLEDGE / sub
        if not d.exists():
            continue
        for art in sorted(d.glob("*.md")):
            body = art.read_text(encoding="utf-8", errors="ignore")
            cited = {Path(x).name for x in _fileref.findall(body)}
            if not cited:
                continue
            vm = _valid.search(body)
            valid_as_of = vm.group(1)[:10] if vm else None
            bm = _baseline.search(body)
            sha = {name: _kv(bm.group(1), name) for name in state} if bm else None
            um = _updated.search(body)
            updated = um.group(1)[:10] if um else "0000-00-00"

            reasons: list[str] = []
            for name, (dirty, present, changed_since, log_changed) in state.items():
                # sorted: set iteration order varies per process (hash randomization);
                # stable reason order keeps the recheck loop's marker idempotent.
                for b in sorted(cited):
                    if b not in present:
                        continue
                    if sha and valid_as_of and sha.get(name):
                        # PRECISE: committed since the article's baseline SHA, or a
                        # dirty file touched after the article's vintage.
                        if b in changed_since(sha[name]):
                            reasons.append(f"{name}/{b} (committed since baseline)")
                        elif b in dirty and _mtime_date(dirty[b]) > valid_as_of:
                            reasons.append(f"{name}/{b} (uncommitted, edited {_mtime_date(dirty[b])})")
                    else:
                        # FALLBACK (unstamped): coarse updated-date heuristic.
                        if b in dirty:
                            reasons.append(f"{name}/{b} (uncommitted, unstamped)")
                        elif b in log_changed and log_changed[b] > updated:
                            reasons.append(f"{name}/{b} (committed {log_changed[b]}, unstamped)")
            if reasons:
                seen: set[str] = set()
                out.append((art.stem, [r for r in reasons if not (r in seen or seen.add(r))]))
    return out


def staleness_note(limit: int = 14) -> str:
    """A compact, injectable nudge (or '' when nothing is stale-risk)."""
    risks = stale_risk()
    if not risks:
        return ""
    lines = [
        "## ⚠ KB code-parity check (Tier-2, deterministic — PR D)",
        f"{len(risks)} article(s) cite code that changed since their knowledge vintage "
        "(`valid_as_of` / `code_baseline`). Their **code-level** claims may be stale — "
        "verify against the working tree (run `/verify`) before relying on them. "
        "Conceptual/business claims are unaffected.",
    ]
    for stem, reasons in sorted(risks)[:limit]:
        lines.append(f"- **{stem}** — {', '.join(reasons[:4])}")
    if len(risks) > limit:
        lines.append(f"- …and {len(risks) - limit} more")
    return "\n".join(lines)


if __name__ == "__main__":
    note = staleness_note()
    print(note if note else "no stale-risk articles (all cited code unchanged since each article's vintage)")
