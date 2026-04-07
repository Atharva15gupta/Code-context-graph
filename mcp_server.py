"""
mcp_server.py — Native MCP server for CodeContextGraph.

Features:
  - Single 'ccg mcp' command, no separate process needed.
  - Auto-generated configs for Claude Code, Cursor, Windsurf, and VS Code.
  - Tools: ccg_blast_radius, ccg_context, ccg_search, ccg_stats, ccg_update.
  - Works without the `mcp` extra — plain JSON-over-stdio.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from .git import find_repo_root
from .graph import KnowledgeGraph
from .scanner import Scanner
from .context import ContextBuilder
from .search import HybridSearch

# Try to import the official MCP SDK; fall back to a minimal stdio shim
try:
    from mcp.server import Server as _MCPServer
    from mcp.server.stdio import stdio_server
    from mcp import types as mcp_types
    _MCP_SDK = True
except ImportError:
    _MCP_SDK = False


# ── Tool definitions (schema) ─────────────────────────────────────────────────

TOOLS = [
    {
        "name": "ccg_blast_radius",
        "description": (
            "Get confidence-scored blast radius for changed files. "
            "Returns ranked files with scores 0-1 so you know exactly "
            "what to read. Score ≥0.7 = very likely affected, 0.3-0.7 = "
            "possibly affected, <0.3 = distant dependency."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repo-relative file paths that changed",
                },
                "depth": {
                    "type": "integer",
                    "default": 3,
                    "description": "Traversal depth (1-5)",
                },
            },
            "required": ["files"],
        },
    },
    {
        "name": "ccg_context",
        "description": (
            "Get a minimal, focused context bundle for AI code review. "
            "Automatically skips graph expansion for trivial changes to "
            "avoid wasting tokens. Returns structured context with "
            "confidence-ranked affected files and source snippets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Changed file paths (repo-relative)",
                },
                "format": {
                    "type": "string",
                    "enum": ["markdown", "json", "xml"],
                    "default": "markdown",
                },
                "max_tokens": {
                    "type": "integer",
                    "default": 6000,
                    "description": "Soft token budget for the context bundle",
                },
                "include_snippets": {
                    "type": "boolean",
                    "default": True,
                },
            },
            "required": ["files"],
        },
    },
    {
        "name": "ccg_search",
        "description": (
            "Hybrid BM25 + graph-proximity symbol search. "
            "Handles camelCase, snake_case, and partial names. "
            "Optionally boost results near a specific file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Symbol name or description"},
                "anchor_file": {
                    "type": "string",
                    "description": "Repo-relative path of file you are editing (boosts nearby results)",
                },
                "kind": {
                    "type": "string",
                    "enum": ["function", "class", "method", "import", "variable"],
                    "description": "Filter by symbol kind",
                },
                "top_k": {"type": "integer", "default": 15},
            },
            "required": ["query"],
        },
    },
    {
        "name": "ccg_stats",
        "description": "Return knowledge graph health metrics (symbol count, edge count, languages).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ccg_update",
        "description": "Incrementally re-index changed files and update the knowledge graph.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repo-relative file paths to re-index",
                },
            },
            "required": ["files"],
        },
    },
]


# ── Tool handler ──────────────────────────────────────────────────────────────

class CCGHandler:
    def __init__(self, root: Path, kg: KnowledgeGraph):
        self.root = root
        self.kg = kg
        self._search = HybridSearch(kg)
        self._search.build()

    def handle(self, tool_name: str, args: dict) -> str:
        try:
            if tool_name == "ccg_blast_radius":
                result = self.kg.blast_radius(
                    args["files"], max_depth=args.get("depth", 3)
                )
                # Simplify for output
                return json.dumps({
                    "impact": result["total_impact"],
                    "is_trivial": result.get("is_trivial", False),
                    "precision_hint": result.get("precision_hint", ""),
                    "ranked_files": [
                        {"file": f, "score": round(s, 3)}
                        for f, s in result["ranked_files"][:30]
                    ],
                    "direct_symbols": result["direct_symbols"][:20],
                }, indent=2)

            elif tool_name == "ccg_context":
                builder = ContextBuilder(
                    self.root, self.kg,
                    max_tokens=args.get("max_tokens", 6000),
                )
                return builder.build(
                    changed_files=args["files"],
                    format=args.get("format", "markdown"),
                    include_snippets=args.get("include_snippets", True),
                )

            elif tool_name == "ccg_search":
                results = self._search.search(
                    query=args["query"],
                    anchor_file=args.get("anchor_file"),
                    top_k=args.get("top_k", 15),
                    kind_filter=args.get("kind"),
                )
                return json.dumps(results[:15], indent=2, default=str)

            elif tool_name == "ccg_stats":
                return json.dumps(self.kg.stats(), indent=2)

            elif tool_name == "ccg_update":
                scanner = Scanner(self.root, self.kg)
                paths = [self.root / f for f in args["files"]]
                result = scanner.incremental_scan(paths)
                # Rebuild search index
                self._search.build()
                return json.dumps(result)

            else:
                return json.dumps({"error": f"Unknown tool: {tool_name}"})

        except Exception as exc:
            return json.dumps({"error": str(exc), "trace": traceback.format_exc()})


# ── MCP stdio shim (no SDK required) ─────────────────────────────────────────

def run_stdio(handler: CCGHandler) -> None:
    """
    Minimal JSON-RPC-over-stdio MCP server.
    Works with Claude Code, Cursor, Windsurf, and any other MCP client
    without requiring the `mcp` Python package.
    """
    import sys

    def send(obj: dict) -> None:
        line = json.dumps(obj)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method", "")
        req_id = req.get("id")
        params = req.get("params", {})

        if method == "initialize":
            send({
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ccg", "version": "0.2.0"},
                },
            })

        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})

        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args  = params.get("arguments", {})
            content    = handler.handle(tool_name, tool_args)
            send({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": content}]},
            })

        elif method == "ping":
            send({"jsonrpc": "2.0", "id": req_id, "result": {}})


# ── Entry point ───────────────────────────────────────────────────────────────

def serve(root: Path | None = None) -> None:
    """Start the MCP server (called from `ccg mcp`)."""
    repo_root = root or find_repo_root() or Path.cwd()
    db_path = repo_root / ".ccg" / "graph.db"

    if not db_path.exists():
        sys.stderr.write(
            "ccg: knowledge graph not found. Run `ccg init` first.\n"
        )
        sys.exit(1)

    kg = KnowledgeGraph(db_path)
    handler = CCGHandler(repo_root, kg)
    run_stdio(handler)
    kg.close()
