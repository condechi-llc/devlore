"""
PreCompact hook - captures conversation transcript before auto-compaction.

When the agent's context window fills up, it auto-compacts (summarizes and
discards detail). This hook fires BEFORE that happens, extracting conversation
context and spawning flush.py to extract knowledge that would otherwise
be lost to summarization.

The hook itself does NO API calls - only local file I/O for speed (<10s).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from capture_gate import should_capture, get_limits, resolve_worktree

# Recursion guard
if os.environ.get("CLAUDE_INVOKED_BY"):
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
STATE_DIR = SCRIPTS_DIR
# v0.9.25+ — transcripts moved to ~/.devlore/lib/. Add both paths so this hook
# works in pre-v0.9.25 (KB-local) and v0.9.25+ (shared lib) layouts.
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(Path.home() / ".devlore" / "lib"))
from transcripts import extract_delta, parse_iso  # noqa: E402
from utils import devlore_python, devlore_child_env  # noqa: E402

logging.basicConfig(
    filename=str(SCRIPTS_DIR / "flush.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [pre-compact] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_LIMITS = get_limits()       # from scripts/capture-config (editable, no code change)
MAX_TURNS = _LIMITS["max_turns"]        # fallback tail when no save marker yet
DELTA_CAP_CHARS = _LIMITS["max_chars"]  # cap on a single captured context
MIN_TURNS_TO_FLUSH = 5

def load_last_ts(session_id: str):
    """The high-water timestamp of this session's last flush (None if never)."""
    p = SCRIPTS_DIR / f"flush-marker-{session_id}.json"
    if p.exists():
        try:
            return parse_iso(json.loads(p.read_text(encoding="utf-8")).get("last_ts"))
        except Exception:
            return None
    return None


def extract_conversation_context(transcript_path: Path, session_id: str) -> tuple[str, int, str, int, int]:
    """Delta capture since this session's last save (see transcripts.extract_delta).
    PreCompact applies the roll-forward char cap (keep oldest that fit, DEFER the
    newer overflow to the next flush) and — like SessionEnd — reports first-flush
    `truncated` so a capture that races past MAX_TURNS before any save leaves a
    visible recovery breadcrumb instead of dropping the oldest turns silently.
    Returns (context, kept_turn_count, high_water_iso, deferred, truncated)."""
    context, kept, high_water_iso, deferred, truncated = extract_delta(
        transcript_path, load_last_ts(session_id),
        max_turns=MAX_TURNS, cap_chars=DELTA_CAP_CHARS,
    )
    if deferred:
        logging.info(
            "Capture capped at %d chars: kept %d turn(s), DEFERRED %d to the next flush",
            DELTA_CAP_CHARS, kept, deferred,
        )
    if truncated:
        logging.info(
            "FIRST flush (no marker) at compaction: kept most recent %d turns, "
            "TRUNCATED %d earlier turn(s) — reported to flush.py for a recovery breadcrumb",
            MAX_TURNS, truncated,
        )
    return context, kept, high_water_iso, deferred, truncated


def main() -> None:
    # Read hook input from stdin
    try:
        raw_input = sys.stdin.read()
        try:
            hook_input: dict = json.loads(raw_input)
        except json.JSONDecodeError:
            fixed_input = re.sub(r'(?<!\\)\\(?!["\\])', r'\\\\', raw_input)
            hook_input = json.loads(fixed_input)
    except (json.JSONDecodeError, ValueError, EOFError) as e:
        logging.error("Failed to parse stdin: %s", e)
        return

    session_id = hook_input.get("session_id", "unknown")
    transcript_path_str = hook_input.get("transcript_path", "")
    cwd = hook_input.get("cwd", "")

    logging.info("PreCompact fired: session=%s cwd=%s", session_id, cwd)

    # Opt-in gate: only capture sessions whose cwd is listed in scripts/capture-roots.
    if not should_capture(cwd):
        logging.info("SKIP: cwd not opted-in for capture: %s", cwd)
        return

    # transcript_path can be empty (known Claude Code bug #13668)
    if not transcript_path_str or not isinstance(transcript_path_str, str):
        logging.info("SKIP: no transcript path")
        return

    transcript_path = Path(transcript_path_str)
    if not transcript_path.exists():
        logging.info("SKIP: transcript missing: %s", transcript_path_str)
        return

    # Extract conversation context in the hook (delta since last save)
    try:
        context, turn_count, high_water_iso, deferred, truncated = extract_conversation_context(transcript_path, session_id)
    except Exception as e:
        logging.error("Context extraction failed: %s", e)
        return

    if not context.strip():
        logging.info("SKIP: empty context")
        return

    if turn_count < MIN_TURNS_TO_FLUSH:
        logging.info("SKIP: only %d turns (min %d)", turn_count, MIN_TURNS_TO_FLUSH)
        return

    # Write context to a temp file for the background process
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    context_file = STATE_DIR / f"flush-context-{session_id}-{timestamp}.md"
    context_file.write_text(context, encoding="utf-8")

    # Spawn flush.py as a background process
    flush_script = SCRIPTS_DIR / "flush.py"

    # Spawn under the shared venv directly. `uv run --directory <kb>` (what this
    # was) makes uv materialize a per-KB .venv from that KB's pyproject.toml on
    # EVERY session event — resurrecting the very per-KB venvs v0.9.27 removed.
    # These two hooks fire far more often than the Stop bootstrap, so they were
    # the main engine of that resurrection.
    cmd = [
        devlore_python(),
        str(flush_script),
        str(context_file),
        session_id,
        high_water_iso or "none",
        str(deferred),
        str(truncated),     # first-flush truncation — earlier turns hidden from backfill
    ]

    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    # Tell flush.py which codebase this session ran in (worktree-resolved), so the
    # daily entry it writes is tagged with its source project.
    # Without uv the child no longer inherits an interpreter that can find the
    # shared modules, so PYTHONPATH and DEVLORE_KB_ROOT must be supplied explicitly.
    flush_env = devlore_child_env(ROOT, {
        **os.environ,
        "DEVLORE_CAPTURE_PROJECT": Path(resolve_worktree(cwd)).name if cwd else ""})

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            env=flush_env,
        )
        logging.info("Spawned flush.py for session %s (%d turns, %d chars)", session_id, turn_count, len(context))
    except Exception as e:
        logging.error("Failed to spawn flush.py: %s", e)


if __name__ == "__main__":
    main()
