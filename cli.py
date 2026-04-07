"""
cli.py — CodeContextGraph CLI

Commands
--------
  init      Bootstrap graph, first scan, set up all integrations
  scan      Full re-index
  update    Incremental re-index (files / --git-diff / --staged)
  watch     Auto-update on file saves
  context   Output confidence-scored AI context bundle
  blast     Show blast radius with confidence scores
  search    Hybrid BM25 + graph-proximity symbol search
  stats     Graph statistics
  setup     (Re-)install integrations and git hooks
  mcp       Start the MCP server (for Claude Code, Cursor, Windsurf)
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .git import changed_files_from_diff, changed_files_staged, find_repo_root, install_hooks, remove_hooks
from .graph import KnowledgeGraph
from .integrations import setup_all
from .scanner import Scanner
from .context import ContextBuilder
from .search import HybridSearch

console = Console()


def _db_path(root: Path) -> Path:
    return root / ".ccg" / "graph.db"


def _open_kg(root: Path) -> KnowledgeGraph:
    db = _db_path(root)
    if not db.exists():
        console.print(
            "[red]✗ Knowledge graph not found.[/red] "
            "Run [bold cyan]ccg init[/bold cyan] first."
        )
        sys.exit(1)
    return KnowledgeGraph(db)


def _resolve_root(root_opt: str | None) -> Path:
    if root_opt:
        return Path(root_opt).resolve()
    return find_repo_root() or Path.cwd()


def _rel_files(files: tuple, root: Path) -> list[str]:
    result = []
    for f in files:
        p = Path(f)
        try:
            result.append(str(p.relative_to(root)))
        except ValueError:
            result.append(str(p))
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
@click.version_option(package_name="code-context-graph")
def main() -> None:
    """
    ⚡ CodeContextGraph (ccg) — precision-first context for AI coding assistants.

    Tree-sitter parsing • Confidence-scored blast radius • Native MCP server
    """


@main.command()
@click.option("--root", default=None)
@click.option("--setup/--no-setup", default=True)
@click.option("--hooks/--no-hooks", default=True)
def init(root, setup, hooks) -> None:
    """Bootstrap the knowledge graph: scan + integrations + hooks."""
    repo_root = _resolve_root(root)
    console.print(Panel(
        f"Initialising CodeContextGraph for [cyan]{repo_root}[/cyan]",
        style="bold green",
    ))

    db = _db_path(repo_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    kg = KnowledgeGraph(db)
    console.print(f"[green]✓[/green] Graph DB → [cyan]{db}[/cyan]")

    console.print("\n[bold]Running initial scan (Tree-sitter)…[/bold]")
    scanner = Scanner(repo_root, kg)
    result = scanner.full_scan()
    _print_scan_result(result, kg)

    if setup:
        setup_all(repo_root)

    if hooks:
        console.print("[bold]Installing git hooks…[/bold]")
        try:
            install_hooks(repo_root)
        except FileNotFoundError as e:
            console.print(f"  [yellow]⚠[/yellow] {e}")

    console.print(
        "\n[bold green]✓ Done![/bold green]  "
        "Start the MCP server with [bold cyan]ccg mcp[/bold cyan] "
        "so your AI tools call it automatically."
    )
    kg.close()


@main.command()
@click.option("--root", default=None)
@click.option("--verbose", "-v", is_flag=True)
def scan(root, verbose) -> None:
    """Full re-index of the repository (Tree-sitter, skips unchanged files)."""
    repo_root = _resolve_root(root)
    kg = _open_kg(repo_root)
    console.print(f"[bold]Scanning[/bold] [cyan]{repo_root}[/cyan]…")
    result = Scanner(repo_root, kg, verbose=verbose).full_scan()
    _print_scan_result(result, kg)
    kg.close()


@main.command()
@click.argument("files", nargs=-1, type=click.Path())
@click.option("--root", default=None)
@click.option("--git-diff", default=None, metavar="RANGE")
@click.option("--staged", is_flag=True)
@click.option("--quiet", "-q", is_flag=True)
def update(files, root, git_diff, staged, quiet) -> None:
    """Incremental re-index of changed files."""
    repo_root = _resolve_root(root)
    kg = _open_kg(repo_root)
    scanner = Scanner(repo_root, kg)

    paths: list[Path] = []
    if git_diff:
        paths = changed_files_from_diff(repo_root, git_diff)
        if not quiet:
            console.print(f"Git diff {git_diff}: {len(paths)} file(s)")
    elif staged:
        paths = changed_files_staged(repo_root)
        if not quiet:
            console.print(f"Staged: {len(paths)} file(s)")
    elif files:
        paths = [Path(f) for f in files]
    else:
        console.print("[yellow]Specify files, --git-diff, or --staged.[/yellow]")
        kg.close()
        return

    result = scanner.incremental_scan(paths)
    if not quiet:
        _print_scan_result(result, kg)
    kg.close()


@main.command()
@click.option("--root", default=None)
def watch(root) -> None:
    """Watch the repository and auto-update the graph on file saves."""
    from .watcher import Watcher
    repo_root = _resolve_root(root)
    kg = _open_kg(repo_root)
    w = Watcher(repo_root, kg)
    w.start()
    w.wait()
    kg.close()


@main.command()
@click.argument("files", nargs=-1, type=click.Path(), required=True)
@click.option("--root", default=None)
@click.option("--format", "fmt",
              type=click.Choice(["markdown", "json", "xml"]), default="markdown")
@click.option("--max-tokens", default=8000, show_default=True)
@click.option("--depth", default=3, show_default=True)
@click.option("--no-snippets", is_flag=True)
@click.option("--output", "-o", type=click.Path(), default=None)
@click.option("--threshold", default=0.3, show_default=True,
              help="Minimum confidence score to include a file (0.0-1.0)")
def context(files, root, fmt, max_tokens, depth, no_snippets, output, threshold) -> None:
    """
    Generate confidence-scored AI context for changed files.

    \b
    Examples:
      ccg context src/auth.py
      ccg context --format json src/api.ts src/models.ts
      ccg context --threshold 0.7 src/main.go   # only high-confidence files
    """
    repo_root = _resolve_root(root)
    kg = _open_kg(repo_root)
    rel = _rel_files(files, repo_root)
    builder = ContextBuilder(repo_root, kg, max_tokens=max_tokens)
    ctx = builder.build(
        changed_files=rel, format=fmt,
        include_snippets=not no_snippets, depth=depth,
        confidence_threshold=threshold,
    )
    if output:
        Path(output).write_text(ctx)
        console.print(f"[green]✓[/green] Context → [cyan]{output}[/cyan]")
    else:
        click.echo(ctx)
    kg.close()


@main.command()
@click.argument("files", nargs=-1, type=click.Path(), required=True)
@click.option("--root", default=None)
@click.option("--depth", default=3, show_default=True)
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def blast(files, root, depth, fmt) -> None:
    """Show confidence-scored blast radius for changed files."""
    repo_root = _resolve_root(root)
    kg = _open_kg(repo_root)
    rel = _rel_files(files, repo_root)
    result = kg.blast_radius(rel, max_depth=depth)

    if fmt == "json":
        import json
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        _print_blast_table(result, rel)
    kg.close()


@main.command()
@click.argument("query")
@click.option("--root", default=None)
@click.option("--anchor", default=None, metavar="FILE",
              help="Boost symbols near this file (repo-relative)")
@click.option("--kind", default=None,
              type=click.Choice(["function", "class", "method", "import", "variable"]))
@click.option("--limit", default=15, show_default=True)
def search(query, root, anchor, kind, limit) -> None:
    """
    Hybrid symbol search: BM25 text ranking + graph proximity boosting.

    Handles camelCase, snake_case, and partial matches.

    \b
    Examples:
      ccg search authenticate
      ccg search "user login" --anchor src/api/routes.py
      ccg search DataLoader --kind class
    """
    repo_root = _resolve_root(root)
    kg = _open_kg(repo_root)
    hs = HybridSearch(kg)
    hs.build()
    results = hs.search(query, anchor_file=anchor, top_k=limit, kind_filter=kind)
    kg.close()

    if not results:
        console.print(f"[yellow]No results for[/yellow] '{query}'")
        return

    table = Table(title=f"Search: '{query}'", show_lines=False)
    table.add_column("Score", style="dim", justify="right", width=6)
    table.add_column("Name", style="bold cyan")
    table.add_column("Kind", style="dim", width=10)
    table.add_column("File", style="green")
    table.add_column("Line", justify="right", style="dim", width=5)

    for r in results:
        table.add_row(
            f"{r.get('search_score', 0):.3f}",
            r["name"], r["kind"], r["file"], str(r["line"]),
        )
    console.print(table)


@main.command()
@click.option("--root", default=None)
def stats(root) -> None:
    """Print knowledge graph statistics."""
    repo_root = _resolve_root(root)
    kg = _open_kg(repo_root)
    s = kg.stats()
    kg.close()

    table = Table(title="📊 CodeContextGraph Stats", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", style="cyan", justify="right")
    table.add_row("Symbols", f"{s['symbols']:,}")
    table.add_row("Edges", f"{s['edges']:,}")
    table.add_row("Files", f"{s['files']:,}")
    table.add_row("Lines of code", f"{s.get('total_loc', 0):,}")
    table.add_row("", "")
    for lang, count in sorted(s["languages"].items(), key=lambda x: -x[1]):
        table.add_row(f"  {lang}", f"{count:,}")
    console.print(table)


@main.command()
@click.option("--root", default=None)
@click.option("--platform", "-p", multiple=True,
              type=click.Choice(["claude-code", "cursor", "windsurf", "vscode", "claude-md"]))
@click.option("--hooks/--no-hooks", default=True)
@click.option("--remove-hooks", "remove", is_flag=True)
def setup(root, platform, hooks, remove) -> None:
    """Install integrations (Claude Code, Cursor, Windsurf, VS Code) and git hooks."""
    repo_root = _resolve_root(root)
    if remove:
        remove_hooks(repo_root)
        return
    setup_all(repo_root, list(platform) or None)
    if hooks:
        console.print("[bold]Installing git hooks…[/bold]")
        try:
            install_hooks(repo_root)
        except FileNotFoundError as e:
            console.print(f"  [yellow]⚠[/yellow] {e}")


@main.command()
@click.option("--root", default=None)
def mcp(root) -> None:
    """
    Start the MCP server (stdio transport).

    Add this to your editor's MCP config:

    \b
      Claude Code:  .claude/mcp_servers.json
      Cursor:       .cursor/mcp.json
      Windsurf:     .windsurf/mcp.json

    Run `ccg setup` to generate these configs automatically.

    \b
    Tools exposed:
      ccg_context       — confidence-scored context bundle
      ccg_blast_radius  — impact analysis
      ccg_search        — hybrid symbol search
      ccg_stats         — graph metrics
      ccg_update        — incremental re-index
    """
    from .mcp_server import serve
    repo_root = _resolve_root(root)
    serve(repo_root)


# ── Pretty printers ───────────────────────────────────────────────────────────

def _print_scan_result(result: dict, kg: KnowledgeGraph) -> None:
    s = kg.stats()
    console.print(
        f"\n[bold green]Scan complete[/bold green] — "
        f"indexed [cyan]{result['scanned']}[/cyan], "
        f"skipped {result['skipped']} (unchanged), "
        f"{result['errors']} error(s) — [dim]{result['elapsed']}s[/dim]"
    )
    console.print(
        f"Graph: [bold]{s['symbols']:,}[/bold] symbols  "
        f"[bold]{s['edges']:,}[/bold] edges  "
        f"[bold]{s['files']:,}[/bold] files  "
        f"[bold]{s.get('total_loc', 0):,}[/bold] LOC\n"
    )


def _print_blast_table(result: dict, changed_files: list[str]) -> None:
    color = {"critical": "bold red", "high": "red",
              "medium": "yellow", "low": "green"}.get(result["total_impact"], "white")

    console.print(
        f"\n[bold]Blast Radius[/bold] for {', '.join(f'[cyan]{f}[/cyan]' for f in changed_files)}"
    )
    console.print(f"Impact: [{color}]{result['total_impact'].upper()}[/{color}]")
    if result.get("is_trivial"):
        console.print("[dim]Trivial change — graph expansion skipped.[/dim]")

    console.print(f"\n[dim]{result.get('precision_hint', '')}[/dim]\n")

    ranked = result.get("ranked_files", [])
    if ranked:
        table = Table(show_lines=False)
        table.add_column("File", style="green")
        table.add_column("Confidence", justify="right")
        table.add_column("Tier", justify="center")

        for f, s in ranked[:30]:
            tier = "🔴 HIGH" if s >= 0.7 else "🟡 MED" if s >= 0.3 else "⚪ LOW"
            table.add_row(f, f"{s:.2f}", tier)
        console.print(table)
