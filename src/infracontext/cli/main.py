"""Main CLI entry point for infracontext."""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from infracontext import __version__
from infracontext.cli import config, describe, doctor, graph, import_cmd, mcp, migrate, query, triage_cmd
from infracontext.cli.completion import complete_node_id, complete_project
from infracontext.cli.describe import OutputFormat
from infracontext.paths import INFRACONTEXT_DIR, LOCAL_OVERRIDES_FILE, EnvironmentPaths, find_environment_root

app = typer.Typer(
    name="ic",
    help="Infrastructure context for humans and agents",
    no_args_is_help=True,
    rich_markup_mode="rich",
    # Locals can hold credentials/tokens (query commands resolve bearer tokens
    # etc.); never dump them in a traceback.
    pretty_exceptions_show_locals=False,
)

console = Console()

# Register sub-command groups
app.add_typer(describe.app, name="describe", help="Document and manage infrastructure")
app.add_typer(import_cmd.app, name="import", help="Import infrastructure from sources")
app.add_typer(graph.app, name="graph", help="Analyze infrastructure graph")
app.add_typer(config.app, name="config", help="Manage configuration and credentials")
app.add_typer(migrate.app, name="migrate", help="Migrate data from legacy locations")
app.add_typer(query.app, name="query", help="Query monitoring sources")
app.add_typer(doctor.app, name="doctor", help="Validate infrastructure data")
app.add_typer(mcp.app, name="mcp", help="Run the MCP server for agents")
app.add_typer(triage_cmd.app, name="triage", help="Bundled triage checker checklists for agents")


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", "-v", help="Show version and exit"),
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            "-p",
            help="Project to use (overrides config)",
            envvar="IC_PROJECT",
            autocompletion=complete_project,
        ),
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


def _ensure_gitignored(root: Path, entry: str) -> None:
    """Ensure ``entry`` appears as its own line in ``root/.gitignore``.

    Creates the file if missing, appends the line if absent, and is a no-op if
    an exact-line match already exists. Existing content and trailing newline
    conventions are preserved.
    """
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(f"{entry}\n", encoding="utf-8")
        return

    text = gitignore.read_text(encoding="utf-8")
    if entry in (line.strip() for line in text.splitlines()):
        return

    separator = "" if text == "" or text.endswith("\n") else "\n"
    gitignore.write_text(f"{text}{separator}{entry}\n", encoding="utf-8")


