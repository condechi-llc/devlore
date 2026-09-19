# AGENTS.md - Personal Knowledge Base Schema

> Adapted from [Andrej Karpathy's LLM Knowledge Base](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) architecture.
> Instead of ingesting external articles, this system compiles knowledge from your own AI conversations.

## The Compiler Analogy

```
daily/          = source code    (your conversations - the raw material)
LLM             = compiler       (extracts and organizes knowledge)
knowledge/      = executable     (structured, queryable knowledge base)
lint            = test suite     (health checks for consistency)
queries         = runtime        (using the knowledge)
```

You don't manually organize your knowledge. You have conversations, and the LLM handles the synthesis, cross-referencing, and maintenance.

---

## Architecture

### Layer 1: `daily/` - Conversation Logs (Immutable Source)

Daily logs capture what happened in your AI coding sessions. These are the "raw sources" - append-only, never edited after the fact.

```
daily/
├── 2026-04-01.md
├── 2026-04-02.md
├── ...
```

Each file follows this format:

```markdown
# Daily Log: YYYY-MM-DD

## Sessions

### Session (HH:MM) - Brief Title

**Context:** What the user was working on.

**Key Exchanges:**
- User asked about X, assistant explained Y
- Decided to use Z approach because...
- Discovered that W doesn't work when...

**Decisions Made:**
- Chose library X over Y because...
- Architecture: went with pattern Z

**Lessons Learned:**
- Always do X before Y to avoid...
- The gotcha with Z is that...

**Action Items:**
- [ ] Follow up on X
- [ ] Refactor Y when time permits
```

### Layer 2: `knowledge/` - Compiled Knowledge (LLM-Owned)

The LLM owns this directory entirely. Humans read it but rarely edit it directly.

```
knowledge/
├── index.md              # Master catalog - every article with one-line summary
├── log.md                # Append-only chronological build log
├── concepts/             # Atomic knowledge articles
├── connections/          # Cross-cutting insights linking 2+ concepts
└── qa/                   # Filed query answers (compounding knowledge)
```

### Layer 3: This File (AGENTS.md)

The schema that tells the LLM how to compile and maintain the knowledge base. This is the "compiler specification."

---

## Structural Files

### `knowledge/index.md` - Master Catalog

The primary retrieval mechanism — the LLM reads this FIRST when answering any query, then
selects relevant articles to read in full. It is **generated**, not hand-edited: run
`scripts/build_index.py` (the compiler calls it automatically after every compile) to
rebuild it from article frontmatter. Each row's text comes from that article's `summary:`
field — so to change the catalog, edit the article's `summary:`, never the index directly.

The catalog is **sectioned by project → subsystem** so it mirrors the architecture: an `H2`
per `project:`, an `H3` per `subsystem:` within it, and one table per subsystem.

Format:

```markdown
# Knowledge Base Index

## acme-app

### auth

| Article | Type | Status | Summary | Updated |
|---------|------|--------|---------|---------|
| [[concepts/supabase-auth]] | concept | shipped | Row-level security patterns and JWT gotchas | 2026-04-02 |
| [[connections/auth-and-webhooks]] | connection | active | Token verification shared across Supabase auth and Stripe webhooks | 2026-04-04 |
```

A row's section is its article's `project:` / `subsystem:`; its Summary is the article's
`summary:`. Keep those frontmatter fields current and the index follows.

### `knowledge/log.md` - Build Log

Append-only chronological record of every compile, query, lint, and supersede operation.

It is also the **home for decision history**. Articles always state only the *current*
truth (no "we used to do X but now Y" clutter); whenever a compile reverses or replaces a
prior decision, the old→new change is recorded here as a `supersede` entry. A decision that
changes several times leaves several `supersede` entries — the full chronology lives in the
log while the article stays clean and current.

Format:

```markdown
# Build Log

## [2026-04-01T14:30:00] compile | Daily Log 2026-04-01
- Source: daily/2026-04-01.md
- Articles created: [[concepts/nextjs-project-structure]], [[concepts/tailwind-setup]]
- Articles updated: (none)
- Decisions superseded: (none)

## [2026-04-03T11:00:00] supersede | Daily Log 2026-04-03
- Article: [[concepts/auth-strategy]]
- Changed: session storage mechanism
- From: JWT in localStorage
- To: httpOnly cookie sessions
- Reason: XSS exposure of localStorage tokens raised in 2026-04-03 review

## [2026-04-02T09:00:00] query | "How do I handle auth redirects?"
- Consulted: [[concepts/supabase-auth]], [[concepts/nextjs-middleware]]
- Filed to: [[qa/auth-redirect-handling]]
```

---

## Article Formats

### Concept Articles (`knowledge/concepts/`)

One article per atomic piece of knowledge. These are facts, patterns, decisions, preferences, and lessons extracted from your conversations.

```markdown
---
title: "Concept Name"
project: project-slug
type: concept
status: active
subsystem: subsystem-slug
milestone: M5
summary: "One-line catalog summary — this is the text the generated index shows."
aliases: [alternate-name]
tags: [project-slug, domain, topic]
sources:
  - "daily/2026-04-01.md"
  - "daily/2026-04-03.md"
created: 2026-04-01T00:00:00-06:00
updated: 2026-04-03T00:00:00-06:00
valid_as_of: 2026-04-03
code_baseline: { crm: <sha>, metadata: <sha>, dirty: <bool>, stamped: 2026-04-03 }
---

# Concept Name

[2-4 sentence core explanation]

## Key Points

- [Bullet points, each self-contained]

## Details

[Deeper explanation, encyclopedia-style paragraphs]

## Related Concepts

- [[concepts/related-concept]] - How it connects

## Sources

- [[daily/2026-04-01.md]] - Initial discovery during project setup
- [[daily/2026-04-03.md]] - Updated after debugging session
```

### Frontmatter fields (all article types)

These fields drive both index-guided retrieval and Obsidian Dataview/Bases queries
(e.g. `TABLE status, updated FROM "knowledge/concepts" WHERE subsystem = "credit-notes"
AND status != "superseded" SORT updated DESC`). Populate them on every article.

| Field | Required | Values / format |
|-------|----------|-----------------|
| `title` | yes | Human title (quoted). |
| `project` | yes | The app/platform slug (e.g. `stripe-ledger`). **Taken verbatim from the `**Project:**` line of the daily entry the article comes from, when present — never inferred and never copied from a neighbouring article, since one daily holds entries from several codebases.** Only when no entry carries the tag: reuse an existing related article's slug, with a new slug for a genuinely different app. |
| `type` | yes | `concept` \| `connection` \| `moc` \| `reference` \| `qa`. Derives from the folder for the three core kinds; `moc`/`reference` are curated. |
| `status` | yes | `active` (current, in-flight) \| `shipped` (done, current) \| `superseded` (replaced — kept as a page only if the concept still exists) \| `needs-reverification` (cited code moved; see PR D Tier-2). The `needs-reverification` ↔ `active` transitions are **automated** by the re-check loop (`scripts/recheck.py`, run after every compile): flagged articles self-mark and gain a loop-owned `reverify_reasons:` key; when the cause clears (recompile with fresh baseline, reverted code, or a human attesting via `recheck.py --verified <slug>`) the loop restores `active`. A human-set `needs-reverification` (no `reverify_reasons`) is never auto-cleared. |
| `subsystem` | yes | The architecture area this belongs to — **this groups the index** and is the primary Dataview facet. Reuse an existing subsystem slug (see the sections in `index.md`); introduce a new one only for a genuinely new area. |
| `summary` | yes | The one-line catalog summary, **always double-quoted** (it routinely contains colons — unquoted `: ` is invalid YAML and breaks Obsidian). **Lives here, not in the index** — the index is regenerated from this field. Keep it current when the article changes. |
| `milestone` | optional | The milestone/phase the work belongs to (e.g. `M5`, `Phase-7`, `v1.5`). Omit if not applicable. |
| `unverifiable` | optional | PR D Tier-4: tag articles whose **load-bearing claims cannot be verified against our code** — `business_rule` (tax law, filing rules, accounting policy: needs a cited statute/authority or the operator) and/or `external_api` (Stripe/Hospitable/BANXICO/OS behavior: needs vendor docs or empirical re-test). Comma-combine when both apply. The verification ladder must NEVER auto-trust these claims; code-grounding gates (Tier-1/2/3) only cover the article's incidental code citations. |
| `component` | optional | A finer-grained area within the subsystem (e.g. `orchestrator`, `audit.py`). |
| `aliases` | optional | 0–2 only. Never duplicate the filename; scope generic terms so they don't collide. |
| `tags` | yes | Inline list whose FIRST entry is always the article's `project:` slug — **machine-enforced**: the compiler's tag guard (`utils.ensure_required_tags`, run after every compile) inserts/reorders it and normalizes every tag to lowercase-kebab. After the slug, 2–5 curated domain tags chosen to make Obsidian's tag pane, graph filters, and Bases facets useful: reuse the existing vocabulary (the compile prompt shows tag → count), coin a new tag only when it will group 2+ articles, never add a near-synonym of an existing tag or duplicate the `subsystem:` value. |
| `sources` | yes (concept/connection) | The daily log(s) that contributed. |
| `created` / `updated` | yes | Full ISO 8601 with offset (`2026-04-03T00:00:00-06:00`) so Dataview can sort. `updated` = the compile/edit date. |
| `valid_as_of` | yes | The knowledge *vintage* (the daily date the knowledge is from), date-only. NOT the compile date. Stamped by `stamp_baseline.py` / born-stamped by `compile.py`. |
| `code_baseline` | yes | `{ crm, metadata, dirty, stamped }` — repo SHAs that were HEAD at the vintage. Drives the Tier-2 staleness scan. |
| `connects` | connection only | The 2+ concepts the connection links. |
| `question` / `consulted` / `filed` | qa only | The question, the articles read, and the file date. |

### Connection Articles (`knowledge/connections/`)

Cross-cutting synthesis linking 2+ concepts. Created when a conversation reveals a non-obvious relationship.

```markdown
---
title: "Connection: X and Y"
project: project-slug
type: connection
status: active
subsystem: subsystem-slug
summary: "One-line catalog summary of the relationship."
tags: [project-slug, domain]
connects:
  - "concepts/concept-x"
  - "concepts/concept-y"
sources:
  - "daily/2026-04-04.md"
created: 2026-04-04T00:00:00-06:00
updated: 2026-04-04T00:00:00-06:00
valid_as_of: 2026-04-04
code_baseline: { crm: <sha>, metadata: <sha>, dirty: <bool>, stamped: 2026-04-04 }
---

# Connection: X and Y

## The Connection

[What links these concepts]

## Key Insight

[The non-obvious relationship discovered]

## Evidence

[Specific examples from conversations]

## Related Concepts

- [[concepts/concept-x]]
- [[concepts/concept-y]]
```

### Q&A Articles (`knowledge/qa/`)

Filed answers from queries. Every complex question answered by the system can be permanently stored, making future queries smarter.

```markdown
---
title: "Q: Original Question"
project: project-slug
type: qa
status: active
subsystem: subsystem-slug
summary: "One-line summary of the answer."
tags: [project-slug, domain]
question: "The exact question asked"
consulted:
  - "concepts/article-1"
  - "concepts/article-2"
created: 2026-04-05T00:00:00-06:00
updated: 2026-04-05T00:00:00-06:00
filed: 2026-04-05
---

# Q: Original Question

## Answer

[The synthesized answer with [[wikilinks]] to sources]

## Sources Consulted

- [[concepts/article-1]] - Relevant because...
- [[concepts/article-2]] - Provided context on...

## Follow-Up Questions

- What about edge case X?
- How does this change if Y?
```

### MOC Articles (`knowledge/mocs/`)

Map-of-Content hubs (`type: moc`) — the curated "architecture map" entry points, one per
project (and per large subsystem when warranted). **Links, not walls of text**: a MOC is a
navigable map of the existing articles, with one-line annotations and a clickable Mermaid
architecture diagram, never a re-statement of their content.

Conventions:
- Filenames stay kebab-case (`moc-<scope>.md`) — NOT Title-Case — so the deterministic
  slug links keep working. The `title:` carries the human form (`"MOC: Stripe Ledger"`).
- The Mermaid diagram uses article slugs as node ids and the Obsidian `internal-link`
  class so nodes are clickable: `class slug-a,slug-b internal-link;`
- MOCs are exempt from the orphan / backlink-reciprocity lint checks (hubs link out by
  design); they still require the full frontmatter (`type: moc`, `status`, `subsystem`,
  `summary`, `tags`).
- **Compiler contract:** when a compile creates a new article, add a link for it (with a
  one-line annotation) to the relevant section of its project's MOC. Do not rewrite or
  restructure the MOC beyond that.

````markdown
---
title: "MOC: Project Name"
project: project-slug
type: moc
status: active
subsystem: architecture
summary: "Architecture map + curated entry points for the project."
tags: [project-slug, architecture-map]
created: 2026-04-05T00:00:00-06:00
updated: 2026-04-05T00:00:00-06:00
---

# MOC: Project Name

> [!summary] One-paragraph orientation: what the system is, the load-bearing idea.

## Architecture

```mermaid
graph TD
    slug-a --> slug-b
    class slug-a,slug-b internal-link;
```

## <Subsystem>

- [[concepts/slug-a]] — one-line annotation
- [[concepts/slug-b]] — one-line annotation
````

---

## Core Operations

### 1. Compile (daily/ -> knowledge/)

When processing a daily log:

1. Read the daily log file
2. Read `knowledge/index.md` to understand current knowledge state
3. Read existing articles that may need updating
4. For each piece of knowledge found in the log:
   - If an existing concept article covers this topic: UPDATE it with new information, add the daily log as a source
   - If it's a new topic: CREATE a new `concepts/` article
5. If the log reveals a non-obvious connection between 2+ existing concepts: CREATE a `connections/` article
6. APPEND to `knowledge/log.md`

The index is **not** hand-edited — it is regenerated from frontmatter by
`scripts/build_index.py` after the compile. So instead of writing index rows, make sure
every new/updated article carries a current `summary:`, the right `subsystem:`, and the
other frontmatter fields; the catalog follows automatically.

**Important guidelines:**
- A single daily log may touch 3-10 knowledge articles
- Prefer updating existing articles over creating near-duplicates
- Reuse an existing `subsystem:` slug where one fits; only add a new subsystem for a
  genuinely new area (it becomes a new index section)
- Use Obsidian-style `[[wikilinks]]` with full relative paths from knowledge/
- Write in encyclopedia style - factual, concise, self-contained
- Every article must carry the full frontmatter (see "Frontmatter fields")
- Every article must link back to its source daily logs

### 2. Query (Ask the Knowledge Base)

1. Read `knowledge/index.md` (the master catalog)
2. Based on the question, identify 3-10 relevant articles from the index
3. Read those articles in full
4. Synthesize an answer with `[[wikilink]]` citations
5. If `--file-back` is specified: create a `knowledge/qa/` article and update index.md and log.md

**Why this works without RAG:** At personal knowledge base scale (50-500 articles), the LLM reading a structured index outperforms cosine similarity. The LLM understands what the question is really asking and selects pages accordingly. Embeddings find similar words; the LLM finds relevant concepts.

### 3. Lint (Health Checks)

Nine checks, run periodically:

1. **Broken links** - `[[wikilinks]]` pointing to non-existent articles
2. **Orphan pages** - Articles with zero inbound links from other articles
3. **Orphan sources** - Daily logs that haven't been compiled yet
4. **Stale articles** - Source daily log changed since article was last compiled
5. **Contradictions** - Conflicting claims across articles (requires LLM judgment)
6. **Missing backlinks** - A links to B but B doesn't link back to A
7. **Sparse articles** - Below 200 words, likely incomplete
8. **Frontmatter schema** - Missing/invalid `type`/`status`/`subsystem`/`summary`, unquoted YAML
9. **Tags hygiene** - Missing `tags:`, project slug not first, malformed tags, singleton-tag sprawl

Output: a markdown report with severity levels (error, warning, suggestion).

---

## Conventions

- **Wikilinks:** Use Obsidian-style `[[path/to/article]]` without `.md` extension
- **Writing style:** Encyclopedia-style, factual, third-person where appropriate
- **Dates:** ISO 8601. `created`/`updated` carry a full timestamp with offset
  (`2026-04-03T00:00:00-06:00`) so Dataview can sort by them; `valid_as_of`/`filed` are
  date-only; `log.md` timestamps are full ISO.
- **Callouts:** Use Obsidian callouts for scannability — `> [!summary]` for the core
  takeaway, `> [!warning]` for gotchas/footguns, `> [!note]` for asides. Prefer these over
  plain bold labels for Key Points and caveats.
- **Aliases:** 0–2 per article, never the filename itself, and scope generic terms so they
  don't collide across articles.
- **Tags:** the first tag is ALWAYS the article's `project:` slug — the deterministic tag
  guard (compile post-pass) inserts/reorders it, so Obsidian can always filter or color a
  whole project's articles by tag. The 2–5 domain tags after it are a **curated
  vocabulary**, not free association: lowercase-kebab, reused across articles (a tag only
  pays off when it groups 2+ articles), never a near-synonym of an existing tag, never a
  duplicate of `subsystem:`. Lint flags singleton-tag sprawl.
- **File naming:** lowercase, hyphens for spaces (e.g., `supabase-row-level-security.md`)
- **Frontmatter YAML safety:** any scalar value containing `: ` MUST be double-quoted — `summary:` always. Unquoted colons parse in grep-based tools but are invalid YAML.
- **Frontmatter:** Every article must carry the fields in the "Frontmatter fields" table
  above — at minimum `title`, `project`, `type`, `status`, `subsystem`, `summary`,
  `tags`, `sources`, `created`, `updated`.
- **Sources:** Always link back to the daily log(s) that contributed to an article
- **Backticks = literal code only:** Put inside `` `backticks` `` ONLY identifiers that exist
  verbatim in the code — function/method/variable/field/file names, metadata keys, config
  values, API fields. Do **not** backtick formula variables, math placeholders, or prose
  shorthand (write *the gross-receipt accumulator* in prose, not `` `grossReceiptAcc` ``; a
  template like `{cc}_tax_{prefix}_due` is fine only if that literal key appears in the code).
  Backticked tokens are treated as verifiable code symbols by the Tier-1 gate
  (`scripts/verify.py`); backticking a non-symbol manufactures a false "absent" finding — this
  is the dominant Tier-1 false positive. When citing a *rejected* or *superseded* value, keep
  the rejection explicit in the same sentence ("chosen over", "rather than", "no longer", "did
  not …") so the gate reads it as a correct-negative rather than a missing symbol.

---

## Full Project Structure

```
llm-personal-kb/
|-- .claude/
|   |-- settings.json                # Hook configuration (auto-activates in Claude Code)
|-- .codex/
|   |-- hooks.json                   # Optional Codex hook configuration in opted-in codebases
|-- .gitignore                       # Excludes runtime state, temp files, caches
|-- AGENTS.md                        # This file - schema + full technical reference
|-- README.md                        # Concise overview + quick start
|-- pyproject.toml                   # Dependencies (at root so hooks can find it)
|-- daily/                           # "Source code" - conversation logs (immutable)
|-- knowledge/                       # "Executable" - compiled knowledge (LLM-owned)
|   |-- index.md                     #   Master catalog - THE retrieval mechanism
|   |-- log.md                       #   Append-only build log
|   |-- concepts/                    #   Atomic knowledge articles
|   |-- connections/                 #   Cross-cutting insights linking 2+ concepts
|   |-- qa/                          #   Filed query answers (compounding knowledge)
|-- scripts/                         # CLI tools
|   |-- compile.py                   #   Compile daily logs -> knowledge articles
|   |-- query.py                     #   Ask questions (index-guided, no RAG)
|   |-- lint.py                      #   7 health checks
|   |-- flush.py                     #   Extract memories from conversations (background)
|   |-- config.py                    #   Path constants
|   |-- utils.py                     #   Shared helpers
|-- hooks/                           # Claude Code + Codex hooks
|   |-- session-start.py             #   Injects knowledge into every session
|   |-- session-end.py               #   Extracts conversation -> daily log
|   |-- pre-compact.py               #   Safety net: captures context before compaction
|   |-- stop.py                      #   Bootstrap/safety-valve early capture (Claude Stop)
|-- reports/                         # Lint reports (gitignored)
```

---

## Hook System (Automatic Capture)

Hooks are configured in Claude Code's `.claude/settings.local.json` and Codex's
`.codex/hooks.json` for opted-in codebases. Both flows call the same scripts and
write the same daily logs.

### `.claude/settings.json` Format

```json
{
  "hooks": {
    "SessionStart": [{ "matcher": "", "hooks": [{ "type": "command", "command": "uv run python hooks/session-start.py", "timeout": 15 }] }],
    "PreCompact": [{ "matcher": "", "hooks": [{ "type": "command", "command": "uv run python hooks/pre-compact.py", "timeout": 10 }] }],
    "SessionEnd": [{ "matcher": "", "hooks": [{ "type": "command", "command": "uv run python hooks/session-end.py", "timeout": 10 }] }],
    "Stop": [{ "matcher": "", "hooks": [{ "type": "command", "command": "uv run python hooks/stop.py", "timeout": 10 }] }]
  }
}
```

Commands use simple relative paths from the project root. Empty `matcher` catches all events.

### `.codex/hooks.json` Format

```json
{
  "hooks": {
    "SessionStart": [{ "matcher": "startup|resume|clear|compact", "hooks": [{ "type": "command", "command": "uv run --directory <kb> python hooks/session-start.py", "timeout": 15 }] }],
    "PreCompact": [{ "matcher": "manual|auto", "hooks": [{ "type": "command", "command": "uv run --directory <kb> python hooks/pre-compact.py", "timeout": 10 }] }],
    "Stop": [{ "hooks": [{ "type": "command", "command": "uv run --directory <kb> python hooks/session-end.py", "timeout": 10 }] }]
  }
}
```

Codex requires project-local hooks to be trusted before they run.

### Hook Details

**`session-start.py`** (SessionStart)
- Pure local I/O, no API calls, runs in under 1 second
- Reads `knowledge/index.md` and the most recent daily log
- Outputs JSON to stdout: `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}`
- Claude sees the knowledge base index at the start of every session
- Max context: 20,000 characters

**`session-end.py`** (Claude SessionEnd / Codex Stop)
- Reads hook input from stdin (JSON with `session_id`, `transcript_path`, `cwd`)
- Extracts the delta since this session's last save (`transcripts.extract_delta`,
  uncapped — it is the last chance to capture, so flush.py chunks the full delta)
- Writes the delta to a temp `.md` and spawns `flush.py` as a detached background process
- Recursion guard: exits immediately if `CLAUDE_INVOKED_BY` env var is set

**`pre-compact.py`** (PreCompact)
- Same architecture as session-end.py, but applies the roll-forward char cap
  (keep the oldest turns that fit in `max_chars`, defer the newer overflow)
- Fires before Claude Code auto-compacts the context window
- Guards against empty `transcript_path` (known Claude Code bug #13668)
- Reports first-flush `truncated` so a capture that races past `max_turns`
  before any save leaves a visible recovery breadcrumb (not a silent drop)

**`stop.py`** (Claude Stop — Claude Code only)
- Fires after every assistant turn; almost always exits immediately (one
  transcript scan that early-exits at the relevant threshold)
- Spawns a background flush only to **bootstrap** (no save marker yet AND turns
  ≥ `bootstrap_turns`) or as a **safety valve** (a marker exists but
  turns-since-save ≥ `max_turns` — a long session that never compacted)
- Per-session debounce file prevents duplicate spawns while a flush is still running
- Recursion guard like the others; opt-in gate like the others
- Codex does not register this — its Stop event already maps to session-end.py

**Why PreCompact + SessionEnd + Stop?** SessionEnd fires only at the very end and
PreCompact only on compaction, so a long session that has done neither has
captured nothing — and its eventual first flush would keep only the last
`max_turns` turns, hiding the rest behind the marker (the first-flush footgun).
`stop.py` closes that gap by establishing the delta marker EARLY (after
`bootstrap_turns`), making capture genuinely fire-and-forget. After the
bootstrap, PreCompact/SessionEnd delta capture keeps everything current.

### Capture sizing & cadence (`scripts/capture-config`)

All four capture surfaces (the three hooks + the status line) read their limits
from `scripts/capture-config` via `capture_config.get_limits()`. Each hook is a
fresh process per event, so **edits take effect on the next captured session
event — no restart or new session needed.**

| Knob | Default | What it controls | When to change it |
|---|---|---|---|
| `bootstrap_turns` | 45 | Turns after which `stop.py` auto-fires the FIRST flush (sets the delta marker early). `0` disables the bootstrap. | **Lower** (e.g. 25) to bank knowledge sooner. **Raise** (60–80) if your early turns are scaffolding/architecture you'd rather see settled before the first save. Keep it well below `max_turns`. |
| `max_turns` | 120 | First-flush look-back window; status-line gauge; `stop.py` safety-valve threshold. | Raise only if you knowingly want bigger first flushes; with the bootstrap on, the first-flush window rarely matters. |
| `max_chars` | 50000 | Per-flush text cap (PreCompact/Stop); roll-forward defers the overflow to the next flush. | Raise if you compact rarely and want bigger bites per flush. |
| `chunk_chars` | 45000 | Summarizer chunk size for large captures. | Rarely changed; lower if the summarizer model's context is tight. |

Rule of thumb: the bootstrap is what removes the "exit-and-wait" burden, so the
only knob most users touch is `bootstrap_turns`. The status line (`🧠 N/max_turns`)
shows turns since the last *save*; with the bootstrap on it should reset itself
shortly after `bootstrap_turns` without any action from you.

### Background Flush Process (`flush.py`)

Spawned by both hooks as a fully detached background process:
- **Windows:** `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` flags
- **Mac/Linux:** `start_new_session=True`

This ensures flush.py survives after Claude Code's hook process exits.

**What flush.py does:**
1. Sets `CLAUDE_INVOKED_BY=memory_flush` env var (prevents recursive hook firing)
2. Reads the pre-extracted conversation context from the temp `.md` file
3. Skips if context is empty or if same session was flushed within 60 seconds (deduplication)
4. Calls Claude Agent SDK (`query()` with `allowed_tools=[]`, `max_turns=2`)
5. Claude decides what's worth saving - returns structured bullet points or `FLUSH_OK`
6. Appends result to `daily/YYYY-MM-DD.md`
7. Cleans up temp context file
8. **End-of-day auto-compilation:** If it's past 6 PM local time (`COMPILE_AFTER_HOUR = 18`) and today's daily log has changed since its last compilation (hash comparison against `state.json`), spawns `compile.py` as another detached background process. This means compilation happens automatically once a day without needing a cron job or manual trigger.

### JSONL Transcript Format

Claude Code stores conversations as `.jsonl` files with messages nested under a
`message` key:

```python
entry = json.loads(line)
msg = entry.get("message", {})
role = msg.get("role", "")     # "user" or "assistant"
content = msg.get("content", "")  # string or list of content blocks
```

Content can be a string or a list of blocks (`{"type": "text", "text": "..."}` dicts).

Codex stores session transcripts under `$CODEX_HOME/sessions` (default:
`~/.codex/sessions`) as JSONL. User/assistant turns are `response_item` entries
whose payload is a `message` with `input_text`/`output_text` content blocks. The
shared `scripts/transcripts.py` parser normalizes both formats.

---

## Script Details

### compile.py - The Compiler

Uses the Claude Agent SDK's async streaming `query()`:

```python
async for message in query(
    prompt=compile_prompt,
    options=ClaudeAgentOptions(
        cwd=str(ROOT_DIR),
        system_prompt={"type": "preset", "preset": "claude_code"},
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
        permission_mode="acceptEdits",
        max_turns=30,
    ),
):
```

- Builds a prompt with: AGENTS.md schema, current index, all existing articles, and the daily log
- Claude reads the daily log, decides what concepts to extract, and writes files directly
- `permission_mode="acceptEdits"` auto-approves all file operations
- Incremental: tracks SHA-256 hashes of daily logs in `state.json`, skips unchanged files
- Cost: ~$0.45-0.65 per daily log (increases as KB grows)

**CLI:**
```bash
uv run python scripts/compile.py              # compile new/changed only
uv run python scripts/compile.py --all        # force recompile everything
uv run python scripts/compile.py --file daily/2026-04-01.md
uv run python scripts/compile.py --dry-run
```

### query.py - Index-Guided Retrieval

Puts ONLY the index (`knowledge/index.md`) into the prompt, then has the Agent SDK
session pick the 3-10 relevant articles and read just those with the Read tool. No
RAG, and no loading the whole KB — the index is the retrieval mechanism.

At personal KB scale (50-500 articles), the LLM reading a structured index outperforms vector similarity. The LLM understands what you're really asking; cosine similarity just finds similar words.

Like compile.py it sets the `CLAUDE_INVOKED_BY` recursion guard (to `query`) before
the SDK import, runs with the same per-call timeout+retry from `capture-config`, and
emits to `activity.jsonl` (`source=query`) so the Obsidian plugin notifies on answers.
Read-only by default (Read/Glob/Grep); `--file-back` also grants Write/Edit.

**CLI:**
```bash
uv run python scripts/query.py "What auth patterns do I use?"
uv run python scripts/query.py "What CN types exist?" --project stripe-ledger
uv run python scripts/query.py "What's my error handling strategy?" --file-back
```

`--project <slug>` pre-filters the index to one project (the Project column) before the
LLM sees it. With `--file-back`, the agent files a Q&A article in `knowledge/qa/` (with
`project:` + `type: qa` frontmatter), adds an index row, and appends a `query` entry to
`knowledge/log.md`. This is the compounding loop - every question makes the KB smarter.
Thin surfaces: the `/ask` slash command, `~/.devlore/bin/query.sh`, and the devlore Obsidian
plugin's "Ask the devlore knowledge base" command + ribbon button.

### lint.py - Health Checks

Nine checks:

| Check | Type | Catches |
|-------|------|---------|
| Broken links | Structural | `[[wikilinks]]` to non-existent articles |
| Orphan pages | Structural | Articles with zero inbound links |
| Orphan sources | Structural | Daily logs not yet compiled |
| Stale articles | Structural | Source logs changed since compilation |
| Missing backlinks | Structural | A links to B but B doesn't link back |
| Sparse articles | Structural | Under 200 words |
| Frontmatter schema | Structural | Missing/invalid `type`/`status`/`subsystem`/`summary`, unquoted YAML |
| Tags hygiene | Structural | Missing `tags:`, project slug not first, malformed tags, singleton sprawl |
| Contradictions | LLM | Conflicting claims across articles |

**CLI:**
```bash
uv run python scripts/lint.py                    # all checks
uv run python scripts/lint.py --structural-only  # skip LLM check (free)
```

Reports saved to `reports/lint-YYYY-MM-DD.md`.

---

## State Tracking

`scripts/state.json` tracks:
- `ingested` - map of daily log filenames to SHA-256 hashes, compilation timestamps, and costs
- `query_count` - total queries run
- `last_lint` - timestamp of most recent lint
- `total_cost` - cumulative API cost

`scripts/last-flush.json` tracks flush deduplication (session_id + timestamp).

Both are gitignored and regenerated automatically.

---

## Dependencies

`pyproject.toml` (at project root):
- `claude-agent-sdk>=0.1.29` - Claude Agent SDK for LLM calls with tool use
- `python-dotenv>=1.0.0` - Environment variable management
- `tzdata>=2024.1` - Timezone data
- Python 3.12+, managed by [uv](https://docs.astral.sh/uv/)

No API key needed - uses Claude Code's built-in credentials at `~/.claude/.credentials.json`.

---

## Costs

| Operation | Cost |
|-----------|------|
| Compile one daily log | $0.45-0.65 |
| Query (no file-back) | ~$0.15-0.25 |
| Query (with file-back) | ~$0.25-0.40 |
| Full lint (with contradictions) | ~$0.15-0.25 |
| Structural lint only | $0.00 |
| Memory flush (per session) | ~$0.02-0.05 |

---

## Customization

### Additional Article Types

Add directories like `people/`, `projects/`, `tools/` to `knowledge/`. Define the article format in this file (AGENTS.md) and update `utils.py`'s `list_wiki_articles()` to include them.

### Obsidian Integration

The knowledge base is pure markdown with `[[wikilinks]]` - works natively in Obsidian. Point a vault at `knowledge/` for graph view, backlinks, and search. Every article's `tags:` leads with its project slug, so the tag pane, graph filters (`tag:#project-slug`), and Bases views can slice the vault by project out of the box; the curated domain tags behind it power finer filtering.

**Optional side-panel plugin** (`.obsidian/plugins/devlore/`): a single-purpose, hardcoded-script plugin (NOT a general command runner — see the header in `main.js`) that surfaces devlore from the ribbon, command palette, and right-click menu: ingest, compile, ask, **verify**, **status**, **update** (refresh machinery), and **recheck**. Each action runs exactly one shim in `~/.devlore/bin/` (`devlore.sh`, `compile.sh`, `query.sh`, `verify.sh`, `status.sh`, `update.sh`, `recheck.sh` — v0.9.27+; each is a 4-line `exec ~/.devlore/bin/devlore <sub>` so the launcher supplies the venv and `PYTHONPATH`, which Obsidian's minimal GUI shell cannot). The install/refresh path is shared by `init_kb --with-obsidian` and the `devlore obsidian` command via `scripts/obsidian_setup.install_obsidian_layer` (one source of truth for the copy + `__DEVLORE_HOME__` rewrite + `app.json`).

- **Adding it after opting out:** `devlore obsidian` installs/refreshes the layer into the current KB, sourcing the plugin bytes from the distribution cache (`~/.devlore/dist`) so a never-opted-in KB still works. `devlore update` refreshes the plugin too, but only for KBs that already have an `.obsidian/` directory.
- **Activation (new users must do this once):** Obsidian disables third-party plugins until trusted — open the KB as a vault, Settings → Community plugins → turn off Restricted mode (trust author), then toggle `devlore` on. `devlore obsidian` and `init_kb --with-obsidian` both print these steps (`obsidian_setup.activation_steps`).

### Scaling Beyond Index-Guided Retrieval

At ~2,000+ articles / ~2M+ tokens, the index becomes too large for the context window. At that point, add hybrid RAG (keyword + semantic search) as a retrieval layer before the LLM. See Karpathy's recommendation of `qmd` by Tobi Lutke for search at scale.
