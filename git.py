"""
git.py — Git integration: install hooks, detect changed files from commits.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from rich.console import Console

console = Console()

# ── Hook templates ────────────────────────────────────────────────────────────

# post-commit: re-index files changed in HEAD
_POST_COMMIT_HOOK = """\
#!/usr/bin/env sh
# ccg post-commit hook (auto-generated)
set -e
ccg update --git-diff HEAD~1..HEAD --quiet || true
"""

# pre-push: ensure index is up to date
_PRE_PUSH_HOOK = """\
#!/usr/bin/env sh
# ccg pre-push hook (auto-generated)
set -e
ccg update --git-diff HEAD~1..HEAD --quiet || true
"""


def install_hooks(repo_root: Path) -> list[str]:
    """
    Install ccg git hooks into `.git/hooks/`.
    Appends to existing hooks rather than overwriting.

    Returns a list of hook file paths that were written.
    """
    hooks_dir = repo_root / ".git" / "hooks"
    if not hooks_dir.is_dir():
        raise FileNotFoundError(
            f"No .git/hooks directory found at {repo_root}. "
            "Is this a git repository?"
        )

    installed: list[str] = []

    hooks = {
        "post-commit": _POST_COMMIT_HOOK,
    }

    for name, content in hooks.items():
        hook_path = hooks_dir / name
        if hook_path.exists():
            existing = hook_path.read_text()
            if "ccg" in existing:
                console.print(f"  [yellow]~[/yellow] Hook already present: {hook_path}")
                continue
            # Append to existing hook
            with hook_path.open("a") as f:
                f.write("\n" + content)
        else:
            hook_path.write_text("#!/usr/bin/env sh\n" + content)

        # Make executable
        current = stat.S_IMODE(os.lstat(hook_path).st_mode)
        hook_path.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        installed.append(str(hook_path))
        console.print(f"  [green]✓[/green] Installed hook: [cyan]{hook_path}[/cyan]")

    return installed


def remove_hooks(repo_root: Path) -> None:
    """Remove ccg sections from git hooks."""
    hooks_dir = repo_root / ".git" / "hooks"
    for hook_path in hooks_dir.iterdir():
        if not hook_path.is_file():
            continue
        text = hook_path.read_text()
        if "ccg" not in text:
            continue
        # Strip lines that reference ccg
        cleaned = "\n".join(
            line for line in text.splitlines() if "ccg" not in line
        )
        hook_path.write_text(cleaned)
        console.print(f"  [yellow]~[/yellow] Removed ccg from {hook_path}")


# ── Diff helpers ──────────────────────────────────────────────────────────────

def changed_files_from_diff(
    repo_root: Path,
    diff_spec: str = "HEAD~1..HEAD",
) -> list[Path]:
    """
    Return a list of absolute Paths for files changed in `diff_spec`.
    Falls back to HEAD (all tracked files) if the diff fails.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", diff_spec],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
            return [repo_root / l for l in lines]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return []


def changed_files_staged(repo_root: Path) -> list[Path]:
    """Return files staged for commit (git diff --cached --name-only)."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
            return [repo_root / l for l in lines]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return []


def find_repo_root(start: Path | None = None) -> Path | None:
    """Walk up the directory tree to find the git repository root."""
    current = (start or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").is_dir():
            return parent
    return None
