# The History of devlore

> *"Claude Code deletes your transcripts after 30 days. This makes them immortal."*

devlore is a knowledge-base pipeline that compiles AI coding-session logs into a
structured, queryable wiki. It drives the Claude Agent SDK from three callers —
compile, query, and a memory-flush hook — turning ephemeral conversation
transcripts into durable institutional memory about a codebase. This is the
story of how it got here.

## Origins: a memory compiler in a single repo

devlore began on May 22, 2026, not as a product but as a clone of
[coleam00's claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler)
dropped into a single private project directory. That first install wired up the
full capture → compile → inject loop end to end: uv as the Python runner, a
daily auto-compile keyed to the local evening, and SessionStart / PreCompact /
SessionEnd hooks registered in `.claude/settings.json`. Capture was deliberately
scoped to that one repo and nothing else.

The conceptual lineage runs back further still — to Andrej Karpathy's idea of an
LLM that reads your conversations and compiles them into a personal knowledge
base. That original framing assumed one person, one knowledge domain, and a
completely open schema. It was a clean starting point, and the pipeline outgrew
it almost immediately.

The upstream skeleton — daily logs treated as immutable source material, an LLM
compiler that distills them, a knowledge wiki as the output, and context
injected back at the start of each session — is still the shape of the system
today. But over the following weeks nearly every mechanism inside that skeleton
was rebuilt:

- Fixed capture windows became **delta markers**, so the compiler processes
  exactly what is new.
- A single monolithic compile call became **entry-aware chunking**.
- The flat, open schema became a **project / subsystem namespace**, introduced
  the moment a single flat wiki started blending tooling knowledge with the
  founding project's domain knowledge.

## The motivation: transcripts don't survive

A formative lesson arrived through data loss. Claude Code deletes its local
`.jsonl` transcripts after `cleanupPeriodDays` — 30 by default — and that
deletion permanently destroyed an entire project's sessions before any knowledge
base could be built from them. The takeaway was blunt and became a design
principle: **a knowledge base is only as good as the source material that
survives long enough to be compiled.**

That episode is, quite literally, the product's tagline: *"Claude Code deletes
your transcripts after 30 days. This makes them immortal."*

## From personal pipeline to product

Two steps turned a personal, single-project setup into something anyone could
use.

### 1. Making it replicable

Everywhere the pipeline silently assumed it was running against its founding
repo, that assumption was pulled out and moved behind configuration: a
`code-roots` config file, a dict-valued `code_baseline`, and a multi-KB
status-line dispatcher. A new `scripts/init_kb.py` was built to scaffold a
complete, working knowledge base for any codebase from scratch.

The proof came from running it live against a second, unrelated project. The
compiler invented its own thirteen-subsystem taxonomy appropriate to that
codebase, with zero vocabulary from the founding project leaking through —
confirmation that the system was genuinely portable rather than a one-repo tool
wearing a disguise.

### 2. Renaming and publishing

By this point the pipeline was called **wikiLLM**, and the plan was to ship it
under that name. But the `wikillm` GitHub slug was already taken, so it was
renamed **devlore** and published at
[github.com/condechi/devlore](https://github.com/condechi/devlore).

The rename was carried out by `build_dist.py`, using case-insensitive regex
across both file contents and filenames, since the legacy name appeared in
several different casings. A security review ahead of release found and fixed
ten issues. Then a live `devlore add` smoke test — building a "meta-KB of the KB
itself" from the pipeline's own daily logs — surfaced eight more fixes across
v0.9.1 through v0.9.8. That work culminated in the `devlore update` command,
which keeps an installed KB's machinery current without ever touching its
knowledge, daily logs, markers, or git history.

### 3. The Emancipation

Through v0.9.12 the source of truth still lived *inside* the founding private
KB — making it the one knowledge base that could not be managed by the very
CLI it produced. On June 11, 2026 (v0.9.13) the source was extracted into a
dedicated development repo, seeded from the distribution itself — which by
then was brand-clean and placeholder-pathed, so the legacy rename pass was
retired from the build. The founding KB became a normal consumer, updated by
`devlore update` like every other install.

### 4. Beyond Claude Code: Codex, tags, and worktrees

With development now in its own repo, v0.9.14 landed the first wave of
post-Emancipation features in a single release. devlore stopped being
Claude-Code-only: a shared transcript normalizer (`scripts/transcripts.py`)
lets it capture **Codex** sessions too, wiring `.codex/hooks.json` alongside
Claude Code's hooks and discovering past Codex transcripts for backfill (the
Agent SDK still does the compile/query work). Alongside it shipped
project-slug-first **frontmatter tags** for Obsidian, **git-worktree capture**
(sessions in `.claude/worktrees/` now map to their project for capture,
recall, and backfill), and **owning-KB routing** so a bare `devlore add` finds
the right knowledge base when several are installed.

### 5. Fire-and-forget capture

The delta-marker work made the compiler process exactly what was new — but only
*after* a session's first flush, which fired on compaction or exit. A long
session that did neither captured nothing, and its eventual first flush kept only
the most recent `max_turns` turns, hiding the rest behind the very marker it
wrote (the "first-flush footgun"). v0.9.16 closed that gap with a Claude `Stop`
hook that fires the first flush **automatically** once a session reaches
`bootstrap_turns` (default 45), with a safety valve for sessions that run long
without compacting. Capture stopped asking the developer to exit and wait;
PreCompact's first-flush truncation also became visible instead of silent.
(Codex already captured every turn through its `Stop` event, so the gap was
Claude-Code-only.) The same release auto-excluded code-root symlinks from the
KB's own git and tagged each daily entry with its source project.

### 6. The compile that interrupted itself

As the founding KB matured past two hundred articles, the compiler's original
design — inlining the FULL text of every existing article into every compile
prompt — quietly became its own worst enemy: prompts crossed two million
characters, some exceeded the model's context window outright ("Prompt is too
long"), and the rest spent most of the 300-second part budget just ingesting
context before writing anything. The watchdog then killed those healthy-but-slow
SDK sessions mid-write, leaving half-written articles and broken wikilinks — and
logging each kill as "Request interrupted", indistinguishable from a user
pressing Esc. A usage-insights review in August 2026 blamed the user for
"aborting" dozens of compile sessions before forensics on the transcripts showed
every single "interruption" lasted exactly the watchdog's 300 seconds. v0.9.19
fixed the architecture instead of the user: the compile agent now receives only
the wiki INDEX (the summary catalog) and Reads the specific articles it needs on
demand — shrinking prompts roughly tenfold on mature KBs — while the part
timeout tripled to 900s and the turn budget grew to accommodate the reads.
v0.9.20 closed the companion cost hole: the compile agent had always inherited
the interactive CLI's default model, so a user driving Claude Code with a
top-tier model was silently billing every compile at that tier. A
`compile_model` knob in capture-config now pins compilation to Sonnet by
default, decoupling the pipeline's spend from the user's interactive choices.
v0.9.22 swept up the last trace of the episode: the machinery's own SDK
transcripts — which had grown to 96% of the founding KB's Claude project dir
(1.5 GB of exhaust masquerading as user sessions) — are now deleted on each
compile, identified by the sentinel prompts only devlore's own sessions begin
with, with a 24-hour grace window for anything recent or in flight. v0.9.23
extended the model pin to queries (`query_model`, Sonnet by default), moved the
multi-KB registry from `~/.claude/kb-dirs` to the harness-agnostic
`~/.devlore/kb-dirs` (auto-migrated on first touch — devlore captures Codex as
well as Claude Code, and its own state belongs in its own home), and scrubbed
the last founding-KB repo names out of the tier-3 verifier's prompt, which now
derives its repo list from `scripts/code-roots` like everything else. v0.9.24
uncoupled the dispatcher from its KB: `devlore` no longer needs to be run from
inside a KB. A new `~/.devlore/registry.json` adds named KBs (the flat
`kb-dirs` file stays for legacy routing and the status line), and
`~/.devlore/state.json` holds a per-user default KB. The new subcommands are
`devlore` (lists KBs), `devlore use <name>` (sets default), `devlore which`
(prints the resolved KB for cwd). Read commands (`ask`, `status`, `compile`,
`verify`, `recheck`, `docs`, `backfill`, `obsidian`) route through cwd
detection first, the default KB second, and re-exec the resolved KB's
launcher transparently. `--kb <name>` is now a peer of the existing `--kb
<path>`. A first-detection prompt offers to register any KB the user is
standing in; declines are remembered so the launcher never nags.

### 7. Global launcher + shared machinery

Through v0.9.24 every KB on a user's machine carried its own identical copy of
all 40+ Python scripts, five hooks, `pyproject.toml`, `uv.lock`, and a full
`.venv/`. The PATH entrypoint `~/.local/bin/devlore` was a symlink into one of
the KBs — fine until that KB had a corrupted install or until you tried to
update machinery across four KBs at once. v0.9.25 splits the codebase by
responsibility. The **shared machinery** — `config.py`, `utils.py`, `kb_resolve.py`,
`kb_registry.py`, `capture_config.py`, `transcripts.py`, `activity.py`,
`kb_commit.py`, `staleness.py`, `stamp_baseline.py` — ships once into
`~/.devlore/lib/` and is shared by every KB. The **global launcher** lives at
`~/.devlore/bin/devlore` and is what `~/.local/bin/devlore` symlinks to; it
routes commands to the right KB via the same cwd-decoupling + named-registry
foundation that v0.9.24 added. KB-local scripts (`compile.py`, `query.py`,
`flush.py`, `verify.py`, `recheck.py`, the `*.sh` wrappers, the Obsidian plugin,
and the per-KB `devlore` launcher copy for backward compatibility) stay in
`<kb>/scripts/` because they own per-KB state (`state.json`, `capture-roots`,
`code-roots`, `capture-config`, runtime markers, logs). Hooks stay per-KB at
`<kb>/hooks/` so the absolute paths in every captured project's
`.claude/settings.local.json` and `.codex/hooks.json` don't need re-wiring.
`config.py` resolves the active KB from the launcher-injected `DEVLORE_KB_ROOT`
env var (falling back to `__file__`-relative when called directly) so a single
shared module serves every KB at call time. `init_kb.py` ships the per-KB
`pyproject.toml` as a stub (no deps; they live in `~/.devlore/lib/pyproject.toml`)
and `update_kb.py --to-new-layout` migrates any v0.9.24 KB in place: removes the
ten shared files from `<kb>/scripts/`, installs the shared lib, backs up the old
`pyproject.toml` to `pyproject.toml.v0924.bak`, and repoints `~/.local/bin/devlore`
to the global launcher. Slash commands (`/devlore`, `/ask`, `/compile`, `/verify`)
now invoke the per-KB launcher (`__DEVLORE_HOME__/scripts/devlore <sub> $ARGS`)
instead of `uv run` directly; the launcher injects `DEVLORE_KB_ROOT` and
`PYTHONPATH=~/.devlore/lib`. The result: a `devlore update` re-materializes only
KB-local files (was ~53, now ~30), and a fix to `kb_registry.py` lands once
instead of N times.

### 7b. Single global launcher + shared venv (v0.9.27)

Two redundancies from the v0.9.25 split remained parked:
the per-KB `scripts/devlore` copy (and seven sibling `.sh` shells —
`compile.sh`, `status.sh`, `update.sh`, `query.sh`, `recheck.sh`, `verify.sh`,
`devlore.sh`) that every KB still carried to host logic the global launcher
couldn't run on its own, and the per-KB `.venv/` materialized each time hooks
spawned as `uv run --directory <kb>` — four identical venvs per user, four
`uv sync` runs per update for nothing.

The v0.9.27 collapse:

- **One launcher.** `scripts/devlore` is the *complete* dispatcher now: the
  case-branch table, `status` walker, and the help heredoc all live in the
  file that ships at `~/.devlore/bin/devlore`. The per-KB `scripts/devlore`
  and seven `scripts/*.sh` siblings are dropped from the install payload
  (~30 → 19 KB-aware Python scripts per KB). A fix lands once — at the source
  — and reaches every KB on the next update. Dropping them from the payload is
  not enough on its own: an existing KB keeps whatever it was last given, so
  `update_kb` also *deletes* the stale copies. They were not dead weight but
  live traps — each shell exec'd `<kb>/.venv/bin/python3` directly, skipping
  the launcher's `PYTHONPATH=~/.devlore/lib` injection (so shared imports died
  with `ModuleNotFoundError: capture_config`) and pointing at the very venv
  v0.9.27 removes.
