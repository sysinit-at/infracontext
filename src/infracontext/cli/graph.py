"""Graph analysis commands for understanding infrastructure dependencies."""

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from infracontext.cli import require_environment, require_project
from infracontext.graph.analysis import calculate_impact, find_cycles, find_orphans, find_spofs
from infracontext.graph.loader import load_graph, load_merged_graph, unqualify_node_id
from infracontext.graph.query import get_all_paths, get_downstream, get_upstream

app = typer.Typer(no_args_is_help=True)
console = Console()


def _load_graph(all_projects: bool):
    """Load a single-project or merged graph depending on the flag."""
    if all_projects:
        require_environment()
        return load_merged_graph()
    return load_graph(require_project())



@app.command("analyze")
def analyze(
    node_id: Annotated[str, typer.Argument(help="Node ID to analyze")],
    upstream: Annotated[bool, typer.Option("--upstream", "-u", help="Show upstream dependencies")] = False,
    downstream: Annotated[bool, typer.Option("--downstream", "-d", help="Show downstream dependents")] = False,
    paths_to: Annotated[str | None, typer.Option("--paths-to", help="Find all paths to another node")] = None,
    depth: Annotated[int | None, typer.Option("--depth", help="Maximum traversal depth")] = None,
) -> None:
    """Analyze node dependencies and relationships."""
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
    node_id: Annotated[str, typer.Argument(help="Node ID to analyze impact for")],
    all_projects: Annotated[
        bool, typer.Option("--all-projects", "-A", help="Analyze across all projects")
    ] = False,
) -> None:
    """Analyze impact if a node fails.

    With --all-projects, uses qualified node IDs (project/type:slug) and
    shows which projects are affected.
    """
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
