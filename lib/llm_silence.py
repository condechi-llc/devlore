"""
LLM silence — one machine-wide switch that stops every LLM call devlore makes.

    devlore --llm-silence     stop the LLM
    devlore --llm-resume      let it run again
    devlore --llm-status      say which, and what is waiting in the spools

Three flags rather than `--llm-silence on|off`: "silence off" means "LLM on",
and a kill switch is the wrong place for a double negative.

devlore has no cron: its automatic spend is hook-driven. Every capture hook
spawns flush.py, which summarises the session delta with the Agent SDK, and
flush.py then spawns compile.py. While silence is on, none of that reaches the
LLM — but capture keeps COLLECTING: flush.py stores the raw delta in the KB's
`spool/` instead of summarising it (see flush.spool_capture), and `devlore
drain` summarises the spool once silence is lifted.

The switch is a single flag file under the devlore home, not a per-KB setting,
so it covers every registered KB at once and needs no KB context. Hooks are
spawned by the agent without the launcher's environment, so the path must be
derivable from nothing but the home directory.

Every LLM call site asks this module before importing the SDK:

    exit_if_silenced("compile")    command entry points — message on stderr,
                                   exit EXIT_SILENCED, nothing on stdout
    require_llm("flush")           inside a call path — raises LLMSilenced
    is_silenced()                  loops that should stop at the next boundary
                                   (a compile already running when silence is
                                   turned on finishes its current part and stops,
                                   rather than being killed mid-write)

Pure stdlib: this is imported by flush.py on every capture event.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DEVLORE_HOME = Path(os.environ.get("DEVLORE_HOME") or Path.home() / ".devlore")
FLAG_FILE = DEVLORE_HOME / "llm-silence"
SPOOL_DIRNAME = "spool"

# EX_TEMPFAIL: the request was fine, the service is deliberately unavailable.
# Distinct from 1 so a consumer (an agent calling `devlore ask`) can tell
# "silenced, try later" from a real failure without parsing prose.
EXIT_SILENCED = 75


class LLMSilenced(RuntimeError):
    """Raised by require_llm() when an LLM call is attempted during silence."""


def state() -> dict | None:
    """The silence record ({"since": <UTC ISO>}), or None when silence is off.

    A flag file that exists but cannot be parsed still means ON: the file's
    presence is the switch, and an unreadable switch must fail closed — failing
    open is the direction that spends money."""
    try:
        raw = FLAG_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return {"since": ""}
    try:
        rec = json.loads(raw)
        return rec if isinstance(rec, dict) else {"since": ""}
    except ValueError:
        return {"since": ""}


def is_silenced() -> bool:
    return state() is not None


def notice(what: str) -> str:
    since = (state() or {}).get("since", "")
    when = f" since {since}" if since else ""
    return (f"devlore: LLM silence is ON{when} — {what} did not run (no LLM call was "
            f"made). Resume with `devlore --llm-resume`.")


def require_llm(what: str) -> None:
    if is_silenced():
        raise LLMSilenced(notice(what))


def exit_if_silenced(what: str) -> None:
    if is_silenced():
        print(notice(what), file=sys.stderr)
        sys.exit(EXIT_SILENCED)


def enable() -> bool:
    """Turn silence on. Returns False if it was already on (the original `since`
    is kept — it records when the LLM actually stopped)."""
    if is_silenced():
        return False
    DEVLORE_HOME.mkdir(parents=True, exist_ok=True)
    FLAG_FILE.write_text(
        json.dumps({"since": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n",
        encoding="utf-8")
    return True


def disable() -> bool:
    """Turn silence off. Returns False if it was already off."""
    try:
        FLAG_FILE.unlink()
        return True
    except FileNotFoundError:
        return False


def spool_files(kb: Path) -> list[Path]:
    """Spooled captures in one KB, oldest first (names start with a UTC stamp)."""
    return sorted((Path(kb) / SPOOL_DIRNAME).glob("*.json"))


def _registered_kbs() -> dict[str, Path]:
    try:
        reg = json.loads((DEVLORE_HOME / "registry.json").read_text(encoding="utf-8"))
        return {name: Path(rec["path"]) for name, rec in reg.get("kbs", {}).items()
                if isinstance(rec, dict) and rec.get("path")}
    except (OSError, ValueError):
        return {}


def spool_report() -> list[str]:
    """One line per registered KB that has captures waiting to be drained."""
    lines = []
    for name, path in sorted(_registered_kbs().items()):
        n = len(spool_files(path))
        if n:
            lines.append(f"  {name}: {n} capture(s) spooled — `devlore drain --kb {name}`")
    return lines


def unsupported_kbs() -> list[tuple[str, Path]]:
    """Registered KBs whose flush.py predates LLM silence.

    The switch is machine-wide but flush.py is per-KB machinery, refreshed one KB
    at a time by `devlore update`. A KB that has not been updated never asks this
    module anything, so it keeps summarising and compiling straight through a
    silence — and nothing about that fails. Name them, every time."""
    stale = []
    for name, path in sorted(_registered_kbs().items()):
        flush = path / "scripts" / "flush.py"
        try:
            if "llm_silence" not in flush.read_text(encoding="utf-8"):
                stale.append((name, path))
        except OSError:
            continue  # no flush.py: not a capturing KB (or gone) — nothing to warn about
    return stale


def _warn_unsupported() -> None:
    for name, path in unsupported_kbs():
        print(f"  ⚠ {name}: machinery predates LLM silence and IGNORES it — it keeps "
              f"calling the LLM until you run `devlore update --kb {path}`.")


def main() -> int:
    # The launcher maps each flag to one verb; anything after a flag is an error
    # rather than a guess (`--llm-silence off` must not be read as "silence").
    arg = sys.argv[1] if len(sys.argv) == 2 else ""
    if arg == "silence":
        fresh = enable()
        since = (state() or {}).get("since", "")
        print(f"LLM silence {'ON' if fresh else 'was already ON'}"
              f"{f' since {since}' if since else ''}.")
        print("  No flush summaries, no compiles, no ask/backfill/tier-3 — in every KB.")
        print("  Capture keeps collecting: session deltas are spooled raw in <kb>/spool/.")
        print("  A compile or backfill already running stops at its next part boundary.")
        print("  Resume with: devlore --llm-resume")
        _warn_unsupported()
        return 0
    if arg == "resume":
        was_on = disable()
        if is_silenced():
            print(f"LLM silence is still ON — could not remove {FLAG_FILE}.", file=sys.stderr)
            return 1
        print("LLM resumed — silence is OFF." if was_on
              else "LLM was not silenced — nothing to resume.")
        waiting = spool_report()
        if waiting:
            print("Captured while silent, not yet summarised (nothing is spent until you drain):")
            print("\n".join(waiting))
        return 0
    if arg == "status":
        rec = state()
        if rec is None:
            print("LLM silence: off")
        else:
            since = rec.get("since", "")
            print(f"LLM silence: ON{f' since {since}' if since else ''}")
            _warn_unsupported()
        waiting = spool_report()
        if waiting:
            print("\n".join(waiting))
        return 0
    print("usage: devlore --llm-silence | --llm-resume | --llm-status   (no arguments)",
          file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
