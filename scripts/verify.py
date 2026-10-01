"""Tier-1 symbol-verification gate (PR D) — the hallucination check.

Codifies the hand-proven 2026-06-02 Tier-1 sweep (0 hallucinations / 386 tokens):
extract each article's distinctive cited identifiers → check them against a
full-repo identifier index (`.js/.ts/.py/.json` **plus `.sh`/`.mjs`/spec-`.md`/
configs` — the omission of those was the source of every false "absent" that
session) → deterministically triage each ABSENT token, no LLM:

  (a) fuzzy re-check (case/underscore-insensitive) → RENAME vs genuine miss;
  (b) grep the article's SOURCE DAILY → CONVO-SOURCED (discussed, just not in code:
      external API field, planned name, …) vs COMPILE-FABRICATED (nowhere — the
      strongest hallucination signal);
  (c) read the citing sentence → CORRECT-NEGATIVE (cited as a rejected/old value,
      e.g. `tax_recall_only`, `stripeCnCapacity`) vs a real claim.

Only genuinely-ambiguous true-misses survive that funnel. `verify.py` orchestrates
Tier-1 + Tier-2 (reuses `staleness.stale_risk()`) and AUTO-EMITS the hand-off prompt
for the flagged set — the loop run by hand this session — so the active/joint
session (the only layer holding the in-flight, uncommitted truth) can resolve it.

Tier-3 (adversarial Sonnet, refute-by-default, quote file:line) is OPT-IN via
`--tier3`: it costs money and "never auto-trust" applies. Without it, true-misses
go straight into the hand-off prompt instead.

Usage:
    uv run python scripts/verify.py                 # Tier-1 + Tier-2, emit hand-off
    uv run python scripts/verify.py --article crm-stripe-preview-finalize-pipeline
    uv run python scripts/verify.py --tier3         # also run the Sonnet refute pass
    uv run python scripts/verify.py --json          # machine-readable
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Recursion guard: --tier3 spawns an internal Agent SDK session that runs in this
# captured project; without this the capture hooks would fire on verify.py's OWN
# session and try to flush its transcript (recursive self-capture). Set BEFORE any
# Agent SDK import. setdefault preserves an outer value (e.g. a parent compile).
os.environ.setdefault("CLAUDE_INVOKED_BY", "verify")

# scripts/ on path so this runs both as `scripts/verify.py` and from inside scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DAILY_DIR, KNOWLEDGE_DIR, now_iso, system_cli_path  # noqa: E402
from staleness import stale_risk  # noqa: E402 — Tier-2 is reused wholesale

ROOT = Path(__file__).resolve().parent.parent

# Repos whose code the KB documents (scripts/code-roots), PLUS this pipeline's own
# code. Deliberately excludes knowledge/ and daily/ — indexing the article text itself
# would make every token trivially "present" (circular).
from config import code_repos  # noqa: E402

INDEX_ROOTS = [*code_repos().values(), ROOT / "scripts", ROOT / "hooks"]

# .sh/.mjs/spec-.md/configs included on purpose — every false "absent" in the manual
# sweep came from omitting them (`KEEP_INVOICES` lived in a `.mjs` smoke script).
INDEX_EXTS = {
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py", ".json",
    ".sh", ".bash", ".zsh", ".md", ".toml", ".yaml", ".yml", ".cfg", ".ini", ".env",
}
PRUNE_DIRS = {"node_modules", ".git", "dist", "build", ".next", "coverage",
              "__pycache__", ".venv", "venv", ".obsidian"}
MAX_INDEX_FILE = 2_000_000  # skip pathological blobs (lockfiles, data dumps)

# Long all-lowercase English words that clear the ≥14 length gate but aren't symbols.
GENERIC_WORDS = {
    "reconciliation", "implementation", "infrastructure", "responsibilities",
    "characteristics", "representation", "transformation", "configuration",
    "documentation", "specification", "authentication", "authorization",
    "synchronization", "initialization", "serialization", "normalization",
    "classification", "identification", "compatibility", "accountability",
}

# Markers that, in the citing sentence, signal the token is named as a REJECTED or
# SUPERSEDED value — so its absence from code is correct, not a hallucination. Kept
# deliberately tight (no bare "not") to avoid masking real misses.
REJECT_MARKERS = (
    "reject", "instead of", "rather than", "no longer", "deprecat", "supersed",
    "abandon", "discarded", "obsolet", "renamed from", "previously called",
    "considered", "never shipped", "not used", "old value", "former",
    "chosen over", "incompatible", "did not", "didn't", "replac",  # design-decision rejections
    "~~",  # strikethrough
)

# ── token extraction ─────────────────────────────────────────────────────────

_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.S)
_INLINE = re.compile(r"`([^`\n]+)`")
_ATOMIC = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")  # no dots → dotted paths split to segments
_CAMEL = re.compile(r"[a-z][A-Z]")
# Stripe object IDs (in_/cn_/pi_/ch_/cus_/acct_…) are opaque DATA values, never code
# symbols — they pass the length gate but can't (and shouldn't) be found in source. They
# are provenance, not claims; exclude them from the symbol sweep like pure numbers.
_STRIPE_ID = re.compile(
    r"^(in|cn|pi|ch|re|il|sub|cus|acct|price|prod|txn|po|tr|seti|pm|py|rcpt)_[A-Za-z0-9]{12,}$"
)


def split_frontmatter(text: str) -> tuple[str, str]:
    """(frontmatter, body). Body excludes the YAML so SHAs/aliases don't leak in."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[3:end], text[end + 4:]
    return "", text


