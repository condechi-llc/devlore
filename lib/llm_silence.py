"""
LLM silence — one machine-wide switch that stops every LLM call devlore makes.

    devlore --llm-silence     stop the LLM (and, from a terminal, lock it)
    devlore --llm-resume      let it run again — a person, a terminal, sudo
    devlore --llm-status      say which, whether it is locked, and what is spooled

Three flags rather than `--llm-silence on|off`: "silence off" means "LLM on",
and a kill switch is the wrong place for a double negative.

Stopping is easy; starting again needs a person. Silence is a deliberate human
decision, and the processes it inconveniences — hooks, scripts, agent sessions —
are exactly the ones that must not be able to reverse it (condechi-llc/devlore#19):

  - `--llm-silence` takes effect at once, with no prompt: stopping must never
    wait on a password. Run from a terminal it then LOCKS the silence — the flag
    is written through sudo into a root-owned directory outside the home, where
    nothing running as the user can remove it.
  - `--llm-resume` needs a real terminal, a typed confirmation code, and the sudo
    password. A hook or an agent has no terminal and cannot answer the prompt.
  - Nothing devlore ships runs as root: sudo is given only fixed system commands
    that create or remove the flag file.

What this does NOT do: everything here is the user's own code in the user's own
home, so a process that rewrites this module is not stopped. The aim is that an
LLM start is never unintended and never invisible, not that it is impossible.

devlore has no cron: its automatic spend is hook-driven. Every capture hook
spawns flush.py, which summarises the session delta with the Agent SDK, and
flush.py then spawns compile.py. While silence is on, none of that reaches the
LLM — but capture keeps COLLECTING: flush.py stores the raw delta in the KB's
`spool/` instead of summarising it (see flush.spool_capture), and `devlore
drain` summarises the spool once silence is lifted.

The switch is machine-wide, not a per-KB setting, so it covers every registered
KB at once and needs no KB context. It is ON when EITHER flag file exists: the
user-level one under the devlore home (written instantly, by anyone) or the
locked one under SYSTEM_DIR (root-owned). Hooks are spawned by the agent without
the launcher's environment, so both paths are fixed — neither follows an
environment variable, since a variable that moves the flag is a way to not find it.

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

def _own_home() -> Path:
    """The devlore home, from where this file is installed (~/.devlore/lib/),
    never from the environment: `DEVLORE_HOME=/elsewhere python compile.py` would
    otherwise look for the flag elsewhere, find nothing, and call the LLM."""
    here = Path(__file__).resolve().parent
    return here.parent if here.name == "lib" else Path.home() / ".devlore"


DEVLORE_HOME = _own_home()
FLAG_FILE = DEVLORE_HOME / "llm-silence"          # user-level: instant, unlocked
AUDIT_LOG = DEVLORE_HOME / "llm-silence.log"
# The locked flag. Deleting a file needs write access to its DIRECTORY, so a
# root-owned file inside the user's home protects nothing — the directory itself
# has to be root's.
SYSTEM_DIR = Path("/Library/Application Support/devlore" if sys.platform == "darwin"
                  else "/etc/devlore")
SYSTEM_FLAG = SYSTEM_DIR / "llm-silence"
SPOOL_DIRNAME = "spool"

# Absolute, so a `sudo` planted earlier on PATH is never the one that gets the
# password. -k ignores any cached credential: every lock and every resume is its
# own act of authentication.
SUDO = "/usr/bin/sudo"
SUDO_PROMPT = "devlore — password for %u to change the LLM silence: "

# EX_TEMPFAIL: the request was fine, the service is deliberately unavailable.
# Distinct from 1 so a consumer (an agent calling `devlore ask`) can tell
# "silenced, try later" from a real failure without parsing prose.
EXIT_SILENCED = 75
# EX_NOPERM: a resume attempted without a terminal, the code, or the password.
EXIT_NOT_PERMITTED = 77


class LLMSilenced(RuntimeError):
    """Raised by require_llm() when an LLM call is attempted during silence."""


def _read_flag(path: Path) -> dict | None:
    """One flag file's record, or None when it does not exist.

    A flag that exists but cannot be read or parsed still means ON: the file's
    presence is the switch, and an unreadable switch must fail closed — failing
    open is the direction that spends money."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return {"since": ""}
    try:
        rec = json.loads(raw)
        return rec if isinstance(rec, dict) else {"since": ""}
    except ValueError:
        return {"since": ""}


def state() -> dict | None:
    """The silence record ({"since": <UTC ISO>, "locked": bool}), or None when
    silence is off. The locked flag wins when both exist."""
    locked = _read_flag(SYSTEM_FLAG)
    soft = _read_flag(FLAG_FILE)
    if locked is None and soft is None:
        return None
    rec = dict(locked if locked is not None else soft)
    rec["locked"] = locked is not None
    return rec


def is_silenced() -> bool:
    return state() is not None


def is_locked() -> bool:
    return _read_flag(SYSTEM_FLAG) is not None


