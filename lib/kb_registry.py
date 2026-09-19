"""devlore's user-level multi-KB registry and cwd-decoupling layer.

Builds ON TOP of v0.9.23's flat-path registry (~/.devlore/kb-dirs, owned by
utils.kb_dirs_registry() — the source of truth for KB paths). This module
adds the NAMING and per-user DEFAULT layers the bare-path file can't carry:

  ~/.devlore/kb-dirs       flat list of KB paths (legacy + status line + add/remove routing)
  ~/.devlore/registry.json NEW: {name: {path, description, peers}} — named KBs
  ~/.devlore/state.json    NEW: {default_kb, skip_register_prompt_for} — per-user defaults

Two files, not one: keeping names out of kb-dirs means the existing
`kb_resolve.py` and `statusline-wrapper.sh` readers never see anything but
paths — zero risk to the legacy interlock. Pure stdlib so the bash
launcher can call into it via plain `python3` BEFORE the active KB is known
(migration, first-detection prompts from any KB).

The first-detection prompt is OPT-OUT per path: declines are remembered in
state.json's `skip_register_prompt_for` so the launcher never nags.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ── Paths ───────────────────────────────────────────────────────────────
CONFIG_DIR = Path.home() / ".devlore"
KB_DIRS = CONFIG_DIR / "kb-dirs"            # legacy flat file (paths only)
REGISTRY_FILE = CONFIG_DIR / "registry.json"   # NEW: name → {path, description, peers}
STATE_FILE = CONFIG_DIR / "state.json"         # NEW: default_kb + skip list

SCHEMA_VERSION = 1


# ── Registry I/O ────────────────────────────────────────────────────────

def load_registry() -> list[dict]:
    """Read registry.json → [{name, path, description, peers}, ...] sorted by name.

    Missing file → empty list (NOT an error: a freshly-installed devlore has no
    named KBs until the user runs `devlore use` or the first-detection prompt
    accepts). Corrupt JSON → empty list + stderr note (the next write fixes it).
    Entries without a `peers` key get `peers: []` (forward compat for the
    peer-tier wave that this module does not yet implement)."""
    if not REGISTRY_FILE.exists():
        return []
    try:
        data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    kbs = []
    for name, entry in (data.get("kbs") or {}).items():
        if not isinstance(entry, dict) or "path" not in entry:
            continue
        kbs.append({
            "name": name,
            "path": str(Path(entry["path"]).resolve()),
            "description": entry.get("description", ""),
            "peers": list(entry.get("peers") or []),
        })
    return sorted(kbs, key=lambda e: e["name"])


def save_registry(kbs: list[dict]) -> None:
    """Atomic write of registry.json (write tmp, fsync, rename). Preserves
    unknown keys by routing through `kbs:` rather than replacing the whole
    document — a future field (e.g. `peers_meta`) added by a newer build
    survives a downgrade that only reads `kbs`."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(REGISTRY_FILE.read_text(encoding="utf-8")) if REGISTRY_FILE.exists() else {}
    except (json.JSONDecodeError, OSError):
        existing = {}
    payload = dict(existing)
    payload["schema_version"] = SCHEMA_VERSION
    payload["kbs"] = {
        e["name"]: {
            "path": e["path"],
            "description": e.get("description", ""),
            "peers": e.get("peers", []),
        }
        for e in kbs
    }
    tmp = REGISTRY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, REGISTRY_FILE)


def get_kb(name: str) -> dict | None:
    """By name. Returns None on miss."""
    for e in load_registry():
        if e["name"] == name:
            return e
    return None


def get_kb_by_path(path: Path) -> dict | None:
    """By resolved path. Symlink-aware so cwd-followed-by-resolve matches."""
    target = str(Path(path).resolve())
    for e in load_registry():
        if e["path"] == target:
            return e
    return None


def looks_like_kb(path: Path) -> bool:
    """Canonical "is this a devlore KB?" test.

    Every site used to ask for `scripts/devlore` — the per-KB launcher. v0.9.27
    deleted that file and made the launcher global, so the test started returning
    False for correctly-formed KBs. Worse, it returns False for a KB that was just
    created, which is why `register_kb` rejected every new KB outright and
    `devlore init` could not complete. Ask for the two directories that actually
    define a KB and that `init_kb`/`update_kb` have always validated instead.
    """
    path = Path(path)
    return (path / "knowledge").is_dir() and (path / "scripts").is_dir()


# Descriptions written by bootstrap/auto-registration, which say nothing about the
# KB. Consumers read registry.json directly and will render whatever is here, so a
# placeholder is worse than an empty string: it looks like an answer.
PLACEHOLDER_DESCRIPTIONS = (
    "(bootstrapped from kb-dirs)",
    "(auto-registered on first detection)",
    "",
)