def code_spans(body: str) -> list[str]:
    """Every backtick-delimited span (fenced blocks + inline code) in article order."""
    spans = _FENCE.findall(body)
    no_fence = _FENCE.sub("\n", body)
    spans.extend(_INLINE.findall(no_fence))
    return spans


def is_distinctive(tok: str) -> bool:
    """A cited identifier worth verifying: snake_case / camelCase / dotted-segment
    ≥8 chars, OR any single token ≥14 (minus generic English). Filters out the
    formula-variable / English-prose noise that dominates Tier-1 false positives."""
    n = len(tok)
    if tok.isdigit() or _STRIPE_ID.match(tok):
        return False
    has_sep = "_" in tok
    has_camel = bool(_CAMEL.search(tok))
    if (has_sep or has_camel) and n >= 8:
        return True
    if n >= 14 and tok.lower() not in GENERIC_WORDS:
        return True
    return False


def _line_at(body: str, idx: int) -> str:
    """The whole line (≈ one claim/bullet) containing position idx."""
    start = body.rfind("\n", 0, idx) + 1
    end = body.find("\n", idx)
    return body[start: end if end != -1 else len(body)].strip()


def extract_tokens(body: str) -> dict[str, str]:
    """{distinctive_token: citing_line}. Tokens come only from backtick spans; the
    citing line is located in the full body for the rejected-value read in step (c)."""
    out: dict[str, str] = {}
    for span in code_spans(body):
        for m in _ATOMIC.finditer(span):
            tok = m.group(0)
            if not is_distinctive(tok) or tok in out:
                continue
            idx = body.find(tok)
            out[tok] = _line_at(body, idx) if idx != -1 else span.strip()
    return out


# ── repo index ─────────────────────────────────────────────────────────────

def build_repo_index(roots: list[Path]) -> tuple[set[str], dict[str, set[str]]]:
    """(tokens, norm) over all indexed repo text. `tokens` = every atomic identifier
    seen in code; `norm` maps an underscore/case-folded form → the originals (for the
    fuzzy RENAME re-check). Walks the filesystem (not `git ls-files`) so UNTRACKED /
    uncommitted files count — the newest truth lives in the dirty working tree."""
    tokens: set[str] = set()
    norm: dict[str, set[str]] = {}
    for root in roots:
        rp = root.resolve()
        if not rp.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(rp):
            dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
            for fn in filenames:
                if Path(fn).suffix.lower() not in INDEX_EXTS:
                    continue
                fp = Path(dirpath) / fn
                try:
                    if fp.stat().st_size > MAX_INDEX_FILE:
                        continue
                    text = fp.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for tok in _ATOMIC.findall(text):
                    tokens.add(tok)
                    norm.setdefault(tok.lower().replace("_", ""), set()).add(tok)
    return tokens, norm


