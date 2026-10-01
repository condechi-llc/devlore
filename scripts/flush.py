"""
Memory flush agent - extracts important knowledge from conversation context.

Spawned by session-end.py or pre-compact.py as a background process. Reads
pre-extracted conversation context from a .md file, uses the Claude Agent SDK
to decide what's worth saving, and appends the result to today's daily log.

While LLM silence is on (`devlore --llm-silence`, see llm_silence.py) the delta
is NOT summarised: it is stored raw in the KB's spool/ and summarised later by
`devlore drain` (flush.py --drain), once silence is lifted.

Usage:
    uv run python flush.py <context_file.md> <session_id>
    uv run python flush.py --drain [--yes]
"""

from __future__ import annotations

# Recursion prevention: set this BEFORE any imports that might trigger Claude
import os
os.environ["CLAUDE_INVOKED_BY"] = "memory_flush"

import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "daily"
SCRIPTS_DIR = ROOT / "scripts"
STATE_FILE = SCRIPTS_DIR / "last-flush.json"
LOG_FILE = SCRIPTS_DIR / "flush.log"

# Shared machinery lives at ~/.devlore/lib/ (v0.9.25+). Add both <kb>/scripts
# (for any KB-local helpers) and the shared lib (for capture_config, config,
# transcripts, etc.). Hooks now spawn this under the shared venv WITH
# PYTHONPATH set (v0.9.28) — but a stray `python3 flush.py` from a shell, or a
# hook config still carrying a pre-v0.9.28 spelling, arrives with neither, so
# these inserts remain the only thing that reliably finds the shared modules.
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(Path.home() / ".devlore" / "lib"))

# Delta capture: a since-last-save delta can be large (a long session compacted
# at hundreds of K tokens). Split it into context-sized chunks so each flush LLM
# call stays bounded, then summarize each. MAX_CHUNKS caps cost on pathological
# deltas (the status line warns the user long before this). chunk size comes
# from scripts/capture-config (editable, no code change).
import llm_silence  # noqa: E402
from capture_config import get_limits  # noqa: E402
from config import system_cli_path  # noqa: E402
from utils import devlore_child_env, devlore_python  # noqa: E402

CHUNK_CHARS = get_limits()["chunk_chars"]
MAX_CHUNKS = 24


def chunk_context(context: str) -> list[str]:
    """Split context into <=CHUNK_CHARS pieces on turn boundaries."""
    if len(context) <= CHUNK_CHARS:
        return [context]
    chunks: list[str] = []
    cur = ""
    for turn in re.split(r"(?=\*\*(?:User|Assistant):\*\*)", context):
        if cur and len(cur) + len(turn) > CHUNK_CHARS:
            chunks.append(cur)
            cur = turn
        else:
            cur += turn
    if cur.strip():
        chunks.append(cur)
    if len(chunks) > MAX_CHUNKS:
        logging.info("Delta split into %d chunks; keeping most recent %d", len(chunks), MAX_CHUNKS)
        chunks = chunks[-MAX_CHUNKS:]
    return chunks


def write_marker(session_id: str, high_water_iso: str) -> None:
    """Advance this session's delta high-water mark so the next flush only
    captures turns newer than what we just processed."""
    marker = SCRIPTS_DIR / f"flush-marker-{session_id}.json"
    try:
        marker.write_text(
            json.dumps({
                "last_ts": high_water_iso,
                "updated": datetime.now(timezone.utc).astimezone().isoformat(),
            }),
            encoding="utf-8",
        )
    except OSError as e:
        logging.error("Failed to write delta marker: %s", e)

