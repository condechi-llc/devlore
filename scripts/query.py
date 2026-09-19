"""
Ask the knowledge base a question — index-guided retrieval, no RAG.

This is the headless entry point for the retrieval breakthrough: the LLM reads
`knowledge/index.md` (the master catalog), picks the handful of articles that
actually answer the question, reads ONLY those in full, and synthesizes a cited
answer. At personal-KB scale (50-500 articles) an LLM reading a structured index
beats cosine similarity — it understands what the question is really asking and
selects pages accordingly. See AGENTS.md §"Query".

With `--file-back` the answer is filed as a proper `knowledge/qa/` article (with
`project:` + `type: qa` frontmatter), an index row is added, and a `query` entry
is appended to `knowledge/log.md`. That is the compounding loop — every answered
question makes the next query smarter.

With `--dev` (PR D code-linking, behind a dev wall) the agent additionally resolves the
answer's load-bearing cited symbols to their CURRENT `file:line` via grep over `crm/` and
`metadata/` — lazily at query time, NEVER stored (line numbers rot; the KB keeps symbols).
The pointers section is framed experimental/unverified and flags consulted articles whose
`status: needs-reverification`. Normal queries stay conceptual and authoritative.

Usage:
    uv run python query.py "How does the correction-method rule work?"
    uv run python query.py "What CN types exist?" --project stripe-ledger
    uv run python query.py "What's our OOB amount policy?" --file-back
    uv run python query.py "Where is the 1¢ OOB amount applied?" --dev
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Recursion guard: the query spawns an internal Agent SDK (Claude Code) session
# to read the wiki and synthesize the answer. That session runs in this captured
# project, so without this flag the capture hooks would fire on the QUERY'S OWN
# session and try to flush its transcript (recursive self-capture). The hooks
# skip when this is set; it must be set BEFORE any Agent SDK subprocess spawns.
# setdefault preserves an outer value if a parent already set one.
os.environ.setdefault("CLAUDE_INVOKED_BY", "query")

from capture_config import get_limits
from config import KNOWLEDGE_DIR, QA_DIR, now_iso, system_cli_path
from utils import load_state, read_wiki_index, save_state, slugify

ROOT_DIR = Path(__file__).resolve().parent.parent

# Durable trail of every query (start / answer / cost / errors). Errors here are
# otherwise invisible: the bundled-CLI failure mode dies with no stderr, and a
# query run from Obsidian/the wrapper has no terminal to print to. Mirrors
# flush.py's flush.log. Append-only; the answer text still goes to stdout, not here.
LOG_FILE = Path(__file__).resolve().parent / "query.log"
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def filter_index_by_project(index_text: str, slug: str) -> str:
    """Return the index narrowed to one project's section.

    The index (PR C) is sectioned by project → subsystem: an `## <project>` H2 per
    project, `### <subsystem>` H3s and tables beneath it. To restrict retrieval to one
    project, keep the preamble (everything before the first `## ` H2) plus only the
    matching project's H2 block, so the LLM never sees other projects' articles."""
    lines = index_text.splitlines()
    kept: list[str] = []
    in_target = False
    seen_h2 = False
    for line in lines:
        is_h2 = line.startswith("## ") and not line.startswith("### ")
        if not seen_h2 and not is_h2:
            kept.append(line)  # title + preamble, before any project section
            continue
        if is_h2:
            seen_h2 = True
            in_target = line[3:].strip() == slug
            if in_target:
                kept.append(line)
            continue
        if in_target:
            kept.append(line)
    return "\n".join(kept)


def unique_qa_slug(question: str) -> str:
    """Deterministic, collision-free slug for a filed Q&A article."""
    base = slugify(question)[:60].strip("-") or "query"
    slug, n = base, 2
    while (QA_DIR / f"{slug}.md").exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


def build_prompt(question: str, index_text: str, project: str | None,
                 file_back_slug: str | None, timestamp: str, dev: bool = False) -> str:
    project_note = (
        f"\n\n**Project filter:** restrict your answer to the **{project}** project. "
        f"The index below has already been narrowed to that project — only consider "
        f"these articles."
        if project else ""
    )

    file_back_section = ""
    if file_back_slug:
        qa_rel = f"qa/{file_back_slug}"
        project_cell = project or "<the project the consulted articles belong to>"
        file_back_section = f"""

## Also: file this answer back into the knowledge base

After writing the answer, persist it so future queries compound on it:

1. **Create the Q&A article** at `{QA_DIR / (file_back_slug + '.md')}` with this exact frontmatter:
   ```yaml
   ---
   title: "Q: {question}"
   question: "{question}"
   project: {project_cell}
   type: qa
   tags: [<this article's project slug FIRST>, <2-3 lowercase-kebab domain tags reused from the consulted articles>]
   consulted:
     - "concepts/<each-article-you-actually-read>"
   created: {timestamp[:10]}
   updated: {timestamp[:10]}
   filed: {timestamp[:10]}
   ---
   ```
   Body sections: `# Q: {question}`, then `## Answer` (the synthesized answer WITH
   `[[wikilink]]` citations), `## Sources Consulted` (one bullet per article, why it
   was relevant), and `## Follow-Up Questions` (2-3). The `project:` value MUST match
   the consulted articles' project (use `{project_cell}`).