def derived_description(path: Path) -> str:
    """A description inferred from what the KB actually documents.

    Names the codebases it covers, which is the one fact a consumer choosing
    between knowledge bases needs. Empty when the KB has no code roots — better
    nothing than a sentence that describes no KB in particular.
    """
    try:
        names = [n.strip() for n in (path / "scripts" / "code-roots").read_text(
            encoding="utf-8").splitlines() if n.strip() and not n.strip().startswith("#")]
    except OSError:
        return ""
    if not names:
        return ""
    if len(names) <= 4:
        return "Documents " + ", ".join(names)
    return f"Documents {len(names)} codebases: " + ", ".join(names[:3]) + ", …"


def backfill_descriptions() -> list[tuple[str, str]]:
    """Replace placeholder descriptions with derived ones. Returns what changed.

    Run from `devlore update`, so the registry heals without anyone typing a
    description. A human-written one is never overwritten — only the placeholders
    are, and only when something can be derived.
    """
    kbs = load_registry()
    changed: list[tuple[str, str]] = []
    for e in kbs:
        if e.get("description", "") not in PLACEHOLDER_DESCRIPTIONS:
            continue
        new = derived_description(Path(e["path"]))
        if new != e.get("description", ""):
            # Clearing to "" is deliberate when nothing can be derived: an empty
            # description shows as absent, a placeholder shows as an answer.
            e["description"] = new
            changed.append((e["name"], new or "(cleared — nothing to derive)"))
    if changed:
        save_registry(kbs)
    return changed


def register_kb(name: str, path: Path, *, description: str = "") -> dict:
    """Add or look up an entry. Idempotent: if `path` already has an entry
    (by any name), return it unchanged. If `name` already maps to a DIFFERENT
    path, raise ValueError — names are unique keys. Validates that `path`
    looks like a KB before accepting, so a typo'd path doesn't pollute the
    registry."""
    path = Path(path).resolve()
    if not looks_like_kb(path):
        raise ValueError(f"{path} does not look like a devlore KB "
                         f"(missing knowledge/ or scripts/)")
    kbs = load_registry()
    existing_by_path = get_kb_by_path(path)
    if existing_by_path:
        return existing_by_path
    for e in kbs:
        if e["name"] == name:
            raise ValueError(f"KB name {name!r} already registered at {e['path']}")
    entry = {"name": name, "path": str(path), "description": description, "peers": []}
    kbs.append(entry)
    save_registry(kbs)
    return entry


def unregister_kb(name: str) -> bool:
    """Remove by name. Returns True if removed, False if unknown."""
    kbs = load_registry()
    new_kbs = [e for e in kbs if e["name"] != name]
    if len(new_kbs) == len(kbs):
        return False
    save_registry(new_kbs)
    return True


# ── State ───────────────────────────────────────────────────────────────

def _state_defaults() -> dict:
    return {
        "default_kb": None,
        "skip_register_prompt_for": [],
        "schema_version": SCHEMA_VERSION,
    }


def load_state() -> dict:
    """state.json with all known keys defaulted. Missing or corrupt file → defaults."""
    if not STATE_FILE.exists():
        return _state_defaults()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _state_defaults()
    merged = _state_defaults()
    merged.update({k: v for k, v in (data or {}).items() if k in merged})
    return merged