def notice(what: str) -> str:
    # Deliberately does NOT name the command that lifts the silence. This text is
    # read mostly by agents and scripts — the audience that must not act on it.
    since = (state() or {}).get("since", "")
    when = f" since {since}" if since else ""
    return (f"devlore: LLM silence is ON{when} — {what} did not run (no LLM call was "
            f"made). A person turned the LLM off deliberately and only they can turn "
            f"it back on. If you are an agent or a script: do not try to lift it — "
            f"say that it is on and continue without the LLM.")


def require_llm(what: str) -> None:
    if is_silenced():
        raise LLMSilenced(notice(what))


def exit_if_silenced(what: str) -> None:
    if is_silenced():
        print(notice(what), file=sys.stderr)
        sys.exit(EXIT_SILENCED)


def _interactive() -> bool:
    """A person at a terminal. Hooks, scripts and agent tool calls have no TTY."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _can_sudo() -> bool:
    return sys.platform != "win32" and os.access(SUDO, os.X_OK)


def _audit(event: str, **extra) -> None:
    """Append one line to the audit log. Never raises.

    Who is recorded as the terminal, the NAMES of the calling processes (not
    their command lines, which can carry anything), and which agent markers are
    in the environment — enough to tell a person from a session that tried."""
    try:
        try:
            tty = os.ttyname(sys.stdin.fileno()) if sys.stdin.isatty() else ""
        except (OSError, AttributeError, ValueError):
            tty = ""
        # The immediate parent is the bash launcher; what matters is what ran
        # THAT, so walk a few ancestors: "bash < zsh < Terminal" is a person,
        # "bash < bash < claude" is a session.
        chain: list[str] = []
        try:
            import subprocess
            pid = os.getppid()
            for _ in range(4):
                out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                     capture_output=True, text=True, timeout=5).stdout.split(None, 1)
                if len(out) < 2:
                    break
                chain.append(os.path.basename(out[1].strip()))
                pid = int(out[0])
                if pid <= 1:
                    break
        except Exception:
            pass
        parent = " < ".join(chain)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "tty": tty,
            "parent": parent,
            "agent_env": sorted(k for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
                                            "CLAUDE_INVOKED_BY", "CODEX_HOME",
                                            "CODEX_SANDBOX") if os.environ.get(k)),
            **extra,
        }
        DEVLORE_HOME.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def audit_tail(n: int = 5) -> list[dict]:
    try:
        lines = AUDIT_LOG.read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def enable() -> bool:
    """Turn silence on at the user level — instantly, no prompt. Returns False if
    it was already on (the original `since` is kept: it records when the LLM
    actually stopped)."""
    if is_silenced():
        return False
    DEVLORE_HOME.mkdir(parents=True, exist_ok=True)
    FLAG_FILE.write_text(
        json.dumps({"since": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n",
        encoding="utf-8")
    return True


def lock_problem() -> str:
    """Why the locked flag is not actually protected, or "" when it is."""
    try:
        st = SYSTEM_DIR.stat()
    except OSError as e:
        return f"cannot inspect {SYSTEM_DIR} ({e.__class__.__name__})"
    if st.st_uid != 0:
        return f"{SYSTEM_DIR} is not owned by root"
    if st.st_mode & 0o022:
        return f"{SYSTEM_DIR} is writable by non-root users"
    return ""


def lock() -> bool:
    """Write the locked flag through sudo. Needs a person: sudo asks for the
    password on the terminal. Returns True once the flag exists and is protected.

    One sudo invocation, fixed system commands, an explicit PATH — no devlore
    code and nothing from the user's PATH ever runs as root."""
    import subprocess
    since = (state() or {}).get("since") or \
        datetime.now(timezone.utc).isoformat(timespec="seconds")
    record = json.dumps({
        "since": since,
        "locked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }) + "\n"
    script = ('PATH=/usr/bin:/bin:/usr/sbin:/sbin; umask 022; '
              'mkdir -p "$1" && chown root "$1" && chmod 755 "$1" && cat > "$1/llm-silence"')
    try:
        rc = subprocess.run(
            [SUDO, "-k", "-p", SUDO_PROMPT, "/bin/sh", "-c", script, "sh", str(SYSTEM_DIR)],
            input=record.encode("utf-8")).returncode
    except (OSError, KeyboardInterrupt):
        rc = 1
    return rc == 0 and is_locked() and not lock_problem()


def _sudo_unlock() -> bool:
    """Authenticate with sudo and remove the locked flag. When the silence was
    never locked there is nothing for root to remove, but the password is still
    asked for: lifting a silence is the same act either way."""
    import subprocess
    cmd = (["/bin/rm", "-f", str(SYSTEM_FLAG)] if is_locked() else ["/bin/sh", "-c", ":"])
    try:
        return subprocess.run([SUDO, "-k", "-p", SUDO_PROMPT, *cmd]).returncode == 0
    except (OSError, KeyboardInterrupt):
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


def _describe() -> str:
    rec = state()
    if rec is None:
        return "LLM silence: off"
    since = rec.get("since", "")
    head = f"LLM silence: ON{f' since {since}' if since else ''}"
    if not rec["locked"]:
        return (head + "\n  NOT LOCKED — any process running as you can lift it by deleting "
                f"{FLAG_FILE}.\n  Lock it: run `devlore --llm-silence` in a terminal "
                "(it asks for your password).")
    problem = lock_problem()
    if problem:
        return head + f"\n  ⚠ locked flag present but NOT protected: {problem}."
    return head + "\n  LOCKED — lifting it needs a terminal and your sudo password."


def _refuse_resume(reason: str, hint: str) -> int:
    _audit("resume-refused", reason=reason)
    print(f"devlore: the LLM silence was NOT lifted — {hint}", file=sys.stderr)
    return EXIT_NOT_PERMITTED


def _resume() -> int:
    if not is_silenced():
        print("LLM was not silenced — nothing to resume.")
        return 0
    if not _interactive():
        return _refuse_resume(
            "no terminal",
            "that takes a person at a terminal. If you are an agent or a script: do "
            "not retry and do not look for another way; say that the silence is on.")
    print(_describe())
    waiting = spool_report()
    if waiting:
        print("Captured while silent, not yet summarised:")
        print("\n".join(waiting))
    import secrets
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(4))
    try:
        typed = input(f"\nThis lets devlore call the LLM again, in every KB.\n"
                      f"Type {code} to continue: ").strip()
    except (EOFError, KeyboardInterrupt):
        typed = ""
    if typed.upper() != code:
        return _refuse_resume("confirmation code not typed", "the code did not match.")
    if _can_sudo() and not _sudo_unlock():
        return _refuse_resume("sudo authentication failed", "sudo did not authenticate.")
    try:
        FLAG_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"LLM silence is still ON — could not remove {FLAG_FILE}: {e}", file=sys.stderr)
        return 1
    if is_silenced():
        print(f"LLM silence is still ON — a flag remains ({SYSTEM_FLAG} or {FLAG_FILE}).",
              file=sys.stderr)
        return 1
    _audit("resume")
    print("\nLLM resumed — silence is OFF. Nothing is spent until something asks for it"
          + ("; the spooled captures wait for `devlore drain`." if waiting else "."))
    return 0