# ── source-daily provenance ──────────────────────────────────────────────────

_DAILY_REF = re.compile(r"daily/(\d{4}-\d{2}-\d{2})\.md")


_ALL_DAILIES: str | None = None


def all_dailies_text() -> str:
    """Every daily, concatenated (cached). Fallback corpus for the fabricated-vs-
    convo-sourced discriminator when an article cites no dailies — MOC hubs carry no
    `sources:`, so without this every real token a compile adds to a MOC was
    unprovable and got branded COMPILE-FABRICATED (meta-KB smoke finding #7:
    `cleanupPeriodDays`, a real settings key, quarantined a clean ingest)."""
    global _ALL_DAILIES
    if _ALL_DAILIES is None:
        _ALL_DAILIES = "\n".join(
            p.read_text(encoding="utf-8", errors="ignore")
            for p in sorted(DAILY_DIR.glob("*.md")))
    return _ALL_DAILIES


def source_daily_text(article_text: str) -> str:
    """Concatenated text of every daily this article cites (frontmatter `sources:`
    + body `[[daily/…]]`), for the step-(b) convo-vs-fabricated discriminator.
    Articles citing NO dailies fall back to the full daily corpus."""
    blobs = []
    for date in sorted(set(_DAILY_REF.findall(article_text))):
        p = DAILY_DIR / f"{date}.md"
        if p.exists():
            blobs.append(p.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(blobs) or all_dailies_text()


# ── deterministic triage ──────────────────────────────────────────────────────

def triage_absent(tok: str, sentence: str, norm: dict[str, set[str]],
                  daily_text: str) -> tuple[str, str]:
    """Classify an ABSENT token. Order mirrors the hand-proven funnel:
    (a) fuzzy → RENAME, (c) citing sentence → CORRECT-NEGATIVE, (b) daily grep →
    CONVO-SOURCED vs COMPILE-FABRICATED. The last two are the true-miss bucket."""
    key = tok.lower().replace("_", "")
    variants = norm.get(key, set()) - {tok}
    if variants:
        return "RENAME", f"case/underscore variant in code: {', '.join(sorted(variants)[:3])}"

    if any(m in sentence.lower() for m in REJECT_MARKERS):
        return "CORRECT-NEGATIVE", "cited as a rejected/superseded value"

    if daily_text and tok.lower() in daily_text.lower():
        return "CONVO-SOURCED", "in source daily but not code (external/planned/renamed?)"

    return "COMPILE-FABRICATED", "absent from code AND source daily — possible hallucination"


# True-miss dispositions that warrant Tier-3 / the hand-off (FABRICATED first = higher concern).
TRUE_MISS = ("COMPILE-FABRICATED", "CONVO-SOURCED")


_UNVERIF = re.compile(r"^unverifiable:\s*(.+)$", re.M)


def unverifiable_tag(text_or_stem: str) -> str:
    """The article's Tier-4 `unverifiable:` tag ('' if none). Accepts full article text,
    or an article stem (resolved across the knowledge folders and read)."""
    text = text_or_stem
    if "\n" not in text_or_stem:  # a stem, not article text
        for sub in ("concepts", "connections", "qa", "mocs"):
            p = KNOWLEDGE_DIR / sub / f"{text_or_stem}.md"
            if p.exists():
                text = p.read_text(encoding="utf-8", errors="ignore")
                break
        else:
            return ""
    fm, _ = split_frontmatter(text)
    m = _UNVERIF.search(fm)
    return m.group(1).strip() if m else ""


def verify_article(path: Path, repo_tokens: set[str], norm: dict[str, set[str]]) -> dict:
    """Tier-1 result for one article: confirmed count + per-disposition absences."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    _, body = split_frontmatter(text)
    tokens = extract_tokens(body)
    daily_text = source_daily_text(text)

    confirmed: list[str] = []
    absent: dict[str, dict] = {}  # token -> {disposition, detail, sentence}
    for tok, sentence in tokens.items():
        if tok in repo_tokens:
            confirmed.append(tok)
            continue
        disposition, detail = triage_absent(tok, sentence, norm, daily_text)
        absent[tok] = {"disposition": disposition, "detail": detail, "sentence": sentence}

    true_misses = {t: a for t, a in absent.items() if a["disposition"] in TRUE_MISS}
    return {
        "slug": path.stem,
        "path": str(path.relative_to(ROOT)),
        "n_tokens": len(tokens),
        "n_confirmed": len(confirmed),
        "absent": absent,
        "true_misses": true_misses,
        "unverifiable": unverifiable_tag(text),
    }


# ── Tier-3 (opt-in, adversarial, never below Sonnet) ──────────────────────────

DEFAULT_TIER3_MODEL = "claude-sonnet-4-6"


def build_tier3_prompt(slug: str, tok: str, sentence: str, disposition: str) -> str:
    # The repo set comes from scripts/code-roots (generic across KBs) — never
    # hardcode repo names here; they differ per install.
    roots = ", ".join(f"`{name}/`" for name in code_repos()) or "the linked code roots"
    return f"""You are an adversarial code-grounding verifier. DEFAULT TO REFUTED.
Your job is to try to DISPROVE that a cited code symbol exists in this repository.

A knowledge-base article (`knowledge/concepts/{slug}.md` or similar) cites the
identifier `{tok}` as a real code symbol. A deterministic Tier-1 sweep could not find
it in the indexed source (disposition: {disposition}).

Citing claim from the article:
> {sentence}

Search the code yourself with Grep/Glob/Read across {roots}
(symlinks under the project root) AND any `.sh`/`.mjs`/spec-`.md` files. Try
genuinely to find it — exact, then case/underscore variants, then as a substring of a
longer dotted path. Then decide:

- CONFIRMED — you found concrete evidence (give the exact file:line).
- UNVERIFIED — you could not find it but it's plausibly external (a Stripe API field,
  a planned/renamed name, a value discussed but not yet in code).
