"""devlore init — bootstrap a NET-NEW knowledge base for another project.

Creates a self-sufficient KB directory from THIS repo's machinery (the committed
state is the template), wires Claude Code/Codex capture for the target codebase, and
git-inits with the standard commit strategy. **Obsidian is optional**: the core
install has zero Obsidian artifacts; `--with-obsidian` additionally drops in the
devlore plugin + vault config for users who open the KB directory as a vault.

What it does, in order:
  1. validate target KB dir (new/empty) + codebase path(s)
  2. copy the machinery payload: hooks/, the core scripts/, AGENTS.md,
     .claude/commands + settings.json, pyproject.toml + uv.lock, .gitignore
     — rewriting this KB's absolute path → the target's in every copied file
  3. symlink each codebase into the KB dir; write scripts/capture-roots
     (subtree mode) + scripts/code-roots; fresh capture-config
  4. register the capture hooks in each CODEBASE's .claude/settings.local.json
     and .codex/hooks.json (merge-aware — never clobbers existing settings)
  5. skeleton knowledge/{concepts,connections,qa,mocs}/ + daily/ + generated
     empty index.md + log.md header
  6. `uv sync` the venv; git init + initial commit (local-only strategy)
  7. append the KB to ~/.devlore/kb-dirs (multi-KB registry: routing + status line)
  8. [--with-obsidian] copy .obsidian/plugins/devlore + app.json ignore filters

After init: start Claude Code or Codex sessions in the codebase (capture is live),
`/devlore <docs>` to ingest existing documentation, and
`scripts/ingest_all_context.py` to backfill any surviving past conversations.

Usage:
    uv run python scripts/init_kb.py <kb-dir> --code <codebase-path> [--code …]
                                     [--with-obsidian] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent  # the template KB (this repo)

# v0.9.25 split: SHARED modules ship once into ~/.devlore/lib/ (handled by
# install.sh + init_kb.py's shared-lib installer) and get imported by every
# KB via DEVLORE_KB_ROOT + PYTHONPATH=~/.devlore/lib. KB-LOCAL scripts stay
# in <kb>/scripts/ because they own per-KB state (knowledge/, daily/, state.json,
# capture-roots, code-roots, capture-config, flush markers, runtime logs).
PAYLOAD_SHARED = [
    "config.py", "utils.py", "kb_resolve.py", "kb_registry.py",
    "capture_config.py", "transcripts.py", "activity.py",
    "kb_commit.py", "staleness.py", "stamp_baseline.py",
]
PAYLOAD_LOCAL = [
    "add_codebase.py", "build_index.py", "capture-config",
    "compile.py", "flush.py",
    "ingest_all_context.py", "ingest_doc.py", "init_kb.py", "lint.py",
    "obsidian_setup.py", "optin.py", "query.py",
    "recheck.py", "remove_codebase.py", "snapshot.py", "status.py",
    "statusline-wrapper.sh", "statusline.py",
    "update_kb.py", "verify.py",
]
# PAYLOAD_SCRIPTS kept as the union for back-compat with code that iterates it
# (notably scripts/build_dist.py, scripts/update_kb.py). New code should prefer
# PAYLOAD_SHARED + PAYLOAD_LOCAL.
PAYLOAD_SCRIPTS = sorted(set(PAYLOAD_SHARED) | set(PAYLOAD_LOCAL))
PAYLOAD_HOOKS = ["capture_gate.py", "pre-compact.py", "session-end.py",
                 "session-start.py", "stop.py"]
# v0.9.27: uv.lock is no longer per-KB (single shared venv at ~/.devlore/.venv/);
# pyproject.toml ships as a no-deps stub from dist-assets/kb-pyproject.toml.
PAYLOAD_ROOT = ["AGENTS.md", ".gitignore"]  # pyproject.toml shipped as a stub below
PAYLOAD_CLAUDE = ["settings.json"]  # + commands/ tree
# Claude fires SessionEnd only at session end and PreCompact only on compaction,
# so a long session captures nothing until one of those — Stop (every turn) runs
# the bootstrap/safety-valve flush that establishes the delta marker early.
CLAUDE_HOOK_EVENTS = ("SessionStart", "PreCompact", "SessionEnd", "Stop")
# Codex's Stop already maps to session-end.py (it captures every turn), so Codex
# does not need the separate bootstrap hook.
CODEX_HOOK_EVENTS = ("SessionStart", "PreCompact", "Stop")


_SHELL_SAFE = re.compile(r'^[A-Za-z0-9_\-/.~ ]+$')


def _assert_path_safe(p: Path, label: str = "path") -> None:
    """Reject paths with shell metacharacters before they can be embedded in command strings."""
    if not _SHELL_SAFE.match(str(p)):
        sys.exit(f"error: {label} contains characters not allowed in command strings: {p}\n"
                 "Use a path with only alphanumeric, '-', '_', '/', '.', '~', or space characters.")


def _rewrite(text: str, target: Path) -> str:
    """Point every reference to the template KB at the new KB instead. Also resolves
    the distribution home placeholder written by build_dist.py, so a cloned
    distribution works no matter where it was cloned. The placeholder literal is
    split below because this file is itself materialized through this rewrite —
    an intact literal would be resolved to the KB path, corrupting the installed
    copy's ability to resolve future placeholders.

    The BIN_DIR placeholder resolves to `$HOME/.devlore/bin` (the centralized
    launcher location, used by the Obsidian plugin's allowlist constants since
    v0.9.27). It is NOT kb-scoped — those shims live in one place globally."""
    return (text.replace(str(SOURCE_ROOT), str(target))
                .replace("__DEVLORE" + "_BIN_DIR__", str(Path.home() / ".devlore" / "bin"))
                .replace("__DEVLORE" + "_HOME__", str(target)))


def _copy(src: Path, dst: Path, target: Path, dry: bool) -> None:
    if dry:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = src.read_text(encoding="utf-8")
        dst.write_text(_rewrite(text, target), encoding="utf-8")
        dst.chmod(src.stat().st_mode)
    except UnicodeDecodeError:  # binary (none expected, but be safe)
        shutil.copy2(src, dst)


def _slug(path: Path) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", path.name.lower()).strip("-") or "code"


# ── Shared lib installer (v0.9.25+) ────────────────────────────────────
# The shared machinery lives at ~/.devlore/lib/. install.sh copies it on a
# fresh install; init_kb.py / update_kb.py install it on every subsequent
# KB init/update so a user can have a working `devlore` even if they never ran
# install.sh (e.g. cloned the dist directly and ran init_kb.py by hand).

def _shared_lib_root() -> Path:
    return Path.home() / ".devlore" / "lib"


def _shared_lib_installed(source_version: str) -> bool:
    """True iff ~/.devlore/lib/VERSION is at or beyond `source_version`."""
    p = _shared_lib_root() / "VERSION"
    if not p.exists():
        return False
    try:
        installed = p.read_text(encoding="utf-8").strip()
        pa = tuple(int(x) for x in installed.split(".") if x.isdigit())
        pb = tuple(int(x) for x in source_version.split(".") if x.isdigit())
        return pa >= pb
    except (OSError, ValueError):
        return False


def _install_shared_venv(source_root: Path, source_version: str, dry: bool = False) -> bool:
    """Materialize ~/.devlore/.venv/ with deps from dist/lib/pyproject.toml.
    Idempotent via ~/.devlore/.venv/DEVLORE_VERSION stamp; returns False when
    already at source_version. Falls back to a no-op (warns) if uv is unavailable."""
    venv = Path.home() / ".devlore" / ".venv"
    stamp = venv / "DEVLORE_VERSION"
    try:
        if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == source_version \
                and (venv / "bin" / "python3").exists():
            return False
    except OSError:
        pass
    if dry:
        return True
    # Layout note: build_dist maps dist-assets/lib/pyproject.toml → lib/pyproject.toml,
    # so a BUILT dist (what ~/.devlore/dist is) carries it at lib/ and has no
    # dist-assets/ at all. Looking only under dist-assets/ meant this always
    # missed against a real dist and silently fell back to system python3 —
    # while the caller went on to delete the per-KB .venv. Check the dist layout
    # first, then the source-tree layout for a dev checkout.
    pyproject = source_root / "lib" / "pyproject.toml"
    if not pyproject.exists():
        pyproject = source_root / "dist-assets" / "lib" / "pyproject.toml"
    if not pyproject.exists():
        # Neither layout — skip; the launcher falls back to bare `python3`.
        # Do NOT create the venv directory before this point: an empty
        # ~/.devlore/.venv/ reads as "installed" to anything checking existence.
        print(f"  ⚠ no lib/pyproject.toml under {source_root} — skipping shared "
              f"venv install (using system python3)")
        return False
    venv.mkdir(parents=True, exist_ok=True)
    uv_check = subprocess.run(["uv", "--version"], capture_output=True, text=True)
    if uv_check.returncode != 0:
        print(f"  ⚠ uv not on PATH — shared venv install skipped (using system python3)")
        return False
    # --clear: uv refuses outright when a venv already exists, so without it every
    # refresh after the first (a version bump, a dep change) fails and the venv
    # stays pinned at whatever version created it.
    r = subprocess.run(["uv", "venv", "--clear", "--python", "3.12", str(venv)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ⚠ uv venv failed: {r.stderr.strip()[:160]}")
        return False
    # Read the dependency list from the shared lib's pyproject.toml and install
    # via uv pip. Avoids needing a second pyproject file in the repo.
    pip = subprocess.run(
        ["uv", "pip", "install", "--python", str(venv / "bin" / "python3"),
         "claude-agent-sdk>=0.1.29", "python-dotenv>=1.0.0", "tzdata>=2024.1"],
        capture_output=True, text=True)
    if pip.returncode != 0:
        print(f"  ⚠ shared venv pip install failed: {pip.stderr.strip()[:160]}")
        return False
    stamp.write_text(source_version + "\n", encoding="utf-8")
    return True


def _install_shared_lib(source_root: Path, source_version: str, dry: bool = False) -> bool:
    """Copy `source_root/dist-assets/lib/` into ~/.devlore/lib/. Stamps VERSION.
    Idempotent: returns False when the installed version already meets source_version.
    Returns True when files were written or overwritten."""
    if _shared_lib_installed(source_version):
        return False
    if dry:
        return True
    # Two layouts reach this function and they are not the same shape.
    #
    # A BUILT DIST already has a composed `lib/` — that is what update_kb installs,
    # and copying it wholesale is correct.
    #
    # The SOURCE-OF-TRUTH repo has no `lib/` at all. Its real shared modules live in
    # `scripts/`, and `dist-assets/lib/` holds only the three assets build_dist takes
    # from there (pyproject.toml, __init__.py, bin/) — plus, historically, stale
    # v0.9.25 copies of the modules that build_dist never reads. Copying that
    # directory wholesale, which is what this did, installed those stale copies OVER
    # the good shared lib on every `init_kb.py` run from a checkout: the normal dev
    # bootstrap silently downgrading the machinery it was about to use.
    #
    # Composing from `scripts/` + PAYLOAD_SHARED here mirrors build_dist exactly, so
    # both paths install the same bytes and `dist-assets/lib/*.py` has no reader.
    pairs: list[tuple[Path, Path]] = []   # (source file, path relative to lib root)
    composed = source_root / "lib"
    if composed.is_dir():
        pairs = [(f, f.relative_to(composed)) for f in composed.rglob("*") if f.is_file()]
    else:
        assets = source_root / "dist-assets" / "lib"
        scripts_dir = source_root / "scripts"
        for name in PAYLOAD_SHARED:
            f = scripts_dir / name
            if f.is_file():
                pairs.append((f, Path(name)))
        if assets.is_dir():
            for extra in ("pyproject.toml", "__init__.py"):
                f = assets / extra
                if f.is_file():
                    pairs.append((f, Path(extra)))
            bin_dir = assets / "bin"
            if bin_dir.is_dir():
                pairs += [(f, f.relative_to(assets)) for f in bin_dir.rglob("*") if f.is_file()]
    if not pairs:
        # Neither layout present (e.g. a checkout mid-refactor): skip silently —
        # init_kb.py falls back to copying per-KB.
        return False
    lib_dst = _shared_lib_root()
    lib_dst.mkdir(parents=True, exist_ok=True)
    for f, rel in pairs:
        dst = lib_dst / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = f.read_text(encoding="utf-8")
            # No KB-specific placeholder rewriting here: shared modules reference
            # the KB via DEVLORE_KB_ROOT at call time, never baked paths.
            dst.write_text(text, encoding="utf-8")
            dst.chmod(f.stat().st_mode)
        except UnicodeDecodeError:
            shutil.copy2(f, dst)
    # Stamp the version so future init/update runs know to skip.
    (lib_dst / "VERSION").write_text(source_version + "\n", encoding="utf-8")
    # Also write the launcher + sibling shims to ~/.devlore/bin/ so a `devlore`
    # PATH symlink works AND the Obsidian plugin's allowlist (which now points at
    # ~/.devlore/bin/<name>.sh shims, post-v0.9.27) resolves to real files.
    bin_dst = Path.home() / ".devlore" / "bin"
    # Same two layouts as above: a built dist keeps the launcher under lib/bin,
    # the source repo under dist-assets/lib/bin.
    launcher_src = (source_root / "lib" / "bin") if (source_root / "lib" / "bin").is_dir() \
        else (source_root / "dist-assets" / "lib" / "bin")
    if launcher_src.is_dir():
        for f in sorted(launcher_src.iterdir()):
            if not f.is_file():
                continue
            dst = bin_dst / f.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                text = f.read_text(encoding="utf-8")
                dst.write_text(text, encoding="utf-8")
                dst.chmod(f.stat().st_mode | 0o111)  # always executable
            except UnicodeDecodeError:
                shutil.copy2(f, dst)
                dst.chmod(f.stat().st_mode | 0o111)
        # Repoint ~/.local/bin/devlore → ~/.devlore/bin/devlore (best-effort;
        # install.sh already does this for fresh installs, but a KB created via
        # init alone still gets the global entrypoint).
        global_devlore = bin_dst / "devlore"
        if global_devlore.exists():
            link = Path.home() / ".local" / "bin" / "devlore"
            if link.parent.exists() or link.parent.mkdir(parents=True, exist_ok=True) or True:
                try:
                    if link.exists() or link.is_symlink():
                        link.unlink()
                    link.symlink_to(global_devlore)
                except OSError:
                    pass  # ~/.local/bin may not exist or be writable — non-fatal.
    return True


# The KB's own structure — a codebase symlink must never shadow these.
RESERVED_NAMES = {"knowledge", "daily", "scripts", "hooks", "quarantine", "reports",
                  ".claude", ".obsidian", ".git", ".venv"}


def link_name(kb: Path, codebase: Path) -> str:
    """Symlink name for a codebase inside the KB. Falls back to a parent-qualified
    slug when the basename collides with a reserved KB directory or an existing
    non-symlink path (e.g. a codebase literally named `daily` → `<parent>-daily`)."""
    name = _slug(codebase)
    if name in RESERVED_NAMES or ((kb / name).exists() and not (kb / name).is_symlink()):
        name = f"{_slug(codebase.parent)}-{name}"
    return name


NO_HOOK_ROOTS_NAME = "no-hook-roots"

# Registering a directory and wiring live capture into it are two decisions that
# devlore has always made as one. `wire()` appends to capture-roots AND writes the
# project's own agent config in a single breath, which is right for "I want this
# codebase documented from now on" and wrong for "I want a one-time snapshot of what
# already happened". The snapshot case must leave the project untouched.
#
# Splitting them needs somewhere durable to record the intent, because capture-roots
# membership is the ONLY thing `_rewire_capture_hooks` consults: without a marker it
# re-wires every registered root on the next `devlore update`, and a deliberately
# hook-free directory would quietly acquire hooks. This file is that marker.
NO_HOOK_ROOTS_HEADER = """\
# Directories registered for CONTENT ONLY — historical backfill and markdown docs.
# devlore must NEVER write capture hooks into these projects, and `devlore update`
# must not retrofit them later. One absolute path per line; '#' comments ignored.
# Written by `devlore add --no-hooks` and `devlore snapshot`.
"""


def no_hook_roots(kb: Path) -> set[str]:
    """Paths opted out of hook wiring, normalized without a trailing slash."""
    f = kb / "scripts" / NO_HOOK_ROOTS_NAME
    if not f.exists():
        return set()
    try:
        return {ln.strip().rstrip("/") for ln in f.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")}
    except OSError:
        return set()


def is_no_hook_root(kb: Path, path: Path) -> bool:
    """True when `path`, or any ancestor of it, is registered content-only.

    Ancestors count because capture-roots entries and the project root that
    `project_root_of()` resolves them to are frequently different directories —
    opting out `<repo>/docs` must also protect `<repo>` from being wired.
    """
    opted = no_hook_roots(kb)
    if not opted:
        return False
    p = str(Path(path).resolve()).rstrip("/")
    return any(p == o or p.startswith(o + "/") or o.startswith(p + "/") for o in opted)


def add_no_hook_root(kb: Path, path: Path) -> bool:
    """Record `path` as content-only. Returns False when already present."""
    f = kb / "scripts" / NO_HOOK_ROOTS_NAME
    entry = str(Path(path).resolve()).rstrip("/")
    if entry in no_hook_roots(kb):
        return False
    f.parent.mkdir(parents=True, exist_ok=True)
    body = f.read_text(encoding="utf-8") if f.exists() else NO_HOOK_ROOTS_HEADER
    if body and not body.endswith("\n"):
        body += "\n"
    f.write_text(body + entry + "\n", encoding="utf-8")
    return True


def _is_devlore_hook(cmd: str, kb: Path, script: str) -> bool:
    """True when `cmd` invokes THIS KB's `script` hook, in any generation's spelling.

    Three have shipped: `uv run --directory <kb> python hooks/<s>.py` (v0.9.24),
    `python3 <kb>/hooks/<s>.py` (v0.9.25) and `<shared-venv>/python3 <kb>/hooks/<s>.py`
    (v0.9.27). Note the first spells the script path RELATIVE, with the KB carried in
    `--directory`, so matching `<kb>/hooks/<s>.py` as a substring misses it. Requiring
    both the KB path and the script filename catches all three and stays specific
    enough not to touch a DIFFERENT KB's hooks living in the same project file.
    """
    return str(kb) in cmd and script in cmd


def _merge_hook_file(
    settings_path: Path,
    kb: Path,
    dry: bool,
    events: tuple[str, ...],
    scripts: dict[str, str],
    timeouts: dict[str, int],
    matchers: dict[str, str | None],
    status_messages: dict[str, str] | None = None,
) -> str:
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return f"⚠ {settings_path} unreadable — hooks NOT registered (register manually)"
    hooks = settings.setdefault("hooks", {})
    added = []
    # v0.9.27: hooks now spawn under the shared venv at ~/.devlore/.venv/bin/python3,
    # not a per-KB `uv run`. The launcher relies on PYTHONPATH-finding the shared
    # lib via the `capture_config`/`transcripts` modules; the hooks themselves only
    # import stdlib + claude_agent_sdk.
    pybin = str(Path.home() / ".devlore" / ".venv" / "bin" / "python3")
    if not Path(pybin).exists():
        # Fallback for a half-installed system (the launcher also handles this);
        # `uv run` would auto-materialize a per-KB venv, which is what we're moving
        # away from — so we use `python3` (system) and rely on hook PYTHONPATH.
        pybin = "python3"
    updated, deduped = [], 0
    for ev in events:
        cmd = f"{pybin} {kb}/hooks/{scripts[ev]}"
        groups = hooks.setdefault(ev, [])
        # Find THIS KB's handler for THIS hook script in any generation's spelling
        # and rewrite it, rather than appending a new one. Matching on the whole
        # command string — which is what this did before — made every change to how
        # devlore spawns Python look like a brand-new hook, so v0.9.24's
        # `uv run --directory <kb> python hooks/x.py`, v0.9.25's `python3 <kb>/hooks/x.py`
        # and v0.9.27's shared-venv spelling all accumulated side by side. Projects
        # ended up firing the same capture two or three times per event, with the
        # older spellings erroring against a venv that no longer exists.
        seen = False
        for g in groups:
            kept = []
            for h in g.get("hooks", []):
                if not _is_devlore_hook(h.get("command", ""), kb, scripts[ev]):
                    kept.append(h)
                    continue
                if seen:
                    deduped += 1      # a stale generation of a hook we already kept
                    continue
                seen = True
                if h.get("command") != cmd:
                    h["command"] = cmd
                    h["timeout"] = timeouts[ev]
                    if status_messages and ev in status_messages:
                        h["statusMessage"] = status_messages[ev]
                    updated.append(ev)
                kept.append(h)
            g["hooks"] = kept
        # A group whose only handler was a dropped duplicate would otherwise linger
        # as `{"hooks": []}` and, with a matcher, read as a configured-but-empty rule.
        groups[:] = [g for g in groups if g.get("hooks")]
        if seen:
            continue
        handler = {"type": "command", "command": cmd, "timeout": timeouts[ev]}
        if status_messages and ev in status_messages:
            handler["statusMessage"] = status_messages[ev]
        group = {"hooks": [handler]}
        matcher = matchers.get(ev)
        if matcher is not None:
            group["matcher"] = matcher
        groups.append(group)
        added.append(ev)
    if not dry and (added or updated or deduped):
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    parts = []
    if added:
        parts.append(f"registered {', '.join(added)}")
    if updated:
        parts.append(f"rewired {', '.join(updated)}")
    if deduped:
        parts.append(f"removed {deduped} stale generation(s)")
    return "; ".join(parts) if parts else "already registered"


def merge_claude_hooks(codebase: Path, kb: Path, dry: bool) -> str:
    """Register capture hooks in Claude Code settings.local.json."""
    return _merge_hook_file(
        codebase / ".claude" / "settings.local.json",
        kb,
        dry,
        CLAUDE_HOOK_EVENTS,
        {"SessionStart": "session-start.py", "PreCompact": "pre-compact.py",
         "SessionEnd": "session-end.py", "Stop": "stop.py"},
        {"SessionStart": 15, "PreCompact": 10, "SessionEnd": 10, "Stop": 10},
        {"SessionStart": "", "PreCompact": "", "SessionEnd": "", "Stop": ""},
    )


def merge_codex_hooks(codebase: Path, kb: Path, dry: bool) -> str:
    """Register capture hooks in Codex's project-local hooks.json."""
    return _merge_hook_file(
        codebase / ".codex" / "hooks.json",
        kb,
        dry,
        CODEX_HOOK_EVENTS,
        {"SessionStart": "session-start.py", "PreCompact": "pre-compact.py",
         "Stop": "session-end.py"},
        {"SessionStart": 15, "PreCompact": 10, "Stop": 10},
        {"SessionStart": "startup|resume|clear|compact", "PreCompact": "manual|auto",
         "Stop": None},
        {"SessionStart": "Loading devlore context",
         "PreCompact": "Capturing devlore context",
         "Stop": "Capturing devlore session"},
    )


