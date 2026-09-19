"""Gated batch backfill of never-flushed conversations (PR D, final item).

Sweeps the Claude Code and Codex transcript stores for conversations in the
CAPTURED project roots (scripts/capture-roots) that have no flush marker, and
runs each through the full pipeline, OLDEST first:

    distill (tiered model) → append to the conversation's OWN dated daily →
    compile --file → regression check → Tier-1 verification gate →
    PASS: write flush marker  /  FAIL: QUARANTINE (restore snapshot, park distill)

Gates (non-negotiable — ungated batch ingest is a hallucination AMPLIFIER):
  • DRY-RUN BY DEFAULT: prints the plan + per-conversation cost estimate and exits.
    `--yes` executes. (Empirical anchor: ONE 50MB conversation cost ≈ $24,
    compile-dominated; compile cost GROWS with wiki size — ~$6/part at 73 articles.)
  • Model tiering DEFAULTS UP: Sonnet for everything technical; Haiku only for
    genuinely small conversations (< --haiku-max chars of dialogue); `--force-model
    opus` for critical ones. Misclassifying dense content down to Haiku silently
    under-distills = knowledge loss. Verification is deterministic (Tier-1) here;
    any LLM verification stays ≥ Sonnet.
  • Regression check (deterministic): a PRE-existing article shrinking >30%, or any
    `updated:` moving backwards, fails the conversation.
  • Verification gate (deterministic): the Tier-1 symbol sweep must not introduce
    NEW COMPILE-FABRICATED tokens.
  • Quarantine on fail: knowledge/, the target daily, and compile state are restored
    from the pre-conversation snapshot; the distilled text is parked in
    `quarantine/<sid>.md` with the failure reasons for human review.

Usage:
    uv run python scripts/ingest_all_context.py                  # dry-run (plan + cost)
    uv run python scripts/ingest_all_context.py --yes            # execute the plan
    uv run python scripts/ingest_all_context.py --limit 3 --yes  # oldest 3 only
    uv run python scripts/ingest_all_context.py --session <sid> --yes
    uv run python scripts/ingest_all_context.py --session <sid> --force-model opus --yes
"""

from __future__ import annotations

import os

# Recursion guard: distill spawns Agent SDK sessions in this captured project; the
# capture hooks must not flush the ingester's own sessions. Set BEFORE SDK imports.
os.environ.setdefault("CLAUDE_INVOKED_BY", "ingest_all_context")

import argparse
import asyncio
import json
import math
import re
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Shared machinery lives at ~/.devlore/lib/ (v0.9.25+).
sys.path.insert(0, str(Path.home() / ".devlore" / "lib"))

from capture_config import get_limits  # noqa: E402
from config import DAILY_DIR, KNOWLEDGE_DIR, now_iso, system_cli_path  # noqa: E402
from transcripts import (  # noqa: E402
    transcript_dialogue,
    transcript_metadata,
    transcript_session_id,
)
from utils import devlore_child_env, devlore_python  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from capture_gate import should_capture  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
QUARANTINE = ROOT / "quarantine"
SNAPSHOTS = QUARANTINE / ".snapshots"
PROJECTS_STORE = Path.home() / ".claude" / "projects"
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
CODEX_SESSIONS_STORE = CODEX_HOME / "sessions"
CAPTURE_ROOTS = SCRIPTS / "capture-roots"

LIMITS = get_limits()
CHUNK_CHARS = LIMITS["chunk_chars"]
COMPILE_CHUNK_CHARS = LIMITS["compile_chunk_chars"]

# ── cost model (estimates, stated as such; tune from observed runs) ───────────
DISTILL_COST_PER_CHUNK = {"haiku": 0.03, "sonnet": 0.20, "opus": 1.00}
COMPILE_COST_PER_PART = 6.00          # empirical at 73 articles (2026-06-03: $5.87/part)
DISTILLED_CHARS_PER_CHUNK = 1500      # observed distill output density

MIN_SIZE_DEFAULT = 30_000             # bytes; below this a transcript is ignored
HAIKU_MAX_DEFAULT = 20_000            # dialogue chars; ≤ this → Haiku, else Sonnet (default UP)
MAX_CHUNKS_DEFAULT = 60               # refuse pathological transcripts unless raised


# ── discovery ─────────────────────────────────────────────────────────────────

