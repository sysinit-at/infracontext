"""Shared fuzzy node resolution for the hot-path commands.

The addressing rules that every node-taking command shares:

- An argument containing ``:`` (``type:slug`` or ``@alias:type:slug``) is an
  exact address and takes the fast path through
  :func:`infracontext.cli.describe._resolve_node_target` -- no directory scan.
- Anything else is a fuzzy query, matched against the active project's nodes
  with the same predicate ``ic describe node find`` uses. One hit resolves;
  several print a compact candidates table and exit; none prints a
  did-you-mean suggestion and exits.

This keeps ``ic ssh web``, ``ic ctx web``, ``ic query status web`` etc. usable
during an incident without forcing the operator to type the full ``vm:web-01``.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from infracontext.cli import require_environment, require_project
from infracontext.models.node import Node
from infracontext.paths import ProjectPaths

console = Console()


def resolve_node_or_exit(query: str, *, require_writable: bool = False):
    """Resolve a node query to a ``_NodeTarget`` or exit with a clear error.

    ``query`` containing ``:`` is treated as an exact address (fast path,
    identical to the pre-existing behavior). Otherwise it is fuzzy-matched
    against the active project's nodes.

    Returns the same ``_NodeTarget`` structure
    :func:`infracontext.cli.describe._resolve_node_target` returns, so callers
    can treat exact and fuzzy inputs uniformly.
    """
    # Imported lazily: describe.py imports this module at load time, so a
    # top-level import here would be circular.
    from infracontext.cli.describe import (
        _iter_all_nodes,
        _node_matches_query,
        _NodeTarget,
        _resolve_node_target,
    )
    from infracontext.federation import LOCAL_ROOT_ALIAS

    # Fast path: an explicit address. Never scans.
    if ":" in query:
        return _resolve_node_target(query, require_writable=require_writable)

    environment = require_environment()
    project = require_project()
    paths = ProjectPaths.for_project(project, environment)

    matches: list[tuple[Node, str]] = []
    for node in _iter_all_nodes(paths, environment, project):
        ok, reason = _node_matches_query(node, query)
        if ok:
            matches.append((node, reason))

    if len(matches) == 1:
        node = matches[0][0]
        return _NodeTarget(
            paths=paths,
            environment=environment,
            project=project,
            node_id=node.id,
            root_alias=LOCAL_ROOT_ALIAS,
            writable=True,
        )

    if len(matches) > 1:
        table = Table(title=f"Multiple nodes match '{query}'")
        table.add_column("ID", style="cyan")
        table.add_column("Name")
        table.add_column("Matched on")
        for node, reason in matches:
            table.add_row(node.id, node.name, reason)
        console.print(table)
        console.print(
            "[yellow]Be more specific, or use the exact 'type:slug' ID.[/yellow]"
        )
        raise typer.Exit(1)

    # Zero matches — offer a did-you-mean over the project's node IDs/slugs.
    console.print(f"[red]No node matches '{query}' in project '{project}'.[/red]")
    if suggestions := _suggest_nodes(query, paths, environment, project):
        console.print(f"[yellow]Did you mean: {', '.join(suggestions)}?[/yellow]")
    else:
        console.print(
            f"[dim]List nodes with: ic describe node list  (project '{project}')[/dim]"
        )
    raise typer.Exit(1)


def _suggest_nodes(query: str, paths: ProjectPaths, environment, project) -> list[str]:
    """Suggest node IDs close to ``query`` for a failed fuzzy lookup."""
    from difflib import get_close_matches

    from infracontext.cli.describe import _iter_all_nodes

    try:
        nodes = _iter_all_nodes(paths, environment, project)
    except Exception:
        return []

    # Match against both slugs and full IDs so "web" surfaces "vm:web-01"
    # whether the operator was thinking of the slug or the address.
    by_slug = {node.slug: node.id for node in nodes}
    slug_hits = get_close_matches(query, list(by_slug), n=3, cutoff=0.5)
    if slug_hits:
        return [by_slug[s] for s in slug_hits]
    return get_close_matches(query, [node.id for node in nodes], n=3, cutoff=0.5)
