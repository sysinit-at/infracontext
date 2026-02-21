"""Main CLI entry point for infracontext."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from infracontext import __version__
from infracontext.cli import config, describe, doctor, import_cmd, migrate, query, triage
from infracontext.paths import INFRACONTEXT_DIR, LOCAL_OVERRIDES_FILE, EnvironmentPaths, find_environment_root

app = typer.Typer(
    name="ic",
    help="Infrastructure context for humans and agents",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

console = Console()

# Register sub-command groups
app.add_typer(describe.app, name="describe", help="Document and manage infrastructure")
app.add_typer(import_cmd.app, name="import", help="Import infrastructure from sources")
app.add_typer(triage.app, name="triage", help="Troubleshoot infrastructure issues")
app.add_typer(config.app, name="config", help="Manage configuration and credentials")
app.add_typer(migrate.app, name="migrate", help="Migrate data from legacy locations")
app.add_typer(query.app, name="query", help="Query monitoring sources")
app.add_typer(doctor.app, name="doctor", help="Validate infrastructure data")


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", "-v", help="Show version and exit"),
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Project to use (overrides config)", envvar="IC_PROJECT"),
    ] = None,
    tier: Annotated[
        str | None,
        typer.Option(
            "--tier",
            help="Access tier restriction (local_only/collector/unprivileged/privileged/remediate)",
            envvar="IC_TIER",
        ),
    ] = None,
) -> None:
    """Infracontext: Infrastructure context for humans and agents."""
    import os

    if version:
        console.print(f"infracontext {__version__}")
        raise typer.Exit()

    # Set project override as env var so it propagates to all subcommands
    # (Typer contexts don't propagate to sub-apps)
    if project:
        os.environ["IC_PROJECT"] = project

    # Set tier override as env var (can only restrict, not elevate)
    if tier:
        os.environ["IC_TIER"] = tier


@app.command()
def init(
    path: Annotated[
        Path | None,
        typer.Argument(help="Directory to initialize (default: current directory)"),
    ] = None,
) -> None:
    """Initialize infracontext in the current directory.

    Creates a .infracontext/ directory structure for storing infrastructure
    documentation. Add .infracontext.local.yaml to .gitignore for local overrides.
    """
    target = (path or Path.cwd()).resolve()

    # Check if already initialized
    existing = find_environment_root(target)
    if existing:
        console.print(f"[yellow]Already initialized at {existing}[/yellow]")
        raise typer.Exit(1)

    # Create structure
    environment = EnvironmentPaths.from_root(target)
    environment.ensure_dirs()

    # Create initial config
    (environment.config_yaml).write_text("# active_project: my-project\n")

    console.print(f"[green]Initialized infracontext in {target}[/green]")
    console.print(f"  Created: {INFRACONTEXT_DIR}/")
    console.print()
    console.print("[dim]Next steps:[/dim]")
    console.print(f"  1. Add '{LOCAL_OVERRIDES_FILE}' to .gitignore")
    console.print("  2. Create a project: ic describe project create <name>")
    console.print("  3. Add nodes: ic describe node create --type vm --name 'My Server'")


if __name__ == "__main__":
    app()
