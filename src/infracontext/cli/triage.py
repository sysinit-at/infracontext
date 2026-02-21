"""Triage and troubleshooting commands.

Graph analysis commands for understanding infrastructure dependencies.
Actual triage (SSH, logs) is performed by Claude Code agents using the skill.
"""

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from infracontext.cli import require_project
from infracontext.graph.analysis import calculate_impact, find_cycles, find_orphans, find_spofs
from infracontext.graph.loader import load_graph
from infracontext.graph.query import get_all_paths, get_downstream, get_upstream

app = typer.Typer(no_args_is_help=True)
console = Console()


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
) -> None:
    """Analyze impact if a node fails."""
    project = require_project()
    graph = load_graph(project)

    if node_id not in graph:
        console.print(f"[red]Node '{node_id}' not found.[/red]")
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
        console.print("[dim]Affected nodes (first 20):[/dim]")
        for n in result["affected_nodes"][:20]:
            console.print(f"  - {n}")


@app.command("spof")
def spof(
    min_affected: Annotated[int, typer.Option("--min", "-m", help="Minimum affected nodes to report")] = 2,
) -> None:
    """Find single points of failure in the infrastructure."""
    project = require_project()
    graph = load_graph(project)

    console.print(f"[bold]Single Points of Failure in {project}[/bold]")
    console.print()

    spofs = find_spofs(graph, min_affected=min_affected)

    if not spofs:
        console.print("[green]No significant single points of failure detected.[/green]")
        return

    table = Table()
    table.add_column("Node", style="cyan")
    table.add_column("Type")
    table.add_column("Affected", style="yellow")

    for s in spofs[:20]:
        table.add_row(
            f"{s.node_name}\n[dim]{s.node_id}[/dim]",
            s.node_type,
            str(s.affected_count),
        )

    console.print(table)


@app.command("cycles")
def cycles() -> None:
    """Detect circular dependencies."""
    project = require_project()
    graph = load_graph(project)

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
def orphans() -> None:
    """Find orphaned nodes with no relationships."""
    project = require_project()
    graph = load_graph(project)

    console.print(f"[bold]Orphaned Nodes in {project}[/bold]")
    console.print()

    found_orphans = find_orphans(graph)

    if not found_orphans:
        console.print("[green]No orphaned nodes detected.[/green]")
        return

    table = Table()
    table.add_column("Node", style="cyan")
    table.add_column("Type")

    for o in found_orphans:
        table.add_row(
            f"{o.node_name}\n[dim]{o.node_id}[/dim]",
            o.node_type,
        )

    console.print(table)
    console.print()
    console.print(f"[dim]Total orphans: {len(found_orphans)}[/dim]")