- REFUTED — it appears to be fabricated: nowhere in code, and the article presents it
  as an existing symbol.

When uncertain, choose UNVERIFIED or REFUTED, never CONFIRMED.

Output EXACTLY one line, nothing else:
VERDICT: <CONFIRMED|UNVERIFIED|REFUTED> — <file:line, or one-line reason>"""


async def _run_tier3(prompt: str, model: str) -> str:
    from llm_silence import require_llm
    require_llm("verify tier-3")
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        TextBlock,
        query,
    )

    opts = dict(
        cwd=str(ROOT),
        system_prompt={"type": "preset", "preset": "claude_code"},
        allowed_tools=["Read", "Grep", "Glob"],
        permission_mode="default",
        model=model,
        max_turns=12,
    )
    cli = system_cli_path()
    if cli:
        opts["cli_path"] = cli

    verdict = ""
    async for message in query(prompt=prompt, options=ClaudeAgentOptions(**opts)):
        if isinstance(message, AssistantMessage):
            text = "".join(b.text for b in message.content if isinstance(b, TextBlock)).strip()
            for line in text.splitlines():
                if line.strip().upper().startswith("VERDICT:"):
                    verdict = line.strip()
    return verdict


def run_tier3(results: list[dict], model: str) -> dict[str, dict]:
    """Adversarially check each true-miss token. Returns {f'{slug}::{tok}': verdict}."""
    import asyncio

    verdicts: dict[str, dict] = {}
    for r in results:
        for tok, info in sorted(r["true_misses"].items(),
                                key=lambda kv: kv[1]["disposition"]):  # FABRICATED first
            prompt = build_tier3_prompt(r["slug"], tok, info["sentence"], info["disposition"])
            print(f"  [tier3:{model}] {r['slug']} :: {tok} …", file=sys.stderr, flush=True)
            try:
                line = asyncio.run(_run_tier3(prompt, model)) or "VERDICT: UNVERIFIED — no verdict returned"
            except Exception as e:  # noqa: BLE001 — surface SDK/tool failure, don't abort the sweep
                line = f"VERDICT: UNVERIFIED — tier-3 error: {e}"
            verdict = line.split("—", 1)[0].replace("VERDICT:", "").strip().upper() or "UNVERIFIED"
            verdicts[f"{r['slug']}::{tok}"] = {"verdict": verdict, "line": line}
            print(f"      → {line}", file=sys.stderr, flush=True)
    return verdicts


# ── hand-off prompt for the active/joint session ──────────────────────────────

def build_handoff_prompt(tier1_flags: list[dict],
                         tier2_flags: list[tuple[str, list[str]]],
                         verdicts: dict[str, dict] | None) -> str:
    """The ready-to-paste prompt that hands ONLY the flagged set to the active session
    — the layer that holds the uncommitted working tree + in-flight context, the only
    one that can supply corrected truth for the stale class."""
    lines = [
        "## KB verification hand-off — flagged set",
        "",
        "`scripts/verify.py` ran the deterministic Tier-1 (symbol presence) + Tier-2 "
        "(working-tree staleness) gates. The items below are the residue that the "
        "machine can't resolve on its own — they need the in-flight truth this session "
        "holds (the uncommitted working tree and design context). For each one: verify "
        "the code-level claim against the CURRENT working tree, then correct the article "
        "(state only the new truth) or confirm it. Conceptual/business claims are out of "
        "scope here.",
        "",
    ]

    if tier1_flags:
        lines.append("### Tier-1 — cited symbols not found in code (possible hallucination / rename)")
        for r in tier1_flags:
            tag = r.get("unverifiable", "")
            tag_note = (f" — tagged `unverifiable: {tag}`: its CORE claims need a cited "
                        f"authority/vendor doc, not code-grounding (Tier-4)" if tag else "")
            lines.append(f"- **{r['slug']}** (`{r['path']}`){tag_note}")
            for tok, info in sorted(r["true_misses"].items(),
                                    key=lambda kv: kv[1]["disposition"]):
                v = (verdicts or {}).get(f"{r['slug']}::{tok}")
                vtag = f" — Tier-3: {v['verdict']}" if v else ""
                lines.append(f"    - `{tok}` ({info['disposition']}: {info['detail']}){vtag}")
                lines.append(f"      ↳ cited: {info['sentence']}")
        lines.append("")

    if tier2_flags:
        lines.append("### Tier-2 — articles whose cited code changed since their knowledge vintage")
        for stem, reasons in sorted(tier2_flags):
            tag = unverifiable_tag(stem)
            tag_note = f" *(also `unverifiable: {tag}` — only its code citations re-check here)*" if tag else ""
            lines.append(f"- **{stem}** — {', '.join(reasons[:4])}{tag_note}")
        lines.append("")
        lines.append("For each Tier-2 article: correct it (state only the new truth + log a "
                     "`supersede` entry), or if its claims still hold, attest with "
                     "`uv run python scripts/recheck.py --verified <slug>` — that re-stamps its "
                     "vintage/baseline and auto-clears the `needs-reverification` status.")
        lines.append("")

    if not tier1_flags and not tier2_flags:
        lines.append("_Nothing flagged — Tier-1 found every cited symbol, Tier-2 found no "
                     "stale-vintage cites. No hand-off needed._")
    return "\n".join(lines)


# ── orchestration / CLI ───────────────────────────────────────────────────────

def _articles(article: str | None) -> list[Path]:
    paths: list[Path] = []
    for sub in ("concepts", "connections", "qa", "mocs"):
        d = KNOWLEDGE_DIR / sub
        if d.exists():
            paths.extend(sorted(d.glob("*.md")))
    if article:
        paths = [p for p in paths if p.stem == article]
    return paths


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="PR D Tier-1 symbol-verification gate (+ Tier-2 staleness).")
    ap.add_argument("--article", help="Verify a single article by slug (stem).")
    ap.add_argument("--tier3", action="store_true",
                    help="Run the adversarial Sonnet refute pass on true-misses (costs money).")
    ap.add_argument("--model", default=DEFAULT_TIER3_MODEL,
                    help=f"Tier-3 model (never below Sonnet). Default {DEFAULT_TIER3_MODEL}.")
    ap.add_argument("--no-tier2", action="store_true", help="Skip the staleness scan.")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = ap.parse_args()

    arts = _articles(args.article)
    if not arts:
        print(f"No articles found{f' for slug {args.article!r}' if args.article else ''}.")
        sys.exit(1)

    # Progress → stderr so `--json` keeps stdout pure.
    print(f"Indexing repo symbols ({', '.join(r.name for r in INDEX_ROOTS if r.exists())})…",
          file=sys.stderr, flush=True)
    repo_tokens, norm = build_repo_index(INDEX_ROOTS)
    print(f"  {len(repo_tokens):,} distinct identifiers indexed.\n", file=sys.stderr, flush=True)

    # ── Tier-1 ──
    results = [verify_article(p, repo_tokens, norm) for p in arts]
    total_tok = sum(r["n_tokens"] for r in results)
    total_ok = sum(r["n_confirmed"] for r in results)
    by_disp: dict[str, int] = {}
    for r in results:
        for info in r["absent"].values():
            by_disp[info["disposition"]] = by_disp.get(info["disposition"], 0) + 1
    tier1_flags = [r for r in results if r["true_misses"]]

    # ── Tier-2 (reused wholesale) ──
    tier2_flags = [] if args.no_tier2 else stale_risk()

    # ── Tier-3 (opt-in) ──
    verdicts: dict[str, dict] | None = None
    from llm_silence import is_silenced
    if args.tier3 and is_silenced():
        # Tier-1/2 are deterministic and still ran; only the LLM tier is skipped.
        print("Tier-3 skipped — LLM silence is ON (`devlore --llm-resume` to lift).",
              file=sys.stderr, flush=True)
    elif args.tier3 and tier1_flags:
        print(f"\nTier-3 adversarial pass on {sum(len(r['true_misses']) for r in tier1_flags)} "
              f"true-miss token(s) via {args.model}…", file=sys.stderr, flush=True)
        verdicts = run_tier3(tier1_flags, args.model)
    elif args.tier3:
        print("Tier-3 requested but no true-misses to check.", file=sys.stderr, flush=True)

    handoff = build_handoff_prompt(tier1_flags, tier2_flags, verdicts)

    if args.json:
        print(json.dumps({
            "generated_at": now_iso(),
            "indexed_identifiers": len(repo_tokens),
            "totals": {"tokens": total_tok, "confirmed": total_ok, "absent_by_disposition": by_disp},
            "articles": results,
            "tier2_stale_risk": [{"slug": s, "reasons": rs} for s, rs in tier2_flags],
            "tier3_verdicts": verdicts or {},
            "handoff_prompt": handoff,
        }, indent=2))
        return

    # ── human report ──
    print("=" * 72)
    print(f"Tier-1 symbol sweep — {len(arts)} article(s), {total_tok} distinctive tokens")
    pct = (100 * total_ok / total_tok) if total_tok else 100.0
    print(f"  CONFIRMED in code: {total_ok}/{total_tok} ({pct:.0f}%)")
    for disp in ("RENAME", "CORRECT-NEGATIVE", "CONVO-SOURCED", "COMPILE-FABRICATED"):
        if by_disp.get(disp):
            tag = " ← true-miss (flagged)" if disp in TRUE_MISS else " (benign)"
            print(f"  {disp:<18} {by_disp[disp]}{tag}")
    print("=" * 72)

    if tier1_flags:
        print(f"\n{sum(len(r['true_misses']) for r in tier1_flags)} true-miss token(s) "
              f"across {len(tier1_flags)} article(s):")
        for r in tier1_flags:
            for tok, info in sorted(r["true_misses"].items(),
                                    key=lambda kv: kv[1]["disposition"]):
                v = (verdicts or {}).get(f"{r['slug']}::{tok}")
                vtag = f"  [Tier-3: {v['verdict']}]" if v else ""
                print(f"  - {r['slug']} :: `{tok}` ({info['disposition']}){vtag}")
    else:
        print("\nZero true-misses — every cited symbol is present in code, a known "
              "rename, or a correctly-cited rejected value. Tier-1 clean.")

    if not args.no_tier2:
        print(f"\nTier-2 staleness: {len(tier2_flags)} article(s) cite code changed since their vintage.")

    print("\n" + "─" * 72)
    print("HAND-OFF PROMPT (paste into the active/joint session):\n")
    print(handoff)


if __name__ == "__main__":
    main()