def save_state(state: dict) -> None:
    """Atomic write of state.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def set_default(name: str) -> None:
    """Set default_kb. Validates the name exists in the registry."""
    if get_kb(name) is None:
        raise ValueError(f"unknown KB: {name!r} — register it first or check `devlore`")
    state = load_state()
    state["default_kb"] = name
    save_state(state)


def get_default() -> str | None:
    """Default KB name, or None. Does NOT validate the name still resolves."""
    return load_state().get("default_kb")


# ── Cwd resolution ──────────────────────────────────────────────────────

def _affinity_paths(kb_path: Path) -> list[str]:
    """The KB dir + every capture/code-root target. Mirrors kb_resolve.affinity_roots
    but for the read-side (cwd-vs-KB) rather than the write-side (target-vs-KB)."""
    roots = [str(Path(kb_path).resolve())]
    kb_path = Path(kb_path)
    for fname in ("capture-roots", "code-roots"):
        f = kb_path / "scripts" / fname
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if fname == "code-roots":
                link = kb_path / line
                if link.is_symlink():
                    try:
                        roots.append(str(link.resolve()))
                    except OSError:
                        pass
            else:
                try:
                    roots.append(str(Path(line.rstrip("/")).resolve()))
                except OSError:
                    pass
    return roots


def kb_from_cwd(cwd: Path) -> dict | None:
    """Most-specific KB whose affinity contains cwd (after resolving symlinks).
    Mirrors kb_resolve.candidates but for the inverse direction: cwd-OR-self.
    Returns the matching entry, or None when cwd is outside every KB."""
    cwd = Path(cwd).resolve()
    best: tuple[int, dict] | None = None
    for e in load_registry():
        for root in _affinity_paths(Path(e["path"])):
            try:
                root_resolved = str(Path(root).resolve())
            except OSError:
                continue
            if str(cwd) == root_resolved or str(cwd).startswith(root_resolved + os.sep):
                # Longer affinity root = more specific.
                if best is None or len(root_resolved) > best[0]:
                    best = (len(root_resolved), e)
    return best[1] if best else None


def resolve_active_kb(cwd: Path, *, explicit_kb: str | None = None) -> dict | None:
    """The cwd-decoupling decision in one place.

    Precedence (each step short-circuits):
      1. explicit_kb: a registered NAME, or a PATH to a knowledge base — the
         help has always documented both and only the name worked, so a user
         following it with a path got "no KB resolved" and no hint why
         (raises ValueError if neither resolves)
      2. cwd inside a registered KB: kb_from_cwd(cwd)
      3. default KB: get_default()
      4. None: no resolution possible

    Returns the resolved entry, or None."""
    if explicit_kb:
        entry = get_kb(explicit_kb)
        if entry is not None:
            return entry
        # Not a registered name — try it as a path, registered or not. An
        # unregistered but valid KB directory is still a KB the user can mean.
        cand = Path(explicit_kb).expanduser()
        if cand.exists():
            byp = get_kb_by_path(cand)
            if byp is not None:
                return byp
            if looks_like_kb(cand):
                return {"name": cand.name, "path": str(cand.resolve()),
                        "description": "(not registered)", "peers": []}
        raise ValueError(
            f"unknown KB: {explicit_kb!r} — not a registered name and not a "
            f"knowledge-base directory. `devlore kbs` lists the registered ones.")
    found = kb_from_cwd(cwd)
    if found:
        return found
    default = get_default()
    if default:
        entry = get_kb(default)
        if entry:
            return entry
    return None


# ── First-detection prompt ──────────────────────────────────────────────

def prompt_to_register(cwd: Path, *, self_kb: Path) -> dict | None:
    """If cwd is inside a KB that's not in the registry, offer to register it.

    Tty-gated: non-interactive invocations (Obsidian, hooks, statusline)
    skip silently — next interactive session re-prompts unless the user
    already declined (state.skip_register_prompt_for records declines by
    resolved path). If cwd is inside self_kb, no-op (the launcher already
    knows about itself). Returns the new entry, or None on decline / no-tty
    / nothing to register."""
    self_kb = Path(self_kb).resolve()
    cwd = Path(cwd).resolve()

    # cwd inside self_kb → no-op
    if cwd == self_kb or str(cwd).startswith(str(self_kb) + os.sep):
        return None

    # Is cwd inside ANY registered KB? If yes, no-op.
    for e in load_registry():
        for root in _affinity_paths(Path(e["path"])):
            try:
                root_resolved = str(Path(root).resolve())
            except OSError:
                continue
            if str(cwd) == root_resolved or str(cwd).startswith(root_resolved + os.sep):
                return None

    # What KB would cwd belong to? Walk UP looking for scripts/devlore.
    candidate = None
    cur = cwd
    while cur != cur.parent:
        if looks_like_kb(cur):
            candidate = cur
            break
        cur = cur.parent
    if candidate is None:
        return None
    candidate_resolved = str(candidate.resolve())

    # Already declined for this path?
    state = load_state()
    if candidate_resolved in state.get("skip_register_prompt_for", []):
        return None

    # Tty-gated.
    if not sys.stdin.isatty():
        return None

    # Prompt.
    name = candidate.name
    try:
        ans = input(f"Register {candidate_resolved} as {name!r}? [Y/n] ").strip().lower()
    except EOFError:
        ans = "n"
    if ans in ("", "y", "yes"):
        return register_kb(name=name, path=candidate,
                           description="(auto-registered on first detection)")
    # Decline → remember.
    state.setdefault("skip_register_prompt_for", []).append(candidate_resolved)
    save_state(state)
    return None


# ── Recovery ───────────────────────────────────────────────────────────

def _bootstrap_registry_from_kb_dirs() -> int:
    """Populate registry.json from the flat kb-dirs file (one entry per path,
    name = basename(path)). Used to recover from a missing/corrupt registry
    without losing the legacy KB list. Idempotent: existing registry entries
    are preserved. Returns the number of new entries added."""
    if not KB_DIRS.exists():
        return 0
    kbs = load_registry()
    known_paths = {e["path"] for e in kbs}
    added = 0
    for line in KB_DIRS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            p = str(Path(line).resolve())
        except OSError:
            continue
        if p in known_paths:
            continue
        if not looks_like_kb(Path(p)):
            continue
        kbs.append({
            "name": Path(p).name,
            "path": p,
            "description": "(bootstrapped from kb-dirs)",
            "peers": [],
        })
        added += 1
    if added:
        save_registry(kbs)
    return added


# ── CLI shim ────────────────────────────────────────────────────────────

def _print_kbs_table(kbs: list[dict], default_name: str | None) -> None:
    """Human-readable listing. Default KB is marked with '*'. Path widths are
    computed from data so the table adapts."""
    if not kbs:
        print("(no KBs registered — run `devlore` from inside a KB and accept the prompt, "
              "or `devlore init <dir>` to create one)")
        return
    name_w = max(len(e["name"]) for e in kbs)
    path_w = min(60, max(len(e["path"]) for e in kbs))
    default_suffix = lambda n: " *" if n == default_name else "  "
    header = f"{'NAME':<{name_w}}  {'PATH':<{path_w}}  DESCRIPTION"
    print(header)
    print("-" * len(header))
    for e in kbs:
        path = e["path"]
        if len(path) > path_w:
            path = "…" + path[-(path_w - 1):]
        marker = default_suffix(e["name"])
        print(f"{e['name']:<{name_w}}{marker}  {path:<{path_w}}  {e.get('description', '')}")
    if default_name:
        print(f"\ndefault: {default_name}  (*)")


def main(argv: list[str]) -> int:
    """Tiny CLI for the bash dispatcher. One subcommand per argv[0].

    All commands print at most one logical line on stdout (multiple lines
    allowed for `--human` table form). Exit 0 on success, 1 on user error
    (e.g. unknown KB name) — the launcher treats non-zero as a hard error
    and surfaces the stderr to the user."""
    if not argv:
        argv = ["list"]
    cmd = argv[0]
    rest = argv[1:]

    if cmd == "list":
        human = "--human" in rest
        kbs = load_registry()
        if human:
            _print_kbs_table(kbs, get_default())
        else:
            for e in kbs:
                print(f"{e['name']}\t{e['path']}")
        return 0

    if cmd == "use":
        if not rest:
            print("usage: devlore use <name>", file=sys.stderr)
            return 1
        try:
            set_default(rest[0])
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"default KB set to {rest[0]}")
        return 0

    if cmd == "describe":
        if not rest:
            print('usage: devlore describe <name> ["text"]   (omit text to derive one)',
                  file=sys.stderr)
            return 1
        name = rest[0]
        kbs = load_registry()
        entry = next((e for e in kbs if e["name"] == name), None)
        if not entry:
            print(f"error: unknown KB {name!r}. `devlore kbs` lists them.", file=sys.stderr)
            return 1
        text = " ".join(rest[1:]).strip() or derived_description(Path(entry["path"]))
        if not text:
            print(f"error: nothing to derive for {name!r} (no code roots) — "
                  f"pass a description.", file=sys.stderr)
            return 1
        entry["description"] = text
        save_registry(kbs)
        print(f"{name}: {text}")
        return 0

    if cmd == "which":
        cwd = Path(os.environ.get("DEVLORE_INVOCATION_CWD") or os.getcwd())
        entry = kb_from_cwd(cwd)
        if entry:
            print(entry["path"])
        return 0

    if cmd == "resolve":
        cwd_arg = None
        explicit = None
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--cwd" and i + 1 < len(rest):
                cwd_arg = Path(rest[i + 1]); i += 2; continue
            if a == "--explicit" and i + 1 < len(rest):
                explicit = rest[i + 1]; i += 2; continue
            i += 1
        cwd = Path(cwd_arg or os.environ.get("DEVLORE_INVOCATION_CWD") or os.getcwd())
        try:
            entry = resolve_active_kb(cwd, explicit_kb=explicit)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if entry:
            print(entry["path"])
        return 0

    if cmd == "maybe-prompt":
        cwd = None
        self_kb = None
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--cwd" and i + 1 < len(rest):
                cwd = Path(rest[i + 1]); i += 2; continue
            if a == "--self" and i + 1 < len(rest):
                self_kb = Path(rest[i + 1]); i += 2; continue
            i += 1
        if cwd is None or self_kb is None:
            return 0
        prompt_to_register(cwd, self_kb=self_kb)
        return 0

    if cmd == "register":
        name = None
        path = None
        i = 0
        while i < len(rest):
            a = rest[i]
            if a == "--name" and i + 1 < len(rest):
                name = rest[i + 1]; i += 2; continue
            if a == "--path" and i + 1 < len(rest):
                path = Path(rest[i + 1]); i += 2; continue
            i += 1
        if not name or path is None:
            print("usage: kb_registry.py register --name <n> --path <p>", file=sys.stderr)
            return 1
        try:
            entry = register_kb(name=name, path=path)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(entry["path"])
        return 0

    if cmd == "bootstrap":
        n = _bootstrap_registry_from_kb_dirs()
        print(f"bootstrapped {n} KB(s) from kb-dirs")
        return 0

    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))