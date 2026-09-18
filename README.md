<div align="center">

# 📜 devlore

**Every codebase has lore — the decisions, the dead ends, the hard-won lessons.<br>Yours is locked inside your Claude Code and Codex conversations. devlore sets it free.**

*Every session you run contains architectural decisions, debugging journeys, and hard-won
lessons — and today it all evaporates into compacted context and 30-day-old transcripts.
devlore captures it, compiles it into a living wiki, verifies it against your code,
and lets you ask it questions. Locally. Automatically. Forever.*

`one command` · `local-first` · `self-checking` · `Obsidian-optional`

<img src="demo/hero.gif" alt="devlore add . — months of conversations become a queryable wiki" width="840">

</div>

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/condechi/devlore/main/install.sh | bash
```

That's it. You now have a knowledge base at `~/devlore` and a `devlore` command.

<sub>Needs: [Claude Code](https://claude.com/claude-code) (logged in, used for compile/query), [uv](https://docs.astral.sh/uv/), git. Optional: [Codex](https://developers.openai.com/codex) for Codex conversation capture. macOS/Linux.</sub>

## The magic moment — 60 seconds

```bash
cd ~/code/my-project        # any repo you've been working on with Claude Code or Codex
devlore add .
```

devlore will:

1. **Wire live capture** — every future Claude Code or Codex session in this repo flows into the KB automatically (hooks, zero effort).
2. **Find your past conversations** for this repo and show you a plan **with a cost estimate** — say yes and it distills *months of existing sessions* into wiki articles. Instant value from data you already have.
3. **Offer the repo's markdown docs** for ingestion too.
4. **Brief you** on what it learned.

Then ask it things:

```bash
devlore ask "Why did we switch away from the queue-based design?"
devlore ask "What are the known footguns in the payment flow?" --dev   # + live file:line pointers
```

Answers come back **cited** — every claim links to the articles (and the daily logs) it came from:

<img src="demo/receipts.gif" alt="devlore ask --dev — cited answers with live code pointers and a self-doubt caution" width="840">

## What you get

- 📥 **Automatic capture** — session-end and pre-compaction hooks distill each conversation into an immutable daily log. The knowledge survives compaction, `/clear`, and transcript expiry.
- 📚 **An LLM-compiled wiki** — an agent reads the daily logs and writes encyclopedia-style articles (concepts, connections, Q&A, maps-of-content), organized by project → subsystem, with a generated catalog. When a decision changes, the article is **rewritten to the new truth** and the old one is preserved in an append-only history log.
- 🔍 **Index-guided retrieval, no RAG** — `devlore ask` has an LLM read the catalog, pick the few relevant articles, and synthesize a cited answer. At personal-KB scale this beats cosine similarity, every time.
- 🛡️ **A self-checking KB** (the part we're proudest of) — an LLM-written wiki *will* drift from reality, so devlore ships a verification ladder:
  - **Tier 1 — hallucination gate**: every cited code identifier is checked against a full-repo symbol index; absences are deterministically triaged (rename? rejected-value citation? genuinely fabricated?).
  - **Tier 2 — staleness**: every article is stamped with the git SHAs of the code it describes; when that code changes (even uncommitted!), the article **marks itself** `needs-reverification` — and clears itself when re-verified or recompiled.
  - **Tier 3 — adversarial review** (opt-in): a refute-by-default agent attacks surviving claims with file:line evidence.
  - **Tier 4 — honesty about limits**: claims resting on law, policy, or external-API behavior are tagged as never auto-trustable.
- 💸 **Cost transparency** — anything that spends money shows a plan + estimate first and waits for your yes. Batch backfills run behind regression checks and auto-quarantine with full rollback.
- 🗃️ **Git-versioned by design** — the KB auto-commits after every pipeline write. Your knowledge history is one `git log` away. Local-only; nothing ever leaves your machine.
- 🔌 **Obsidian as an optional superpower** — open the KB directory as a vault and get graph view, clickable wikilinks, Dataview-queryable frontmatter, clickable architecture diagrams, and a side-panel plugin that drives ingest / compile / ask / verify / status from the ribbon and command palette. Didn't opt in at install? `devlore obsidian` adds it anytime. (One-time: Obsidian disables third-party plugins until you trust the vault and toggle devlore on under Settings → Community plugins — `devlore obsidian` prints the exact steps.) But the whole system runs headless; Obsidian is a skin, never a dependency.

## How it works

```
 Claude Code / Codex sessions  daily/               knowledge/