def _encode(path: str) -> str:
    """Claude Code's project-dir encoding of a cwd path."""
    return path.replace("/", "-").replace("_", "-").replace(".", "-")


def _under_root(project_dir_name: str, root: str) -> bool:
    """True if a Claude project-dir name is the encoded `root`, or a subtree /
    worktree of it. Used to SCOPE discovery to a single codebase (e.g. `devlore
    add <dir>` backfills only that dir's own conversations, not every capture
    root). The encoding is lossy, so this is deliberately over-inclusive toward
    the root's own tree — far better than the global sweep, and the dialogue-size
    floor filters noise."""
    enc = _encode(root)
    return project_dir_name == enc or project_dir_name.startswith(enc + "-")


def captured_project_dirs() -> list[Path]:
    """Transcript dirs under ~/.claude/projects/ whose cwd is opted-in per
    scripts/capture-roots (exact root → exact dir; subtree root → prefix match)."""
    exact, subtree = [], []
    for line in CAPTURE_ROOTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        (subtree if line.endswith("/") else exact).append(line.rstrip("/"))
    dirs: list[Path] = []
    if not PROJECTS_STORE.exists():
        return dirs
    for d in sorted(PROJECTS_STORE.iterdir()):
        if not d.is_dir():
            continue
        name = d.name
        # Worktree sessions (<root>/.claude/worktrees/<name>) belong to <root>:
        # "." encodes to "-", so ".claude" yields the "--claude" double dash.
        # The encoding is lossy (worktree names may contain dashes), so this
        # prefix match is deliberately over-inclusive — better for backfill,
        # and the dialogue-size floor filters noise.
        if any(name == _encode(r) or name.startswith(_encode(r) + "--claude-worktrees-")
               for r in exact):
            dirs.append(d)
        elif any(name == _encode(r) or name.startswith(_encode(r) + "-") for r in subtree):
            dirs.append(d)
    return dirs


def captured_codex_transcripts() -> list[Path]:
    """Codex stores transcripts by date, with cwd in session_meta."""
    if not CODEX_SESSIONS_STORE.exists():
        return []
    out: list[Path] = []
    for t in sorted(CODEX_SESSIONS_STORE.glob("**/*.jsonl")):
        cwd = transcript_metadata(t).get("cwd", "")
        if isinstance(cwd, str) and should_capture(cwd):
            out.append(t)
    return out


def _flushed_session_ids() -> set[str]:
    return {p.stem.replace("flush-marker-", "") for p in SCRIPTS.glob("flush-marker-*.json")}


def extract_dialogue(transcript: Path) -> tuple[str, str, str]:
    """(dialogue, first_date, last_iso) — every user/assistant text turn in the
    transcript, formatted like the hooks' capture (so chunking splits identically)."""
    return transcript_dialogue(transcript)


# First-user-turn fingerprints of the pipeline's OWN internal Agent SDK sessions
# (compile/query/flush/verify/distill run with cwd=the-KB-root, so their transcripts
# land in the captured project dir). Ingesting them would be recursive self-capture —
# the batch equivalent of what CLAUDE_INVOKED_BY prevents at flush time.
# The prompt list now lives in utils as MACHINERY_PROMPT_SENTINELS, shared with
# the purge sweep. Two hand-maintained copies had already drifted apart, and a
# prompt added to one but not the other is invisible in exactly the way that
# costs money: an unfiltered machinery transcript is a billable backfill.


def is_machinery(dialogue: str) -> bool:
    """Prompt-text fallback for a dialogue already extracted from a transcript.

    Prefer utils.is_machinery_transcript(path, kb_root) where the file is at
    hand: it also reads the structural signal (a Python-SDK session whose cwd is
    the KB), which no rewording can defeat. This stays for the dialogue-only
    call sites."""
    from utils import MACHINERY_PROMPT_SENTINELS
    # Whitespace-normalized: the prompts line-wrap in transcripts ("index-guided\n
    # retrieval"), so a plain substring match misses them across the newline.
    head = " ".join(dialogue[:3000].split())
    return any(fp in head for fp in MACHINERY_PROMPT_SENTINELS)