# Set up file-based logging so we can verify the background process ran.
# The parent process sends stdout/stderr to DEVNULL (to avoid the inherited
# file handle bug on Windows), so this is our only observability channel.
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def load_flush_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_flush_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def append_to_daily_log(content: str, section: str = "Session", project: str = "",
                        when: datetime | None = None) -> None:
    """Append content to the daily log of `when` (default: now).

    A drained capture passes the moment it was CAPTURED, so its entry lands in
    the daily of the day the conversation happened, not the day silence ended."""
    today = (when or datetime.now(timezone.utc)).astimezone()
    log_path = DAILY_DIR / f"{today.strftime('%Y-%m-%d')}.md"

    if not log_path.exists():
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"# Daily Log: {today.strftime('%Y-%m-%d')}\n\n## Sessions\n\n## Memory Maintenance\n\n",
            encoding="utf-8",
        )

    time_str = today.strftime("%H:%M")
    # The source project goes in the entry BODY, not the header: compile splits and
    # hashes entries off ENTRY_HEADER_RE (`### Session (`), so the header MUST stay
    # verbatim. As a body line it still gives compile (and a human skimming a daily
    # that mixes several projects) ground truth about which codebase an entry came
    # from — the signal that keeps one shared KB from cross-attributing knowledge.
    # Redact before anything is written. A credential in the conversation would
    # otherwise land in the daily, be committed, and become eligible for an
    # article — see condechi-llc/devlore#16. Kinds and counts are logged; values
    # never are, since this log is itself captured.
    try:
        from utils import redact_secrets, redaction_note
        content, _found = redact_secrets(content)
        if _found:
            logging.warning("flush: %s", redaction_note(_found))
            print(f"  ⚠ {redaction_note(_found)}")
    except Exception as _e:
        logging.warning("flush: redaction skipped (%s)", _e)
    tag = f"**Project:** {project}\n\n" if project else ""
    entry = f"### {section} ({time_str})\n\n{tag}{content}\n\n"

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)


async def run_flush(context: str) -> str:
    """Use Claude Agent SDK to extract important knowledge from conversation context."""
    llm_silence.require_llm("flush")
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    prompt = f"""Review the conversation context below and respond with a concise summary
of important items that should be preserved in the daily log.
Do NOT use any tools — just return plain text.

Format your response as a structured daily log entry with these sections:

**Context:** [One line about what the user was working on]

**Key Exchanges:**
- [Important Q&A or discussions]

**Decisions Made:**
- [Any decisions with rationale]

**Lessons Learned:**
- [Gotchas, patterns, or insights discovered]

**Action Items:**
- [Follow-ups or TODOs mentioned]

Skip anything that is:
- Routine tool calls or file reads
- Content that's trivial or obvious
- Trivial back-and-forth or clarification exchanges

Only include sections that have actual content. If nothing is worth saving,
respond with exactly: FLUSH_OK

## Conversation Context

{context}"""

    response = ""

    # Use the system CLI, not the SDK's stale/flaky bundled one (see system_cli_path).
    opts = dict(cwd=str(ROOT), allowed_tools=[], max_turns=2)
    cli = system_cli_path()
    if cli:
        opts["cli_path"] = cli

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(**opts),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
            elif isinstance(message, ResultMessage):
                pass
    except Exception as e:
        import traceback
        logging.error("Agent SDK error: %s\n%s", e, traceback.format_exc())
        response = f"FLUSH_ERROR: {type(e).__name__}: {e}"

    return response


COMPILE_AFTER_HOUR = 18  # 6 PM local time


def _daily_log_is_stale(log_path: Path, ingested: dict) -> bool:
    """True if this daily log has never been compiled, or changed since last compile."""
    from hashlib import sha256

    rec = ingested.get(log_path.name)
    if not rec:
        return True
    try:
        current_hash = sha256(log_path.read_bytes()).hexdigest()[:16]
    except OSError:
        return False
    return rec.get("hash") != current_hash


def has_pending_compilation() -> bool:
    """Decide whether compile.py should run now.

    Two independent triggers, so knowledge never gets stranded:
      1. Any *past-day* log that is uncompiled or changed -> compile anytime. Past
         days are complete, so it's always safe, and this catches up backlog that
         accumulates when no session happens to end after the compile hour.
      2. *Today's* log -> only once we're past COMPILE_AFTER_HOUR, so we don't
         recompile the in-progress day on every intraday flush.
    """
    now = datetime.now(timezone.utc).astimezone()
    today_log = f"{now.strftime('%Y-%m-%d')}.md"

    compile_state_file = SCRIPTS_DIR / "state.json"
    ingested: dict = {}
    if compile_state_file.exists():
        try:
            compile_state = json.loads(compile_state_file.read_text(encoding="utf-8"))
            ingested = compile_state.get("ingested", {})
        except (json.JSONDecodeError, OSError):
            ingested = {}

    for log_path in sorted(DAILY_DIR.glob("*.md")):
        is_today = log_path.name == today_log
        if is_today and now.hour < COMPILE_AFTER_HOUR:
            continue  # don't recompile today's still-in-progress log before the cutoff
        if _daily_log_is_stale(log_path, ingested):
            return True
    return False


