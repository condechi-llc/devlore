"""Shared utilities for the personal knowledge base."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from config import (
    CONCEPTS_DIR,
    CONNECTIONS_DIR,
    DAILY_DIR,
    INDEX_FILE,
    KNOWLEDGE_DIR,
    LOG_FILE,
    MOCS_DIR,
    QA_DIR,
    STATE_FILE,
)


# ── User-path resolution ──────────────────────────────────────────────

def resolve_invocation_path(arg: str) -> Path:
    """Resolve a user-supplied path against the directory devlore was invoked
    FROM, not the Python process's cwd.

    A script may be started with its cwd inside the KB (historically `uv run
    --directory <KB>` did this unconditionally) — so a relative argument like `.`
    would resolve to the KB itself (and `add` would reject it as "the knowledge
    base itself"). The launcher exports DEVLORE_INVOCATION_CWD (the caller's shell cwd) so
    relative paths resolve where the user actually is. Absolute paths are
    unaffected; the env var falls back to os.getcwd() when unset (e.g. a script
    run directly, not through the launcher)."""
    p = Path(arg).expanduser()
    if not p.is_absolute():
        base = os.environ.get("DEVLORE_INVOCATION_CWD") or os.getcwd()
        p = Path(base) / p
    return p.resolve()


# ── Child Python processes (v0.9.27+ shared venv) ─────────────────────

# v0.9.27 collapsed the per-KB `<kb>/.venv/` into ONE shared venv at
# ~/.devlore/.venv, and moved the KB-agnostic modules to ~/.devlore/lib. The
# `devlore` launcher spawns every script as
#   DEVLORE_KB_ROOT=<kb> PYTHONPATH=~/.devlore/lib ~/.devlore/.venv/bin/python3 …
# (see the PY() helper in scripts/devlore). Python code that spawns ANOTHER
# devlore script must reproduce that contract itself. `uv run --directory <kb>`
# must NOT be used for this: uv materializes a venv from that KB's
# pyproject.toml, resurrecting the very per-KB `.venv/` v0.9.27 removed.
DEVLORE_HOME = Path.home() / ".devlore"
DEVLORE_VENV_PY = DEVLORE_HOME / ".venv" / "bin" / "python3"
DEVLORE_LIB = DEVLORE_HOME / "lib"


def devlore_python() -> str:
    """Interpreter for a devlore child process: the shared venv's python3 (it has
    claude-agent-sdk installed), falling back to the CURRENT interpreter — already
    the right one whenever the parent was itself launched by `devlore` — and last
    to whatever `python3` is on PATH."""
    if os.access(DEVLORE_VENV_PY, os.X_OK):
        return str(DEVLORE_VENV_PY)
    return sys.executable or shutil.which("python3") or "python3"


def devlore_child_env(kb_root: Path, env: dict | None = None) -> dict:
    """Environment for a devlore child process: `env` (default os.environ) with
    ~/.devlore/lib PREPENDED to PYTHONPATH and DEVLORE_KB_ROOT set to `kb_root`.

    Both are load-bearing. Without PYTHONPATH the child cannot import the shared
    modules (config, capture_config, transcripts, …); without DEVLORE_KB_ROOT the
    shared config.py falls back to its own __file__ location and the child writes
    into the WRONG knowledge base. Any inherited PYTHONPATH is preserved after
    ours; an empty one is not turned into a bare separator (which would silently
    put the cwd on the child's import path)."""
    child = dict(os.environ if env is None else env)
    inherited = child.get("PYTHONPATH", "")
    child["PYTHONPATH"] = (f"{DEVLORE_LIB}{os.pathsep}{inherited}" if inherited
                           else str(DEVLORE_LIB))
    child["DEVLORE_KB_ROOT"] = str(Path(kb_root).resolve())
    return child


# ── Local git excludes ────────────────────────────────────────────────

def git_exclude(repo: Path, name: str, *, add: bool) -> bool:
    """Idempotently add/remove `name` in <repo>/.git/info/exclude — git's LOCAL,
    never-shared, update-safe ignore list.

    Code-root symlinks (machine-specific paths, recreated by `devlore add`/`init`)
    belong HERE, not in the dist-managed `.gitignore` (which `devlore update`
    overwrites wholesale, so per-KB entries there would be clobbered — and stale
    names baked into the template would leak to every KB). No-op when `repo` is not
    a git checkout. Returns True iff the exclude file changed."""
    git_dir = repo / ".git"
    if not git_dir.exists():
        return False
    excl = git_dir / "info" / "exclude"
    lines = excl.read_text(encoding="utf-8").splitlines() if excl.exists() else []
    present = any(line.strip() == name for line in lines)
    if add == present:
        return False
    if add:
        lines.append(name)
    else:
        lines = [line for line in lines if line.strip() != name]
    excl.parent.mkdir(parents=True, exist_ok=True)
    excl.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
    return True


# ── Multi-KB registry (~/.devlore/kb-dirs) ────────────────────────────

# The registry of installed KB roots is HARNESS-AGNOSTIC state (add/remove
# owning-KB routing, status-line dispatch), so it lives in devlore's own
# ~/.devlore home. It began life in ~/.claude/kb-dirs — wrong home once devlore
# captures more than Claude Code — and is migrated from there on first touch.
KB_DIRS_FILE = Path.home() / ".devlore" / "kb-dirs"
LEGACY_KB_DIRS_FILE = Path.home() / ".claude" / "kb-dirs"


def kb_dirs_registry() -> Path:
    """Path of the multi-KB registry, MOVING the legacy ~/.claude/kb-dirs file
    to ~/.devlore/kb-dirs the first time any devlore code touches it. Falls back
    to the legacy path only if the move itself fails (e.g. permissions), so
    readers and writers always agree on a single live file."""
    if not KB_DIRS_FILE.exists() and LEGACY_KB_DIRS_FILE.exists():
        try:
            KB_DIRS_FILE.parent.mkdir(parents=True, exist_ok=True)
            LEGACY_KB_DIRS_FILE.replace(KB_DIRS_FILE)
        except OSError:
            return LEGACY_KB_DIRS_FILE
    return KB_DIRS_FILE


# ── State management ──────────────────────────────────────────────────

def load_state() -> dict:
    """Load persistent state from state.json."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"ingested": {}, "query_count": 0, "last_lint": None, "total_cost": 0.0}


def save_state(state: dict) -> None:
    """Save state to state.json."""
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ── File hashing ──────────────────────────────────────────────────────

def file_hash(path: Path) -> str:
    """SHA-256 hash of a file (first 16 hex chars)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ── Slug / naming ─────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert text to a filename-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


# ── Frontmatter YAML safety ───────────────────────────────────────────

# A top-level frontmatter scalar whose UNQUOTED value contains ": " — invalid YAML
# (breaks Obsidian's parser even though our regex readers shrug it off). Matches only
# unquoted plain scalars: value must not start with a quote, [, {, |, >, or #.
FM_UNSAFE_SCALAR = re.compile(r"""^([A-Za-z_][\w-]*):[ \t]+([^"'\[{|>#\n][^\n]*: [^\n]*)$""")


def quote_unsafe_frontmatter(article_paths) -> int:
    """Deterministically double-quote frontmatter scalars whose unquoted value
    contains ': ' (the LLM compiler writes prose summaries that legitimately contain
    colons). Idempotent; inner double quotes become single quotes. Returns the number
    of files fixed."""
    fixed = 0
    for p in article_paths:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end == -1:
            continue
        lines = text[3:end].split("\n")
        changed = False
        for i, ln in enumerate(lines):
            m = FM_UNSAFE_SCALAR.match(ln)
            if m:
                val = m.group(2).rstrip().replace('"', "'")
                lines[i] = f'{m.group(1)}: "{val}"'
                changed = True
        if changed:
            p.write_text(text[:3] + "\n".join(lines) + text[end:], encoding="utf-8")
            fixed += 1
    return fixed


# ── Frontmatter tags (Obsidian) ───────────────────────────────────────

# Obsidian-legal tag characters: letters, digits, '-', '_', and '/' for nesting.
TAG_UNSAFE = re.compile(r"[^a-z0-9_/-]+")

_TAGS_INLINE = re.compile(r"^tags:\s*\[(.*)\]\s*$")
_TAGS_BARE = re.compile(r"^tags:\s*([^\[\s#].*)$")
_TAGS_KEY = re.compile(r"^tags:\s*(#.*)?$")
_LIST_ITEM = re.compile(r"^\s+-\s*(.+?)\s*$")
_FM_PROJECT = re.compile(r"^project:\s*(.+?)\s*$")


def normalize_tag(tag: str) -> str:
    """Lowercase-kebab an Obsidian tag, preserving '/' nesting. '' if nothing survives."""
    t = tag.strip().strip("\"'").lstrip("#").lower().replace(" ", "-")
    t = re.sub(r"-{2,}", "-", TAG_UNSAFE.sub("-", t))
    return "/".join(s for s in (seg.strip("-") for seg in t.split("/")) if s)


def _fm_lines(text: str) -> tuple[list[str], int] | tuple[None, None]:
    """(frontmatter lines, end offset of the closing ---) or (None, None)."""
    if not text.startswith("---"):
        return None, None
    end = text.find("\n---", 3)
    if end == -1:
        return None, None
    return text[3:end].split("\n"), end


def _find_tags(lines: list[str]) -> tuple[list[str] | None, int | None, int | None]:
    """Locate the top-level `tags:` key. Returns (items, start, stop) where
    lines[start:stop] is the tags region — inline `[a, b]`, bare `a, b`, or a
    block list — or (None, None, None) when the key is absent."""
    for i, ln in enumerate(lines):
        m = _TAGS_INLINE.match(ln) or _TAGS_BARE.match(ln)
        if m:
            items = [p for p in (s.strip() for s in m.group(1).split(",")) if p]
            return items, i, i + 1
        if _TAGS_KEY.match(ln):
            j, items = i + 1, []
            while j < len(lines) and (m2 := _LIST_ITEM.match(lines[j])):
                items.append(m2.group(1))
                j += 1
            return items, i, j
    return None, None, None


def read_article_tags(text: str) -> list[str] | None:
    """The article's frontmatter `tags:` as written (unnormalized), or None when the
    article has no frontmatter or no tags key."""
    lines, _end = _fm_lines(text)
    if lines is None:
        return None
    items, start, _stop = _find_tags(lines)
    return items if start is not None else None


def ensure_required_tags(article_paths) -> int:
    """Deterministically guarantee every article's `tags:` exists as an inline list
    led by the article's `project:` slug — Obsidian tag filtering and graph maps key
    off it, so it can't be left to the compiler LLM's discretion. Domain tags after
    it are normalized to lowercase-kebab and deduped; articles with no `project:`
    are left alone. Idempotent. Returns the number of files changed."""
    fixed = 0
    for p in article_paths:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        lines, end = _fm_lines(text)
        if lines is None:
            continue
        project = normalize_tag(next(
            (m.group(1) for ln in lines if (m := _FM_PROJECT.match(ln))), ""))
        if not project:
            continue
        existing, start, stop = _find_tags(lines)
        tags = [project]
        for t in (normalize_tag(x) for x in existing or []):
            if t and t not in tags:
                tags.append(t)
        new_line = f"tags: [{', '.join(tags)}]"
        if start is not None:
            if lines[start:stop] == [new_line]:
                continue
            lines[start:stop] = [new_line]
        else:
            # Insert after the last header-ish scalar (skipping any block list that
            # follows it) so the new line can never split an existing block.
            keys = ("aliases:", "summary:", "milestone:", "subsystem:",
                    "status:", "type:", "project:", "title:")
            anchor = max((i for i, ln in enumerate(lines) if ln.startswith(keys)),
                         default=len(lines) - 1)
            j = anchor + 1
            while j < len(lines) and _LIST_ITEM.match(lines[j]):
                j += 1
            lines.insert(j, new_line)
        p.write_text(text[:3] + "\n".join(lines) + text[end:], encoding="utf-8")
        fixed += 1
    return fixed


def collect_tag_vocabulary(article_paths) -> dict[str, int]:
    """{tag: article count} across the wiki, EXCLUDING each article's own project
    slug (structural, not domain vocabulary). Ordered by count desc, then name —
    the compiler shows this to the LLM so tags converge instead of sprawling."""
    counts: dict[str, int] = {}
    for p in article_paths:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        lines, _end = _fm_lines(text)
        if lines is None:
            continue
        project = normalize_tag(next(
            (m.group(1) for ln in lines if (m := _FM_PROJECT.match(ln))), ""))
        items, _s, _e = _find_tags(lines)
        for t in {normalize_tag(x) for x in items or []}:
            if t and t != project:
                counts[t] = counts.get(t, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


# ── Markdown doc collection (devlore add / devlore docs) ─────────────

# Path segments that mark vendored or generated trees — excluded at ANY depth,
# even when tracked by git (Go's vendor/ is idiomatically committed; legacy
# repos commit node_modules; pip ships _vendor). Directories only — the
# filename itself is never tested against this list.
DOC_DENY_SEGMENTS = frozenset({
    "node_modules", "bower_components", "vendor", "vendors", "third_party",
    "thirdparty", "dist", "build", "out", "target", "site-packages",
    "venv", "env", "coverage", "__pycache__",
})

DOC_MIN_BYTES = 200   # at or below this a .md is noise (badge stubs, empty templates)
DOC_TRIPWIRE = 50     # a first-level dir contributing ≥ this many docs smells vendored


def collect_markdown_docs(root: Path, recursive: bool = False,
                          ignore_gitignore: bool = False) -> tuple[list[Path], dict[str, int]]:
    """Find human-written markdown docs under `root` for ingestion.

    Candidate set: `git ls-files` (tracked + untracked-but-not-ignored) when
    `root` is a git repo — the repo's own intent signal, so gitignored vendor
    trees and build output vanish for free — falling back to a filesystem walk
    otherwise.

    `ignore_gitignore=True` drops `--exclude-standard`, so gitignored markdown
    is a candidate too: research notes and drafts deliberately kept out of the
    remote are exactly the writing you may want in the KB. It only widens the
    git gate — the deny-list, depth, size and tripwire gates below still run,
    and it is a no-op on the non-git filesystem walk (which never consulted
    .gitignore in the first place).

    Candidates then pass four gates:

      deny-list  drop any path with a vendored/hidden DIRECTORY segment at any
                 depth (catches tracked vendor trees git intent can't)
      depth      default keeps root-level files + first-level subdirs only;
                 recursive=True keeps the whole tree
      size       drop files ≤ DOC_MIN_BYTES
      tripwire   a first-level dir contributing ≥ DOC_TRIPWIRE surviving docs
                 is excluded wholesale — ingest it by passing that dir
                 explicitly (it then becomes the root and is not tripwired)

    Returns (kept, excluded): kept is sorted; excluded maps each tripwired
    first-level dir name to the number of docs it would have contributed.
    """
    root = root.resolve()
    git = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others",
         *([] if ignore_gitignore else ["--exclude-standard"]),
         "-z", "--", "*.md"],
        capture_output=True, text=True)
    if git.returncode == 0:
        rels = [Path(r) for r in git.stdout.split("\0") if r]
    else:
        rels = [p.relative_to(root) for p in root.rglob("*.md")]

    by_top: dict[str, list[Path]] = {}
    for rel in rels:
        parts = rel.parts
        dirs = parts[:-1]
        if any(seg in DOC_DENY_SEGMENTS or seg.startswith(".") for seg in dirs):
            continue
        if not recursive and len(parts) > 2:  # root file = 1 part, first-level = 2
            continue
        p = root / rel
        try:
            if not p.is_file() or p.stat().st_size <= DOC_MIN_BYTES:
                continue
        except OSError:
            continue
        by_top.setdefault(parts[0] if len(parts) > 1 else ".", []).append(p)

    kept: list[Path] = []
    excluded: dict[str, int] = {}
    for top, paths in sorted(by_top.items()):
        if top != "." and len(paths) >= DOC_TRIPWIRE:
            excluded[top] = len(paths)
        else:
            kept.extend(paths)
    return sorted(kept), excluded


