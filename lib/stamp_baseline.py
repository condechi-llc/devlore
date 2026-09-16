"""Stamp `valid_as_of` + `code_baseline` onto KB article frontmatter (PR D).

Anchors each article to its knowledge *vintage* (not the compile date) and the repo
SHAs that were HEAD then, so the Tier-2 staleness scan (scripts/staleness.py) can diff
precisely instead of over-flagging on perpetually-dirty hot files.

Two entry points:
  • main()            — one-time RETROFIT of existing articles with the per-class policy
                        (reconciled / backfill / real-time). Idempotent; re-runnable.
  • stamp_compiled()  — called by compile.py after a successful compile so NEW/updated
                        articles are born stamped (vintage = latest source-daily date,
                        baseline = HEAD; never regresses an existing vintage; only
                        touches unstamped or just-rewritten files).

Usage:  uv run python scripts/stamp_baseline.py [--dry-run]
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import code_repos  # noqa: E402 — scripts/code-roots drives the repo set

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = ROOT / "knowledge"
REPOS = code_repos()
# BACKUP_TAR / BACKFILL_VINTAGE / RECONCILED are intentionally absent from the
# distributed version of this file — they are private to the original author's KB
# and have no meaning for other users. The main() retrofit path below is likewise
# stripped from the distribution by build_dist.py.
BACKUP_TAR = Path.home() / ".devlore" / "backups" / "knowledge.tar"
BACKFILL_VINTAGE = ""   # set per-KB when running the one-time retrofit (main)
RECONCILED: set[str] = set()  # override per-KB when running main()

_daily = re.compile(r"daily/(\d{4}-\d{2}-\d{2})\.md")
_valid = re.compile(r"^valid_as_of:\s*(\S+)", re.M)


def _today() -> str:
    return datetime.now(timezone.utc).astimezone().date().isoformat()


def sha_at(repo: Path, date: str) -> str:
    """First-parent HEAD at end of <date> (the code state as of that day)."""
    out = subprocess.run(
        ["git", "-C", str(repo.resolve()), "rev-list", "-1", "--first-parent",
         f"--before={date} 23:59", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    return out[:12]


def head(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo.resolve()), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()[:12]


def repo_dirty(repo: Path) -> bool:
    return bool(subprocess.run(["git", "-C", str(repo.resolve()), "status", "--porcelain"],
                               capture_output=True, text=True).stdout.strip())


_pushed_cache: dict[tuple[str, str], bool] = {}


def repo_pushed(repo: Path, sha: str) -> bool:
    """True when `sha` is reachable from some REMOTE-tracking ref — i.e. PUBLISHED.

    A stamped SHA that exists only in this clone is useless to anyone else: the
    Tier-2 scan (scripts/staleness.py) diffs cited code against it, and on a machine
    that only ever pulled, that diff cannot be computed AT ALL. A repo with no
    remote, a SHA no remote branch contains, or any git failure all read as NOT
    pushed — the conservative direction, since the flag's whole point is certainty
    that the baseline is shared. Memoized: the retrofit asks about the same
    (repo, sha) once per article."""
    if not sha:
        return False
    key = (str(repo), sha)
    if key not in _pushed_cache:
        try:
            out = subprocess.run(
                ["git", "-C", str(repo.resolve()), "branch", "-r", "--contains", sha],
                capture_output=True, text=True, timeout=20)
            _pushed_cache[key] = out.returncode == 0 and bool(out.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            _pushed_cache[key] = False
    return _pushed_cache[key]


def _pushed_flag(shas: dict[str, str]) -> bool | None:
    """Global `pushed`, with the same all-or-nothing semantics as `dirty`: True only
    when EVERY root that contributed a SHA has that SHA published. Roots with no SHA
    (non-git, or a git call that failed) never appear in the map, so they cannot drag
    the flag down on their own. None — meaning "omit the key" — when NO root
    contributed a SHA: with nothing stamped there is no fact to assert either way."""
    contributing = {n: s for n, s in shas.items() if s and n in REPOS}
    if not contributing:
        return None
    return all(repo_pushed(REPOS[n], s) for n, s in contributing.items())


def latest_source_daily(text: str) -> str:
    dates = _daily.findall(text)
    return max(dates) if dates else ""


def _baseline_line(shas: dict[str, str], dirty: bool) -> str:
    """`code_baseline: { <name>: <sha>, …, dirty, pushed, stamped }` — one key per GIT
    code root (scripts/code-roots); non-git roots have no SHA and are simply omitted.

    `pushed` is derived from `shas` here rather than passed in, so every caller of
    apply_stamp (compile.py's stamp_compiled, recheck.py's re-stamp, the retrofit)
    emits it without a signature change. It is dropped entirely when no root
    contributed a SHA — see _pushed_flag."""
    pairs = "".join(f"{n}: {s}, " for n, s in shas.items() if s)
    pushed = _pushed_flag(shas)
    pub = "" if pushed is None else f"pushed: {str(pushed).lower()}, "
    return (f"code_baseline: {{ {pairs}"
            f"dirty: {str(dirty).lower()}, {pub}stamped: {_today()} }}")


def apply_stamp(text: str, vintage: str, shas: dict[str, str], dirty: bool) -> str | None:
    """Return `text` with valid_as_of/code_baseline (re)written, or None if no frontmatter."""
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    fm = parts[1]
    lines = [ln for ln in fm.splitlines()
             if not ln.startswith(("valid_as_of:", "code_baseline:"))]
    new = [f"valid_as_of: {vintage}", _baseline_line(shas, dirty)]
    out, inserted = [], False
    for ln in lines:
        out.append(ln)
        if ln.startswith("updated:") and not inserted:
            out.extend(new)
            inserted = True
    if not inserted:
        while out and not out[-1].strip():
            out.pop()
        out.extend(new)
    new_fm = "\n".join(out)
    if not new_fm.endswith("\n"):
        new_fm += "\n"
    head_sep = "" if new_fm.startswith("\n") else "\n"
    return parts[0] + "---" + head_sep + new_fm + "---" + parts[2]


def _articles():
    for sub in ("concepts", "connections", "qa", "mocs"):
        d = KNOWLEDGE / sub
        if d.exists():
            yield from sorted(d.glob("*.md"))


def stamp_compiled(changed: set[Path] | None = None) -> int:
    """Born-stamped pass for compile.py. Stamps an article when it is UNSTAMPED, or
    when it is in `changed` (just rewritten this compile). Vintage = max(existing,
    latest-source-daily) so it never regresses a manual override on an untouched file.
    `changed=None` means "consider every article" (used as a manual stopgap)."""
    head_shas = {n: head(r) for n, r in REPOS.items()}
    dirty = any(repo_dirty(r) for r in REPOS.values())
    n = 0
    for art in _articles():
        text = art.read_text(encoding="utf-8")
        has = _valid.search(text)
        touched = changed is None or art in changed
        if has and not touched:
            continue
        src = latest_source_daily(text) or _today()
        vintage = max(has.group(1)[:10], src) if has else src
        # baseline pinned to the vintage's code state (current HEAD when vintage==today)
        shas = head_shas if vintage >= _today() else {
            name: sha_at(repo, vintage) for name, repo in REPOS.items()}
        rebuilt = apply_stamp(text, vintage, shas, dirty if vintage >= _today() else False)
        if rebuilt and rebuilt != text:
            art.write_text(rebuilt, encoding="utf-8")
            n += 1
    return n


# ───────────────────────── one-time retrofit (main) ─────────────────────────

def _backfill_set() -> set[str]:
    import tarfile, tempfile
    if not BACKUP_TAR.exists():
        return set()
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(BACKUP_TAR) as t:
            t.extractall(td, filter="data")  # prevents path-traversal (zip-slip)
        bk = next(Path(td).rglob("concepts"), None)
        if not bk:
            return set()
        old = {p.stem for p in bk.parent.rglob("*.md")}
    cur = {p.stem for p in _articles()}
    return cur - old


def main():
    dry = "--dry-run" in sys.argv
    today = _today()
    head_shas = {n: head(r) for n, r in REPOS.items()}
    bf_shas = {n: sha_at(r, BACKFILL_VINTAGE) for n, r in REPOS.items()}
    bf = _backfill_set()
    print(f"backfill articles: {len(bf)} | HEAD "
          + " ".join(f"{n}@{s}" for n, s in head_shas.items())
          + " | backfill-baseline " + " ".join(f"{n}@{s}" for n, s in bf_shas.items()))
    print(f"{'DRY RUN — ' if dry else ''}retrofitting...\n")
    counts = {"reconciled": 0, "backfill": 0, "real-time": 0}
    for art in _articles():
        text = art.read_text(encoding="utf-8")
        stem = art.stem
        if stem in RECONCILED:
            vintage, shas, d, kind = today, head_shas, True, "reconciled"
        elif stem in bf:
            vintage, shas, d, kind = BACKFILL_VINTAGE, bf_shas, False, "backfill"
        else:
            vintage = latest_source_daily(text) or today
            shas = {n: sha_at(r, vintage) for n, r in REPOS.items()}
            d, kind = False, "real-time"
        rebuilt = apply_stamp(text, vintage, shas, d)
        if rebuilt is None:
            continue
        counts[kind] += 1
        shastr = " ".join(f"{n}@{s}" for n, s in shas.items())
        print(f"  {kind:<10} valid_as_of={vintage}  {shastr} dirty={d}  {stem}")
        if not dry:
            art.write_text(rebuilt, encoding="utf-8")
    print(f"\nstamped — reconciled:{counts['reconciled']} backfill:{counts['backfill']} "
          f"real-time:{counts['real-time']}")


if __name__ == "__main__":
    main()