2. **Add an index row** to `{KNOWLEDGE_DIR / 'index.md'}` (the table has columns
   Article | Project | Summary | Compiled From | Updated):
   `| [[{qa_rel}]] | {project_cell} | <one-line answer summary> | (query) | {timestamp[:10]} |`
   The Project cell MUST equal this article's `project:` value.

3. **Append a `query` entry** to `{KNOWLEDGE_DIR / 'log.md'}`:
   ```
   ## [{timestamp}] query | "{question}"
   - Consulted: [[concepts/x]], [[concepts/y]]
   - Filed to: [[{qa_rel}]]
   ```

Do this with the Write/Edit tools, then end your reply with the answer itself."""

    dev_section = ""
    if dev:
        filed_note = (
            "\n- If you are also filing the answer back (`--file-back`), the filed Q&A "
            "article gets ONLY the conceptual answer — never include this pointers section "
            "in any file you write." if file_back_slug else ""
        )
        dev_section = f"""

## Dev mode: live code pointers (--dev)

After (and only after) synthesizing the conceptual answer, ground it in the CURRENT code:

1. From the articles you actually read, pick the **3-8 most load-bearing cited code
   identifiers** (the function/field/file names central to the answer).
2. Resolve each to its CURRENT location with the Grep tool over `crm/` and `metadata/`
   (OUR code only — never `node_modules`, `dist`, or `.venv`).
3. End your reply with one extra section, after the answer:

   ## Code pointers (resolved live — experimental)
   - `symbol` → `crm/lib/file.js:123` — one line each; mark with ⚠ any symbol you could
     NOT find (a possible rename — say so rather than guessing)

Rules:
- Pointers are resolved lazily AT QUERY TIME and are valid only right now. Do **not**
  write them into any article or file — the KB stores symbols, never line numbers
  (line numbers rot on every edit).{filed_note}
- This section is experimental/unverified: it locates symbols, it does not re-verify the
  articles' claims about them.
- If any article you consulted carries `status: needs-reverification` in its frontmatter,
  add a caution line naming it — its code-level claims may lag the working tree."""

    project_pick_note = (
        f" Only pick articles in the **{project}** project." if project else ""
    )
    return f"""You are answering a question from a personal knowledge base using index-guided
retrieval (no RAG). The knowledge base lives under `knowledge/` in this project.

## Question

{question}{project_note}

## Knowledge Base Index

This catalog lists every article with a one-line summary. Read it, then decide which
articles are relevant.

{index_text}

## Your Task

1. From the index above, pick the **3-10 articles** most relevant to the question.
   Prefer fewer, highly-relevant articles over a broad sweep.{project_pick_note}
2. **Read those articles in full** with the Read tool (paths are relative to the
   project root, e.g. `knowledge/concepts/<slug>.md`). Read ONLY the ones you chose —
   do not read the entire knowledge base.
3. **Synthesize a direct, cited answer.** Cite the articles you used inline with
   Obsidian `[[wikilink]]` syntax (e.g. `[[concepts/correction-method-rule]]`). Be
   specific and faithful to the articles — if the knowledge base does not contain the
   answer, say so plainly rather than inventing one.
4. Your **final message must be the answer itself** (it is printed to the user). Do
   not wrap it in preamble like "Here is the answer".{file_back_section}{dev_section}
