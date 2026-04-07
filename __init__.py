"""
integrations/__init__.py — Auto-configure AI platforms with MCP server.

Supports: Claude Code, Cursor, Windsurf, VS Code, generic CLAUDE.md
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from rich.console import Console

console = Console()


def _ccg_cmd() -> str:
    """Return 'uvx code-context-graph' if uvx is available, else 'ccg'."""
    if shutil.which("uvx"):
        return "uvx code-context-graph"
    return "ccg"


# ── MCP config generators ─────────────────────────────────────────────────────

def _mcp_server_block(root: Path) -> dict:
    cmd = _ccg_cmd()
    parts = cmd.split()
    return {
        "command": parts[0],
        "args": parts[1:] + ["mcp", "--root", str(root)],
        "env": {},
    }


def setup_claude_code(root: Path) -> None:
    """Write .claude/mcp_servers.json for Claude Code."""
    claude_dir = root / ".claude"
    claude_dir.mkdir(exist_ok=True)
    config_path = claude_dir / "mcp_servers.json"

    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}

    if "ccg" in data.get("mcpServers", {}):
        console.print("  [yellow]~[/yellow] Claude Code MCP config already present.")
        return

    data.setdefault("mcpServers", {})["ccg"] = _mcp_server_block(root)
    config_path.write_text(json.dumps(data, indent=2))
    console.print(f"  [green]✓[/green] Claude Code MCP → [cyan]{config_path}[/cyan]")


def setup_cursor(root: Path) -> None:
    """Write .cursor/mcp.json for Cursor."""
    cursor_dir = root / ".cursor"
    cursor_dir.mkdir(exist_ok=True)
    mcp_path = cursor_dir / "mcp.json"

    if mcp_path.exists():
        try:
            data = json.loads(mcp_path.read_text())
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}

    if "ccg" in data.get("mcpServers", {}):
        console.print("  [yellow]~[/yellow] Cursor MCP config already present.")
        return

    data.setdefault("mcpServers", {})["ccg"] = _mcp_server_block(root)
    mcp_path.write_text(json.dumps(data, indent=2))
    console.print(f"  [green]✓[/green] Cursor MCP       → [cyan]{mcp_path}[/cyan]")

    # Also write a rules file
    rules_path = cursor_dir / "ccg.mdc"
    if not rules_path.exists():
        rules_path.write_text(
            "---\ndescription: Use ccg MCP tools before reviewing code\n"
            "globs: [\"**/*.py\",\"**/*.ts\",\"**/*.tsx\",\"**/*.js\",\"**/*.go\",\"**/*.rs\"]\n"
            "alwaysApply: false\n---\n\n"
            "Before reviewing or modifying any file call `ccg_context` with the changed "
            "file paths. This returns confidence-scored blast radius and focused context "
            "so you read only what matters.\n"
        )
        console.print(f"  [green]✓[/green] Cursor rules      → [cyan]{rules_path}[/cyan]")


def setup_windsurf(root: Path) -> None:
    """Write .windsurf/mcp.json for Windsurf."""
    ws_dir = root / ".windsurf"
    ws_dir.mkdir(exist_ok=True)
    mcp_path = ws_dir / "mcp.json"

    if mcp_path.exists():
        try:
            data = json.loads(mcp_path.read_text())
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}

    if "ccg" in data.get("mcpServers", {}):
        console.print("  [yellow]~[/yellow] Windsurf MCP config already present.")
        return

    data.setdefault("mcpServers", {})["ccg"] = _mcp_server_block(root)
    mcp_path.write_text(json.dumps(data, indent=2))
    console.print(f"  [green]✓[/green] Windsurf MCP      → [cyan]{mcp_path}[/cyan]")


def setup_vscode(root: Path) -> None:
    """Add ccg tasks to .vscode/tasks.json."""
    vscode_dir = root / ".vscode"
    vscode_dir.mkdir(exist_ok=True)
    tasks_path = vscode_dir / "tasks.json"

    if tasks_path.exists():
        try:
            data = json.loads(tasks_path.read_text())
        except json.JSONDecodeError:
            data = {"version": "2.0.0", "tasks": []}
    else:
        data = {"version": "2.0.0", "tasks": []}

    existing = {t.get("label", "") for t in data.get("tasks", [])}
    new_tasks = [
        {"label": "ccg: scan", "type": "shell", "command": "ccg scan",
         "group": "build", "presentation": {"reveal": "always"}},
        {"label": "ccg: context for active file", "type": "shell",
         "command": "ccg context ${file}", "group": "test",
         "presentation": {"reveal": "always"}},
        {"label": "ccg: start MCP server", "type": "shell",
         "command": "ccg mcp", "isBackground": True,
         "presentation": {"reveal": "silent"}},
    ]
    added = []
    for t in new_tasks:
        if t["label"] not in existing:
            data.setdefault("tasks", []).append(t)
            added.append(t["label"])

    tasks_path.write_text(json.dumps(data, indent=2))
    if added:
        console.print(f"  [green]✓[/green] VS Code tasks     → [cyan]{tasks_path}[/cyan]")


def setup_claude_md(root: Path) -> None:
    """Create or update CLAUDE.md with MCP-aware instructions."""
    path = root / "CLAUDE.md"
    content = """\
# CodeContextGraph (ccg)

This project uses [CodeContextGraph](https://github.com/your-username/CodeContextGraph)
to give you precise, token-efficient context for code reviews.

## MCP Tools Available

| Tool | Use when |
|---|---|
| `ccg_context` | Before reviewing any file — returns confidence-scored blast radius + snippets |
| `ccg_blast_radius` | To understand what could break from a change |
| `ccg_search` | To find where a function/class is defined across the repo |
| `ccg_update` | After saving files, to keep the graph fresh |
| `ccg_stats` | To check graph health |

## Workflow

1. When asked to review or edit `src/foo.py`, call `ccg_context(files=["src/foo.py"])`
2. Read files scored **≥0.7** (high confidence). Glance at **0.3–0.7** if relevant.
3. Skip files scored **<0.3** unless explicitly relevant.
4. After making changes, call `ccg_update(files=[...changed files...])`.

"""
    if path.exists():
        existing = path.read_text()
        if "ccg_context" in existing:
            console.print("  [yellow]~[/yellow] CLAUDE.md already has ccg instructions.")
            return
        path.write_text(content + "\n---\n\n" + existing)
    else:
        path.write_text(content)
    console.print(f"  [green]✓[/green] CLAUDE.md         → [cyan]{path}[/cyan]")


def update_gitignore(root: Path) -> None:
    path = root / ".gitignore"
    entry = "\n# CodeContextGraph knowledge graph\n.ccg/\n"
    if path.exists():
        if ".ccg" in path.read_text():
            return
        path.write_text(path.read_text() + entry)
    else:
        path.write_text(entry.lstrip())
    console.print(f"  [green]✓[/green] .gitignore        → .ccg/ added")


def setup_all(root: Path, platforms: list[str] | None = None) -> None:
    if platforms is None:
        platforms = ["claude-code", "cursor", "windsurf", "vscode", "claude-md"]

    console.print("\n[bold]Setting up integrations…[/bold]")

    if "claude-code" in platforms:
        setup_claude_code(root)
    if "cursor" in platforms:
        setup_cursor(root)
    if "windsurf" in platforms:
        setup_windsurf(root)
    if "vscode" in platforms:
        setup_vscode(root)
    if "claude-md" in platforms:
        setup_claude_md(root)

    update_gitignore(root)
    console.print("")