def chunk_dialogue(dialogue: str) -> list[str]:
    """Split on turn boundaries into <=CHUNK_CHARS pieces (mirrors flush.py)."""
    if len(dialogue) <= CHUNK_CHARS:
        return [dialogue]
    chunks, cur = [], ""
    for turn in re.split(r"(?=\*\*(?:User|Assistant):\*\*)", dialogue):
        if cur and len(cur) + len(turn) > CHUNK_CHARS:
            chunks.append(cur)
            cur = turn
        else:
            cur += turn
    if cur.strip():
        chunks.append(cur)
    return chunks


def discover(min_size: int, haiku_max: int, only_session: str | None,
             force: bool = False, only_root: str | None = None) -> tuple[list[dict], list[dict]]:
    """(candidates oldest-first, warm_skipped). Candidates: [{sid, path, project, size,
    dialogue_chars, chunks, date, last_iso, model}]. Skips flushed and too-small
    transcripts silently; still-warm ones (possibly ACTIVE sessions) are returned
    separately so callers can print a named TODO instead of hiding them.

    only_root (an absolute codebase path) restricts discovery to that codebase's
    own conversations — its transcript dir, subdirectories, and worktrees — so
    `devlore add <dir>` backfills just <dir>, not every capture root."""
    flushed = _flushed_session_ids()
    import time
    out = []
    warm: list[dict] = []
    for d in captured_project_dirs():
        if only_root and not _under_root(d.name, only_root):
            continue
        for t in sorted(d.glob("*.jsonl")):
            sid = t.stem
            if only_session and not sid.startswith(only_session):
                continue
            if sid in flushed and not (force and only_session):
                continue  # --force --session re-ingests despite a (partial) flush marker
            size = t.stat().st_size
            if size < min_size:
                continue
            if time.time() - t.stat().st_mtime < 1800:
                warm.append({"sid": sid, "project": d.name.split("-Code-")[-1],
                             "size": size,
                             "age_min": int((time.time() - t.stat().st_mtime) / 60)})
                continue  # still warm — possibly an active session
            dialogue, date, last_iso = extract_dialogue(t)
            if len(dialogue) < min_size // 3 or not date:
                continue
            if is_machinery(dialogue):
                continue  # the pipeline's own internal sessions — never knowledge
            chunks = len(chunk_dialogue(dialogue))
            out.append({
                "sid": sid, "path": t, "project": d.name.split("-Code-")[-1],
                "agent": "Claude Code",
                "size": size, "dialogue_chars": len(dialogue), "chunks": chunks,
                "date": date, "last_iso": last_iso,
                "model": "haiku" if len(dialogue) <= haiku_max else "sonnet",
            })
    for t in captured_codex_transcripts():
        sid = transcript_session_id(t)
        if only_session and not sid.startswith(only_session):
            continue
        if sid in flushed and not (force and only_session):
            continue
        size = t.stat().st_size
        if size < min_size:
            continue
        meta = transcript_metadata(t)
        cwd = meta.get("cwd", "")
        if only_root and not (isinstance(cwd, str)
                              and (cwd == only_root or cwd.startswith(only_root + "/"))):
            continue
        project = Path(cwd).name if isinstance(cwd, str) and cwd else "codex"
        if time.time() - t.stat().st_mtime < 1800:
            warm.append({"sid": sid, "project": project,
                         "size": size,
                         "age_min": int((time.time() - t.stat().st_mtime) / 60)})
            continue
        dialogue, date, last_iso = extract_dialogue(t)
        if len(dialogue) < min_size // 3 or not date:
            continue
        if is_machinery(dialogue):
            continue
        chunks = len(chunk_dialogue(dialogue))
        out.append({
            "sid": sid, "path": t, "project": project, "agent": "Codex",
            "size": size, "dialogue_chars": len(dialogue), "chunks": chunks,
            "date": date, "last_iso": last_iso,
            "model": "haiku" if len(dialogue) <= haiku_max else "sonnet",
        })
    out.sort(key=lambda c: c["last_iso"])
    return out, warm


def print_warm_todo(warm: list[dict]) -> None:
    if not warm:
        return
    print(f"\n⏳ {len(warm)} session(s) skipped — modified <30 min ago (possibly still RUNNING):")
    for w in warm:
        print(f"   - {w['sid'][:8]}  ({w['project']}, {w['size']/1e6:.1f}MB, "
              f"last activity {w['age_min']} min ago)")
    print("   TODO → after those sessions end (+30 min), run `devlore backfill` to sweep them in.")