def maybe_trigger_compilation() -> None:
    """Run compile.py in the background if any daily log is pending compilation."""
    import subprocess as _sp

    if not has_pending_compilation():
        return

    compile_script = SCRIPTS_DIR / "compile.py"
    if not compile_script.exists():
        return

    logging.info("Compilation triggered: pending daily log(s) detected")

    # Spawn under the SHARED venv with the launcher's env contract (v0.9.27+).
    # `uv run --directory <kb>` would rebuild a per-KB .venv from the KB's
    # pyproject.toml — the layout v0.9.27 deleted. See utils.devlore_python.
    cmd = [devlore_python(), str(compile_script)]

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP | _sp.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    try:
        log_handle = open(str(SCRIPTS_DIR / "compile.log"), "a")
        _sp.Popen(cmd, stdout=log_handle, stderr=_sp.STDOUT, cwd=str(ROOT),
                  env=devlore_child_env(ROOT), **kwargs)
    except Exception as e:
        logging.error("Failed to spawn compile.py: %s", e)


# ── LLM silence: collect without summarising ──────────────────────────
# One JSON file per capture under <kb>/spool/. It is raw conversation, so it is
# gitignored (never committed by kb_commit) and credential-redacted on the way in
# — the same root-not-write rule as the daily log (condechi-llc/devlore#16).
SPOOL_DIR = ROOT / llm_silence.SPOOL_DIRNAME


def spool_capture(context: str, session_id: str, project: str,
                  high_water: str, deferred: int, truncated: int) -> Path:
    """Store one raw session delta for `devlore drain`. Returns the spool file."""
    try:
        from utils import redact_secrets, redaction_note
        context, _found = redact_secrets(context)
        if _found:
            logging.warning("spool: %s", redaction_note(_found))
    except Exception as _e:
        logging.warning("spool: redaction skipped (%s)", _e)
    now = datetime.now(timezone.utc)
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    safe_sid = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
    # UTC stamp first so a plain name sort is capture order, which is drain order.
    dest = SPOOL_DIR / f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{safe_sid}.json"
    record = {
        "v": 1,
        "session_id": session_id,
        "captured_utc": now.isoformat(timespec="seconds"),
        "project": project,
        "high_water": high_water,
        "deferred": deferred,
        "truncated": truncated,
        "context": context,
    }
    # Write-then-rename: a drain must never read a half-written capture.
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    tmp.replace(dest)
    return dest


def summarize_and_append(context: str, session_id: str, project: str,
                         deferred: int, truncated: int,
                         when: datetime | None = None) -> bool:
    """Summarise one delta (chunked) and append the result to the daily log of
    `when` (default: now). Shared by the live flush and by drain.

    Returns True when a chunk failed in transport — the caller must then NOT
    treat the delta as processed (re-capture is tolerable; silent loss is not)."""
    # Split a (possibly large) delta into bounded chunks and summarize each.
    chunks = chunk_context(context)
    logging.info("Flushing session %s: %d chars in %d chunk(s)", session_id, len(context), len(chunks))

    saved_parts: list[str] = []
    had_error = False
    for idx, chunk in enumerate(chunks, 1):
        response = asyncio.run(run_flush(chunk))
        resp_s = response.strip()
        # Sentinels must be matched exactly (prefix for the code-generated error, exact
        # for the model's nothing-to-save reply) — NEVER by substring: a conversation
        # about this pipeline legitimately contains these strings in its summary.
        if resp_s.startswith("FLUSH_ERROR"):
            logging.error("Chunk %d/%d: %s", idx, len(chunks), response)
            had_error = True
        elif resp_s == "FLUSH_OK" or (len(resp_s) <= 40 and "FLUSH_OK" in resp_s):
            logging.info("Chunk %d/%d: FLUSH_OK", idx, len(chunks))
        else:
            header = "" if len(chunks) == 1 else f"<!-- chunk {idx}/{len(chunks)} -->\n"
            saved_parts.append(header + response)

    # A visible note in the daily log when the size cap deferred earlier turns to
    # the next flush (so it's not a silent truncation; they are NOT lost — the
    # next flush captures them. Raise max_chars in scripts/capture-config to take
    # bigger bites per flush).
    defer_note = ""
    if deferred > 0:
        cap = get_limits()["max_chars"]
        defer_note = (
            f"\n\n_⚠ {deferred} earlier turn(s) exceeded the {cap}-char capture cap "
            f"and were deferred to the next flush._"
        )
        logging.info("Daily note: %d turn(s) deferred to next flush", deferred)
    if truncated > 0:
        # Unlike `deferred`, these turns will NOT arrive on a later flush — the
        # marker this flush writes hides them from the backfill too. Leave a
        # visible recovery breadcrumb (devlore smoke finding #8).
        window = get_limits()["max_turns"]
        defer_note += (
            f"\n\n_⚠ FIRST capture of this session caught only its most recent "
            f"{window} turns; {truncated} earlier turn(s) are NOT in the daily and the "
            f"flush marker hides them from plain backfill. Recover the full history "
            f"with: `devlore backfill --session {session_id} --force`._"
        )
        logging.info("Daily note: first-flush truncated %d earlier turn(s)", truncated)

    # Append to daily log + emit a background-activity event (for the notifier).
    from activity import emit
    if saved_parts:
        append_to_daily_log("\n\n".join(saved_parts) + defer_note, "Session", project, when)
        logging.info("Result: saved %d/%d chunk(s) to daily log", len(saved_parts), len(chunks))
        emit("flush", "saved", f"captured ~{max(1, len(context)//1000)}K of dialogue → daily")
    elif had_error:
        append_to_daily_log("FLUSH_ERROR during delta flush — see flush.log" + defer_note,
                            "Memory Flush", project, when)
        emit("flush", "error", "flush failed (CLI error — see flush.log)", "error")
    else:
        logging.info("Result: FLUSH_OK")
        append_to_daily_log(
            "FLUSH_OK - Nothing worth saving from this session" + defer_note,
            "Memory Flush", project, when
        )
        emit("flush", "ok", "flush: nothing new worth saving")
    if deferred > 0:
        emit("flush", "deferred", f"{deferred} turn(s) deferred to next flush (size cap)", "warn")
    return had_error