"""


async def run_query(prompt: str, file_back: bool) -> tuple[str, float]:
    """Run the index-guided query via the Agent SDK. Returns (answer, cost_usd).

    Read-only by default (Read/Glob/Grep). `--file-back` also grants Write/Edit so
    the agent can file the Q&A article and update the index + log. The answer is the
    text of the LAST assistant message that carried text (the final synthesis turn)."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    tools = ["Read", "Glob", "Grep"]
    if file_back:
        tools += ["Write", "Edit"]

    # Use the system CLI, not the SDK's stale/flaky bundled one (see system_cli_path).
    opts = dict(
        cwd=str(ROOT_DIR),
        system_prompt={"type": "preset", "preset": "claude_code"},
        allowed_tools=tools,
        permission_mode="acceptEdits",
        max_turns=20,
    )
    cli = system_cli_path()
    if cli:
        opts["cli_path"] = cli
    # Pin the query model (capture-config `query_model`) — mirrors compile_model:
    # without a pin the SDK inherits the interactive CLI's default model, billing
    # every ask at whatever tier the user last chose interactively.
    from capture_config import get_limits
    query_model = str(get_limits().get("query_model", "")).strip()
    if query_model and query_model != "inherit":
        opts["model"] = query_model

    answer = ""
    cost = 0.0
    async for message in query(prompt=prompt, options=ClaudeAgentOptions(**opts)):
        if isinstance(message, AssistantMessage):
            text = "".join(b.text for b in message.content if isinstance(b, TextBlock)).strip()
            if text:
                answer = text  # keep only the latest text turn = the final synthesis
        elif isinstance(message, ResultMessage):
            cost = message.total_cost_usd or 0.0
    return answer, cost


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Ask the knowledge base a question (index-guided, no RAG)."
    )
    parser.add_argument("question", type=str, help="The question to ask")
    parser.add_argument("--project", type=str, default=None,
                        help="Restrict retrieval to one project slug (the index Project column)")
    parser.add_argument("--file-back", action="store_true",
                        help="File the answer as a knowledge/qa/ article + index row + log entry")
    parser.add_argument("--dev", action="store_true",
                        help="Append live code pointers (symbol → current file:line, resolved "
                             "lazily via grep, never stored). Experimental framing; normal "
                             "queries stay conceptual.")
    args = parser.parse_args()

    question = args.question.strip()
    if not question:
        print("Error: empty question.", file=sys.stderr)
        sys.exit(1)

    index_text = read_wiki_index()
    if args.project:
        index_text = filter_index_by_project(index_text, args.project)

    timestamp = now_iso()
    file_back_slug = unique_qa_slug(question) if args.file_back else None
    prompt = build_prompt(question, index_text, args.project, file_back_slug, timestamp,
                          dev=args.dev)

    from activity import emit

    scope = f" [{args.project}]" if args.project else ""
    emit("query", "start", f"querying{scope}: {question[:80]}")
    logging.info("query start%s file_back=%s: %r", scope, args.file_back, question)

    # Per-call timeout + one retry mirrors compile.py — the bundled CLI hangs
    # intermittently and a stalled call almost always clears on a second attempt.
    # File-back gets a SINGLE attempt: a retry after a partial write could create
    # a duplicate Q&A article or a double index/log row.
    timeout = get_limits()["compile_part_timeout"]
    attempts = (1,) if args.file_back else (1, 2)
    answer, cost = "", 0.0
    for attempt in attempts:
        try:
            answer, cost = asyncio.run(asyncio.wait_for(run_query(prompt, args.file_back), timeout))
            break
        except asyncio.TimeoutError:
            if attempt != attempts[-1]:
                print(f"TIMEOUT after {timeout}s — retrying once", file=sys.stderr, flush=True)
                logging.warning("query timeout after %ss (attempt %d) — retrying", timeout, attempt)
                continue
            print(f"TIMEOUT after {timeout}s — giving up", file=sys.stderr, flush=True)
            logging.error("query timed out after %ss, gave up: %r", timeout, question)
            emit("query", "error", f"query timed out: {question[:80]}", "warn")
            sys.exit(1)
        except Exception as e:  # noqa: BLE001 — surface any SDK/tool failure to the user
            import traceback
            if attempt != attempts[-1]:
                print(f"Error ({e}) — retrying once", file=sys.stderr, flush=True)
                logging.warning("query error (attempt %d), retrying: %s", attempt, e)
                continue
            print(f"Error: {e}", file=sys.stderr, flush=True)
            logging.error("query failed: %r\n%s", question, traceback.format_exc())
            emit("query", "error", f"query failed: {question[:80]}", "error")
            sys.exit(1)

    if not answer:
        print("No answer was produced.", file=sys.stderr)
        logging.warning("query produced empty answer: %r", question)
        emit("query", "error", f"empty answer: {question[:80]}", "warn")
        sys.exit(1)

    # stdout carries the ANSWER and nothing else. Every failure path above writes to
    # stderr and exits non-zero, so a consumer can tell an answer from a failure
    # without parsing prose. It could not before: query.py printed its errors to
    # stdout, and the steward's oracle returned "Error: empty question." to another
    # agent as if it were knowledge (condechi-llc/condechi-steward#7).
    print(answer)

    # State: bump query_count + cumulative cost (same ledger compile.py writes to).
    state = load_state()
    state["query_count"] = state.get("query_count", 0) + 1
    state["total_cost"] = state.get("total_cost", 0.0) + cost
    save_state(state)

    logging.info("query answered%s cost=$%.4f file_back=%s: %r",
                 scope, cost, bool(file_back_slug), question)
    if file_back_slug:
        emit("query", "filed", f"filed answer → qa/{file_back_slug} (${cost:.3f})")
        print(f"\n— filed to knowledge/qa/{file_back_slug}.md  (cost ${cost:.4f})")
    else:
        emit("query", "answer", f"answered{scope}: {question[:80]} (${cost:.3f})")


if __name__ == "__main__":
    main()