# ── Machinery-transcript hygiene ──────────────────────────────────────

# Opening text of the FIRST user prompt of every SDK session devlore itself
# spawns (compile / flush summarizer / backfill distill / query / tier-3
# verify). Matched as a PREFIX of the first user message only — an interactive
# session that merely quotes one of these strings mid-conversation never matches.
# ONE list, used by both the purge sweep here and the backfill filter in
# ingest_all_context. They used to be two that had already drifted apart, and
# neither knew lint.py's prompt — so `devlore recheck` transcripts were never
# swept AND never filtered, leaving them permanent backfill candidates.
MACHINERY_PROMPT_SENTINELS = (
    "You are a knowledge compiler",
    "Review the conversation context below",
    "You are answering a question from a personal knowledge base",
    "You are an adversarial code-grounding verifier",
    "Review this knowledge base for contradictions",   # lint.py / `devlore recheck`
    "index-guided retrieval",
    "HISTORICAL conversation being backfilled",
)

# Claude Code records how a session was started. devlore's own SDK sessions are
# Python ones whose cwd is the knowledge base itself — a pairing nothing else
# produces, and one that needs no maintenance when a prompt is reworded.
#
# The entrypoint ALONE is not enough and the tempting shortcut is wrong: a survey
# of every transcript on the development host found `sdk-ts` carrying a genuine
# human question ("why are smoke tests called that in programming?"). Treating
# any SDK session as machinery would discard real conversations. The cwd is what
# makes it safe.
MACHINERY_ENTRYPOINT = "sdk-py"


