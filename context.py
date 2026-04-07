"""
context.py — Adaptive, confidence-aware context builder.

KEY IMPROVEMENTS over code-review-graph
-----------------------------------------
1. Adaptive sizing  — trivial single-file changes skip graph expansion
   entirely, so the AI gets a lean context instead of a bloated one
   (fixes their <1x efficiency regression on small changes).

2. Confidence tiers — context groups files into HIGH / MEDIUM / LOW
   confidence buckets so the AI knows how much to trust each inclusion.

3. Three formats    — Markdown, JSON, XML, all carrying confidence scores.
"""

from __future__ import annotations

import json
from pathlib import Path

from .graph import KnowledgeGraph

_CHARS_PER_TOKEN = 4


def _tok(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _truncate(text: str, max_tokens: int) -> str:
    limit = max_tokens * _CHARS_PER_TOKEN
    return text[:limit] + "\n… [truncated]" if len(text) > limit else text


class ContextBuilder:
    def __init__(self, root: Path, kg: KnowledgeGraph, max_tokens: int = 8_000):
        self.root = root
        self.kg = kg
        self.max_tokens = max_tokens

    def build(
        self,
        changed_files: list[str],
        format: str = "markdown",
        include_snippets: bool = True,
        depth: int = 3,
        confidence_threshold: float = 0.3,
    ) -> str:
        blast = self.kg.blast_radius(changed_files, max_depth=depth)

        # Tier files by confidence
        high   = [(f, s) for f, s in blast["ranked_files"] if s >= 0.7]
        medium = [(f, s) for f, s in blast["ranked_files"] if 0.3 <= s < 0.7]
        low    = [(f, s) for f, s in blast["ranked_files"] if s < 0.3]

        # Source snippets — only for changed files + high-confidence neighbours
        snippets: dict[str, str] = {}
        if include_snippets and not blast.get("is_trivial"):
            budget = self.max_tokens
            priority = list(changed_files) + [f for f, _ in high if f not in changed_files]
            per_file = max(500, budget // max(len(priority), 1))
            for f in priority:
                if budget <= 0:
                    break
                snippet = self._read_snippet(f, max_tokens=min(per_file, budget))
                snippets[f] = snippet
                budget -= _tok(snippet)
        elif include_snippets and blast.get("is_trivial"):
            # For trivial changes, just include the changed file
            for f in changed_files:
                snippets[f] = self._read_snippet(f, max_tokens=2000)

        ctx = {
            "summary": self._summary(blast, changed_files),
            "changed_files": changed_files,
            "blast": blast,
            "high_confidence": high,
            "medium_confidence": medium,
            "low_confidence": low,
            "direct_symbols": blast.get("direct_symbols", []),
            "snippets": snippets,
            "is_trivial": blast.get("is_trivial", False),
        }

        if format == "json":
            return self._render_json(ctx)
        elif format == "xml":
            return self._render_xml(ctx)
        else:
            return self._render_markdown(ctx)

    # ── Renderers ─────────────────────────────────────────────────────────────

    def _render_markdown(self, ctx: dict) -> str:
        lines: list[str] = []
        lines.append("## 🔍 CodeContextGraph Bundle\n")
        lines.append(ctx["summary"] + "\n")

        if ctx["is_trivial"]:
            lines.append("> ⚡ **Trivial change detected** — graph expansion skipped to save tokens.\n")

        lines.append("### Changed Files")
        for f in ctx["changed_files"]:
            lines.append(f"- `{f}`")
        lines.append("")

        blast = ctx["blast"]
        impact_emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(
            blast["total_impact"], "⚪"
        )
        lines.append(f"### Blast Radius — {impact_emoji} Impact: **{blast['total_impact'].upper()}**\n")
        lines.append(f"> {blast.get('precision_hint', '')}\n")

        if ctx["high_confidence"]:
            lines.append("**🔴 High confidence (≥0.7) — very likely affected:**")
            for f, s in ctx["high_confidence"][:15]:
                lines.append(f"  - `{f}` — score: {s:.2f}")
            lines.append("")

        if ctx["medium_confidence"]:
            lines.append("**🟡 Medium confidence (0.3–0.7) — possibly affected:**")
            for f, s in ctx["medium_confidence"][:10]:
                lines.append(f"  - `{f}` — score: {s:.2f}")
            lines.append("")

        if ctx["low_confidence"]:
            lines.append(
                f"**⚪ Low confidence (<0.3):** {len(ctx['low_confidence'])} distant file(s) omitted.\n"
            )

        # Direct symbols
        direct = ctx["direct_symbols"]
        if direct:
            lines.append("### Directly Changed Symbols\n")
            for sym in direct:
                conf = f"score: {sym['score']:.2f}" if "score" in sym else ""
                lines.append(
                    f"- **{sym['kind']}** `{sym['name']}` — `{sym['file']}:{sym['line']}` {conf}"
                )
            lines.append("")

        # Source snippets
        if ctx["snippets"]:
            lines.append("### Source\n")
            for f, snippet in ctx["snippets"].items():
                tag = " _(changed)_" if f in ctx["changed_files"] else " _(high-confidence neighbour)_"
                lines.append(f"**`{f}`**{tag}\n```\n{snippet}\n```\n")

        return "\n".join(lines)

    def _render_json(self, ctx: dict) -> str:
        return json.dumps({
            "summary":          ctx["summary"],
            "changed_files":    ctx["changed_files"],
            "impact":           ctx["blast"]["total_impact"],
            "is_trivial":       ctx["is_trivial"],
            "precision_hint":   ctx["blast"].get("precision_hint", ""),
            "high_confidence":  [{"file": f, "score": s} for f, s in ctx["high_confidence"]],
            "medium_confidence":[{"file": f, "score": s} for f, s in ctx["medium_confidence"]],
            "low_confidence_count": len(ctx["low_confidence"]),
            "direct_symbols":   ctx["direct_symbols"],
            "snippets":         ctx["snippets"],
        }, indent=2, default=str)

    def _render_xml(self, ctx: dict) -> str:
        lines: list[str] = ["<ccg_context>"]
        lines.append(f"  <summary>{ctx['summary']}</summary>")
        lines.append(f"  <impact level=\"{ctx['blast']['total_impact']}\" trivial=\"{ctx['is_trivial']}\">")
        lines.append(f"    <precision_hint>{ctx['blast'].get('precision_hint','')}</precision_hint>")
        lines.append("    <high_confidence>")
        for f, s in ctx["high_confidence"][:20]:
            lines.append(f'      <file score="{s:.2f}">{f}</file>')
        lines.append("    </high_confidence>")
        lines.append("    <medium_confidence>")
        for f, s in ctx["medium_confidence"][:10]:
            lines.append(f'      <file score="{s:.2f}">{f}</file>')
        lines.append("    </medium_confidence>")
        lines.append("  </impact>")
        lines.append("  <direct_symbols>")
        for sym in ctx["direct_symbols"]:
            lines.append(
                f'    <symbol kind="{sym["kind"]}" file="{sym["file"]}" '
                f'line="{sym["line"]}" score="{sym.get("score",1.0):.2f}">'
                f'{sym["name"]}</symbol>'
            )
        lines.append("  </direct_symbols>")
        if ctx["snippets"]:
            lines.append("  <snippets>")
            for f, snippet in ctx["snippets"].items():
                lines.append(f'    <snippet file="{f}"><![CDATA[')
                lines.append(snippet)
                lines.append("    ]]></snippet>")
            lines.append("  </snippets>")
        lines.append("</ccg_context>")
        return "\n".join(lines)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _summary(self, blast: dict, changed_files: list[str]) -> str:
        high = len([f for f, s in blast["ranked_files"] if s >= 0.7])
        med  = len([f for f, s in blast["ranked_files"] if 0.3 <= s < 0.7])
        trivial = " (trivial change — graph expansion skipped)" if blast.get("is_trivial") else ""
        return (
            f"Changes in {len(changed_files)} file(s){trivial}. "
            f"Blast radius: {high} high-confidence + {med} medium-confidence files affected. "
            f"Overall impact: {blast['total_impact']}."
        )

    def _read_snippet(self, rel_path: str, max_tokens: int = 1_500) -> str:
        path = self.root / rel_path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return f"[file not found: {rel_path}]"
        return _truncate(text, max_tokens)
