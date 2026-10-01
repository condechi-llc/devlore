"""
Lint the knowledge base for structural and semantic health.

Runs 9 checks: broken links, orphan pages, orphan sources, stale articles,
missing backlinks, sparse articles, frontmatter schema (PR C type/status/subsystem/
summary), tags hygiene (project slug first + vocabulary sprawl), and
contradictions (LLM).

Usage:
    uv run python lint.py                    # all checks
    uv run python lint.py --structural-only  # skip LLM checks (faster, cheaper)
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from config import KNOWLEDGE_DIR, REPORTS_DIR, now_iso, today_iso
from utils import (
    count_inbound_links,
    extract_wikilinks,
    file_hash,
    get_article_word_count,
    list_raw_files,
    list_wiki_articles,
    load_state,
    read_all_wiki_content,
    save_state,
    wiki_article_exists,
)

ROOT_DIR = Path(__file__).resolve().parent.parent


def check_broken_links() -> list[dict]:
    """Check for [[wikilinks]] that point to non-existent articles."""
    issues = []
    for article in list_wiki_articles():
        content = article.read_text(encoding="utf-8")
        rel = article.relative_to(KNOWLEDGE_DIR)
        for link in extract_wikilinks(content):
            if link.startswith("daily/"):
                continue  # daily log references are valid
            if not wiki_article_exists(link):
                issues.append({
                    "severity": "error",
                    "check": "broken_link",
                    "file": str(rel),
                    "detail": f"Broken link: [[{link}]] - target does not exist",
                })
    return issues


def check_orphan_pages() -> list[dict]:
    """Check for articles with zero inbound links. MOC hubs are exempt — they are
    entry points reached from outside the article graph (index, Obsidian, humans)."""
    issues = []
    for article in list_wiki_articles():
        if article.parent.name == "mocs":
            continue
        rel = article.relative_to(KNOWLEDGE_DIR)
        link_target = str(rel).replace(".md", "").replace("\\", "/")
        inbound = count_inbound_links(link_target)
        if inbound == 0:
            issues.append({
                "severity": "warning",
                "check": "orphan_page",
                "file": str(rel),
                "detail": f"Orphan page: no other articles link to [[{link_target}]]",
            })
    return issues


def check_orphan_sources() -> list[dict]:
    """Check for daily logs that haven't been compiled yet."""
    state = load_state()
    ingested = state.get("ingested", {})
    issues = []
    for log_path in list_raw_files():
        if log_path.name not in ingested:
            issues.append({
                "severity": "warning",
                "check": "orphan_source",
                "file": f"daily/{log_path.name}",
                "detail": f"Uncompiled daily log: {log_path.name} has not been ingested",
            })
    return issues


def check_stale_articles() -> list[dict]:
    """Check if source daily logs have changed since compilation."""
    state = load_state()
    ingested = state.get("ingested", {})
    issues = []
    for log_path in list_raw_files():
        rel = log_path.name
        if rel in ingested:
            stored_hash = ingested[rel].get("hash", "")
            current_hash = file_hash(log_path)
            if stored_hash != current_hash:
                issues.append({
                    "severity": "warning",
                    "check": "stale_article",
                    "file": f"daily/{rel}",
                    "detail": f"Stale: {rel} has changed since last compilation",
                })
    return issues


def check_missing_backlinks() -> list[dict]:
    """Check for asymmetric links: A links to B but B doesn't link to A. Links FROM a
    MOC hub are exempt — hubs link out to many spokes by design, without reciprocity."""
    issues = []
    for article in list_wiki_articles():
        if article.parent.name == "mocs":
            continue
        content = article.read_text(encoding="utf-8")
        rel = article.relative_to(KNOWLEDGE_DIR)
        source_link = str(rel).replace(".md", "").replace("\\", "/")

        for link in extract_wikilinks(content):
            if link.startswith("daily/"):
                continue
            target_path = KNOWLEDGE_DIR / f"{link}.md"
            if target_path.exists():
                target_content = target_path.read_text(encoding="utf-8")
                # Compare link TARGETS, so an aliased backlink ([[a|text]]) counts.
                if source_link not in extract_wikilinks(target_content):
                    issues.append({
                        "severity": "suggestion",
                        "check": "missing_backlink",
                        "file": str(rel),
                        "detail": f"[[{source_link}]] links to [[{link}]] but not vice versa",
                        "auto_fixable": True,
                    })
    return issues


def check_sparse_articles() -> list[dict]:
    """Check for articles with fewer than 200 words."""
    issues = []
    for article in list_wiki_articles():
        word_count = get_article_word_count(article)
        if word_count < 200:
            rel = article.relative_to(KNOWLEDGE_DIR)
            issues.append({
                "severity": "suggestion",
                "check": "sparse_article",
                "file": str(rel),
                "detail": f"Sparse article: {word_count} words (minimum recommended: 200)",
            })
    return issues


