"""Migration commands for importing data from legacy locations."""

import shutil
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from infracontext.paths import EnvironmentNotFoundError, EnvironmentPaths, ProjectPaths

app = typer.Typer(no_args_is_help=True)
console = Console()

# Legacy data locations
LEGACY_PATHS = [
    Path.home() / ".local" / "share" / "sysplainer-ng",
    Path.home() / ".local" / "share" / "infracontext",
]


def _find_legacy_data() -> Path | None:
    """Find legacy data directory if it exists."""
    for legacy_path in LEGACY_PATHS:
        # Check for both old "tenants" dir and new "projects" dir
        if legacy_path.exists() and ((legacy_path / "tenants").is_dir() or (legacy_path / "projects").is_dir()):
            return legacy_path
    return None


def _list_legacy_projects(legacy_root: Path) -> list[str]:
    """List project slugs in the legacy location."""
    # Support both old "tenants" and new "projects" directory names
    projects_dir = legacy_root / "tenants"
    if not projects_dir.exists():
        projects_dir = legacy_root / "projects"
    if not projects_dir.exists():
        return []

    projects = []
    # Handle hierarchical projects (customer/project)
    for item in projects_dir.iterdir():
        if not item.is_dir():
            continue
        # Check if this is a direct project (has nodes/ dir) or a customer dir
        if (item / "nodes").is_dir():
            projects.append(item.name)
        else:
            # Check subdirectories for projects
            for sub in item.iterdir():
                if sub.is_dir() and (sub / "nodes").is_dir():
                    projects.append(f"{item.name}/{sub.name}")

    return sorted(projects)


@app.command("legacy")
def migrate_legacy(
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Specific project to migrate (default: all)"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would be migrated without making changes"),
    ] = False,
) -> None:
    """Migrate data from legacy ~/.local/share location.

    Finds data from the old sysplainer-ng or infracontext storage location
    and copies it to the current environment's .infracontext/ directory.

    Examples:
        ic migrate legacy                    # Migrate all projects
        ic migrate legacy -p acme/prod       # Migrate specific project
        ic migrate legacy --dry-run          # Preview what would be migrated
    """
    # Ensure we're in an environment
    try:
        environment = EnvironmentPaths.current()
    except EnvironmentNotFoundError:
        console.print("[red]Not in an infracontext environment. Run 'ic init' first.[/red]")
        raise typer.Exit(1) from None

    # Find legacy data
    legacy_root = _find_legacy_data()
    if legacy_root is None:
        console.print("[yellow]No legacy data found.[/yellow]")
        console.print(f"[dim]Checked: {', '.join(str(p) for p in LEGACY_PATHS)}[/dim]")
        raise typer.Exit(0)

    console.print(f"[cyan]Found legacy data at: {legacy_root}[/cyan]")

    # List projects to migrate
    legacy_projects = _list_legacy_projects(legacy_root)
    if not legacy_projects:
        console.print("[yellow]No projects found in legacy location.[/yellow]")
        raise typer.Exit(0)

    # Filter to specific project if requested
    if project:
        if project not in legacy_projects:
            console.print(f"[red]Project '{project}' not found in legacy data.[/red]")
            console.print(f"[dim]Available: {', '.join(legacy_projects)}[/dim]")
            raise typer.Exit(1)
        projects_to_migrate = [project]
    else:
        projects_to_migrate = legacy_projects

    console.print(f"[dim]Projects to migrate: {', '.join(projects_to_migrate)}[/dim]")
    console.print()

    if dry_run:
        console.print("[yellow]DRY RUN - no changes will be made[/yellow]")
        console.print()

    migrated = 0
    skipped = 0

    for p in projects_to_migrate:
        # Support both old "tenants" and new "projects" directory names
        legacy_project_path = legacy_root / "tenants" / p
        if not legacy_project_path.exists():
            legacy_project_path = legacy_root / "projects" / p
        new_paths = ProjectPaths.for_project(p, environment)

        if new_paths.root.exists():
            console.print(f"  [yellow]SKIP[/yellow] {p} (already exists)")
            skipped += 1
            continue

        if dry_run:
            console.print(f"  [cyan]WOULD MIGRATE[/cyan] {p}")
            # Count what would be migrated
            node_count = 0
            if (legacy_project_path / "nodes").exists():
                for type_dir in (legacy_project_path / "nodes").iterdir():
                    if type_dir.is_dir():
                        node_count += len(list(type_dir.glob("*.yaml")))
            has_rels = (legacy_project_path / "relationships.yaml").exists()
            console.print(f"    [dim]{node_count} nodes, relationships: {has_rels}[/dim]")
        else:
            # Perform the migration
            console.print(f"  [green]MIGRATE[/green] {p}")

            # Create parent directories
            new_paths.root.parent.mkdir(parents=True, exist_ok=True)

            # Copy the entire project directory
            shutil.copytree(legacy_project_path, new_paths.root)

            migrated += 1

    console.print()
    if dry_run:
        console.print(f"[cyan]Would migrate {len(projects_to_migrate) - skipped} project(s), skip {skipped}[/cyan]")
    else:
        console.print(f"[green]Migrated {migrated} project(s), skipped {skipped}[/green]")

        if migrated > 0:
            console.print()
            console.print("[dim]Next steps:[/dim]")
            console.print("  1. Review migrated data in .infracontext/projects/")
            console.print("  2. Set active project: ic describe project switch <name>")
            console.print("  3. Optionally delete legacy data (after verification)")


@app.command("status")
def migrate_status() -> None:
    """Show migration status and legacy data locations."""
    console.print("[bold]Migration Status[/bold]")
    console.print()

    # Check environment
    try:
        environment = EnvironmentPaths.current()
        console.print(f"  [green]Environment:[/green] {environment.root}")
    except EnvironmentNotFoundError:
        console.print("  [red]Environment:[/red] Not initialized (run 'ic init')")
        environment = None

    console.print()

    # Check legacy locations
    console.print("[bold]Legacy Data Locations[/bold]")
    for legacy_path in LEGACY_PATHS:
        if legacy_path.exists():
            projects = _list_legacy_projects(legacy_path)
            if projects:
                console.print(f"  [yellow]{legacy_path}[/yellow]")
                console.print(f"    [dim]Projects: {', '.join(projects)}[/dim]")
            else:
                console.print(f"  [dim]{legacy_path}[/dim] (exists but no projects)")
        else:
            console.print(f"  [dim]{legacy_path}[/dim] (not found)")

    if environment:
        console.print()
        console.print("[dim]Run 'ic migrate legacy' to import legacy data.[/dim]")