- **One venv.** Every Python invocation (KB-local scripts and shared ones
  alike) resolves `~/.devlore/.venv/bin/python3`. The per-KB
  `pyproject.toml` stub loses its dependency list (it's now
  `name = "devlore-kb-stub"` with no deps); the dist's `uv.lock` is no longer
  carried per-KB. `devlore init` and `devlore update` call a new
  `_install_shared_venv` once instead of running `uv sync --directory <kb>`
  four times.
- **Hooks** change from `uv run --directory <kb> python hooks/<name>.py` to
  `<venv-python> <kb>/hooks/<name>.py`. The five hook files are unchanged;
  they resolve their imports the same way they did before.
- **Slash commands** (`/devlore`, `/ask`, `/compile`, `/verify`) move from
  `Bash(<kb>/scripts/devlore:*)` allowlists to `Bash(devlore:*)`, routing
  through the global launcher on PATH.
- **Obsidian plugin** keeps the same allowlist of seven shims, but each
  `scripts/<name>.sh` path is replaced by a `~/.devlore/bin/<name>.sh` shim
  that `exec`s `devlore <sub> "$@"`. The Obsidian plugin's allowlist
  auditability is preserved verbatim; the seven shims are 4-line
  `exec`-forwarders.

The KB's per-KB `pyproject.toml` `v0924.bak` backup added in v0.9.25 stays;
the v0.9.27 update additionally removes any `scripts/devlore` and
`<kb>/.venv/` left over from a pre-v0.9.27 install and writes a
`scripts/.venv-removed-by-v0927` marker so the deletion is visible in
`git status` (the marker itself is on the update path: subsequent updates
quietly overwrite it when the centralization is complete).