def estimate(conv: dict) -> tuple[float, float, int]:
    """(distill_cost, compile_cost, compile_parts) — estimates."""
    distill = conv["chunks"] * DISTILL_COST_PER_CHUNK[conv["model"]]
    distilled = conv["chunks"] * DISTILLED_CHARS_PER_CHUNK
    parts = max(1, math.ceil(distilled / COMPILE_CHUNK_CHARS))
    return distill, parts * COMPILE_COST_PER_PART, parts


# ── distill ───────────────────────────────────────────────────────────────────

DISTILL_PROMPT = """Review the conversation context below (a HISTORICAL conversation being
backfilled into a knowledge base) and respond with a concise summary of the items worth
preserving in a daily log. Do NOT use any tools — just return plain text.

Format your response as a structured daily log entry with these sections (only the ones
with actual content):

**Context:** [One line about what the user was working on]

**Key Exchanges:**
- [Important Q&A or discussions]

**Decisions Made:**
- [Any decisions with rationale — including decisions later superseded; record what was
  decided THEN, the compile step handles supersession]

**Lessons Learned:**
- [Gotchas, patterns, or insights discovered]

**Action Items:**
- [Follow-ups or TODOs mentioned]

Rules:
- Skip routine tool calls, file reads, and trivial back-and-forth.
- Backtick ONLY literal code identifiers that exist verbatim in code (function/field/file
  names, metadata keys) — never formula variables, math placeholders, or prose shorthand.
  When citing a rejected/superseded value, keep the rejection explicit in the sentence.
- If nothing is worth saving, respond with exactly: FLUSH_OK

## Conversation Context

{chunk}"""


async def _distill_chunk(chunk: str, model: str) -> tuple[str, float]:
    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query,
    )
    opts = dict(cwd=str(ROOT), allowed_tools=[], max_turns=2, model=model)
    cli = system_cli_path()
    if cli:
        opts["cli_path"] = cli
    text, cost = "", 0.0
    async for message in query(prompt=DISTILL_PROMPT.format(chunk=chunk),
                               options=ClaudeAgentOptions(**opts)):
        if isinstance(message, AssistantMessage):
            text += "".join(b.text for b in message.content if isinstance(b, TextBlock))
        elif isinstance(message, ResultMessage):
            cost = message.total_cost_usd or 0.0
    return text.strip(), cost


def distill(conv: dict) -> tuple[str, float, list[int]]:
    """Full-transcript distill → (daily entry body, cost, failed_chunk_numbers).

    Chunk failures are TOLERATED, not fatal: 3 attempts with backoff (the CLI
    intermittently dies with "Fatal error in message reader"), then the chunk is
    skipped with a VISIBLE note in the daily entry — 8/9 chunks captured beats an
    all-or-nothing quarantine of the whole conversation (meta-KB smoke finding #4)."""
    import time as _time
    dialogue, _, _ = extract_dialogue(conv["path"])
    chunks = chunk_dialogue(dialogue)
    parts, cost = [], 0.0
    failed: list[int] = []
    timeout = LIMITS["compile_part_timeout"]
    for i, chunk in enumerate(chunks, 1):
        print(f"    distill chunk {i}/{len(chunks)} ({conv['model']})…", flush=True)
        text, c = "", 0.0
        for attempt in (1, 2, 3):
            try:
                text, c = asyncio.run(asyncio.wait_for(
                    _distill_chunk(chunk, conv["model"]), timeout))
                break
            except Exception as e:  # noqa: BLE001 — includes TimeoutError + CLI flake
                if attempt < 3:
                    print(f"      chunk {i} error ({e}) — retry {attempt}/2", flush=True)
                    _time.sleep(5 * attempt)
                    continue
                print(f"      chunk {i} FAILED after 3 attempts — skipping (visibly noted)",
                      flush=True)
                failed.append(i)
        cost += c
        # Sentinel handling must be EXACT-match, never substring: conversations about
        # this very pipeline legitimately CONTAIN the sentinel strings (first meta-KB
        # smoke finding — both backfills quarantined on their own summaries). Errors
        # surface as exceptions from the retry wrapper above, not as in-band text.
        ts = text.strip()
        if not ts or ts == "FLUSH_OK" or (len(ts) <= 40 and "FLUSH_OK" in ts):
            continue
        header = "" if len(chunks) == 1 else f"<!-- chunk {i}/{len(chunks)} -->\n"
        parts.append(header + text)
    if failed and parts:
        parts.append(f"_⚠ chunk(s) {', '.join(map(str, failed))} of {len(chunks)} failed to "
                     f"distill after 3 attempts — that span of the conversation is NOT captured "
                     f"here (transcript retained; re-ingest later if needed)._")
    return "\n\n".join(parts), cost, failed


