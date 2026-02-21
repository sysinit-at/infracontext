"""Import commands for infracontext."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

import infracontext.sources  # noqa: F401 - triggers plugin registration
from infracontext.cli import require_project
from infracontext.paths import ProjectPaths
from infracontext.sources.registry import get_plugin_instance
from infracontext.sources.ssh_config import derive_config_path_from_project
from infracontext.storage import read_yaml, write_yaml

app = typer.Typer(
    name="import",
    help="Import infrastructure from various sources",
    no_args_is_help=True,
)

console = Console()


@app.command("ssh")
def import_ssh(
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Explicit path to SSH config file"),
    ] = None,
    source_name: Annotated[
        str,
        typer.Option("--name", "-n", help="Name for the source (default: ssh-config)"),
    ] = "ssh-config",
) -> None:
    """Import hosts from SSH config file.

    If no path is provided, auto-discovers based on project hierarchy:
    Project <customer>/<project> → ~/.ssh/conf.d/<customer>/<project>.conf
    """
    project = require_project()
    paths = ProjectPaths.for_project(project)

    # Determine config path
    if path:
        config_path = path.expanduser()
    else:
        config_path = derive_config_path_from_project(project)
        if not config_path:
            console.print("[red]Cannot derive SSH config path from project.[/red]")
            console.print(f"[dim]Project '{project}' is not hierarchical (needs customer/project format).[/dim]")
            console.print("[dim]Use --path to specify the SSH config file explicitly.[/dim]")
            raise typer.Exit(1)

    if not config_path.exists():
        console.print(f"[red]SSH config file not found: {config_path}[/red]")
        raise typer.Exit(1)

    console.print(f"[cyan]Importing from {config_path}...[/cyan]")

    # Create or update source configuration
    try:
        source_file = paths.source_file(source_name)
    except ValueError as e:
        console.print(f"[red]Invalid source name '{source_name}': {e}[/red]")
        raise typer.Exit(1) from None
    paths.sources_dir.mkdir(exist_ok=True)

    if source_file.exists():
        config = read_yaml(source_file)
        console.print(f"[dim]Using existing source '{source_name}'[/dim]")
    else:
        config = {
            "version": "2.0",
            "name": source_name,
            "type": "ssh_config",
            "status": "configured",
            "config_path": str(config_path) if path else None,  # Only store if explicit
            "default_node_type": "vm",
            "type_patterns": {
                "physical_host": ["^pve-", "^proxmox-"],
                "lxc_container": ["^ct-", "^lxc-"],
            },
        }
        write_yaml(source_file, config)
        console.print(f"[green]Created source '{source_name}'[/green]")

    # Run sync
    plugin = get_plugin_instance("ssh_config")
    if not plugin:
        console.print("[red]SSH config plugin not found.[/red]")
        raise typer.Exit(1)

    result = plugin.sync(project, source_name)

    if result.status == "success":
        console.print("[green]Import completed successfully[/green]")
    elif result.status == "partial":
        console.print("[yellow]Import completed with warnings[/yellow]")
    else:
        console.print(f"[red]Import failed: {result.message}[/red]")
        raise typer.Exit(1)

    console.print(f"  Nodes created: {result.nodes_created}")
    console.print(f"  Nodes updated: {result.nodes_updated}")
    console.print(f"  Duration: {result.duration_ms}ms")