Net effect: from v0.9.27 onward, a launcher fix is a one-place edit, a fresh
`devlore update` installs one venv instead of four, and the seven per-KB
shell scripts are gone.

### 7c. The update that could not update itself (v0.9.27.1–.2)

v0.9.27 shipped and bricked the first KB that took it. The update ran, deleted
`<kb>/scripts/devlore` as designed — and then every `devlore` subcommand,
`devlore update` included, died with *"KB has no scripts/devlore"*. The KB had
no way back: the command that would have repaired it was the command that was
broken.

Three defects lined up:

- **The shared lib was only ever installed by the legacy migration branch.**
  `_install_shared_lib` sat inside `if args.to_new_layout or <KB still has
  v0.9.24 files>`. A KB already on the v0.9.25 layout matched neither, so
  `~/.devlore/lib` and `~/.devlore/bin/devlore` froze at whatever version first
  migrated them while the per-KB payload kept advancing. v0.9.27 then removed
  the per-KB launcher that the frozen v0.9.25 global launcher hard-requires.
- **`_install_shared_venv` looked in a path that never exists in a dist.**
  It read `<src>/dist-assets/lib/pyproject.toml`, but `build_dist` maps that to
  `lib/pyproject.toml` and a built dist carries no `dist-assets/` at all. It
  therefore always missed, warned, and returned False — after which the caller
  deleted the per-KB `.venv` anyway, leaving the KB with no interpreter.