def merge_codebase_hooks(codebase: Path, kb: Path, dry: bool) -> str:
    """Register capture hooks for supported local coding agents."""
    return (
        f"Claude: {merge_claude_hooks(codebase, kb, dry)}; "
        f"Codex: {merge_codex_hooks(codebase, kb, dry)}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Bootstrap a new knowledge base (devlore init).")
    ap.add_argument("kb_dir", help="Target KB directory (created; must not be a non-empty dir).")
    ap.add_argument("--code", action="append", default=[],
                    help="Path to a codebase to document (repeatable; omit to create an "
                         "empty KB and opt codebases in later with `devlore add`).")
    ap.add_argument("--with-obsidian", action="store_true",
                    help="Also install the OPTIONAL Obsidian layer (devlore plugin + vault config).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    dry = args.dry_run

    kb = Path(args.kb_dir).expanduser().resolve()
    if kb.exists() and any(kb.iterdir()):
        sys.exit(f"error: {kb} exists and is not empty")
    if kb == SOURCE_ROOT or str(kb).startswith(str(SOURCE_ROOT) + "/"):
        sys.exit("error: target must be outside the template KB")
    _assert_path_safe(kb, "kb-dir")
    codebases = []
    for c in args.code:
        p = Path(c).expanduser().resolve()
        if not p.is_dir():
            sys.exit(f"error: codebase not found: {p}")
        if str(p) == "/" or not str(p).startswith(str(Path.home())):
            sys.exit(f"error: codebase must be inside your home directory: {p}")
        _assert_path_safe(p, "codebase")
        codebases.append(p)

    print(f"{'DRY RUN — ' if dry else ''}initializing KB at {kb}")
    print(f"  template: {SOURCE_ROOT}")

    # 2. machinery payload (path-rewritten)
    # v0.9.25: only KB-LOCAL scripts land in <kb>/scripts/. The shared
    # machinery (config, utils, kb_*, capture_config, transcripts, activity,
    # kb_commit, staleness, stamp_baseline) goes into ~/.devlore/lib/ instead,
    # installed once and reused by every KB.
    source_version = (SOURCE_ROOT / "VERSION").read_text(encoding="utf-8").strip() \
        if (SOURCE_ROOT / "VERSION").exists() else "0.0.0"
    shared_installed = _install_shared_lib(SOURCE_ROOT, source_version, dry)
    if not dry and shared_installed:
        print(f"  ✓ shared lib installed at ~/.devlore/lib (v{source_version})")
    elif not dry and not shared_installed:
        print(f"  · shared lib already at v{source_version} or newer (skipped)")
    for name in PAYLOAD_LOCAL:
        _copy(SOURCE_ROOT / "scripts" / name, kb / "scripts" / name, kb, dry)
    for name in PAYLOAD_HOOKS:
        _copy(SOURCE_ROOT / "hooks" / name, kb / "hooks" / name, kb, dry)
    for name in PAYLOAD_ROOT:
        _copy(SOURCE_ROOT / name, kb / name, kb, dry)
    # pyproject.toml is the per-KB STUB (no deps — shared lib carries them).
    if not dry:
        stub = SOURCE_ROOT / "dist-assets" / "kb-pyproject.toml"
        if stub.exists():
            (kb / "pyproject.toml").write_text(
                stub.read_text(encoding="utf-8"), encoding="utf-8")
    # .claude/settings.json lives at dist-assets/claude/settings.json in the
    # source-of-truth repo (so the same file works for both source checkouts
    # and built dists). The dist lays it out at dist/.claude/settings.json.
    claude_src = SOURCE_ROOT / "dist-assets" / "claude"
    if not claude_src.exists():
        claude_src = SOURCE_ROOT / ".claude"
    for name in PAYLOAD_CLAUDE:
        _copy(claude_src / name, kb / ".claude" / name, kb, dry)
    for cmd in sorted(claude_src.glob("commands/*.md")):
        _copy(cmd, kb / ".claude" / "commands" / cmd.name, kb, dry)
    print(f"  ✓ machinery copied ({len(PAYLOAD_LOCAL)} KB-local scripts, "
          f"{len(PAYLOAD_HOOKS)} hooks, commands, AGENTS.md)")

    # 3. symlinks + capture-roots + code-roots + fresh runtime config
    links = []
    for cb in codebases:
        name = link_name(kb, cb)
        if not dry:
            (kb / name).symlink_to(cb)
        links.append((name, cb))
    if not dry:
        (kb / "scripts" / "capture-roots").write_text(
            "# Knowledge-base capture opt-in (one dir per line; trailing '/' = subtree).\n"
            + "".join(f"{cb}/\n" for _, cb in links), encoding="utf-8")
        (kb / "scripts" / "code-roots").write_text(
            "# Code roots: symlinks under the KB root holding the project code.\n"
            + "".join(f"{name}\n" for name, _ in links), encoding="utf-8")
    if links:
        print("  ✓ linked: " + ", ".join(f"{name} → {cb}" for name, cb in links))
    else:
        print("  · no codebase linked yet — opt one in later with `devlore add <path>`")

    # 4. codebase hook registration (merge-aware)
    for name, cb in links:
        note = merge_codebase_hooks(cb, kb, dry)
        print(f"  ✓ capture hooks for {cb.name}: {note}")

    # The KB captures sessions run inside itself too, and its .claude/settings.json
    # ships with the machinery above — but nothing has ever written its .codex side,
    # so a Codex session in a new KB went uncaptured. Claude's half is deliberately
    # NOT re-registered here: it comes from the dist and duplicating it would stack
    # a second generation of the same hooks.
    if not dry:
        note = merge_codex_hooks(kb, kb, dry)
        print(f"  ✓ this KB's own Codex capture hooks: {note}")

    # 5. skeletons
    if not dry:
        for sub in ("concepts", "connections", "qa", "mocs"):
            (kb / "knowledge" / sub).mkdir(parents=True, exist_ok=True)
            (kb / "knowledge" / sub / ".gitkeep").touch()
        (kb / "daily").mkdir(exist_ok=True)
        (kb / "daily" / ".gitkeep").touch()
        (kb / "knowledge" / "log.md").write_text("# Build Log\n\n", encoding="utf-8")
        subprocess.run([sys.executable, str(kb / "scripts" / "build_index.py")],
                       capture_output=True, cwd=str(kb))
    print("  ✓ knowledge/ + daily/ skeletons (+ empty generated index.md)")

    # 6. shared venv at ~/.devlore/.venv/ (single install for all KBs — v0.9.27+).
    # Each KB used to run `uv sync --directory <kb>` here, materializing a per-KB
    # .venv/ — four times the work for one set of dependencies. Now there's one
    # ~/.devlore/.venv/ and every Python script (KB-local or shared) runs under
    # it via the global launcher's DEVLORE_PY resolution.
    if not dry:
        if _install_shared_venv(SOURCE_ROOT, source_version):
            print(f"  ✓ shared venv at ~/.devlore/.venv (v{source_version})")
        else:
            print(f"  · shared venv already at v{source_version} or newer (skipped)")
        # Belt and braces: nuke any leftover per-KB .venv/ from a pre-v0.9.27 install.
        legacy = kb / ".venv"
        if legacy.is_dir():
            shutil.rmtree(legacy, ignore_errors=True)
            print(f"  ✓ removed legacy per-KB .venv/ (centralization complete)")

    # 7. multi-KB registry (owning-KB routing + status-line dispatch).
    #    Two files in parallel:
    #      ~/.devlore/kb-dirs    — flat paths (legacy readers: kb_resolve.py,
    #                              statusline-wrapper.sh)
    #      ~/.devlore/registry.json — named KBs + descriptions (kb_registry.py
    #                              list/use/which; cwd-decoupling uses the
    #                              `path` here to find the entry by cwd)
    from utils import kb_dirs_registry
    from config import now_iso
    reg = kb_dirs_registry()
    if not dry:
        existing = reg.read_text(encoding="utf-8") if reg.exists() else \
            "# devlore multi-KB registry: one KB root per line (add/remove routing,\n" \
            "# status-line dispatch). Managed by devlore init; safe to hand-edit.\n"
        if str(kb) not in existing:
            reg.write_text(existing.rstrip("\n") + f"\n{kb}\n", encoding="utf-8")
        # Named registry: register by directory basename; bootstrap from the
        # legacy flat file if it doesn't exist yet (handles first init after
        # this code lands). If this is the user's only KB, it's the default.
        # `load_registry`, not `list_kbs`: the latter has never existed. This import
        # has raised ImportError on every real `devlore init` since v0.9.25 — unseen
        # because it sits inside `if not dry`, so --dry-run exercised none of it and
        # no KB had been created since.
        from kb_registry import (
            _bootstrap_registry_from_kb_dirs,
            get_default,
            load_registry,
            register_kb,
            set_default,
        )
        _bootstrap_registry_from_kb_dirs()
        register_kb(name=kb.name, path=kb,
                    description=f"devlore KB (initialized {now_iso()})")
        if get_default() is None and len(load_registry()) == 1:
            set_default(kb.name)
    print(f"  ✓ registered in {reg} + ~/.devlore/registry.json")

    # 8. OPTIONAL Obsidian layer. The install step is shared with `devlore
    #    obsidian` (obsidian_setup.install_obsidian_layer) — one source of truth
    #    for the copy + placeholder rewrite + vault config.
    if args.with_obsidian:
        from obsidian_setup import activation_steps, install_obsidian_layer
        n = install_obsidian_layer(kb, SOURCE_ROOT, dry=dry)
        print(f"  ✓ Obsidian layer (OPTIONAL): devlore plugin + vault config ({n} file(s))")
        if not dry:
            print("    " + activation_steps(kb).replace("\n", "\n    "))
    else:
        print("  · Obsidian layer skipped (core is fully functional without it; "
              f"add it anytime with:  {kb}/scripts/devlore obsidian)")

    # 9. git init + initial commit (after everything, so the commit is complete)
    if not dry:
        subprocess.run(["git", "init", "-q"], cwd=str(kb), capture_output=True)
        # Keep code-root symlinks (machine-specific) out of git via the LOCAL,
        # update-safe exclude — never the dist-managed .gitignore.
        from utils import git_exclude
        for name, _ in links:
            git_exclude(kb, name, add=True)
        subprocess.run(["git", "add", "-A"], cwd=str(kb), capture_output=True)
        what = ", ".join(n for n, _ in links) if links else "(no codebase yet)"
        subprocess.run(["git", "commit", "-q", "-m",
                        f"devlore init: knowledge base for {what}\n\n"
                        f"Bootstrapped from {SOURCE_ROOT} (template). Local-only commit\n"
                        f"strategy; the pipeline auto-commits after each write."],
                       cwd=str(kb), capture_output=True)
        print("  ✓ git init + initial commit")

    first = (f"  1. cd {codebases[0]}  &&  start a Claude Code or Codex session — capture is LIVE"
             if codebases else
             f"  1. opt your first codebase in:  {kb}/scripts/devlore add <codebase-path>")
    print(f"""
Next steps:
{first}
  2. ask the KB:              {kb}/scripts/devlore ask "your question"
  3. anytime:                 {kb}/scripts/devlore help{'''
  4. open ''' + str(kb) + ''' as an Obsidian vault for the rendered experience''' if args.with_obsidian else ''}""")


if __name__ == "__main__":
    main()
