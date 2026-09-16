"""Stop hook (Claude Code) — bootstrap + safety-valve capture.

Claude Code's SessionEnd fires only once, at the very end of a session, and
PreCompact only when the context window fills. So a long session that hasn't
ended or compacted yet has captured NOTHING — and when it finally does, the
first flush keeps only the most recent `max_turns` turns and the marker it
writes hides the older ones from backfill (the first-flush footgun).

This hook closes that gap. It fires after every assistant turn and triggers an
early BACKGROUND flush so the delta marker is established while the session is
still small. After that, ordinary PreCompact/SessionEnd delta capture keeps
everything current — no exit, no waiting, no developer babysitting.

It spawns a flush only when:
  - BOOTSTRAP:     no save marker yet AND turns >= bootstrap_turns, or
  - SAFETY VALVE:  a marker exists but turns-since-save >= max_turns
                   (a long session that simply never compacted).
Otherwise it exits immediately. The turn count scan early-exits at the relevant
threshold, and a per-session debounce file prevents duplicate spawns while a
flush from an earlier turn is still running.

Codex does NOT need this: its Stop event already maps to session-end.py, which
captures on every turn. This hook is Claude-Code-only (registered for Claude's
Stop event in .claude/settings.local.json).

The hook itself does NO API calls — only local file I/O for speed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from capture_gate import should_capture, get_limits, resolve_worktree

# Recursion guard: flush.py runs the Agent SDK (a Claude Code subprocess) which
# would fire this very hook again.
if os.environ.get("CLAUDE_INVOKED_BY"):
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
STATE_DIR = SCRIPTS_DIR
# v0.9.25+ — transcripts moved to ~/.devlore/lib/. Add both paths so this hook
# works in pre-v0.9.25 (KB-local) and v0.9.25+ (shared lib) layouts.
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(Path.home() / ".devlore" / "lib"))
from transcripts import iter_transcript_turns, extract_delta, parse_iso  # noqa: E402
from utils import devlore_child_env, devlore_python  # noqa: E402

logging.basicConfig(
    filename=str(SCRIPTS_DIR / "flush.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [stop] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

_LIMITS = get_limits()                      # from scripts/capture-config (live per fire)
MAX_TURNS = _LIMITS["max_turns"]            # safety-valve threshold + first-flush window
BOOTSTRAP_TURNS = _LIMITS["bootstrap_turns"]  # auto first-flush threshold (0 = disabled)
DELTA_CAP_CHARS = _LIMITS["max_chars"]      # per-flush char cap (roll-forward defer)

# A flush spawned on one turn takes ~10-30s (Agent SDK) before it writes the
# marker. Without a debounce, the Stop hooks firing on the intervening turns
# would each see "no marker yet" and spawn again. This bounds spawns to one per
# window per session; flush.py's own 60s dedup is the second line of defense.
SPAWN_DEBOUNCE_SECONDS = 120


def load_last_ts(session_id: str):
    """The high-water timestamp of this session's last flush (None if never)."""
    p = SCRIPTS_DIR / f"flush-marker-{session_id}.json"
    if p.exists():
        try:
            return parse_iso(json.loads(p.read_text(encoding="utf-8")).get("last_ts"))
        except Exception:
            return None
    return None


def count_delta(transcript_path: Path, last_ts, limit: int) -> int:
    """Count the turns that WOULD be in this flush's delta, early-exiting at
    `limit` so a per-turn hook never does more work than the threshold needs."""
    n = 0
    for turn in iter_transcript_turns(transcript_path):
        if last_ts is None or turn.timestamp is None or turn.timestamp > last_ts:
            n += 1
            if n >= limit:
                break
    return n


def _spawn_file(session_id: str) -> Path:
    return SCRIPTS_DIR / f"stop-spawn-{session_id}"


def recently_spawned(session_id: str) -> bool:
    p = _spawn_file(session_id)
    try:
        return p.exists() and (time.time() - p.stat().st_mtime) < SPAWN_DEBOUNCE_SECONDS
    except OSError:
        return False


def mark_spawned(session_id: str) -> None:
    try:
        _spawn_file(session_id).write_text(str(time.time()), encoding="utf-8")
    except OSError as e:
        logging.error("Failed to write spawn debounce file: %s", e)


def main() -> None:
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

    # Opt-in gate: only capture sessions whose cwd is listed in scripts/capture-roots.
    if not should_capture(cwd):
        return

    if not transcript_path_str or not isinstance(transcript_path_str, str):
        return
    transcript_path = Path(transcript_path_str)
    if not transcript_path.exists():
        return

    last_ts = load_last_ts(session_id)

    # Decide whether an early flush is warranted (cheap, early-exiting count).
    if last_ts is None:
        if BOOTSTRAP_TURNS <= 0:
            return  # bootstrap disabled — leave first capture to PreCompact/SessionEnd
        if count_delta(transcript_path, None, BOOTSTRAP_TURNS) < BOOTSTRAP_TURNS:
            return
        reason = "bootstrap"
    else:
        if count_delta(transcript_path, last_ts, MAX_TURNS) < MAX_TURNS:
            return
        reason = "safety-valve"

    # Debounce: a flush spawned on a recent turn is probably still running.
    if recently_spawned(session_id):
        return

    logging.info("Stop %s flush: session=%s cwd=%s", reason, session_id, cwd)

    # Build the delta (roll-forward char cap, like PreCompact: this is an
    # intraday flush, not the last-chance one).
    try:
        context, turn_count, high_water_iso, deferred, truncated = extract_delta(
            transcript_path, last_ts, max_turns=MAX_TURNS, cap_chars=DELTA_CAP_CHARS,
        )
    except Exception as e:
        logging.error("Context extraction failed: %s", e)
        return

    if not context.strip() or turn_count < 1:
        return

    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    context_file = STATE_DIR / f"stop-context-{session_id}-{timestamp}.md"
    context_file.write_text(context, encoding="utf-8")

    flush_script = SCRIPTS_DIR / "flush.py"
    # Shared venv + launcher env contract (v0.9.27+): `uv run --directory <kb>`
    # would rebuild a per-KB .venv from the KB's pyproject.toml — the layout
    # v0.9.27 deleted. See utils.devlore_python / devlore_child_env.
    cmd = [
        devlore_python(), str(flush_script),
        str(context_file),
        session_id,
        high_water_iso or "none",
        str(deferred),
        str(truncated),
    ]
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    # Tag the daily entry with the source project (worktree-resolved), like the
    # other capture hooks. devlore_child_env layers the shared-lib PYTHONPATH and
    # DEVLORE_KB_ROOT on top — flush.py can't import capture_config or resolve the
    # right KB without them.
    flush_env = devlore_child_env(ROOT, {
        **os.environ,
        "DEVLORE_CAPTURE_PROJECT": Path(resolve_worktree(cwd)).name if cwd else "",
    })

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            env=flush_env,
        )
        mark_spawned(session_id)
        logging.info("Spawned %s flush for session %s (%d turns, %d chars)",
                     reason, session_id, turn_count, len(context))
    except Exception as e:
        logging.error("Failed to spawn flush.py: %s", e)


if __name__ == "__main__":
    main()