def append_backfill_entry(conv: dict, body: str) -> Path:
    """Write the distilled body into the conversation's OWN dated daily (so the
    born-stamp vintage is the knowledge's true date, not today)."""
    log_path = DAILY_DIR / f"{conv['date']}.md"
    if not log_path.exists():
        log_path.write_text(
            f"# Daily Log: {conv['date']}\n\n## Sessions\n\n## Memory Maintenance\n\n",
            encoding="utf-8")
    span = f"{conv['date']}→{conv['last_iso'][:10]}" if conv["last_iso"][:10] != conv["date"] else conv["date"]
    # The project also goes in the BODY, matching flush.py: compile reads this tag
    # as authoritative for `project:`. It is in the header too, but the header is a
    # human label that compile splits on — parsing it would couple the compiler to
    # a display string.
    entry = (f"### Session (backfill {conv['sid'][:8]} · {span} · "
             f"{conv['project']})\n\n**Project:** {conv['project']}\n\n{body}\n\n")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)
    return log_path


# ── snapshot / restore (quarantine machinery) ────────────────────────────────

def take_snapshot(sid: str, daily_file: Path) -> Path:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    snap = SNAPSHOTS / f"{sid}.tar"
    with tarfile.open(snap, "w") as tar:
        tar.add(KNOWLEDGE_DIR, arcname="knowledge")
        if daily_file.exists():
            tar.add(daily_file, arcname=f"daily/{daily_file.name}")
        state = SCRIPTS / "state.json"
        if state.exists():
            tar.add(state, arcname="scripts/state.json")
    return snap


def restore_snapshot(snap: Path, daily_file: Path) -> None:
    import shutil
    shutil.rmtree(KNOWLEDGE_DIR, ignore_errors=True)
    daily_file.unlink(missing_ok=True)  # tar restores it only if it pre-existed
    with tarfile.open(snap) as tar:
        tar.extractall(ROOT, filter="data")


# ── regression + verification gates ─────────────────────────────────────────

def article_fingerprints() -> dict[str, tuple[int, str]]:
    """{stem: (word_count, updated)} for every article."""
    out = {}
    for sub in ("concepts", "connections", "qa", "mocs"):
        d = KNOWLEDGE_DIR / sub
        if not d.exists():
            continue
        for p in d.glob("*.md"):
            text = p.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"^updated:\s*(\S+)", text, re.M)
            out[p.stem] = (len(text.split()), m.group(1) if m else "")
    return out


def regression_check(before: dict, after: dict) -> list[str]:
    fails = []
    for stem, (w0, u0) in before.items():
        if stem not in after:
            fails.append(f"article REMOVED: {stem}")
            continue
        w1, u1 = after[stem]
        if w0 >= 200 and w1 < w0 * 0.7:
            fails.append(f"content loss: {stem} shrank {w0}→{w1} words")
        if u0 and u1 and u1[:10] < u0[:10]:
            fails.append(f"backwards updated: {stem} {u0[:10]}→{u1[:10]}")
    return fails


def fabricated_tokens() -> set[tuple[str, str]]:
    """{(slug, token)} currently classified COMPILE-FABRICATED by the Tier-1 gate."""
    import verify
    repo_tokens, norm = verify.build_repo_index(verify.INDEX_ROOTS)
    out = set()
    for p in verify._articles(None):
        r = verify.verify_article(p, repo_tokens, norm)
        for tok, info in r["true_misses"].items():
            if info["disposition"] == "COMPILE-FABRICATED":
                out.add((r["slug"], tok))
    return out


# ── per-conversation pipeline ─────────────────────────────────────────────────

