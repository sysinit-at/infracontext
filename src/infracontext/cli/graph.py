"""Graph analysis commands for understanding infrastructure dependencies."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from infracontext.cli import require_environment, require_project
from infracontext.cli.completion import complete_node_id
from infracontext.cli.resolve import resolve_node_or_exit
from infracontext.graph.analysis import calculate_impact, find_cycles, find_orphans, find_spofs
from infracontext.graph.loader import load_graph, load_merged_graph, unqualify_node_id
from infracontext.graph.query import get_all_paths, get_downstream, get_upstream
from infracontext.graph.render import (
    graph_to_mermaid,
    mermaid_size_warning,
    render_graphml,
    render_html,
    render_mermaid,
    render_svg,
)

app = typer.Typer(no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)


class RenderFormat(StrEnum):
    """Output formats supported by `ic graph render`."""

    HTML = "html"
    THREED = "3d"
    SVG = "svg"
    GRAPHML = "graphml"
    MERMAID = "mermaid"


# Default file extension per format, where it differs from the format name.
_FORMAT_EXTENSIONS = {RenderFormat.MERMAID: "mmd", RenderFormat.THREED: "3d.html"}


def _load_graph(all_projects: bool):
    """Load a single-project or merged graph depending on the flag."""
    if all_projects:
        require_environment()
        return load_merged_graph()
    return load_graph(require_project())



@app.command("analyze")
def analyze(
    node_id: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or fuzzy query", autocompletion=complete_node_id),
    ],
    upstream: Annotated[bool, typer.Option("--upstream", "-u", help="Show upstream dependencies")] = False,
    downstream: Annotated[bool, typer.Option("--downstream", "-d", help="Show downstream dependents")] = False,
    paths_to: Annotated[str | None, typer.Option("--paths-to", help="Find all paths to another node")] = None,
    depth: Annotated[int | None, typer.Option("--depth", help="Maximum traversal depth")] = None,
) -> None:
    """Analyze node dependencies and relationships."""
    # A bare query is fuzzy-resolved against the active project; an explicit
    # ``type:slug`` is left as typed and matched against the graph directly.
    if ":" not in node_id:
        node_id = resolve_node_or_exit(node_id).node_id
    project = require_project()
    graph = load_graph(project)

    if node_id not in graph:
        console.print(f"[red]Node '{node_id}' not found.[/red]")
        raise typer.Exit(1)

    node_data = graph.nodes[node_id]
    console.print(f"[bold]Analyzing: {node_data.get('name', node_id)}[/bold] ({node_id})")
    console.print()

    # If no specific option, show both
    if not upstream and not downstream and not paths_to:
        upstream = True
        downstream = True

    if upstream:
        up_nodes = get_upstream(graph, node_id, max_depth=depth)
        console.print(f"[cyan]Upstream dependencies ({len(up_nodes)}):[/cyan]")
        if up_nodes:
            for n in sorted(up_nodes):
                n_data = graph.nodes[n]
                console.print(f"  - {n_data.get('name', n)} ({n})")
        else:
            console.print("  [dim]None[/dim]")
        console.print()

    if downstream:
        down_nodes = get_downstream(graph, node_id, max_depth=depth)
        console.print(f"[cyan]Downstream dependents ({len(down_nodes)}):[/cyan]")
        if down_nodes:
            for n in sorted(down_nodes):
                n_data = graph.nodes[n]
                console.print(f"  - {n_data.get('name', n)} ({n})")
        else:
            console.print("  [dim]None[/dim]")
        console.print()

    if paths_to:
        if paths_to not in graph:
            console.print(f"[red]Target node '{paths_to}' not found.[/red]")
            raise typer.Exit(1)

        paths = get_all_paths(graph, node_id, paths_to)
        console.print(f"[cyan]Paths to {paths_to} ({len(paths)}):[/cyan]")
        if paths:
            for i, path in enumerate(paths, 1):
                path_str = " -> ".join(path)
                console.print(f"  {i}. {path_str}")
        else:
            console.print("  [dim]No paths found[/dim]")


@app.command("impact")
def impact(
    node_id: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or fuzzy query", autocompletion=complete_node_id),
    ],
    all_projects: Annotated[
        bool, typer.Option("--all-projects", "-A", help="Analyze across all projects")
    ] = False,
) -> None:
    """Analyze impact if a node fails.

    With --all-projects, uses qualified node IDs (project/type:slug) and
    shows which projects are affected.
    """
    # Fuzzy-resolve a bare query against the active project. Qualified/explicit
    # IDs (which contain ':') pass through unchanged — including the
    # ``project/type:slug`` form used under --all-projects.
    if ":" not in node_id:
        node_id = resolve_node_or_exit(node_id).node_id
    graph = _load_graph(all_projects)

    if node_id not in graph:
        console.print(f"[red]Node '{node_id}' not found.[/red]")
        if all_projects:
            console.print("[dim]With --all-projects, use qualified IDs: project/type:slug[/dim]")
        raise typer.Exit(1)

    result = calculate_impact(graph, node_id)

    console.print(f"[bold]Impact Analysis: {result['node_name']}[/bold] ({result['node_id']})")
    console.print()
    console.print(f"  [dim]Type:[/dim] {result['node_type']}")
    console.print()
    console.print(f"  [cyan]Direct dependents:[/cyan] {result['direct_dependents']}")
    console.print(f"  [cyan]Total affected:[/cyan] {result['total_affected']}")
    console.print(f"  [yellow]Applications affected:[/yellow] {result['applications_affected']}")

    if result["affected_nodes"]:
        console.print()

        if all_projects:
            # Group affected nodes by project
            by_project: dict[str, list[str]] = {}
            for n in result["affected_nodes"][:20]:
                proj, nid = unqualify_node_id(n)
                by_project.setdefault(proj or "(unknown)", []).append(nid)

            console.print("[dim]Affected nodes by project (first 20):[/dim]")
            for proj in sorted(by_project):
                console.print(f"  [cyan]{proj}:[/cyan]")
                for nid in sorted(by_project[proj]):
                    name = graph.nodes.get(f"{proj}/{nid}", {}).get("name", nid)
                    console.print(f"    - {name} ({nid})")

            console.print()
            console.print(f"  [yellow]Projects affected:[/yellow] {len(by_project)}")
        else:
            console.print("[dim]Affected nodes (first 20):[/dim]")
            for n in result["affected_nodes"][:20]:
                console.print(f"  - {n}")


@app.command("spof")
def spof(
    min_affected: Annotated[int, typer.Option("--min", "-m", help="Minimum affected nodes to report")] = 2,
    all_projects: Annotated[
        bool, typer.Option("--all-projects", "-A", help="Analyze across all projects")
    ] = False,
) -> None:
    """Find single points of failure in the infrastructure."""
    graph = _load_graph(all_projects)

    if all_projects:
        console.print("[bold]Single Points of Failure across all projects[/bold]")
    else:
        project = require_project()
        console.print(f"[bold]Single Points of Failure in {project}[/bold]")
    console.print()

    spofs = find_spofs(graph, min_affected=min_affected)

    if not spofs:
        console.print("[green]No significant single points of failure detected.[/green]")
        return

    table = Table()
    if all_projects:
        table.add_column("Project", style="dim")
    table.add_column("Node", style="cyan")
    table.add_column("Type")
    table.add_column("Affected", style="yellow")

    for s in spofs[:20]:
        if all_projects:
            proj, nid = unqualify_node_id(s.node_id)
            table.add_row(
                proj,
                f"{s.node_name}\n[dim]{nid}[/dim]",
                s.node_type,
                str(s.affected_count),
            )
        else:
            table.add_row(
                f"{s.node_name}\n[dim]{s.node_id}[/dim]",
                s.node_type,
                str(s.affected_count),
            )

    console.print(table)


@app.command("cycles")
def cycles(
    all_projects: Annotated[
        bool, typer.Option("--all-projects", "-A", help="Analyze across all projects")
    ] = False,
) -> None:
    """Detect circular dependencies."""
    graph = _load_graph(all_projects)

    if all_projects:
        console.print("[bold]Circular Dependencies across all projects[/bold]")
    else:
        project = require_project()
        console.print(f"[bold]Circular Dependencies in {project}[/bold]")
    console.print()

    found_cycles = find_cycles(graph)

    if not found_cycles:
        console.print("[green]No circular dependencies detected.[/green]")
        return

    console.print(f"[red]Found {len(found_cycles)} circular dependencies:[/red]")
    console.print()

    for i, c in enumerate(found_cycles[:10], 1):
        cycle_str = " -> ".join(c.node_names) + f" -> {c.node_names[0]}"
        console.print(f"  {i}. {cycle_str}")


@app.command("orphans")
def orphans(
    all_projects: Annotated[
        bool, typer.Option("--all-projects", "-A", help="Analyze across all projects")
    ] = False,
) -> None:
    """Find orphaned nodes with no relationships."""
    graph = _load_graph(all_projects)

    if all_projects:
        console.print("[bold]Orphaned Nodes across all projects[/bold]")
    else:
        project = require_project()
        console.print(f"[bold]Orphaned Nodes in {project}[/bold]")
    console.print()

    found_orphans = find_orphans(graph)

    if not found_orphans:
        console.print("[green]No orphaned nodes detected.[/green]")
        return

    table = Table()
    if all_projects:
        table.add_column("Project", style="dim")
    table.add_column("Node", style="cyan")
    table.add_column("Type")

    for o in found_orphans:
        if all_projects:
            proj, nid = unqualify_node_id(o.node_id)
            table.add_row(
                proj,
                f"{o.node_name}\n[dim]{nid}[/dim]",
                o.node_type,
            )
        else:
            table.add_row(
                f"{o.node_name}\n[dim]{o.node_id}[/dim]",
                o.node_type,
            )

    console.print(table)
    console.print()
    console.print(f"[dim]Total orphans: {len(found_orphans)}[/dim]")


@app.command("render")
def render(
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o",
            help="Output file (default: infracontext-graph.<ext> in the current "
            "directory). With --format mermaid, '-' writes to stdout.",
        ),
    ] = None,
    fmt: Annotated[
        RenderFormat,
        typer.Option("--format", "-f", help="Output format"),
    ] = RenderFormat.HTML,
    all_projects: Annotated[
        bool,
        typer.Option("--all-projects", "-A", help="Render across all projects and external roots"),
    ] = False,
    title: Annotated[
        str | None,
        typer.Option("--title", help="Diagram title (defaults to the project name)"),
    ] = None,
    cdn: Annotated[
        bool,
        typer.Option(
            "--cdn",
            help="HTML only: load vis-network from the pinned CDN URL instead of "
            "inlining it (much smaller file, but needs internet to view)",
        ),
    ] = False,
    open_after: Annotated[
        bool,
        typer.Option("--open", help="Open the rendered file with the default application"),
    ] = False,
) -> None:
    """Render the infrastructure graph as HTML, 3D, SVG, GraphML, or mermaid.

    --format 3d writes a self-contained interactive 3D page (three.js
    inlined) with click-to-simulate outage mode: selecting a node highlights
    its precomputed blast radius, consistent with `ic graph impact`.

    The HTML output is interactive and self-contained — vis-network is
    inlined so the file opens offline (use --cdn for a smaller file that
    loads the pinned CDN URL instead). SVG is static and embeddable in
    markdown — requires the ``viz`` extra (``pip install 'infracontext[viz]'``).
    GraphML opens in Gephi, yEd, and Cytoscape. Mermaid emits flowchart text
    for markdown code fences (``-o -`` pipes it to stdout).
    """
    if all_projects:
        require_environment()
        graph = load_merged_graph()
        default_title = "All projects"
    else:
        project = require_project()
        graph = load_graph(project)
        default_title = project

    to_stdout = output is not None and str(output) == "-"
    if to_stdout and fmt is not RenderFormat.MERMAID:
        console.print("[red]--output - (stdout) is only supported with --format mermaid.[/red]")
        raise typer.Exit(1)

    resolved_title = title or default_title
    ext = _FORMAT_EXTENSIONS.get(fmt, fmt.value)
    out_path = output or Path(f"infracontext-graph.{ext}")

    if fmt is RenderFormat.MERMAID:
        # Warn (never refuse) on graphs too big for mermaid renderers. Goes
        # to stderr so `-o -` piping stays clean.
        warning = mermaid_size_warning(graph)
        if warning:
            err_console.print(f"[yellow]{warning}[/yellow]")

    if to_stdout:
        print(graph_to_mermaid(graph), end="")
        return

    if graph.number_of_nodes() == 0:
        console.print("[yellow]Graph is empty — no nodes to render.[/yellow]")

    # Signal but don't refuse: rendering is a workflow people repeat (edit
    # YAML → re-render). A yellow line is enough to flag accidental clobbers
    # without making `-o /existing/file` a two-step ceremony.
    overwriting = out_path.exists()

    try:
        if fmt is RenderFormat.HTML:
            render_html(graph, out_path, title=resolved_title, inline_js=not cdn)
        elif fmt is RenderFormat.THREED:
            from infracontext.graph.render3d import render_html_3d

            render_html_3d(graph, out_path, title=resolved_title)
        elif fmt is RenderFormat.SVG:
            render_svg(graph, out_path, title=resolved_title)
        elif fmt is RenderFormat.MERMAID:
            render_mermaid(graph, out_path)
        else:
            render_graphml(graph, out_path)
    except (ImportError, ValueError) as e:
        # ImportError: missing viz extra. ValueError: SVG node-limit guard
        # or an unsafe vendored bundle. All carry actionable messages;
        # surface them without a traceback.
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None

    verb = "Overwrote" if overwriting else "Rendered"
    console.print(
        f"[green]{verb}[/green] {graph.number_of_nodes()} nodes, "
        f"{graph.number_of_edges()} edges → [cyan]{out_path}[/cyan]"
    )

    if open_after:
        import webbrowser

        webbrowser.open(out_path.resolve().as_uri())