VALID_TYPES = {"concept", "connection", "moc", "reference", "qa"}
VALID_STATUS = {"active", "shipped", "superseded", "needs-reverification"}
VALID_UNVERIFIABLE = {"business_rule", "external_api"}  # PR D Tier-4 classes
import re as _re


def _frontmatter_scalar(text: str, key: str) -> str | None:
    """Read a top-level scalar `key:` from an article's YAML frontmatter."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    fm = text[3:end] if end != -1 else text
    m = _re.search(rf"^{key}:\s*(.+)$", fm, _re.M)
    return m.group(1).strip() if m else None


def check_frontmatter_schema() -> list[dict]:
    """PR C: every article must carry type/status/subsystem/summary with valid values.
    Also surfaces superseded / needs-reverification articles as informational suggestions."""
    issues = []
    for article in list_wiki_articles():
        rel = str(article.relative_to(KNOWLEDGE_DIR))
        text = article.read_text(encoding="utf-8")
        for field in ("type", "status", "subsystem", "summary"):
            if not _frontmatter_scalar(text, field):
                issues.append({
                    "severity": "warning", "check": "frontmatter", "file": rel,
                    "detail": f"Missing required frontmatter field `{field}` (PR C schema)",
                })
        t = _frontmatter_scalar(text, "type")
        if t and t not in VALID_TYPES:
            issues.append({"severity": "warning", "check": "frontmatter", "file": rel,
                           "detail": f"Invalid `type: {t}` (expected one of {sorted(VALID_TYPES)})"})
        s = _frontmatter_scalar(text, "status")
        if s and s not in VALID_STATUS:
            issues.append({"severity": "warning", "check": "frontmatter", "file": rel,
                           "detail": f"Invalid `status: {s}` (expected one of {sorted(VALID_STATUS)})"})
        elif s in ("superseded", "needs-reverification"):
            issues.append({"severity": "suggestion", "check": "frontmatter", "file": rel,
                           "detail": f"Article is `status: {s}` — review before relying on it"})
        # YAML-validity heuristic: an unquoted scalar containing ': ' parses fine in
        # our regex readers but is INVALID YAML (breaks Obsidian). compile.py's guard
        # auto-fixes these; lint surfaces any that slip through other write paths.
        from utils import FM_UNSAFE_SCALAR
        if text.startswith("---") and (fm_end := text.find("\n---", 3)) != -1:
            for ln in text[3:fm_end].split("\n"):
                if FM_UNSAFE_SCALAR.match(ln):
                    issues.append({"severity": "warning", "check": "frontmatter", "file": rel,
                                   "detail": f"Invalid YAML (unquoted scalar with ': '): {ln[:80]}",
                                   "auto_fixable": True})
        u = _frontmatter_scalar(text, "unverifiable")
        if u:
            bad = [p.strip() for p in u.split(",") if p.strip() not in VALID_UNVERIFIABLE]
            if bad:
                issues.append({"severity": "warning", "check": "frontmatter", "file": rel,
                               "detail": f"Invalid `unverifiable: {u}` (allowed: "
                                         f"{sorted(VALID_UNVERIFIABLE)}, comma-combinable)"})
    return issues


def check_tags() -> list[dict]:
    """Tags hygiene: every article carries `tags:` led by its `project:` slug, every
    tag lowercase-kebab (the compile tag guard auto-fixes all three), plus one
    aggregate suggestion when singleton tags accumulate (vocabulary sprawl defeats
    Obsidian filtering — tags only pay off when they group articles)."""
    from utils import collect_tag_vocabulary, normalize_tag, read_article_tags

    issues = []
    for article in list_wiki_articles():
        rel = str(article.relative_to(KNOWLEDGE_DIR))
        text = article.read_text(encoding="utf-8")
        project = normalize_tag(_frontmatter_scalar(text, "project") or "")
        tags = read_article_tags(text)
        if not tags:
            issues.append({
                "severity": "warning", "check": "tags", "file": rel,
                "detail": "Missing `tags:` — every article must carry at least its "
                          "project slug tag",
                "auto_fixable": True,
            })
            continue
        norm = [normalize_tag(t) for t in tags]
        if project and norm[0] != project:
            issues.append({
                "severity": "warning", "check": "tags", "file": rel,
                "detail": f"First tag must be the project slug `{project}` "
                          f"(got `{tags[0]}`)",
                "auto_fixable": True,
            })
        bad = [raw for raw, n in zip(tags, norm) if raw.strip().strip("\"'") != n]
        if bad:
            issues.append({
                "severity": "warning", "check": "tags", "file": rel,
                "detail": "Malformed tag(s) (must be lowercase-kebab): "
                          + ", ".join(f"`{t}`" for t in bad),
                "auto_fixable": True,
            })
    singles = sorted(t for t, n in collect_tag_vocabulary(list_wiki_articles()).items()
                     if n == 1)
    if len(singles) >= 3:
        shown = ", ".join(singles[:15]) + ("…" if len(singles) > 15 else "")
        issues.append({
            "severity": "suggestion", "check": "tags", "file": "(cross-article)",
            "detail": f"Tag vocabulary sprawl: {len(singles)} tag(s) each used by "
                      f"only one article — consider consolidating: {shown}",
        })
    return issues


async def check_contradictions() -> list[dict]:
    """Use LLM to detect contradictions across articles."""
    from llm_silence import require_llm
    require_llm("lint contradictions")
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    wiki_content = read_all_wiki_content()

    prompt = f"""Review this knowledge base for contradictions, inconsistencies, or