- **The venv directory was created before that check**, so a skipped install
  still left an empty `~/.devlore/.venv/` that reads as "installed" to anything
  testing existence.

The fix inverts the order and the conditionality. The shared lib and shared venv
are installed on *every* update, before any legacy prune runs, so the
replacement is in place before the thing it replaces is removed. The per-KB
`.venv` is now deleted only when a working `~/.devlore/.venv/bin/python3`
actually exists; otherwise the fallback stays and the update says so. The
general rule, learned twice now: **never remove the old path until the new one
is verified present** — an install step that can silently no-op must never be
paired with a deletion step that cannot.

A tail followed in v0.9.27.2. `uv venv` refuses outright when a venv already
exists, and the call passed no `--clear`, so every refresh after the very first
— a version bump, a dependency change — failed and left the venv pinned at
whatever version created it. And "verified present" was itself too weak: the
guard tested only that `bin/python3` existed, which a cleared-but-unpopulated
venv satisfies. It now runs `import claude_agent_sdk` in that interpreter,
because what licenses deleting a KB's only other Python is that the replacement
actually works, not that a file is sitting where one should be.

### 7d. Migrations that land in one pass (v0.9.27.3)

`devlore update` executes whatever `<kb>/scripts/update_kb.py` the KB already
has. The payload it installs — including that very file — only takes effect on
the *next* invocation, so every release needed two passes, and the prunes that
did run ran under code a version older than the payload they were pruning for.
That is precisely how v0.9.27 came to delete a per-KB launcher using v0.9.26
logic that had never heard of the global one replacing it.