def drain(execute: bool) -> int:
    """`devlore drain`: summarise the captures spooled during LLM silence.

    Plan by default, like backfill and compile — nothing is spent without --yes.
    Each capture lands in the daily log of the day it was captured and is removed
    from the spool only once it summarised cleanly; a failed one stays for the
    next drain. Compilation is left to `devlore compile` (or the next live flush),
    so --yes consents to the summaries and to nothing else."""
    if execute:
        llm_silence.exit_if_silenced("drain")
    pending = llm_silence.spool_files(ROOT)
    if not pending:
        print("Nothing spooled — no captures are waiting to be summarised.")
        return 0

    records: list[tuple[Path, dict]] = []
    for f in pending:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(rec, dict) or not isinstance(rec.get("context"), str):
                raise ValueError("not a spool record")
            records.append((f, rec))
        except (OSError, ValueError) as e:
            print(f"  ⚠ {f.name}: unreadable ({e}) — left in place")

    print(f"{'DRAINING' if execute else 'PLAN'} — {len(records)} spooled capture(s), oldest first:\n")
    print(f"{'captured (UTC)':<21} {'session':<10} {'project':<22} {'chars':>8} {'chunks':>6}")
    for _f, rec in records:
        ctx = rec["context"]
        print(f"{rec.get('captured_utc', '')[:19]:<21} {rec.get('session_id', '')[:8]:<10} "
              f"{rec.get('project', '')[:21]:<22} {len(ctx):>8} {len(chunk_context(ctx)):>6}")
    total_chunks = sum(len(chunk_context(rec["context"])) for _f, rec in records)
    print(f"\n{total_chunks} summariser call(s) in total — one per chunk.")

    if not execute:
        print("\nDRY RUN — nothing executed. Re-run with --yes to summarise into daily/.")
        return 0

    done = failed = 0
    for f, rec in records:
        # Silence turned on mid-drain: stop here, the rest stays spooled.
        if llm_silence.is_silenced():
            print("LLM silence was turned on — stopping; the rest stays spooled.")
            break
        sid = rec.get("session_id", "unknown")
        try:
            when = datetime.fromisoformat(rec["captured_utc"])
        except (KeyError, TypeError, ValueError):
            when = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
        print(f"  {sid[:8]} ({rec.get('captured_utc', '')[:19]})…", flush=True)
        try:
            had_error = summarize_and_append(
                rec["context"], sid, rec.get("project", ""),
                int(rec.get("deferred") or 0), int(rec.get("truncated") or 0), when)
        except llm_silence.LLMSilenced:
            print("LLM silence was turned on — stopping; the rest stays spooled.")
            break
        if had_error:
            failed += 1
            print("    ✗ failed — kept in the spool (see flush.log)")
        else:
            f.unlink(missing_ok=True)
            done += 1
    print(f"\nDrained {done} capture(s) into daily/"
          f"{f'; {failed} failed and stay spooled' if failed else ''}.")
    if done:
        print("Next: `devlore compile` turns them into articles.")
    return 1 if failed else 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--drain":
        sys.exit(drain(execute="--yes" in sys.argv[2:]))

    if len(sys.argv) < 3:
        logging.error("Usage: %s <context_file.md> <session_id>", sys.argv[0])
        sys.exit(1)

    context_file = Path(sys.argv[1])
    session_id = sys.argv[2]
    # Delta high-water mark passed by the hook ("none" when unavailable).
    high_water = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "none" else ""
    # Turns the hook deferred to the next flush because of the size cap (0 if none).
    try:
        deferred = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    except ValueError:
        deferred = 0
    # FIRST-flush truncation: earlier turns NOT captured and — because this flush
    # writes the session marker — hidden from the batch backfill (finding #8).
    try:
        truncated = int(sys.argv[5]) if len(sys.argv) > 5 else 0
    except ValueError:
        truncated = 0

    # Source project label (basename of the capture cwd), set by the spawning hook
    # so each daily entry records WHICH codebase it came from. "" when unavailable.
    project = os.environ.get("DEVLORE_CAPTURE_PROJECT", "")

    logging.info("flush.py started for session %s, context: %s", session_id, context_file)

    if not context_file.exists():
        logging.error("Context file not found: %s", context_file)
        return

    # Deduplication: skip if same session was flushed within 60 seconds
    state = load_flush_state()
    if (
        state.get("session_id") == session_id
        and time.time() - state.get("timestamp", 0) < 60
    ):
        logging.info("Skipping duplicate flush for session %s", session_id)
        context_file.unlink(missing_ok=True)
        return

    # Read pre-extracted context
    context = context_file.read_text(encoding="utf-8").strip()
    if not context:
        logging.info("Context file is empty, skipping")
        context_file.unlink(missing_ok=True)
        return

    def _spool_instead() -> None:
        # LLM silence: keep collecting, summarise nothing. The spool now holds
        # this delta durably, so the marker advances exactly as after a real
        # flush — the next capture starts where this one ended instead of
        # spooling it twice. No compile is triggered: that is the other half of
        # what silence switches off.
        try:
            dest = spool_capture(context, session_id, project, high_water, deferred, truncated)
        except OSError as e:
            # Not spooled → leave the marker alone so the next event re-captures it.
            logging.error("LLM silence: could not spool session %s: %s", session_id, e)
            context_file.unlink(missing_ok=True)
            return
        logging.info("LLM silence: spooled %d chars for session %s → %s",
                     len(context), session_id, dest.name)
        from activity import emit
        emit("flush", "silenced",
             f"LLM silence: spooled ~{max(1, len(context)//1000)}K of dialogue (not summarised)")
        if high_water:
            write_marker(session_id, high_water)
        save_flush_state({"session_id": session_id, "timestamp": time.time()})
        context_file.unlink(missing_ok=True)

    if llm_silence.is_silenced():
        _spool_instead()
        return

    try:
        had_error = summarize_and_append(context, session_id, project, deferred, truncated)
    except llm_silence.LLMSilenced:
        # Silence was turned on between chunks. Nothing has been appended yet
        # (the daily entry is written only after the last chunk), so the whole
        # delta goes to the spool and no part of it is lost or duplicated.
        _spool_instead()
        return

    # Advance the delta marker so the next flush starts after what we just
    # processed. Skip on transport error so the delta is retried next time
    # (re-capture is tolerable; silent loss is not).
    if high_water and not had_error:
        write_marker(session_id, high_water)

    # Update dedup state
    save_flush_state({"session_id": session_id, "timestamp": time.time()})

    # Clean up context file
    context_file.unlink(missing_ok=True)

    # End-of-day auto-compilation: if it's past the compile hour and today's
    # log hasn't been compiled yet, trigger compile.py in the background.
    maybe_trigger_compilation()

    logging.info("Flush complete for session %s", session_id)


if __name__ == "__main__":
    main()