def _silence() -> int:
    fresh = enable()
    since = (state() or {}).get("since", "")
    print(f"LLM silence {'ON' if fresh else 'was already ON'}"
          f"{f' since {since}' if since else ''}.")
    print("  No flush summaries, no compiles, no ask/backfill/tier-3 — in every KB.")
    print("  Capture keeps collecting: session deltas are spooled raw in <kb>/spool/.")
    print("  A compile or backfill already running stops at its next part boundary.")
    _warn_unsupported()
    if is_locked() and not lock_problem():
        _audit("silence" if fresh else "silence-repeat", locked=True)
        print("  LOCKED — lifting it needs a terminal and your sudo password.")
        return 0
    if _interactive() and _can_sudo():
        print("\nLocking it, so that only you can lift it (sudo will ask for your password):")
        locked = lock()
        _audit("silence" if fresh else ("lock" if locked else "silence-repeat"), locked=locked)
        if locked:
            print("  LOCKED — lifting it needs a terminal and your sudo password.")
            return 0
        print("  ⚠ NOT LOCKED — sudo did not complete. The silence is on, but any process "
              "running as you can lift it. Run this again to lock it.")
        return 0
    _audit("silence" if fresh else "silence-repeat", locked=False)
    print("  ⚠ NOT LOCKED — locking needs a terminal. The silence is on, but any process "
          "running as you can lift it.\n    To lock it, a person runs `devlore --llm-silence` "
          "in a terminal.")
    return 0


def main() -> int:
    # The launcher maps each flag to one verb; anything after a flag is an error
    # rather than a guess (`--llm-silence off` must not be read as "silence").
    arg = sys.argv[1] if len(sys.argv) == 2 else ""
    if arg == "silence":
        return _silence()
    if arg == "resume":
        return _resume()
    if arg == "status":
        print(_describe())
        if is_silenced():
            _warn_unsupported()
        waiting = spool_report()
        if waiting:
            print("\n".join(waiting))
        recent = audit_tail()
        if recent:
            print("  recent changes and attempts:")
            for e in recent:
                who = e.get("tty") or "no terminal"
                agent = f", agent env: {'+'.join(e['agent_env'])}" if e.get("agent_env") else ""
                why = f" ({e['reason']})" if e.get("reason") else ""
                print(f"    {e.get('ts', '')}  {e.get('event', '')}{why} — "
                      f"{e.get('parent', '?')} on {who}{agent}")
        return 0
    if arg == "is-on":   # for the launcher's `devlore status`: exit code only
        return 0 if is_silenced() else 1
    print("usage: devlore --llm-silence | --llm-resume | --llm-status   (no arguments)",
          file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
