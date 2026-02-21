"""CLI commands for infracontext."""

import typer
from rich.console import Console

from infracontext.config import get_active_project
from infracontext.paths import (
    EnvironmentNotFoundError,
    EnvironmentPaths,
    project_exists,
)

console = Console()


def require_environment() -> EnvironmentPaths:
    """Get environment paths or exit with error."""
    try:
        return EnvironmentPaths.current()
    except EnvironmentNotFoundError:
        console.print("[red]Not in an infracontext environment. Run 'ic init' first.[/red]")
        raise typer.Exit(1) from None


def require_project() -> str:
    """Get active project or exit with error.

    Checks in order:
    1. IC_PROJECT environment variable (set by -p flag or directly)
    2. active_project in .infracontext/config.yaml
    """
    require_environment()

    project = get_active_project()
    if not project:
        console.print("[red]No active project. Use 'ic describe project switch <name>' first.[/red]")
        console.print("[dim]Or use: ic -p <project> describe ...[/dim]")
        raise typer.Exit(1)
    if not project_exists(project):
        console.print(f"[red]Project '{project}' not found. Create it or switch to another.[/red]")
        raise typer.Exit(1)
    return project