def run_compile(daily_file: Path) -> tuple[bool, float]:
    """compile.py --file <daily>; returns (ok, cost).

    Spawned under the SHARED venv with the launcher's env contract (v0.9.27+) —
    `uv run --directory <kb>` would rebuild a per-KB .venv from the KB's
    pyproject.toml, the layout v0.9.27 deleted."""
    proc = subprocess.run(
        [devlore_python(), str(SCRIPTS / "compile.py"), "--file", str(daily_file)],
        capture_output=True, text=True, cwd=str(ROOT), env=devlore_child_env(ROOT))
    out = proc.stdout + proc.stderr
    m = re.search(r"Total cost: \$([\d.]+)", out)
    ok = proc.returncode == 0 and "Another compile is already running" not in out
    return ok, float(m.group(1)) if m else 0.0


def write_marker(sid: str, last_iso: str) -> None:
    (SCRIPTS / f"flush-marker-{sid}.json").write_text(
        json.dumps({"last_ts": last_iso, "updated": now_iso(),
                    "via": "ingest_all_context"}), encoding="utf-8")


def quarantine(sid: str, body: str, reasons: list[str]) -> Path:
    QUARANTINE.mkdir(exist_ok=True)
    qf = QUARANTINE / f"{sid}.md"
    qf.write_text(
        f"# QUARANTINED backfill — session {sid}\n\n"
        f"> Failed the ingest gates on {now_iso()}; knowledge base was restored to its\n"
        f"> pre-ingest snapshot. Review, fix, and re-ingest manually (`/devlore`) or discard.\n\n"
        f"## Failure reasons\n\n" + "\n".join(f"- {r}" for r in reasons) +
        f"\n\n## Distilled content (not compiled)\n\n{body}\n", encoding="utf-8")
    return qf