`update_kb` now snapshots its own bytes at startup and, once the payload is on
disk and before any prune runs, re-execs the freshly installed copy with the
same arguments. Migration therefore always executes under the version that
shipped it. Looping is impossible: the child carries `DEVLORE_UPDATE_REEXEC`,
and an unchanged file re-execs nothing.

Building that exposed an older bug it had been hiding. `_rewrite` substitutes
`__DEVLORE_BIN_DIR__` across the payload — and `update_kb.py` *is* payload, with
that token spelled verbatim in `_rewrite` itself. Installing the file therefore
rewrote its own source, replacing the token with a hardcoded path, which stopped
the *next* install from substituting anything. The two failures alternated: one
update left `update_kb.py` corrupted, the next left the Obsidian plugin's
allowlist holding raw `__DEVLORE_BIN_DIR__/compile.sh` strings that exec nothing.
Both states were live at once across a set of installed KBs — some carrying the
corrupted script, others the broken plugin, depending on which update each had
last taken.

The `__DEVLORE_HOME__` token had been split into halves years earlier for exactly
this reason. The lesson simply had not been carried across when the BIN_DIR token
was added. Both are now built from halves, so neither appears verbatim in a file
that the rewriter will one day walk over.

### 7e. Snapshots, and the cost of matching on strings (v0.9.28)

Registering a directory and instrumenting it had always been one decision. `wire()`
appended to `capture-roots` and wrote the project's own agent config in a single
breath — right for "document this codebase from now on", wrong for "compile what
already happened and leave the project alone". The second case had no path at all:
`--no-backfill --no-docs` skips the ingest and keeps the hooks, exactly backwards.

`devlore add --no-hooks` now performs every KB-local step and writes nothing outside
the KB, recording the choice in `<kb>/scripts/no-hook-roots`. That file is not a
convenience: `capture-roots` membership is the only thing `_rewire_capture_hooks`
consults, and its whole job is retrofitting hooks onto registered roots — so without
a durable marker the next `devlore update` would silently reverse the opt-out.
`devlore snapshot <kb> --code <d> …` composes init, one `--no-hooks` add per
directory, and a single closing compile, so articles can cite across all of them.

The release is otherwise about a single bad habit: **identifying a thing by the exact
string used to invoke it.**

- **Registration stacked generations.** `_merge_hook_file` skipped an event when some
  handler's command matched byte for byte. Every change to how devlore spawns Python
  therefore looked like a brand-new hook, and v0.9.24's `uv run --directory`,
  v0.9.25's bare `python3` and v0.9.27's shared venv accumulated side by side —
  twelve handlers where four belong, the older two spellings erroring against a venv
  that no longer exists, on every session start, compact and stop.
- **Removal matched the same way and found nothing.** `remove_codebase` looked for
  commands starting with `uv run --directory <kb> `, so from v0.9.25 onward
  `devlore remove` silently left every hook in place.
- **Five spawn sites still shelled out through `uv run --directory <kb>`**, which
  makes uv materialize a per-KB `.venv` from that KB's `pyproject.toml`. v0.9.27 had
  deleted those venvs; two KBs already had theirs back. The two highest-traffic sites
  were hooks firing on every session event.

All three now identify a hook by *(this KB, this script)* and spawn through the
shared venv explicitly. Registration rewrites a stale generation in place and drops
duplicates, which makes the fix its own migration: the rewire sweep that runs on
every update collapses the accumulated stacks the first time it sees them.

Two smaller things. `code_baseline` gained `pushed` alongside `dirty` — the baseline
recorded *which* commit an article was anchored to, but not whether that commit was
ever published, and an article anchored to a local-only SHA cannot have its staleness
computed on anyone else's machine. And `scripts/devlore`, a stale duplicate of the
launcher that v0.9.27 had already stopped shipping, was found still drifting from the
real one at `lib/bin/devlore` and removed.

### 8. Capture-config preservation (v0.9.26)