┌─────────────────────┐  ┌────────────────┐  ┌─────────────────────────┐
│ you, working        │  │ immutable      │  │ concepts/  connections/ │
│ normally            ├─►│ conversation   ├─►│ qa/  mocs/  index.md    │
│ (hooks capture)     │  │ logs (source)  │  │ (LLM-compiled wiki)     │
└─────────────────────┘  └────────────────┘  └───────────┬─────────────┘
                                  ▲                      │
                                  │             ┌────────▼─────────┐
 your codebase  ◄────────────────────────────── │ verification     │
 (symlinked, symbol-indexed,                    │ ladder: Tiers 1-4│
  git-SHA staleness tracking)                   │ + self-marking   │
                                                └──────────────────┘
```

Source-code analogy (h/t Karpathy): the daily logs are *source code*, the compile step is a
*compiler*, the wiki is the *build artifact* — and the verification ladder is the *test suite*.

## CLI

| Command | What it does |
|---|---|
| `devlore add <codebase>` | Opt a repo in: live capture + backfill past conversations + ingest docs |
| `devlore remove <codebase>` | Stop capturing a repo — knowledge stays; `--full` also drops the verify/staleness code link |
| `devlore ask "…"` | Cited answer from the KB (`--dev` adds live file:line pointers, `--file-back` saves the Q&A) |
| `devlore compile` | Compile pending daily logs into articles |
| `devlore verify` | Run the hallucination + staleness gates |
| `devlore backfill` | Batch-ingest past Claude Code and Codex conversations (dry-run + cost gate first) |
| `devlore docs <path>` | Ingest markdown docs, then compile (a dir scans root + first-level subdirs, git-aware + vendor-filtered; `--full-recursive` for the whole tree, `--ignore-gitignore` to include gitignored docs — compiling costs real money, review the preview) |
| `devlore status` | What the KB holds: articles, dailies, captured sessions, capture roots, spend |
| `devlore update` | Refresh the KB's machinery from the latest release (your knowledge is never touched) |
| `devlore obsidian` | Add/refresh the optional Obsidian layer (side-panel plugin + vault config) in this KB — use it if you skipped Obsidian at install; prints the one-time activation steps |
| `devlore version` | Print the installed devlore version |
| `devlore init <dir>` | Bootstrap a *second* knowledge base for another project |

## FAQ

**Does my data leave my machine?** No. Everything is local files + your own Claude Code
account doing the compilation. The KB git repo has no remote unless you add one.

**What does it cost to run?** Capture is ~free (one small distillation per session end).
Compilation is the LLM step — typically $1–6 per working day of conversation, and every
batch operation shows you the bill before running. You control the throttle.

**I don't use Obsidian.** Neither does the machinery. Everything works from the terminal;
the wiki is plain markdown readable anywhere. Obsidian just makes it gorgeous.

**Why not RAG?** At 50–500 articles, an LLM reading a human-grade index outperforms
embedding similarity — it understands what your question *means*. The catalog is the
retrieval system.

**Can it be wrong?** Yes — that's why half this project is the verification ladder. An
unverified LLM knowledge base is a hallucination amplifier; devlore treats incorrect
context as seriously as lost context.

**Multiple projects?** One KB can hold several (articles carry a `project:` field), or run
`devlore init` to spin up isolated KBs — the status line knows which one you're in.

## The lore

Every codebase has lore — including this one. From a memory-compiler clone in a
single repo, through a transcript-loss scar that became the tagline, to a smoke
test that built a knowledge base about the knowledge base: the full origin story
is in [HISTORY.md](HISTORY.md). Fittingly, it was reconstructed by asking
devlore's own meta-KB.

## Credits

Built in the open by working it daily on a real production system. devlore started life as a
clone of [coleam00's claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler) —
the capture → compile → inject loop is his scaffold, and nearly every mechanism inside it has
since been rebuilt. The architecture traces back to Andrej Karpathy's "LLM-compiled personal
knowledge base" idea. MIT licensed — PRs welcome.
