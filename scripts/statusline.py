#!/usr/bin/env python3
"""
Status line: shows how many conversation turns have happened since the memory
system last *saved* knowledge for this session.

Why this matters: the flush hooks capture only the last MAX_TURNS turns / 15K
chars at compact/session-end time. On a long-lived session that compacts at
hundreds of K tokens, anything older than that window scrolls out and is lost
(the flush returns FLUSH_OK). This status line makes the danger visible:

  - "since save" counts turns since the last flush that actually *saved*
    (a FLUSH_OK or FLUSH_ERROR does NOT reset it — so a session that keeps
    dropping knowledge shows a climbing number).
  - It warns as you approach the capture window, and again once you've passed
    it (older turns can no longer be captured by a compact).

Reads Claude Code's status-line JSON from stdin (session_id, transcript_path,
model). Stdlib only; meant to run under the project venv python for speed.
"""

import json
import os
import sys
import re
import time
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
FLUSH_LOG = SCRIPTS_DIR / "flush.log"
COMPILE_STATUS = SCRIPTS_DIR / "compile.status.json"

# Defense-in-depth gate: only emit this KB's segment for sessions inside its
# working roots — the KB directory itself plus every captured tree. Derived from
# scripts/capture-roots (the same opt-in file the hooks use) so the gate is
# replicable: a bootstrapped KB gets the right roots with zero edits here.
def _vault_roots() -> list[str]:
    roots = [str(SCRIPTS_DIR.parent)]
    try:
        for line in (SCRIPTS_DIR / "capture-roots").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                roots.append(os.path.normpath(line.rstrip("/")))
    except OSError:
        pass
    return roots


VAULT_ROOTS = _vault_roots()


def within_vault(cwd: str) -> bool:
    if not cwd:
        return False
    cwd = os.path.normpath(cwd)
    return any(cwd == root or cwd.startswith(root + os.sep) for root in VAULT_ROOTS)

from capture_config import get_limits  # noqa: E402

# Reuse the hooks' opt-in gate so the status line knows whether THIS dir is even
# captured (if not, there's no "since save" to show — it would just climb red).
sys.path.insert(0, str(SCRIPTS_DIR.parent / "hooks"))
from capture_gate import should_capture  # noqa: E402

WINDOW = get_limits()["max_turns"]      # the hooks' capture window (scripts/capture-config)
WARN_AT = max(1, int(WINDOW * 0.66))    # approaching the window (~2/3)
DANGER_AT = WINDOW                       # at/over the window

# ANSI colors (Claude Code renders these in the status line)
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
BOLD = "\033[1m"
RESET = "\033[0m"

# Cycled per render so the gear visibly "spins" via color while a compile runs.
SPIN_COLORS = [CYAN, BLUE, MAGENTA, GREEN]


def read_stdin() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def compiling_now() -> bool:
    """True iff a compile is actually running (state running + pid alive)."""
    try:
        data = json.loads(COMPILE_STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if data.get("state") != "running":
        return False
    pid = data.get("pid")
    # Guard against a stale 'running' left by a hard-killed compile.
    try:
        return bool(pid) and (os.kill(int(pid), 0) or True)
    except (OSError, ValueError):
        return False


def gear() -> str:
    """A color-cycling gear, shown only while a compile runs. The color changes
    each render so it reads as 'working'; it disappears when the compile ends."""
    if not compiling_now():
        return ""
    color = SPIN_COLORS[int(time.time() * 3) % len(SPIN_COLORS)]
    return f" {color}{BOLD}⚙{RESET}"


def parse_iso(ts: str):
    """Parse a transcript ISO timestamp -> naive local datetime."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().replace(tzinfo=None)
    except Exception:
        return None


def count_turns(transcript_path: str):
    """Return (total_turns, [timestamps]) matching the flush hooks' turn logic:
    user/assistant messages that carry non-empty text (tool-only msgs skipped)."""
    stamps = []
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message", {})
                if isinstance(msg, dict):
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                else:
                    role = entry.get("role", "")
                    content = entry.get("content", "")
                if role not in ("user", "assistant"):
                    continue
                has_text = False
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                            has_text = True
                        elif isinstance(b, str) and b.strip():
                            has_text = True
                elif isinstance(content, str) and content.strip():
                    has_text = True
                if not has_text:
                    continue
                stamps.append(parse_iso(entry.get("timestamp")))
    except OSError:
        return 0, []
    return len(stamps), stamps


def last_save_time(session_id: str):
    """Scan flush.log for this session's last flush that actually SAVED.
    Returns (last_save_dt, last_flush_dt) as naive local datetimes (or None)."""
    if not session_id or not FLUSH_LOG.exists():
        return None, None
    active = None
    last_save = None
    last_flush = None
    flushing_re = re.compile(r"Flushing session ([0-9a-fA-F-]+)")
    try:
        with open(FLUSH_LOG, encoding="utf-8") as f:
            for line in f:
                m = flushing_re.search(line)
                if m:
                    active = m.group(1)
                    continue
                if active != session_id:
                    continue
                if "Result:" not in line:
                    continue
                try:
                    ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                last_flush = ts
                # Matches both "saved to daily log" (legacy) and the chunked-flush
                # "saved N/M chunk(s) to daily log" — a save is what resets the counter.
                if "to daily log" in line:
                    last_save = ts
    except OSError:
        return None, None
    return last_save, last_flush


def main():
    data = read_stdin()
    cwd = data.get("cwd") or (data.get("workspace") or {}).get("current_dir") or ""
    # Only contribute the KB segment for sessions inside the vault; other
    # projects get plain ccstatusline (empty output here).
    if not within_vault(cwd):
        return

    g = gear()  # compile indicator (global; shows regardless of capture)

    # If this dir isn't opted in for capture (scripts/capture-roots), there's no
    # "since save" to show — flushes are skipped here, so the counter would just
    # climb red forever. Show a neutral, dim 'untracked' instead.
    if not should_capture(cwd):
        sys.stdout.write(f"{DIM}🧠 untracked{RESET}{g}")
        return

    # LLM silence (`devlore --llm-silence`): turns are spooled, never "saved", so
    # the since-save counter would climb red over a state that is deliberate.
    # Checked as a bare file test to keep this script stdlib-only and fast.
    _home = Path(os.environ.get("DEVLORE_HOME") or Path.home() / ".devlore")
    if (_home / "llm-silence").exists():
        sys.stdout.write(f"{DIM}🧠 silenced{RESET}{g}")
        return

    session_id = data.get("session_id", "")
    transcript_path = data.get("transcript_path", "")

    total, stamps = count_turns(transcript_path)
    last_save, _ = last_save_time(session_id)

    # Turns since last actual save (FLUSH_OK does not reset this).
    if last_save is None:
        since = total
    else:
        since = sum(1 for s in stamps if s is None or s > last_save)

    if since >= DANGER_AT:
        color = RED + BOLD
    elif since >= WARN_AT:
        color = YELLOW
    else:
        color = GREEN

    # Minimal by design: emoji + counter + a gear that appears (color-cycling)
    # only while a compile runs. Color of the counter encodes the warn/danger
    # thresholds without taking words.
    sys.stdout.write(f"🧠 {color}{since}/{WINDOW}{RESET}{g}")


if __name__ == "__main__":
    main()
