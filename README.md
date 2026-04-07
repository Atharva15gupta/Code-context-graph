# CodeContextGraph (ccg)

<p align="center"><strong>Precision-first context engine for AI coding assistants.</strong><br>
Tree-sitter parsing · Confidence-scored blast radius · Native MCP server · Adaptive token budgeting</p>

---

## Overview

Existing code analysis tools often return bloated, imprecise context that wastes tokens and confuses AI assistants. CodeContextGraph is built from the ground up to solve these core problems:

- **Precision scoring**: Every affected file gets a confidence score (0–1) based on relationship strength and graph distance, so the AI knows exactly what to trust.
- **Adaptive efficiency**: Trivial single-file changes skip graph expansion entirely, avoiding token bloat.
- **Hybrid search**: Combines BM25 text ranking with graph proximity to find symbols even with partial or mismatched names.
- **Cross-language flow**: Real AST parsing for 7 languages captures accurate call graphs across your entire stack.

The result is a lean, accurate, and intelligent context engine that scales from small tweaks to large refactors.

---

## Install

```bash
pip install code-context-graph
```

---

## Quick start

```bash
cd your-project
ccg init        # scan + install MCP configs for Claude Code / Cursor / Windsurf
ccg mcp         # start MCP server — your AI now calls ccg automatically
```

After `ccg init`, your AI assistant gets five tools it can call:

| MCP Tool | What it does |
|---|---|
| `ccg_context` | Confidence-scored context bundle — reads only what matters |
| `ccg_blast_radius` | Per-file impact scores (0–1) for any change |
| `ccg_search` | Hybrid BM25 + graph-proximity symbol search |
| `ccg_update` | Incremental re-index after file saves |
| `ccg_stats` | Graph health metrics |

---

## Confidence-scored blast radius

The core innovation. Instead of a flat list of "affected files" (theirs), we return a ranked list with a confidence score for each file:

```bash
ccg blast src/auth.py
```

```
File                        Confidence  Tier
─────────────────────────── ──────────  ──────
src/auth.py                 1.00        🔴 HIGH
src/api/routes.py           0.87        🔴 HIGH
src/middleware/session.py   0.66        🟡 MED
tests/test_auth.py          0.58        🟡 MED
src/utils/crypto.py         0.21        ⚪ LOW
```

**Score formula:**  
`score = edge_weight × depth_decay^hops`

- `inherits` edges = 1.0 weight (strongest dependency)  
- `calls` = 0.9, `tests` = 0.8, `uses` = 0.6, `imports` = 0.4  
- Decay of 0.75× per hop away from the changed code

**AI behaviour:** Read everything ≥0.7. Skim 0.3–0.7. Ignore <0.3 unless explicitly relevant.
Their tool returns everything at equal weight — you never know what to trust.

---

## Adaptive token budgeting

When you change a 10-line helper file, expanding the full graph adds overhead with no value.
We detect this automatically:

```
> ⚡ Trivial change detected — graph expansion skipped to save tokens.
```

---

## Hybrid search

```bash
ccg search "authenticate user"
ccg search getUserById --anchor src/api/routes.py  # boost results near this file
ccg search DataLoader --kind class
```

**Three signals combined:**
1. BM25 text ranking on name + signature + docstring
2. Graph proximity — symbols physically close in the dependency graph to your anchor file rank higher
3. Kind boost — `class` > `function` > `method` > `import`

camelCase and snake_case are split into tokens before ranking, so `getUserById` and `get_user_by_id` both match a query for `"get user"`.

---

## Tree-sitter parsing

All 7 languages use real AST parsers, not regex:

| Language | Parser | What we extract |
|---|---|---|
| Python | tree-sitter-python | Functions, classes, methods, imports, call sites, docstrings, inheritance |
| TypeScript / TSX | tree-sitter-typescript | Same + interfaces, generics |
| JavaScript | tree-sitter-javascript | Same + arrow functions, generators |
| Go | tree-sitter-go | Functions, methods, structs, imports |
| Rust | tree-sitter-rust | Functions, structs, traits, enums, use declarations |
| Java | tree-sitter-java | Classes, methods, interfaces, imports, inheritance |

---

## MCP platforms supported

`ccg setup` writes the correct config for each installed tool:

```
Platform        Config written
──────────────  ──────────────────────────────
Claude Code     .claude/mcp_servers.json
Cursor          .cursor/mcp.json + .cursor/ccg.mdc
Windsurf        .windsurf/mcp.json
VS Code         .vscode/tasks.json
Claude Projects CLAUDE.md
```

Auto-detects `uvx` and uses `uvx code-context-graph mcp` when available.

---

## CLI reference

```bash
ccg init                            # bootstrap everything
ccg scan                            # full re-index
ccg update src/foo.py               # re-index specific files
ccg update --git-diff HEAD~1..HEAD  # re-index last commit's files
ccg update --staged                 # re-index staged files
ccg watch                           # auto-update on file saves
ccg context src/auth.py             # markdown context bundle
ccg context --format json src/api.ts # JSON for tool-use
ccg context --threshold 0.7 src/x.py # high-confidence only
ccg blast src/payment.py            # blast radius table
ccg search "authenticate"           # hybrid search
ccg search "login" --anchor src/api.py --kind function
ccg stats                           # graph metrics
ccg setup                           # (re-)install integrations
ccg mcp                             # start MCP server
```

---

## Python API

```python
from ccg import KnowledgeGraph, Scanner, ContextBuilder, HybridSearch
from pathlib import Path

root = Path("/path/to/repo")
kg = KnowledgeGraph(root / ".ccg/graph.db")
Scanner(root, kg).full_scan()

# Confidence-scored blast radius
blast = kg.blast_radius(["src/auth.py"])
for file, score in blast["ranked_files"]:
    if score >= 0.7:
        print(f"Definitely read: {file} ({score:.2f})")

# Hybrid search
hs = HybridSearch(kg)
hs.build()
results = hs.search("authenticate", anchor_file="src/api.py")

# Context bundle
ctx = ContextBuilder(root, kg).build(["src/auth.py"], format="json")
```

---

## Architecture

```
src/ccg/
├── graph.py              — SQLite + NetworkX, confidence-scored blast radius
├── parsers/
│   ├── __init__.py       — dispatch to Tree-sitter backend
│   └── treesitter.py     — Python/JS/TS/Go/Rust/Java extractors
├── search.py             — Hybrid BM25 + graph-proximity search
├── scanner.py            — Full + incremental scanning
├── context.py            — Adaptive confidence-aware context builder
├── mcp_server.py         — Stdio MCP server (no SDK required)
├── watcher.py            — Debounced filesystem watcher
├── git.py                — Hook installation + diff helpers
├── integrations/         — Claude Code, Cursor, Windsurf, VS Code setup
└── cli.py                — ccg commands
```

---

## License

MIT © 2026