def _first_user_record(path: Path) -> dict:
    """The first user record of a transcript, or {} — metadata, not just text."""
    import json as _json
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    d = _json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "user":
                    return d
    except OSError:
        pass
    return {}


def is_machinery_transcript(path: Path, kb_root: Path | None = None) -> bool:
    """True when this transcript is one of devlore's own SDK sessions.

    Two independent signals, either sufficient:

    - Structural: a Python-SDK session whose cwd is the knowledge base. Survives
      any rewording of the prompts.
    - Textual: the first user turn starts with a known machinery prompt. Kept
      because it is what older transcripts can be judged by, and because it
      still catches a machinery session started some other way.
    """
    rec = _first_user_record(path)
    if kb_root is not None and rec.get("entrypoint") == MACHINERY_ENTRYPOINT:
        try:
            if os.path.realpath(str(rec.get("cwd", ""))) == os.path.realpath(str(kb_root)):
                return True
        except OSError:
            pass
    msg = rec.get("message", {}) or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
    return str(content or "").lstrip().startswith(MACHINERY_PROMPT_SENTINELS)


def claude_project_dir(root: Path) -> Path:
    """The ~/.claude/projects/ transcript dir for sessions whose cwd is `root`
    (Claude Code's own path encoding)."""
    enc = str(root).replace("/", "-").replace("_", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / enc


def _first_user_text(path: Path, max_records: int = 20) -> str:
    """Text of the first user-role record in a transcript ('' if none early on)."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > max_records:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") == "user":
                    content = rec.get("message", {}).get("content")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        return "".join(b.get("text", "") for b in content
                                       if isinstance(b, dict))
                    return ""
    except OSError:
        pass
    return ""


def purge_machinery_transcripts(root: Path, grace_hours: int = 24) -> tuple[int, int]:
    """Delete the Claude Code transcripts of devlore's OWN SDK sessions from
    `root`'s project dir.

    Every compile/flush/query/verify call spawns a headless Claude session with
    cwd at the KB root, and its transcript lands in ~/.claude/projects/ exactly
    like a human session. Left in place they come to dominate the dir (873 of
    911 files on the founding KB), pollute usage-insights analyses (a compile
    part killed by the watchdog reads as the USER aborting a session), and are
    candidates for recursive backfill. They are pure exhaust — their input is
    the daily+wiki and their output is committed to the wiki — so they are
    deleted, not archived. Files modified within `grace_hours` are kept (never
    races an in-flight session; recent ones stay inspectable for debugging).
    Human transcripts never match: the sentinel must PREFIX the first user
    message. Returns (files_deleted, bytes_freed).
    """
    import time

    proj = claude_project_dir(root)
    if not proj.is_dir():
        return 0, 0
    cutoff = time.time() - grace_hours * 3600
    n = freed = 0
    for f in proj.glob("*.jsonl"):
        try:
            if f.stat().st_mtime > cutoff:
                continue
            if is_machinery_transcript(f, root):
                size = f.stat().st_size
                f.unlink()
                n += 1
                freed += size
        except OSError:
            continue
    return n, freed


# ── Wikilink helpers ──────────────────────────────────────────────────

def extract_wikilinks(content: str) -> list[str]:
    """Extract the TARGET of every [[wikilink]] in markdown content.

    Obsidian links may carry a display alias ([[target|shown text]]) and/or a
    heading anchor ([[target#Section]]); both are stripped so callers always get
    the bare article path the link resolves to."""
    return [raw.split("|")[0].split("#")[0].strip()
            for raw in re.findall(r"\[\[([^\]]+)\]\]", content)]


def wiki_article_exists(link: str) -> bool:
    """Check if a wikilinked article exists on disk."""
    path = KNOWLEDGE_DIR / f"{link}.md"
    return path.exists()


# ── Wiki content helpers ──────────────────────────────────────────────

def read_wiki_index() -> str:
    """Read the knowledge base index file."""
    if INDEX_FILE.exists():
        return INDEX_FILE.read_text(encoding="utf-8")
    return "# Knowledge Base Index\n\n| Article | Summary | Compiled From | Updated |\n|---------|---------|---------------|---------|"


def read_all_wiki_content() -> str:
    """Read index + all wiki articles into a single string for context."""
    parts = [f"## INDEX\n\n{read_wiki_index()}"]

    for subdir in [CONCEPTS_DIR, CONNECTIONS_DIR, QA_DIR, MOCS_DIR]:
        if not subdir.exists():
            continue
        for md_file in sorted(subdir.glob("*.md")):
            rel = md_file.relative_to(KNOWLEDGE_DIR)
            content = md_file.read_text(encoding="utf-8")
            parts.append(f"## {rel}\n\n{content}")

    return "\n\n---\n\n".join(parts)


def list_wiki_articles() -> list[Path]:
    """List all wiki article files."""
    articles = []
    for subdir in [CONCEPTS_DIR, CONNECTIONS_DIR, QA_DIR, MOCS_DIR]:
        if subdir.exists():
            articles.extend(sorted(subdir.glob("*.md")))
    return articles


def list_raw_files() -> list[Path]:
    """List all daily log files."""
    if not DAILY_DIR.exists():
        return []
    return sorted(DAILY_DIR.glob("*.md"))


# ── Index helpers ─────────────────────────────────────────────────────

def count_inbound_links(target: str, exclude_file: Path | None = None) -> int:
    """Count how many wiki articles link to a given target (aliased and
    anchored links — [[target|alias]], [[target#heading]] — count too)."""
    count = 0
    for article in list_wiki_articles():
        if article == exclude_file:
            continue
        content = article.read_text(encoding="utf-8")
        if target in extract_wikilinks(content):
            count += 1
    return count


def get_article_word_count(path: Path) -> int:
    """Count words in an article, excluding YAML frontmatter."""
    content = path.read_text(encoding="utf-8")
    # Strip frontmatter
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:]
    return len(content.split())


def build_index_entry(rel_path: str, summary: str, sources: str, updated: str) -> str:
    """Build a single index table row."""
    link = rel_path.replace(".md", "")
    return f"| [[{link}]] | {summary} | {sources} | {updated} |"
