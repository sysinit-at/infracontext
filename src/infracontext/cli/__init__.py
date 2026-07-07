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

    # A malformed config.yaml (ConfigError) or unreadable YAML (StorageError)
    # must surface as one clean red line, not a Typer pretty-traceback.
    from infracontext.config import ConfigError
    from infracontext.storage import StorageError

    try:
        project = get_active_project()
    except (ConfigError, StorageError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    if not project:
        console.print("[red]No active project. Use 'ic describe project switch <name>' first.[/red]")
        console.print("[dim]Or use: ic -p <project> describe ...[/dim]")
        raise typer.Exit(1)
    if not project_exists(project):
        import os

        # If the missing project came from the env var, point the user at it
        # specifically -- otherwise they may hunt through config.yaml in vain.
        env_project = os.environ.get("IC_PROJECT")
        if env_project == project:
            console.print(
                f"[red]Project '{project}' (from IC_PROJECT env var / -p flag) not found.[/red]"
            )
            console.print(
                "[dim]Unset IC_PROJECT or switch to an existing project: ic describe project list[/dim]"
            )
        else:
            console.print(f"[red]Project '{project}' not found. Create it or switch to another.[/red]")
        if suggestions := _suggest_projects(project):
            console.print(f"[yellow]Did you mean: {', '.join(suggestions)}?[/yellow]")
        raise typer.Exit(1)
    return project


def _suggest_projects(missing: str) -> list[str]:
    """Suggest existing project slugs close to ``missing``.

    Hierarchical slugs are common (``customer/environment``), so a bare
    environment name like ``stegra`` should surface ``qoncept/stegra`` --
    exact tail-component matches win, then fuzzy matches on the full slug.
    """
    from difflib import get_close_matches

    from infracontext.paths import list_projects

    try:
        projects = list_projects()
    except Exception:
        return []

    tail_matches = [p for p in projects if p.split("/")[-1] == missing]
    if tail_matches:
        return tail_matches
    return get_close_matches(missing, projects, n=3, cutoff=0.6)
