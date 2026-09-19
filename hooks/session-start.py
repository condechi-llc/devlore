"""
SessionStart hook - injects knowledge base context into every conversation.

This is the "context injection" layer. When Claude Code or Codex starts a session,
this hook reads the knowledge base index and recent daily log, then injects
them as additional context so Claude always "remembers" what it has learned.

Configure in .claude/settings.local.json or .codex/hooks.json:
{
    "hooks": {
        "SessionStart": [{
            "matcher": "",
            "command": "uv run python hooks/session-start.py"
        }]
    }
}
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from capture_gate import should_capture

# Paths relative to project root
ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
DAILY_DIR = ROOT / "daily"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"

MAX_CONTEXT_CHARS = 20_000
MAX_LOG_LINES = 30


def capture_health_note() -> str:
    """Warn when capture WILL fail, above everything else, or "" when it is fine.

    Capture runs through the system `claude` CLI. When its auth lapses, every
    chunk fails, the hook writes FLUSH_ERROR into the daily log, and the session
    ends looking normal — while a Claude Code desktop session keeps working,
    because the app refreshes its own OAuth in-process and the CLI reads the
    stored credential. Nothing the user sees says anything is wrong, so
    conversations are lost quietly until someone goes looking for knowledge that
    was never captured.

    Deterministic and cheap: one `claude auth status` (JSON, no tokens spent)
    plus an env check. Never raises — a broken check must not cost a session.
    """
    notes: list[str] = []
    try:
        sys.path.insert(0, str(Path.home() / ".devlore" / "lib"))
        sys.path.insert(0, str(ROOT / "scripts"))
        from config import system_cli_path
        cli = system_cli_path()
        if cli:
            r = subprocess.run([cli, "auth", "status"], capture_output=True,
                               text=True, timeout=10)
            # The CLI exits 0 while reporting failure, so the exit code proves
            # nothing — read the payload.
            try:
                logged_in = json.loads(r.stdout or "{}").get("loggedIn")
            except ValueError:
                # Unreadable answer (an older CLI without `auth status`, a wrapper
                # printing prose). Say so rather than swallowing it: treating an
                # unverifiable check as healthy is the same silence this warning
                # exists to break.
                logged_in = None
                notes.append(
                    "**Could not verify that capture will work.** `claude auth status` "
                    "answered something this hook cannot read, so whether flushes will "
                    "succeed is unknown. If knowledge stops appearing, check the CLI's "
                    "sign-in first — that is the usual cause.")
            if logged_in is False:
                notes.append(
                    "**Capture is OFF: the `claude` CLI is not signed in.** Every flush "
                    "this session will fail and its knowledge will be lost (you will see "
                    "`FLUSH_ERROR` in the daily log, and nothing else). Fix it in a real "
                    "terminal with `claude auth login`, then recover anything already "
                    "lost with `devlore backfill --session <id> --force` while the "
                    "transcripts are still on disk.")
    except Exception:
        pass  # never block a session over a health check
    # The other way capture dies silently: the Agent SDK prefers API-key auth over
    # the subscription's OAuth and fails with the uninformative "returned an error
    # result: success".
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        notes.append(
            "**`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` is set in this environment.** "
            "The Agent SDK prefers it over the subscription's OAuth, so capture may fail "
            "with the misleading message `returned an error result: success`. Unset it "
            "before the session ends if flushes start failing.")
    if not notes:
        return ""
    return "## \u26a0 Capture health\n\n" + "\n\n".join(notes)


def get_recent_log() -> str:
    """Read the most recent daily log (today or yesterday)."""
    today = datetime.now(timezone.utc).astimezone()

    for offset in range(2):
        date = today - timedelta(days=offset)
        log_path = DAILY_DIR / f"{date.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            # Return last N lines to keep context small
            recent = lines[-MAX_LOG_LINES:] if len(lines) > MAX_LOG_LINES else lines
            return "\n".join(recent)

    return "(no recent daily log)"


def build_context() -> str:
    """Assemble the context to inject into the conversation."""
    parts = []

    # Today's date
    today = datetime.now(timezone.utc).astimezone()
    parts.append(f"## Today\n{today.strftime('%A, %B %d, %Y')}")

    # Tier-2 code-parity nudge (PR D): flag compiled articles that cite code which has
    # changed since they were written. Deterministic (git + grep, ~1s), NO LLM. Placed
    # early so it survives context truncation. A bad scan must never block session start.
    try:
        from staleness import staleness_note
        note = staleness_note()
        if note:
            parts.append(note)
    except Exception:
        pass

    # Capture health FIRST: if capture is dead, everything below is knowledge the
    # user is about to lose. Placed above the index so context truncation cannot
    # eat it.
    try:
        note = capture_health_note()
        if note:
            parts.append(note)
    except Exception:
        pass

    # Knowledge base index (the core retrieval mechanism)
    if INDEX_FILE.exists():
        index_content = INDEX_FILE.read_text(encoding="utf-8")
        parts.append(f"## Knowledge Base Index\n\n{index_content}")
    else:
        parts.append("## Knowledge Base Index\n\n(empty - no articles compiled yet)")

    # Recent daily log
    recent_log = get_recent_log()
    parts.append(f"## Recent Daily Log\n\n{recent_log}")

    context = "\n\n---\n\n".join(parts)

    # Truncate if too long
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n\n...(truncated)"

    return context


def main():
    # SessionStart hooks receive JSON on stdin (session_id, cwd, source, ...).
    cwd = ""
    try:
        raw = sys.stdin.read()
        if raw.strip():
            cwd = (json.loads(raw) or {}).get("cwd", "")
    except Exception:
        cwd = ""

    # Only inject knowledge for opted-in directories (scripts/capture-roots).
    # Machinery sessions (e.g. KB-root subdirs) get neither recall nor capture.
    if not should_capture(cwd):
        return

    context = build_context()

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