The one file under `<kb>/scripts/` that the user customizes is
`capture-config` — `compile_model`, `query_model`, `bootstrap_turns`, and the
other capture-sizing knobs. Through v0.9.25 a `devlore update` would clobber
the user's values back to the dist's defaults on every run, forcing the user
to re-pin them by hand. v0.9.26 special-cases the copy: the dist's
`capture-config` is walked line by line, comments and ordering are adopted
verbatim, and any user value that differs from the dist's default is preserved.
A new key added by the dist flows through; a key the user pinned to a
non-default value survives; a key the user had but the dist no longer ships
(a typo, a deprecated knob the loader already ignores) is dropped with a log
line so the file doesn't silently grow. The merge is idempotent — re-running
`devlore update` after the user has accepted the merge produces a byte-identical
file, so the post-update auto-commit stays quiet for users who never
customized anything.

### 9. The docs you never push (v0.9.29)

`collect_markdown_docs` has always taken the repo's own intent as its filter:
candidates come from `git ls-files --cached --others --exclude-standard`, so
gitignored build output and vendor trees vanish without a deny-list entry. That
is right for generated noise and wrong for one real case — research notes,
drafts and working documents deliberately kept out of the remote, which are
often the most worth compiling. `--ignore-gitignore` on `devlore docs` and
`devlore add` drops `--exclude-standard` and nothing else: the vendored-tree
deny-list, the depth limit, the size floor and the per-directory tripwire all
still run, so `node_modules/` stays out whether or not it is gitignored.

The release also closes a gap that had been live since v0.9.27. The flag's help
text shipped without its implementation: the work sat in a stash while the
launcher it had edited was moved to `lib/bin/devlore`, carrying the help along.
For two releases `devlore docs <dir> --ignore-gitignore` read the flag as a
*path*, printed `SKIP (not found)`, scanned without it and exited 0 — which is
the code the launcher's `&&` tests before running `compile`. The user paid for a
compile and did not get the documents they asked for. Documentation that
promises a flag is a contract; the half that shipped was the half that could not
keep it.

One adjacent fix: `--no-hooks`, added in v0.9.28, was missing from the argument
list `add` forwards when it re-execs the owning KB's launcher. A `devlore add
<repo> --no-hooks` aimed at a codebase owned by another KB silently lost the
flag and wrote capture hooks into the repo the user had asked it to leave
untouched.

### 10. The hooks nobody could reach (v0.9.29.1)

Every knowledge base captures the sessions run inside itself, and for Claude
Code that worked: the KB's `.claude/settings.json` is re-materialised from the
distribution on every update. The Codex half was never written by anything.

The reason it went unnoticed for a season is that the sweep which keeps hook
commands current, `_rewire_capture_hooks`, skipped the KB with a note that
looked like a decision: *inside the KB → covered by the KB's own
settings.json*. Half of that was true. Nothing maintained the KB's own
`.codex/hooks.json`, and since that sweep is the only code that ever rewrites a
hook command, the file was not merely out of date — it was unreachable. No
update could fix it, because no update looked.

What it held was the pre-v0.9.25 form, `uv run --directory <kb> python
hooks/X.py`. That is worse than obsolete. Running it materialises a per-KB
virtualenv, the very thing v0.9.27 removed when it consolidated on one shared
venv — and `init_kb` had already been avoiding `uv run` deliberately for that
reason, with a comment saying so. So the machinery knew; only this one path
never asked it. A single invocation rebuilt 231 MB inside the knowledge base,
the hook then succeeded, and nothing anywhere reported a thing.

Three of five knowledge bases on the development host carried that command. The
two created after the change carried no Codex hooks at all, so Codex sessions
inside a new KB were never captured.

The fix covers the Codex half before the skip and registers a new KB's own
Codex hooks at creation. Claude's half stays excluded on purpose: it comes from
the distribution, and re-registering it would stack a second generation of the
same hooks — precisely what v0.9.28 went to some trouble to stop.

The verification said more than expected. Replaying it against a knowledge base
with the old command planted returned *registered PreCompact, Stop; rewired
SessionStart*: the stranded file was not just wrong, it was incomplete, missing
two of its three events. The affected bases do not merely get a corrected
command back. They get capture they had silently lost.

## The lineage, in one line

coleam00's claude-memory-compiler (installed May 22, 2026) → heavily adapted
inside one production repo → renamed wikiLLM → de-hardcoded and made
bootstrappable → productized and published as **devlore** → emancipated from
its founding repo into a dedicated source repo → opened up to Codex alongside
Claude Code.

---

*Provenance: this history was not written from memory. It was reconstructed by
asking devlore's own meta-KB — the knowledge base the smoke test built about the
project itself — with `devlore ask "How did devlore come to exist?"`. The
product documented its own origin story.*
