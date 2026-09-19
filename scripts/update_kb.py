"""devlore update — refresh this KB's MACHINERY from the distribution.

Closes the drift gap: installed KBs are snapshots of the machinery at install
time; fixes shipped upstream never reached them except by hand-copying. This
re-materializes the machinery from the dist (pulling the latest) while NEVER
touching what makes the KB *yours*:

  preserved: knowledge/, daily/, quarantine/, scripts/state.json,
             scripts/capture-roots, scripts/code-roots, flush markers, logs,
             your git history
  updated:   every machinery file present in the dist — KB-local scripts,
             hooks, AGENTS.md, .claude/commands + settings.json, uv.lock,
             .gitignore, and (only if this KB has an .obsidian/) the plugin.
             The shared machinery (config, utils, kb_*, capture_config,
             transcripts, activity, kb_commit, staleness, stamp_baseline)
             lives at ~/.devlore/lib/ and is installed once for all KBs
             (see --to-new-layout for the v0.9.24→v0.9.25 migration).

Usage:
    devlore update                       # pull latest dist, update this KB
    devlore update --from <dist-path>    # update from a local dist/clone
    devlore update --to-new-layout       # one-shot v0.9.24→v0.9.25 migration
    python3 update_kb.py --kb <kb-path>  # update ANOTHER KB (bootstrap case:
                                         #   the target predates this command)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_CACHE = Path.home() / ".devlore" / "dist"
REPO = "https://github.com/condechi/devlore.git"
# Split literal: this file is itself materialized by the very rewrite that
# resolves the placeholder — an intact literal here would be rewritten to the
# KB path, corrupting the constant and leaving placeholders unresolved on the
# NEXT update (same in-band-sentinel class as the flush FLUSH_OK bug).
PLACEHOLDER = "__DEVLORE" + "_HOME__"
# Both placeholders are built from halves so they do NOT appear verbatim in this
# file. _rewrite runs over the payload, and update_kb.py IS payload: a literal
# token here gets substituted when this file is installed into a KB, leaving the
# installed copy with a hardcoded path where the token belongs. That silently
# breaks the next install (the token stops being replaced) and makes the file
# differ on every update. The HOME token was already split for this reason; the
# BIN_DIR token added in v0.9.27 was not, and corrupted both this file and the
# Obsidian plugin's allowlist in alternation.
BIN_PLACEHOLDER = "__DEVLORE" + "_BIN_DIR__"

# Machinery surfaces re-materialized from the dist. scripts/ and hooks/ copy
# every file the DIST ships (the dist contains only machinery), so KB-local
# runtime files (state.json, markers, capture-roots, code-roots, logs) are
# never listed in the dist and therefore never touched.
SURFACES = ["scripts", "hooks", ".claude/commands"]
# v0.9.27: uv.lock is no longer per-KB (single shared venv at ~/.devlore/.venv/);
# pyproject.toml ships as a no-deps stub from dist-assets/kb-pyproject.toml and
# intentionally NOT in ROOT_FILES so a future path-preserving rewrite doesn't
# clobber user's hand-edits.
ROOT_FILES = ["AGENTS.md", ".gitignore",
              ".claude/settings.json", "VERSION"]
# pyproject.toml ships as a stub (per-KB; deps live in ~/.devlore/lib/) — see
# _migrate_to_shared_layout. Not in ROOT_FILES so it doesn't get overwritten
# from the dist (the dist's pyproject.toml IS the stub — see build_dist.py).


def _rewrite(text: str, src: Path, kb: Path) -> str:
    # v0.9.27+: resolve the BIN_DIR placeholder to ~/.devlore/bin (the Obsidian
    # plugin's allowlist constants point at the centralized bin shims, not at the
    # KB path). Both tokens are spelled in halves — see BIN_PLACEHOLDER.
    return (text.replace(BIN_PLACEHOLDER, str(Path.home() / ".devlore" / "bin"))
                .replace(PLACEHOLDER, str(kb))
                .replace(str(src), str(kb)))


def _rewire_own_codex_hooks(kb: Path) -> None:
    """Keep the KB's OWN .codex/hooks.json current.

    Claude Code needs nothing here: the KB's .claude/settings.json is
    re-materialized from the dist on every update. Codex has no equivalent, and
    _rewire_capture_hooks — the only code that ever rewrites a hook command —
    skips everything inside the KB, so this file was unreachable and stayed on the
    pre-v0.9.25 `uv run --directory` form, which rebuilds the per-KB venv that
    v0.9.27 removed.

    This must NOT be folded into the capture-roots loop: a KB only appears in its
    own capture-roots if the user opted its root in, which four of five did not.
    Making the repair conditional on that is how the first attempt at this fix
    silently did nothing for them.
    """
    try:
        import sys
        sys.path.insert(0, str(kb / "scripts"))
        from init_kb import merge_codex_hooks
        note = merge_codex_hooks(kb, kb, dry=False)
        print(f"  ✓ this KB's own Codex capture hooks: {note}")
    except Exception as e:
        print(f"  ⚠ could not wire this KB's own Codex hooks: {e}")


def _rewire_capture_hooks(kb: Path) -> None:
    """Re-register capture hooks in every EXTERNAL captured project's agent config.

    `devlore update` re-materializes the KB's own machinery + its own
    .claude/settings.json, but captured projects OUTSIDE the KB keep their hook
    wiring in their own settings.local.json / .codex/hooks.json — written once at
    opt-in time. A NEW hook event (e.g. the Stop bootstrap hook) would never reach
    them without this. The merge is idempotent: it only adds missing events and
    never clobbers existing settings."""
    cr = kb / "scripts" / "capture-roots"
    if not cr.exists():
        return
    sys.path.insert(0, str(kb / "scripts"))
    try:
        from optin import project_root_of
        from init_kb import merge_codebase_hooks, merge_codex_hooks, is_no_hook_root
    except Exception as e:
        print(f"  ⚠ could not load hook-rewire helpers: {e}")
        return
    seen: set[Path] = set()
    for line in cr.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        d = Path(line.rstrip("/"))
        if d == kb or str(d).startswith(str(kb) + "/"):
            continue  # handled above, unconditionally — see _rewire_own_codex_hooks
        if not d.is_dir():
            continue
        # Content-only roots (`devlore add --no-hooks`, `devlore snapshot`) are
        # registered for backfill but must never receive hooks. Without this check
        # the rewire sweep — whose whole job is retrofitting hooks onto capture-roots
        # entries — would silently undo that choice on the next update, which is
        # exactly the failure a user opting out is trying to avoid.
        if is_no_hook_root(kb, d):
            print(f"  · {d.name}: content-only root, hooks deliberately not wired")
            continue
        proj = project_root_of(d)
        if proj in seen:
            continue
        seen.add(proj)
        try:
            note = merge_codebase_hooks(proj, kb, dry=False)
            print(f"  ✓ rewired hooks for {proj.name}: {note}")
        except Exception as e:
            print(f"  ⚠ could not rewire {proj}: {e}")


def _copy(src_file: Path, dst_file: Path, src: Path, kb: Path) -> None:
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        dst_file.write_text(_rewrite(src_file.read_text(encoding="utf-8"), src, kb),
                            encoding="utf-8")
        dst_file.chmod(src_file.stat().st_mode)
    except UnicodeDecodeError:
        shutil.copy2(src_file, dst_file)


# v0.9.26: capture-config is the one file under <kb>/scripts/ that the user
# customizes (compile_model, query_model, bootstrap_turns, …). The dist ships
# it on every update, but a plain overwrite would clobber the user's pins.
# Special-case the copy: walk the dist's comments + ordering verbatim, but
# preserve any user value that differs from the dist's default. KB-only keys
# (typos, deprecated knobs the loader already ignores) are dropped with a log
# line so the file doesn't silently grow on every dist upgrade.
CAPTURE_CONFIG_NAME = "capture-config"
# _DIST_KNOWN_KEYS is the *core* set the loader knows about (mirrors
# ~/.devlore/lib/capture_config.py:DEFAULTS). The merge actively walks the
# current dist file to learn its full key set, so any new key the dist adds
# is preserved on subsequent merges rather than being dropped as "KB-only".
_CORE_DIST_KEYS = {
    "bootstrap_turns", "max_turns", "max_chars", "chunk_chars",
    "compile_chunk_chars", "compile_model", "query_model",
    "compile_part_timeout",
}


def _merge_capture_config(kb_file: Path, dist_file: Path) -> str | None:
    """Merge dist's capture-config over the KB's, preserving user-customized values.

    The dist wins on comments, ordering, and the `key = value` formatting for any
    key the user didn't actually change. The user wins on the value for any key
    whose value differs from the dist's. Whitespace is normalized for the
    comparison (so `compile_model =  sonnet` doesn't trigger a false "preserved").

    Returns a one-line summary "preserved N key(s) you customized: …" when
    anything was preserved, or None. Falls back to a plain overwrite on
    whole-file parse failure (logged with a warning).
    """
    def _parse_user(path: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        if not path.exists():
            return out
        for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = raw.partition("=")
            out[k.strip()] = v  # keep user's exact whitespace for re-emit
        return out

    try:
        user = _parse_user(kb_file)
        dist_text = dist_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"  ⚠ could not parse {kb_file} ({e}); overwriting with dist defaults")
        kb_file.parent.mkdir(parents=True, exist_ok=True)
        kb_file.write_text(dist_file.read_text(encoding="utf-8"), encoding="utf-8")
        return None

    preserved: list[str] = []
    out_lines: list[str] = []
    dist_keys: set[str] = set()
    for line in dist_text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            out_lines.append(line)
            continue
        k, _, v = line.partition("=")
        key = k.strip()
        dist_keys.add(key)
        if key in user and user[key].strip() != v.strip():
            out_lines.append(f"{k} = {user[key]}")
            preserved.append(key)
        else:
            out_lines.append(line)

    # Drop keys the user has but the dist doesn't recognize. Anything the
    # dist declares (even a brand-new key added this release) is preserved —
    # the KB-only check is against the actual current dist file, not a
    # hard-coded list, so newly-shipped keys flow through cleanly.
    dropped = [k for k in user if k not in dist_keys]
    if dropped:
        print(f"  · dropped {len(dropped)} KB-only key(s) not in dist capture-config: "
              f"{', '.join(sorted(dropped))}")

    kb_file.write_text("\n".join(out_lines) + ("\n" if dist_text.endswith("\n") else ""),
                       encoding="utf-8")
    if preserved:
        return f"preserved {len(preserved)} key(s) you customized: {', '.join(preserved)}"
    return None


# v0.9.25 migration: shared modules move from <kb>/scripts/ to ~/.devlore/lib/.
# Idempotent: running on an already-migrated KB is a no-op.
SHARED_FILES = (
    "config.py", "utils.py", "kb_resolve.py", "kb_registry.py",
    "capture_config.py", "transcripts.py", "activity.py",
    "kb_commit.py", "staleness.py", "stamp_baseline.py",
)


def _shared_lib_installed(version: str) -> bool:
    """True iff ~/.devlore/lib/VERSION is at or beyond `version`."""
    p = Path.home() / ".devlore" / "lib" / "VERSION"
    if not p.exists():
        return False
    try:
        installed = p.read_text(encoding="utf-8").strip()
        pa = tuple(int(x) for x in installed.split(".") if x.isdigit())
        pb = tuple(int(x) for x in version.split(".") if x.isdigit())
        return pa >= pb
    except (OSError, ValueError):
        return False


def _install_shared_lib(src: Path, version: str) -> bool:
    """Copy dist/lib/ → ~/.devlore/lib/ (and dist/lib/bin/* → ~/.devlore/bin/
    for the global launcher). Idempotent via _shared_lib_installed.
    Returns True when files were written."""
    if _shared_lib_installed(version):
        return False
    lib_src = src / "lib"
    if not lib_src.is_dir():
        print(f"  ⚠ {src} has no lib/ directory — is it a v0.9.25+ dist? skipping.")
        return False
    lib_dst = Path.home() / ".devlore" / "lib"
    bin_dst = Path.home() / ".devlore" / "bin"
    lib_dst.mkdir(parents=True, exist_ok=True)
    bin_dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in lib_src.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(lib_src)
        # The launcher lives under dist/lib/bin/ so a single `cp -R dist/lib/. ~/.devlore/lib/`
        # brings it along — but the canonical home is ~/.devlore/bin/, sibling of lib/.
        if rel.parts and rel.parts[0] == "bin":
            dst = bin_dst / Path(*rel.parts[1:])
        else:
            dst = lib_dst / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = f.read_text(encoding="utf-8")
            text = text.replace(str(src), str(lib_dst))
            dst.write_text(text, encoding="utf-8")
            # Anything under bin/ is meant to be executable (the launcher +
            # the seven Obsidian-plugin shims); force +x even if the source's
            # recorded mode didn't carry it across git's content-only diffs.
            mode = f.stat().st_mode
            if rel.parts and rel.parts[0] == "bin":
                mode = mode | 0o111
            dst.chmod(mode)
            n += 1
        except UnicodeDecodeError:
            shutil.copy2(f, dst)
            if rel.parts and rel.parts[0] == "bin":
                dst.chmod(dst.stat().st_mode | 0o111)
            n += 1
    (lib_dst / "VERSION").write_text(version + "\n", encoding="utf-8")
    print(f"  ✓ installed shared lib at ~/.devlore/lib + bin (v{version}, {n} files)")
    return True


def _repoint_devlore_symlink() -> None:
    """Move ~/.local/bin/devlore from the per-KB launcher to the global one."""
    global_launcher = Path.home() / ".devlore" / "bin" / "devlore"
    if not global_launcher.exists():
        return
    link = Path.home() / ".local" / "bin" / "devlore"
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(global_launcher)
        print(f"  ✓ ~/.local/bin/devlore → {global_launcher}")
    except OSError as e:
        print(f"  ⚠ could not repoint ~/.local/bin/devlore: {e}")


def _migrate_to_shared_layout(kb: Path, src: Path, version: str) -> bool:
    """v0.9.24 → v0.9.25 one-shot. Removes SHARED_FILES from <kb>/scripts/,
    installs the shared lib, repoints the PATH symlink, and replaces the KB's
    pyproject.toml with the stub. Idempotent: returns False when already done."""
    # Detect migration status: are any of the SHARED files still in <kb>/scripts/?
    needs_migration = any((kb / "scripts" / f).exists() for f in SHARED_FILES)
    if not needs_migration:
        return False
    removed = []
    for f in SHARED_FILES:
        p = kb / "scripts" / f
        if p.exists():
            p.unlink()
            removed.append(f)
    print(f"  ✓ migrated: removed {len(removed)} shared files from <kb>/scripts/")
    for r in removed:
        print(f"      - {r}")
    # Replace pyproject.toml with the stub from the dist.
    stub_src = src / "pyproject.toml"
    if stub_src.exists():
        current = kb / "pyproject.toml"
        if current.exists():
            backup = kb / "pyproject.toml.v0924.bak"
            if not backup.exists():
                shutil.copy2(current, backup)
                print(f"  · backed up previous pyproject.toml → pyproject.toml.v0924.bak")
        (kb / "pyproject.toml").write_text(
            stub_src.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  ✓ pyproject.toml replaced with per-KB stub (deps live in ~/.devlore/lib/)")
    # Install shared lib + repoint symlink.
    _install_shared_lib(src, version)
    _repoint_devlore_symlink()
    return True


def _reexec_into_fresh_self(self_path: Path, before: bytes | None) -> None:
    """Hand the rest of the update to the copy of this file just installed.

    `devlore update` executes whatever <kb>/scripts/update_kb.py the KB already
    had. Every migration step was therefore inert on the update that delivered
    it and only fired on the NEXT update — two passes per release, and the
    prunes that did run ran under code a version older than the payload they
    were pruning for. That is how v0.9.27 came to delete a per-KB launcher
    using v0.9.26 logic that knew nothing about the global one replacing it.

    Once the payload is on disk, re-exec so the remainder of the update runs
    under the version that shipped it. Looping is impossible: the child carries
    DEVLORE_UPDATE_REEXEC, an unchanged file re-execs nothing (the steady-state
    case), and any read failure falls through to the old behaviour.
    """
    if os.environ.get("DEVLORE_UPDATE_REEXEC") or before is None:
        return
    try:
        after = self_path.read_bytes()
    except OSError:
        return
    if after == before:
        return
    print("  ↻ machinery changed — continuing under the freshly installed "
          "update_kb.py (migrations land in this pass, not the next)")
    sys.stdout.flush()
    os.execve(sys.executable,
              [sys.executable, str(self_path), *sys.argv[1:]],
              dict(os.environ, DEVLORE_UPDATE_REEXEC="1"))


def main() -> None:
    # Snapshot this file before anything can overwrite it — the payload refresh
    # below rewrites the very script we are executing.
    _self_path = Path(__file__).resolve()
    try:
        _self_before: bytes | None = _self_path.read_bytes()
    except OSError:
        _self_before = None
    ap = argparse.ArgumentParser(description="Update this KB's machinery from the devlore dist.")
    ap.add_argument("--from", dest="src", help="Local dist/clone to update from "
                    "(default: ~/.devlore/dist, git-pulled).")
    ap.add_argument("--kb", help="KB to update (default: the KB this script lives in).")
    ap.add_argument("--to-new-layout", action="store_true",
                    help="One-shot migration from v0.9.24 (remove now-shared files "
                         "from <kb>/scripts/, install shared lib, repoint PATH symlink).")
    args = ap.parse_args()

    kb = Path(args.kb).expanduser().resolve() if args.kb \
        else Path(__file__).resolve().parent.parent
    if not (kb / "knowledge").is_dir() or not (kb / "scripts").is_dir():
        sys.exit(f"error: {kb} does not look like a devlore KB (missing knowledge/ or scripts/)")

    if args.src:
        src = Path(args.src).expanduser().resolve()
    else:
        src = DEFAULT_CACHE
        if (src / ".git").exists():
            # fetch + hard-reset rather than pull: survives upstream history
            # rewrites, and a failed refresh is reported instead of silently
            # updating from a stale cache.
            f = subprocess.run(["git", "-C", str(src), "fetch", "-q", "origin"],
                               capture_output=True, text=True)
            if f.returncode == 0:
                r = subprocess.run(["git", "-C", str(src), "reset", "-q", "--hard",
                                    "origin/main"], capture_output=True, text=True)
                if r.returncode != 0:
                    sys.exit(f"error: dist cache at {src} is broken — delete it and "
                             f"re-run: {r.stderr.strip()[:200]}")
            else:
                print("  ⚠ could not refresh the dist cache (offline?) — "
                      "updating from the cached copy")
        else:
            src.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["git", "clone", "-q", "--depth", "1", REPO, str(src)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                sys.exit(f"error: could not fetch the dist: {r.stderr.strip()[:200]}")
    if not (src / "scripts" / "init_kb.py").exists():
        sys.exit(f"error: {src} is not a devlore distribution")
    version = (src / "VERSION").read_text().strip() if (src / "VERSION").exists() else "?"

    if os.environ.get("DEVLORE_UPDATE_REEXEC"):
        # Second half of a single update, not a second update — say so, or the
        # repeated header reads as the very two-pass behaviour this removes.
        print(f"  … resumed under v{version} machinery")
    else:
        print(f"Updating machinery of {kb}")
        print(f"  from {src} (v{version})")

    # v0.9.25 migration step: if --to-new-layout was passed (or the KB still
    # has shared files in <kb>/scripts/ from v0.9.24), strip them, install the
    # shared lib at ~/.devlore/lib/, repoint ~/.local/bin/devlore to the
    # global launcher, and replace the per-KB pyproject.toml with a stub.
    # Idempotent — already-migrated KBs are a no-op.
    if args.to_new_layout or any((kb / "scripts" / f).exists() for f in SHARED_FILES):
        migrated = _migrate_to_shared_layout(kb, src, version)
        if not migrated:
            print(f"  · already on the v0.9.25+ layout (no shared files in <kb>/scripts/)")

    # The shared lib (the global launcher included) and the shared venv are
    # refreshed on EVERY update — not, as before, only when the one-time
    # v0.9.24→v0.9.25 migration branch above happened to fire. A KB already on
    # the v0.9.25 layout took neither, so ~/.devlore/lib and ~/.devlore/bin
    # stayed frozen at whatever version first migrated them while the per-KB
    # payload marched on. At v0.9.27 that combination bricked the KB outright:
    # the update deleted <kb>/scripts/devlore while ~/.devlore/bin/devlore was
    # still the v0.9.25 launcher that hard-requires it, so every subcommand —
    # `devlore update` included — died with "KB has no scripts/devlore".
    # Both installers are version-stamped no-ops once current, and both run
    # BEFORE the legacy prunes below so a failure can never leave the KB with
    # neither the old machinery nor the new.
    _install_shared_lib(src, version)
    _repoint_devlore_symlink()
    from init_kb import _install_shared_venv
    if _install_shared_venv(src, version):
        print(f"  ✓ shared venv installed at ~/.devlore/.venv (v{version})")
    # "Working" means the deps actually import, not merely that an interpreter
    # file exists — a cleared-but-unpopulated venv has python3 and nothing else,
    # and this flag is what licenses deleting the KB's only other interpreter.
    _shared_py = Path.home() / ".devlore" / ".venv" / "bin" / "python3"
    shared_venv_ok = _shared_py.exists() and subprocess.run(
        [str(_shared_py), "-c", "import claude_agent_sdk"],
        capture_output=True).returncode == 0

    n = 0
    capture_note: str | None = None
    for surface in SURFACES:
        sdir = src / surface
        if not sdir.is_dir():
            continue
        for f in sorted(sdir.iterdir()):
            if f.is_file():
                dst = kb / surface / f.name
                if f.name == CAPTURE_CONFIG_NAME:
                    capture_note = _merge_capture_config(dst, f)
                    if capture_note:
                        print(f"  ✓ capture-config: {capture_note}")
                else:
                    _copy(f, dst, src, kb)
                n += 1
    for rel in ROOT_FILES:
        f = src / rel
        if f.exists():
            _copy(f, kb / rel, src, kb)
            n += 1
    # Obsidian layer only if this KB opted into it at install time
    if (kb / ".obsidian").is_dir() and (src / ".obsidian" / "plugins").is_dir():
        for plug in (src / ".obsidian" / "plugins").iterdir():
            if plug.is_dir():
                for f in plug.iterdir():
                    if f.is_file():
                        _copy(f, kb / ".obsidian" / "plugins" / plug.name / f.name, src, kb)
                        n += 1
    # The payload — this file included — is now on disk. Everything below is
    # migration: it must run under the code that shipped it, not the code the
    # KB happened to have when the update started.
    _reexec_into_fresh_self(_self_path, _self_before)

    # v0.9.27: the per-KB <kb>/scripts/devlore copy and the per-KB .venv/ are gone.
    # If a stale install still carries them, drop them here so the diff is final.
    legacy = kb / "scripts" / "devlore"
    if legacy.exists():
        legacy.unlink()
        print(f"  ✓ removed legacy per-KB scripts/devlore (global launcher is canonical)")
    # The seven sibling shells moved to ~/.devlore/bin/ as shims. Their stale
    # per-KB copies are not merely redundant, they are BROKEN: each one execs
    # `<kb>/.venv/bin/python3 <kb>/scripts/<name>.py` directly, which (a) skips
    # the PYTHONPATH=~/.devlore/lib injection the launcher does, so the shared
    # modules fail to import (`ModuleNotFoundError: capture_config`), and (b)
    # points at the per-KB .venv/ removed just below. Anything still calling one
    # — an old Obsidian allowlist, a cron entry, a shell alias — fails silently.
    for _sh in ("compile", "devlore", "query", "recheck", "status", "update", "verify"):
        _legacy_sh = kb / "scripts" / f"{_sh}.sh"
        if _legacy_sh.exists():
            _legacy_sh.unlink()
            print(f"  ✓ removed legacy per-KB scripts/{_sh}.sh (shim is ~/.devlore/bin/{_sh}.sh)")
    legacy_v = kb / ".venv"
    if legacy_v.is_dir() and not shared_venv_ok:
        # The replacement is not usable, so the fallback stays. Removing it here
        # is what left KBs with no Python at all when the shared venv install
        # silently skipped.
        print(f"  ⚠ shared venv missing at ~/.devlore/.venv — KEEPING per-KB .venv/ "
              f"(re-run `devlore update` once uv is available)")
    elif legacy_v.is_dir():
        shutil.rmtree(legacy_v, ignore_errors=True)
        # The .gitignore already excludes .venv/ — make the deletion explicit so a
        # human review of `git status` sees the intent.
        (kb / "scripts" / ".venv-removed-by-v0927").write_text(
            "per-KB .venv/ was removed in v0.9.27 (centralized at ~/.devlore/.venv/).\n"
            "If you find this marker, run `devlore update` to restore the shared venv.\n",
            encoding="utf-8")
        print(f"  ✓ removed legacy per-KB .venv/ (centralized at ~/.devlore/.venv/)")
    extra = f" — capture-config: {capture_note}" if capture_note else ""
    print(f"  ✓ {n} machinery file(s) refreshed (knowledge/daily/config untouched){extra}")

    # Re-wire capture hooks into external captured projects so new hook events
    # (e.g. the Stop bootstrap hook) reach existing installs, not just new opt-ins.
    _rewire_own_codex_hooks(kb)
    _rewire_capture_hooks(kb)

    # Self-heal: code-root symlinks are machine-specific and belong in the LOCAL,
    # update-safe .git/info/exclude — not the dist-managed .gitignore (which this
    # very step just overwrote). Sync every current code root so a later `git add`
    # never tracks them, regardless of what the template .gitignore carries.
    # The shared `utils` module post-v0.9.25 lives at ~/.devlore/lib/, so import
    # it from there (the launcher injects PYTHONPATH, but let's be explicit so
    # a stray `python3 update_kb.py` invocation also works).
    sys.path.insert(0, str(Path.home() / ".devlore" / "lib"))
    from utils import git_exclude
    cr = kb / "scripts" / "code-roots"
    if cr.exists():
        for line in cr.read_text(encoding="utf-8").splitlines():
            nm = line.strip()
            if nm and not nm.startswith("#"):
                git_exclude(kb, nm, add=True)

    subprocess.run(["git", "-C", str(kb), "add", "-A"], capture_output=True)
    c = subprocess.run(["git", "-C", str(kb), "commit", "-q", "-m",
                        f"update: machinery → devlore v{version}"], capture_output=True)
    print(f"  ✓ committed (devlore v{version})" if c.returncode == 0
          else "  · nothing to commit (already current)")


if __name__ == "__main__":
    main()