@app.command()
def init(
    path: Annotated[
        Path | None,
        typer.Argument(help="Directory to initialize (default: current directory)"),
    ] = None,
) -> None:
    """Initialize infracontext in the current directory.

    Creates a .infracontext/ directory structure for storing infrastructure
    documentation and gitignores the local-overrides file.
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

    # Keep advisory-lock and atomic-write droppings out of the user's git
    # status. These are created next to every YAML the tool writes and are
    # never meaningful to commit.
    gitignore = environment.infracontext_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# Advisory-lock and atomic-write temp files created by infracontext.\n"
            "**/.*.lock\n"
            "**/.*.tmp\n"
        )

    # The local-overrides file holds machine-specific settings (ssh_alias,
    # source_paths) and must never be committed -- do it for the user rather
    # than leaving it as a manual "next step" they can forget.
    _ensure_gitignored(target, LOCAL_OVERRIDES_FILE)

    console.print(f"[green]Initialized infracontext in {target}[/green]")
    console.print(f"  Created: {INFRACONTEXT_DIR}/")
    console.print(f"  [green]✓[/green] added '{LOCAL_OVERRIDES_FILE}' to .gitignore")
    console.print()
    console.print("[dim]Next steps:[/dim]")
    console.print("  1. Create a project: ic describe project create <name>")
    console.print("  2. Add nodes: ic describe node add <ssh-alias>")
    console.print(f"  3. Register globally: ic config env add <name> {target} --default")


@app.command()
def switch(
    name: Annotated[
        str,
        typer.Argument(help="Project name to switch to", autocompletion=complete_project),
    ],
) -> None:
    """Switch active project (shortcut for 'describe project switch')."""
    from infracontext.cli import require_environment
    from infracontext.config import set_active_project
    from infracontext.paths import InvalidProjectSlugError, project_exists, validate_project_slug

    require_environment()

    try:
        slug = validate_project_slug(name.lower().replace(" ", "-"))
    except InvalidProjectSlugError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None

    if not project_exists(slug):
        from infracontext.cli import _suggest_projects

        console.print(f"[red]Project '{slug}' not found.[/red]")
        if suggestions := _suggest_projects(slug):
            console.print(f"[yellow]Did you mean: {', '.join(suggestions)}?[/yellow]")
        raise typer.Exit(1)

    set_active_project(slug)
    console.print(f"[green]Switched to project '{slug}'[/green]")


@app.command(
    "ssh",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def ssh(
    ctx: typer.Context,
    query: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or fuzzy query", autocompletion=complete_node_id),
    ],
    no_banner: Annotated[bool, typer.Option("--no-banner", help="Skip the context banner")] = False,
) -> None:
    """SSH into a node, resolving it fuzzily.

    Prints a short context banner to stderr, then execs ssh. Extra arguments
    after the query are run as a remote command:

        ic ssh web              # interactive shell on the 'web' node
        ic ssh web uptime       # run 'uptime' on it
        ic ssh web --no-banner  # connect without the banner
    """
    from infracontext.cli.ssh import run_ssh

    run_ssh(query, command_args=list(ctx.args), no_banner=no_banner)


@app.command("learn")
def learn(
    query: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or fuzzy query", autocompletion=complete_node_id),
    ],
    finding: Annotated[
        str | None,
        typer.Argument(help="What you learned (opens $EDITOR if omitted)"),
    ] = None,
    context: Annotated[
        str, typer.Option("--context", "-c", help="What was being investigated")
    ] = "manual note",
) -> None:
    """Record a learning on a node (human shortcut for 'describe node learning').

    Examples:
        ic learn web-01 "PHP-FPM pool was misconfigured"
        ic learn web-01            # opens $EDITOR for a longer note
    """
    from infracontext.cli.learn import run_learn

    run_learn(query, finding, context)


@app.command("ctx")
def ctx(
    query: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or fuzzy query", autocompletion=complete_node_id),
    ],
    include_relationships: Annotated[
        bool, typer.Option("--relationships", "-r", help="Include relationships")
    ] = True,
    include_learnings: Annotated[bool, typer.Option("--learnings", "-l", help="Include learnings")] = True,
    fmt: Annotated[OutputFormat, typer.Option("--format", "-f", help="Output format")] = OutputFormat.yaml,
    json_out: Annotated[bool, typer.Option("--json", help="Shorthand for --format json")] = False,
) -> None:
    """Show full node context for triage (shortcut for 'describe node context')."""
    from infracontext.cli.describe import run_node_context

    run_node_context(
        query,
        include_relationships=include_relationships,
        include_learnings=include_learnings,
        fmt=OutputFormat.json if json_out else fmt,
    )


@app.command("find")
def find(
    query: Annotated[
        str,
        typer.Argument(help="Search query (domain, IP, name, SSH alias, or node ID)", autocompletion=complete_node_id),
    ],
    show_all: Annotated[bool, typer.Option("--all", "-a", help="Show all matches, not just first")] = False,
    all_roots_flag: Annotated[
        bool,
        typer.Option("--all-roots", "-A", help="Search across the local root and all external roots"),
    ] = False,
    output_json: Annotated[bool, typer.Option("--json", help="Output matches as JSON")] = False,
) -> None:
    """Find nodes by domain, IP, name, SSH alias, or ID (shortcut for 'describe node find')."""
    from infracontext.cli.describe import node_find

    node_find(query=query, show_all=show_all, all_roots_flag=all_roots_flag, output_json=output_json)


@app.command("status")
def status(
    query: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or fuzzy query", autocompletion=complete_node_id),
    ],
    json_out: Annotated[bool, typer.Option("--json", help="Emit aggregated JSON")] = False,
) -> None:
    """Query all monitoring sources for a node (shortcut for 'query status')."""
    from infracontext.cli.query import query_status

    query_status(node_id=query, output_json=json_out)


if __name__ == "__main__":
    app()
