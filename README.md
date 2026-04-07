# CodeContextGraph (ccg)

<p align="center">
<strong>Stop burning tokens. Start reviewing smarter.</strong><br>
Precision-first context engine for AI coding assistants • Confidence-scored blast radius • Adaptive token budgeting • Native MCP server
</p>

<p align="center">
<a href="https://pypi.org/project/code-context-graph/"><img src="https://img.shields.io/pypi/v/code-context-graph?logo=python" alt="PyPI"></a>
<a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
<a ><img src="https://img.shields.io/badge/Python-3.10+-brightgreen" alt="Python 3.10+"></a>
<img src="https://img.shields.io/badge/MCP-v2.0.0-blueviolet" alt="MCP v2.1.0">
</p>

**AI coding tools re-read your entire codebase on every task.**  

CodeContextGraph fixes that. It builds a structural map of your code with Tree‑sitter, tracks changes incrementally, and gives your AI assistant precise context via MCP so it reads only what matters.

**Token Efficiency:** Up to **8x fewer tokens** on real‑world repositories (compared to naive full‑code context).

---

## Quick Start

```bash
pip install code-context-graph               # or: uv tool install code-context-graph
ccg init                                    # first scan + install MCP config
ccg mcp                                     # start the MCP server (or it's started automatically by your editor)
```

`ccg init` auto‑detects your AI coding tools (Claude Code, Cursor, Windsurf, VS Code) and writes the correct MCP configuration. After that, your assistant can call `ccg_context`, `ccg_blast_radius`, `ccg_search`, etc. automatically.

To keep the graph up‑to‑date automatically, a git post‑commit hook is installed.

---

## How It Works

1. **Parse** – Tree‑sitter builds an AST for every supported file (Python, JavaScript/TypeScript, Go, Rust, Java)
2. **Graph** – Symbols and relationships become nodes and edges (calls, imports, inherits, uses) stored in a SQLite database
3. **Score** – When files change, a confidence algorithm computes a score (0–1) for every affected file based on edge weight and graph distance
4. **Deliver** – AI assistants query `ccg_context` and receive a minimal, ranked set of files and snippets, avoiding irrelevant code

The result is precise, token‑efficient reviews.

---

## Confidence‑scored blast radius

Instead of a flat list of affected files, CodeContextGraph returns a **ranked list with confidence scores**:

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

**Scoring:** `score = edge_weight × depth_decay^hops`

- `inherits` = 1.0, `calls` = 0.9, `tests` = 0.8, `uses` = 0.6, `imports` = 0.4
- Decay factor = 0.75 per hop
- AI should read ≥0.7 always, skim 0.3–0.7, ignore <0.3

---

## Adaptive token budgeting

Small, single‑symbol changes don't need the full graph. The system detects trivial edits and **skips graph expansion** entirely, sending only the changed file.

```
> ⚡ Trivial change detected — graph expansion skipped to save tokens.
```

This avoids the <1× efficiency regression seen in other tools for tiny edits.

---

## Hybrid search

`ccg_search` combines BM25 text ranking, graph proximity, and kind boosting:

```bash
ccg search "authenticate user"
ccg search getUserById --anchor src/api/routes.py --kind function
```

- **BM25** on name, signature, docstring
- **Graph proximity** boosts symbols near your anchor file
- **Kind boost** – classes > functions > methods > imports

Tokenization splits camelCase and snake_case, so `getUserById` matches "get user".

---

## Multi‑format context

`ccg_context` outputs Markdown (default), JSON, or XML:

```bash
ccg context src/auth.py --format json --threshold 0.7
```

The context respects a token budget and groups files by confidence tier, making it easy for AI assistants to decide how much to trust each file.

---

## Incremental updates

Graph updates are incremental: changed files are re‑parsed, and only affected edges are recomputed. Most changes propagate in **under 2 seconds** even for large repos.

Git hooks and a file watcher (`ccg watch`) keep the graph fresh automatically.

---

## Language support

Tree‑sitter parsers for:

- **Python** (functions, classes, methods, imports, calls, inheritance, docstrings)
- **JavaScript / TypeScript / TSX** (including interfaces, generics, arrow functions)
- **Go** (functions, methods, structs, imports)
- **Rust** (functions, structs, traits, enums, use declarations)
- **Java** (classes, methods, interfaces, imports, inheritance)

---

## Features at a glance

| Feature | Description |
|---------|-------------|
| Confidence‑scored blast radius | Files get 0–1 scores, AI chooses threshold |
| Adaptive trivial change detection | Skips graph expansion for tiny edits |
| Hybrid search (BM25 + proximity) | Finds symbols even with partial or mismatched names |
| Multi‑format output | Markdown, JSON, XML with token budgeting |
| Incremental updates | <2s on large repos |
| Multi‑language AST parsing | 7 languages via Tree‑sitter |
| MCP integration | 5 tools (`ccg_context`, `ccg_blast_radius`, `ccg_search`, `ccg_stats`, `ccg_update`) |
| Git hooks & file watcher | Automatic graph maintenance |
| FTS5 full‑text search | Built‑in to the SQLite database |
| Architecture map | Auto‑generated dependency graph (via `ccg stats`) |

---

## CLI reference

```bash
ccg init                            # bootstrap graph, first scan, install integrations
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

## MCP tools

When `ccg mcp` runs, your AI assistant can call:

| Tool | What it does |
|------|--------------|
| `ccg_context` | Confidence‑scored context bundle (reads only what matters) |
| `ccg_blast_radius` | Per‑file impact scores (0–1) for any change |
| `ccg_search` | Hybrid BM25 + graph‑proximity symbol search |
| `ccg_stats` | Knowledge graph health metrics |
| `ccg_update` | Incrementally re‑index changed files |

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

## Configuration

`ccg setup` writes configuration files for your editor:

- Claude Code: `.claude/mcp_servers.json`
- Cursor: `.cursor/mcp.json + .cursor/ccg.mdc`
- Windsurf: `.windsurf/mcp.json`
- VS Code: `.vscode/tasks.json`
- Claude Projects: `CLAUDE.md`

It also installs a git post‑commit hook to keep the graph up‑to‑date automatically.

---

## Contributing

Contributions are welcome! Please open an issue or PR.

```bash
git clone https://github.com/your-username/code-context-graph.git
cd code-context-graph
python -m venv .venv && source .venv/bin/activate  # or `uv venv`
pip install -e ".[dev]"
pytest
```

---

## License

MIT. See [LICENSE](LICENSE).