def process(conv: dict, baseline_fabricated: set) -> dict:
    """Run one conversation through the gated pipeline. Returns a result record."""
    from activity import emit
    daily_file = DAILY_DIR / f"{conv['date']}.md"
    snap = take_snapshot(conv["sid"], daily_file)
    before = article_fingerprints()
    rec = {"sid": conv["sid"], "date": conv["date"], "status": "?", "cost": 0.0, "reasons": []}
    try:
        body, dcost, failed_chunks = distill(conv)
        rec["cost"] += dcost
        if not body.strip():
            rec["status"] = "empty (FLUSH_OK)"
            write_marker(conv["sid"], conv["last_iso"])
            (QUARANTINE / f"{conv['sid']}.md").unlink(missing_ok=True)
            return rec
        append_backfill_entry(conv, body)
        ok, ccost = run_compile(daily_file)
        rec["cost"] += ccost
        if not ok:
            raise RuntimeError("compile failed or lock held")
        fails = regression_check(before, article_fingerprints())
        new_fab = fabricated_tokens() - baseline_fabricated
        if new_fab:
            fails.append("new COMPILE-FABRICATED tokens: "
                         + ", ".join(f"{s}::{t}" for s, t in sorted(new_fab)[:6]))
        if fails:
            rec["status"] = "QUARANTINED"
            rec["reasons"] = fails
            restore_snapshot(snap, daily_file)
            quarantine(conv["sid"], body, fails)
            emit("ingest", "quarantined", f"backfill {conv['sid'][:8]} quarantined", "warn")
        else:
            rec["status"] = ("ingested" if not failed_chunks
                             else f"ingested ({len(failed_chunks)} chunk(s) skipped)")
            write_marker(conv["sid"], conv["last_iso"])
            (QUARANTINE / f"{conv['sid']}.md").unlink(missing_ok=True)  # stale prior failure
            emit("ingest", "done", f"backfilled {conv['sid'][:8]} ({conv['date']}) ${rec['cost']:.2f}")
    except (Exception, KeyboardInterrupt) as e:
        rec["status"] = "QUARANTINED"
        rec["reasons"] = [f"{type(e).__name__}: {e}"]
        restore_snapshot(snap, daily_file)
        try:
            quarantine(conv["sid"], locals().get("body", "(distill did not complete)"),
                       rec["reasons"])
        except Exception:
            pass
        emit("ingest", "error", f"backfill {conv['sid'][:8]} failed: {e}", "error")
        if isinstance(e, KeyboardInterrupt):
            raise
    return rec


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Gated batch backfill of never-flushed conversations.")
    ap.add_argument("--yes", action="store_true", help="Execute (default is dry-run plan+estimate).")
    ap.add_argument("--limit", type=int, default=0, help="Process only the oldest N candidates.")
    ap.add_argument("--session", help="Only the session whose id starts with this prefix.")
    ap.add_argument("--root", help="Only conversations whose codebase is this absolute path "
                                   "(its dir, subdirs, and worktrees) — scopes the sweep to one "
                                   "codebase, e.g. how `devlore add <dir>` backfills just <dir>.")
    ap.add_argument("--force", action="store_true",
                    help="With --session: re-ingest even if a flush marker exists (recover a "
                         "history hidden by a partial first-flush; the compiler dedupes overlap).")
    ap.add_argument("--force-model", choices=["haiku", "sonnet", "opus"],
                    help="Override tiering for ALL selected conversations.")
    ap.add_argument("--min-size", type=int, default=MIN_SIZE_DEFAULT)
    ap.add_argument("--haiku-max", type=int, default=HAIKU_MAX_DEFAULT)
    ap.add_argument("--max-chunks", type=int, default=MAX_CHUNKS_DEFAULT)
    args = ap.parse_args()
    if args.force and not args.session:
        ap.error("--force requires --session <sid>")

    convs, warm = discover(args.min_size, args.haiku_max, args.session,
                           force=args.force, only_root=args.root)
    if args.force_model:
        for c in convs:
            c["model"] = args.force_model
    skipped = [c for c in convs if c["chunks"] > args.max_chunks]
    convs = [c for c in convs if c["chunks"] <= args.max_chunks]
    if args.limit:
        convs = convs[:args.limit]
    if not convs:
        print("No candidate conversations (all flushed, too small, or filtered).")
        print_warm_todo(warm)
        return

    total = 0.0
    print(f"{'PLAN' if not args.yes else 'EXECUTING'} — {len(convs)} conversation(s), oldest first:\n")
    print(
        f"{'sid':<10} {'date':<11} {'agent':<12} {'project':<22} "
        f"{'size':>7} {'chunks':>6} {'model':<7} {'est. $':>7}"
    )
    for c in convs:
        d, comp, _parts = estimate(c)
        total += d + comp
        print(
            f"{c['sid'][:8]:<10} {c['date']:<11} "
            f"{c.get('agent', '')[:11]:<12} {c['project'][:21]:<22} "
            f"{c['size']/1e6:>6.1f}M {c['chunks']:>6} "
            f"{c['model']:<7} {d+comp:>7.2f}"
        )
    if skipped:
        print(f"\n⚠ {len(skipped)} conversation(s) exceed --max-chunks={args.max_chunks} and were "
              f"EXCLUDED (raise --max-chunks to include): "
              + ", ".join(s['sid'][:8] for s in skipped[:8]))
    print(f"\nEstimated total: ${total:.2f}  (compile-dominated; grows with wiki size — "
          f"estimates use ${COMPILE_COST_PER_PART:.0f}/compile-part)")
    print_warm_todo(warm)

    if not args.yes:
        print("\nDRY RUN — nothing executed. Re-run with --yes to ingest.")
        return

    print("\nBaseline Tier-1 sweep (for the new-fabrication gate)…", flush=True)
    baseline_fab = fabricated_tokens()

    results = []
    for i, conv in enumerate(convs, 1):
        print(f"\n[{i}/{len(convs)}] {conv['sid'][:8]} ({conv['date']}, {conv['model']}, "
              f"{conv['chunks']} chunk(s))", flush=True)
        results.append(process(conv, baseline_fab))

    print("\n" + "=" * 64)
    spent = sum(r["cost"] for r in results)
    for r in results:
        note = f"  ← {r['reasons'][0]}" if r["reasons"] else ""
        print(f"  {r['sid'][:8]}  {r['status']:<18} ${r['cost']:.2f}{note}")
    n_ok = sum(1 for r in results if r["status"] == "ingested")
    n_q = sum(1 for r in results if r["status"] == "QUARANTINED")
    print(f"Total spent: ${spent:.2f}  ({n_ok} ingested, {n_q} quarantined)")

    # Commit strategy: the compile subprocess commits per conversation; this final
    # commit sweeps in the markers/quarantine files written after those compiles.
    try:
        from kb_commit import kb_commit
        kb_commit(f"ingest: batch backfill — {n_ok} ingested, {n_q} quarantined (${spent:.2f})")
    except Exception:
        pass


if __name__ == "__main__":
    main()