conflicting claims across articles.

## Knowledge Base

{wiki_content}

## Instructions

Look for:
- Direct contradictions (article A says X, article B says not-X)
- Inconsistent recommendations (different articles recommend conflicting approaches)
- Outdated information that conflicts with newer entries

For each issue found, output EXACTLY one line in this format:
CONTRADICTION: [file1] vs [file2] - description of the conflict
INCONSISTENCY: [file] - description of the inconsistency

If no issues found, output exactly: NO_ISSUES

Do NOT output anything else - no preamble, no explanation, just the formatted lines."""

    response = ""
    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT_DIR),
                allowed_tools=[],
                max_turns=2,
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
    except Exception as e:
        return [{"severity": "error", "check": "contradiction", "file": "(system)", "detail": f"LLM check failed: {e}"}]

    issues = []
    if "NO_ISSUES" not in response:
        for line in response.strip().split("\n"):
            line = line.strip()
            if line.startswith("CONTRADICTION:") or line.startswith("INCONSISTENCY:"):
                issues.append({
                    "severity": "warning",
                    "check": "contradiction",
                    "file": "(cross-article)",
                    "detail": line,
                })

    return issues


def generate_report(all_issues: list[dict]) -> str:
    """Generate a markdown lint report."""
    errors = [i for i in all_issues if i["severity"] == "error"]
    warnings = [i for i in all_issues if i["severity"] == "warning"]
    suggestions = [i for i in all_issues if i["severity"] == "suggestion"]

    lines = [
        f"# Lint Report - {today_iso()}",
        "",
        f"**Total issues:** {len(all_issues)}",
        f"- Errors: {len(errors)}",
        f"- Warnings: {len(warnings)}",
        f"- Suggestions: {len(suggestions)}",
        "",
    ]

    for severity, issues, marker in [
        ("Errors", errors, "x"),
        ("Warnings", warnings, "!"),
        ("Suggestions", suggestions, "?"),
    ]:
        if issues:
            lines.append(f"## {severity}")
            lines.append("")
            for issue in issues:
                fixable = " (auto-fixable)" if issue.get("auto_fixable") else ""
                lines.append(f"- **[{marker}]** `{issue['file']}` - {issue['detail']}{fixable}")
            lines.append("")

    if not all_issues:
        lines.append("All checks passed. Knowledge base is healthy.")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Lint the knowledge base")
    parser.add_argument(
        "--structural-only",
        action="store_true",
        help="Skip LLM-based checks (contradictions) - faster and free",
    )
    args = parser.parse_args()

    print("Running knowledge base lint checks...")
    all_issues: list[dict] = []

    # Structural checks (free, instant)
    checks = [
        ("Broken links", check_broken_links),
        ("Orphan pages", check_orphan_pages),
        ("Orphan sources", check_orphan_sources),
        ("Stale articles", check_stale_articles),
        ("Missing backlinks", check_missing_backlinks),
        ("Sparse articles", check_sparse_articles),
        ("Frontmatter schema", check_frontmatter_schema),
        ("Tags hygiene", check_tags),
    ]

    for name, check_fn in checks:
        print(f"  Checking: {name}...")
        issues = check_fn()
        all_issues.extend(issues)
        print(f"    Found {len(issues)} issue(s)")

    # LLM check (costs money)
    from llm_silence import is_silenced
    if not args.structural_only and is_silenced():
        print("  Skipping: Contradictions (LLM silence is ON)")
    elif not args.structural_only:
        print("  Checking: Contradictions (LLM)...")
        issues = asyncio.run(check_contradictions())
        all_issues.extend(issues)
        print(f"    Found {len(issues)} issue(s)")
    else:
        print("  Skipping: Contradictions (--structural-only)")

    # Generate and save report
    report = generate_report(all_issues)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"lint-{today_iso()}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\nReport saved to: {report_path}")

    # Update state
    state = load_state()
    state["last_lint"] = now_iso()
    save_state(state)

    # Summary
    errors = sum(1 for i in all_issues if i["severity"] == "error")
    warnings = sum(1 for i in all_issues if i["severity"] == "warning")
    suggestions = sum(1 for i in all_issues if i["severity"] == "suggestion")
    print(f"\nResults: {errors} errors, {warnings} warnings, {suggestions} suggestions")

    if errors > 0:
        print("\nErrors found - knowledge base needs attention!")
        return 1
    return 0


if __name__ == "__main__":
    exit(main())
