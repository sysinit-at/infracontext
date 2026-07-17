"""System description commands: project, node, relationship, source management."""

import json
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from infracontext.cli import require_environment, require_project
from infracontext.cli.completion import complete_node_id
from infracontext.cli.resolve import resolve_node_or_exit
from infracontext.config import get_active_project, set_active_project
from infracontext.models.node import COMPUTE_NODE_TYPES, Learning, Node, NodeType, slugify
from infracontext.overrides import get_node_overrides
from infracontext.paths import (
    EnvironmentPaths,
    InvalidProjectSlugError,
    ProjectPaths,
    list_projects,
    project_exists,
    validate_project_slug,
)
from infracontext.storage import (
    StorageError,
    append_to_list,
    read_model,
    read_yaml,
    update_yaml,
    write_model,
    write_yaml,
)


class OutputFormat(StrEnum):
    """Output format for LLM-facing commands."""

    yaml = "yaml"
    json = "json"


app = typer.Typer(no_args_is_help=True)
console = Console()

# Sub-apps for different entity types
project_app = typer.Typer(help="Manage projects")
node_app = typer.Typer(help="Manage infrastructure nodes")
relationship_app = typer.Typer(help="Manage relationships between nodes")
chain_app = typer.Typer(help="Manage request-path chains (ordered lb -> app -> db paths)")
source_app = typer.Typer(help="Manage infrastructure sources")

app.add_typer(project_app, name="project")
app.add_typer(node_app, name="node")
app.add_typer(relationship_app, name="relationship")
relationship_app.add_typer(chain_app, name="chain")
app.add_typer(source_app, name="source")


def read_node_with_overrides(
    node_file: Path, environment: EnvironmentPaths | None = None, project: str | None = None
) -> Node | None:
    """Read a node from file and apply local overrides.

    Local overrides from .infracontext.local.yaml are applied for:
    - ssh_alias
    - source_paths

    Project-scoped keys (``<project>/<node_id>``) take precedence over
    global keys (``<node_id>``).
    """
    node = read_model(node_file, Node)
    if node is None:
        return None

    # Apply local overrides
    overrides = get_node_overrides(node.id, environment, project)
    if overrides.ssh_alias is not None:
        node.ssh_alias = overrides.ssh_alias
    if overrides.source_paths is not None:
        node.source_paths = overrides.source_paths

    return node


def _project_slug_or_exit(value: str) -> str:
    """Validate project slug and exit with a user-facing error."""
    try:
        return validate_project_slug(value)
    except InvalidProjectSlugError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None


def _node_file_from_id_or_exit(paths: ProjectPaths, node_id: str) -> Path:
    """Resolve node ID to file path or exit with a user-facing error."""
    if ":" not in node_id:
        console.print("[red]Invalid node ID. Use format: type:slug[/red]")
        raise typer.Exit(1)

    node_type, slug = node_id.split(":", 1)
    try:
        return paths.node_file(node_type, slug)
    except ValueError as e:
        console.print(f"[red]Invalid node ID '{node_id}': {e}[/red]")
        raise typer.Exit(1) from None


def _source_file_or_exit(paths: ProjectPaths, source_name: str) -> Path:
    """Resolve source name to file path or exit with a user-facing error."""
    try:
        return paths.source_file(source_name)
    except ValueError as e:
        console.print(f"[red]Invalid source name '{source_name}': {e}[/red]")
        raise typer.Exit(1) from None


def _resolve_existing_node_ref(ref: str, project: str, paths: ProjectPaths):
    """Resolve a node ref and verify the node exists, or exit with an error.

    Handles same-project (``type:slug``), cross-project (``@project:...``),
    and cross-root (``@alias:...``) references through the federation layer.
    Shared by ``relationship create`` and ``relationship chain add``.
    Returns the federation ``ResolvedRef``.
    """
    from infracontext.federation import LOCAL_ROOT_ALIAS, resolve_node_ref
    from infracontext.graph.loader import load_node

    try:
        resolved = resolve_node_ref(ref, default_project=project)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None

    if resolved.root_alias == LOCAL_ROOT_ALIAS and resolved.project == project:
        # Same-project local: check via file path (covers the common case
        # without round-tripping through the loader).
        node_file = _node_file_from_id_or_exit(paths, resolved.node_id)
        if not node_file.exists():
            console.print(f"[red]Node '{resolved.node_id}' not found in project '{project}'.[/red]")
            raise typer.Exit(1)
    elif resolved.root_alias == LOCAL_ROOT_ALIAS:
        # Local cross-project.
        if not project_exists(resolved.project):
            console.print(f"[red]Project '{resolved.project}' not found.[/red]")
            raise typer.Exit(1)
        if load_node(resolved.project, resolved.node_id) is None:
            console.print(
                f"[red]Node '{resolved.node_id}' not found in project "
                f"'{resolved.project}'.[/red]"
            )
            raise typer.Exit(1)
    else:
        # External root.
        if load_node(resolved.project, resolved.node_id, root_alias=resolved.root_alias) is None:
            console.print(
                f"[red]Node '{resolved.node_id}' not found in external root "
                f"'{resolved.root_alias}' (project '{resolved.project}').[/red]"
            )
            raise typer.Exit(1)

    return resolved


@dataclass(frozen=True)
class _NodeTarget:
    """A resolved node addressing target.

    ``paths`` is rooted at the right project (local or external). ``node_id``
    is the unqualified ``type:slug`` for use with :func:`ProjectPaths.node_file`.
    ``project`` and ``root_alias`` are populated for display / write-guards.
    Pre-existing project context can be reused for relationship and learning
    overrides via ``environment``.
    """

    paths: ProjectPaths
    environment: EnvironmentPaths
    project: str
    node_id: str
    root_alias: str
    writable: bool


def _resolve_node_target(node_id_arg: str, *, require_writable: bool = False) -> _NodeTarget:
    """Resolve a node-ID argument to a concrete project + paths.

    Accepts:

    - Plain ``type:slug``               -> current project in the local root
    - Qualified ``@scope:type:slug``    -> scope is resolved first as an
      external root alias, then as a local project (matches the rest of the
      federation model and `resolve_node_ref`)

    Exits with a clear error on malformed IDs, unknown roots, unknown
    projects, or write attempts against read-only external roots.
    """
    from infracontext.federation import (
        LOCAL_ROOT_ALIAS,
        ReadOnlyRootError,
        all_roots,
        resolve_node_ref,
    )

    environment = require_environment()

    if node_id_arg.startswith("@"):
        roots = all_roots(environment)
        # We need *some* default project; for qualified refs it's only used
        # if the ref turns out to be a local cross-project ref, where it's
        # ignored anyway because parse_node_ref returns the scope.
        default_project = get_active_project(environment) or ""
        try:
            resolved = resolve_node_ref(
                node_id_arg, default_project=default_project, roots=roots
            )
        except ValueError as e:
            console.print(f"[red]Invalid node reference '{node_id_arg}': {e}[/red]")
            raise typer.Exit(1) from None

        if resolved.root_alias == LOCAL_ROOT_ALIAS:
            # Local cross-project. Verify the project exists, then build paths
            # against the local environment.
            if not project_exists(resolved.project, environment):
                console.print(
                    f"[red]Project '{resolved.project}' not found in local root.[/red]"
                )
                raise typer.Exit(1)
            target_env = environment
            target_writable = True
        else:
            root = roots[resolved.root_alias]
            target_env = root.environment
            target_writable = root.writable
            if require_writable:
                try:
                    from infracontext.federation import require_writable_root

                    require_writable_root(resolved.root_alias, environment)
                except ReadOnlyRootError as e:
                    console.print(f"[red]{e}[/red]")
                    raise typer.Exit(1) from None

        try:
            paths = ProjectPaths.for_project(resolved.project, target_env)
        except InvalidProjectSlugError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from None

        return _NodeTarget(
            paths=paths,
            environment=target_env,
            project=resolved.project,
            node_id=resolved.node_id,
            root_alias=resolved.root_alias,
            writable=target_writable,
        )

    # Unqualified ref -> current local project (existing behavior).
    project = require_project()
    paths = ProjectPaths.for_project(project, environment)
    return _NodeTarget(
        paths=paths,
        environment=environment,
        project=project,
        node_id=node_id_arg,
        root_alias=LOCAL_ROOT_ALIAS,
        writable=True,
    )


# ============================================
# Project Commands
# ============================================


@project_app.command("list")
def project_list() -> None:
    """List all projects.

    Supports hierarchical projects (customer/environment format).
    """
    projects = list_projects()
    active = get_active_project()

    if not projects:
        console.print("[dim]No projects found. Create one with 'ic describe project create <name>'[/dim]")
        console.print("[dim]For hierarchical organization: 'ic describe project create customer/environment'[/dim]")
        return

    # Check if any projects use hierarchy (contain /)
    has_hierarchy = any("/" in p for p in projects)

    table = Table(title="Projects")
    if has_hierarchy:
        table.add_column("Customer", style="dim")
        table.add_column("Environment", style="cyan")
    else:
        table.add_column("Name", style="cyan")
    table.add_column("Active", style="green")

    for p in projects:
        is_active = "*" if p == active else ""
        if has_hierarchy:
            if "/" in p:
                customer, env = p.rsplit("/", 1)
                table.add_row(customer, env, is_active)
            else:
                table.add_row("", p, is_active)
        else:
            table.add_row(p, is_active)

    console.print(table)


@project_app.command("create")
def project_create(
    name: Annotated[str, typer.Argument(help="Project name (e.g., 'prod' or 'acme/prod')")],
    switch: Annotated[bool, typer.Option("--switch", "-s", help="Switch to this project after creation")] = True,
) -> None:
    """Create a new project.

    Examples:
        ic describe project create prod
        ic describe project create acme/staging
    """
    require_environment()  # Ensure we're in an environment

    slug = _project_slug_or_exit(name.lower().replace(" ", "-"))

    if project_exists(slug):
        console.print(f"[red]Project '{slug}' already exists.[/red]")
        raise typer.Exit(1)

    paths = ProjectPaths.for_project(slug)
    paths.ensure_dirs()

    # Initialize empty relationships file
    write_yaml(paths.relationships_yaml, {"version": "2.0", "relationships": []})

    console.print(f"[green]Created project '{slug}'[/green]")

    if switch:
        set_active_project(slug)
        console.print(f"[green]Switched to project '{slug}'[/green]")


@project_app.command("switch")
def project_switch(
    name: Annotated[str, typer.Argument(help="Project name to switch to")],
) -> None:
    """Switch to a different project."""
    slug = _project_slug_or_exit(name.lower().replace(" ", "-"))
    if not project_exists(slug):
        from infracontext.cli import _suggest_projects

        console.print(f"[red]Project '{slug}' not found.[/red]")
        if suggestions := _suggest_projects(slug):
            console.print(f"[yellow]Did you mean: {', '.join(suggestions)}?[/yellow]")
        raise typer.Exit(1)

    set_active_project(slug)
    console.print(f"[green]Switched to project '{slug}'[/green]")


@project_app.command("delete")
def project_delete(
    name: Annotated[str, typer.Argument(help="Project name to delete")],
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation")] = False,
) -> None:
    """Delete a project and all its data."""
    slug = _project_slug_or_exit(name.lower().replace(" ", "-"))
    if not project_exists(slug):
        console.print(f"[red]Project '{slug}' not found.[/red]")
        raise typer.Exit(1)

    if not force:
        confirm = typer.confirm(f"Delete project '{slug}' and all its data?")
        if not confirm:
            raise typer.Abort()

    import shutil

    environment = require_environment()
    paths = ProjectPaths.for_project(slug, environment)

    # Defense-in-depth: the slug validation in ProjectPaths.for_project already
    # guards against traversal, but assert containment here too so a future
    # refactor that loosens path construction can't turn `project delete` into
    # an arbitrary directory removal.
    try:
        paths.root.resolve().relative_to(environment.projects_dir.resolve())
    except ValueError:
        console.print(
            f"[red]Refusing to delete '{paths.root}': it is not inside the "
            f"projects directory '{environment.projects_dir}'.[/red]"
        )
        raise typer.Exit(1) from None

    shutil.rmtree(paths.root)

    if get_active_project() == slug:
        set_active_project(None)

    console.print(f"[green]Deleted project '{slug}'[/green]")


# ============================================
# Node Commands
# ============================================


def _iter_all_nodes(
    paths: ProjectPaths, environment: EnvironmentPaths | None = None, project: str | None = None
) -> list[Node]:
    """Iterate all nodes in the project, applying local overrides."""
    nodes = []
    if not paths.nodes_dir.exists():
        return nodes
    for type_dir in sorted(paths.nodes_dir.iterdir()):
        if not type_dir.is_dir():
            continue
        for node_file in sorted(type_dir.glob("*.yaml")):
            node = read_node_with_overrides(node_file, environment, project)
            if node:
                nodes.append(node)
    return nodes


def _node_matches_query(node: Node, query: str) -> tuple[bool, str]:
    """Check if node matches a search query. Returns (matches, match_reason)."""
    query_lower = query.lower()

    # Exact node ID match
    if node.id.lower() == query_lower:
        return True, "id"

    # Partial slug/name match
    if query_lower in node.slug.lower():
        return True, f"slug contains '{query}'"
    if query_lower in node.name.lower():
        return True, f"name contains '{query}'"

    # SSH alias match
    if node.ssh_alias and query_lower in node.ssh_alias.lower():
        return True, f"ssh_alias: {node.ssh_alias}"

    # Domain match (node.domains)
    for domain in node.domains:
        if query_lower in domain.lower():
            return True, f"domain: {domain}"

    # IP match
    for ip in node.ip_addresses:
        if query_lower in ip:
            return True, f"ip: {ip}"

    # Endpoint domain match
    for ep in node.endpoints:
        for domain in ep.domains:
            if query_lower in domain.lower():
                return True, f"endpoint {ep.name}: {domain}"

    return False, ""


def _node_summary(node: Node, *, project: str, root: str | None = None) -> dict:
    """A compact, JSON-friendly summary of a node for machine-readable output."""
    summary: dict = {
        "id": node.id,
        "name": node.name,
        "type": str(node.type),
        "ssh_alias": node.ssh_alias,
        "project": project,
    }
    if root is not None:
        # "" denotes the local root; external roots use their alias.
        summary["root"] = root
    return summary


@node_app.command("find")
def node_find(
    query: Annotated[
        str,
        typer.Argument(
            help="Search query (domain, IP, name, SSH alias, or node ID)",
            autocompletion=complete_node_id,
        ),
    ],
    show_all: Annotated[bool, typer.Option("--all", "-a", help="Show all matches, not just first")] = False,
    all_roots_flag: Annotated[
        bool,
        typer.Option(
            "--all-roots",
            "-A",
            help="Search across the local root (all projects) and all configured external roots",
        ),
    ] = False,
    output_json: Annotated[bool, typer.Option("--json", help="Output matches as JSON")] = False,
) -> None:
    """Find nodes by domain, IP, name, SSH alias, or ID.

    Searches across node domains, endpoint domains, IP addresses, SSH aliases,
    names, and slugs. Useful for resolving "which server handles example.com?"
    type queries.

    Defaults to the current project for backward compatibility. ``--all-roots``
    expands the search to every project in the local root and every
    configured external root; matches outside the current project are
    reported using qualified ``@alias:type:slug`` IDs so they can be passed
    straight to ``ic describe node show / context``.

    Examples:
        ic describe node find kimai.example.com
        ic describe node find 192.168.1.100 --all-roots
        ic describe node find proxy -A
    """
    from infracontext.federation import LOCAL_ROOT_ALIAS, all_roots

    environment = require_environment()

    # (root_alias, root_env, project_slug, paths) tuples to walk.
    #
    # For external roots we deliberately scope the search to the root's
    # *active project*. The current address form `@alias:type:slug` resolves
    # via federation.resolve_node_ref to that active project, so emitting an
    # ID for a node in a non-active project would not round-trip: pasting it
    # into `node show/context/edit/learning` could silently address a
    # different node. A future multi-project address form (e.g.
    # `@alias#project:type:slug`) can lift this restriction; until then,
    # this keeps `find -A` and the rest of the federation surface in sync.
    search_targets: list[tuple[str, EnvironmentPaths, str, ProjectPaths]] = []
    if all_roots_flag:
        roots = all_roots(environment)
        for alias, root in roots.items():
            if alias == LOCAL_ROOT_ALIAS:
                # Local: walk every project (existing behavior; unqualified
                # cross-project IDs are addressable as `@project:type:slug`).
                for proj in list_projects(root.environment):
                    try:
                        p = ProjectPaths.for_project(proj, root.environment)
                    except InvalidProjectSlugError:
                        continue
                    search_targets.append((alias, root.environment, proj, p))
            else:
                # External root: only its active project is addressable.
                proj = get_active_project(root.environment)
                if not proj:
                    continue
                try:
                    p = ProjectPaths.for_project(proj, root.environment)
                except InvalidProjectSlugError:
                    continue
                search_targets.append((alias, root.environment, proj, p))
    else:
        project = require_project()
        search_targets.append(
            (LOCAL_ROOT_ALIAS, environment, project, ProjectPaths.for_project(project, environment))
        )

    matches: list[tuple[str, str, Node, str]] = []  # (root_alias, project, node, reason)
    for alias, root_env, proj, p in search_targets:
        # Overrides come from the root's own environment, not the local one.
        for node in _iter_all_nodes(p, root_env, proj):
            ok, reason = _node_matches_query(node, query)
            if ok:
                matches.append((alias, proj, node, reason))

    if not matches:
        if output_json:
            print("[]")
            return
        console.print(f"[yellow]No nodes found matching '{query}'[/yellow]")
        return

    # Determine the user-facing ID for a match: bare for "here", qualified
    # otherwise. "Here" is the active project in the local root.
    # NB: search_targets is (alias, env, project, paths) -- index 2 is the
    # project slug. A stale [0][1] read here returned an EnvironmentPaths,
    # which then never compared equal to any string project, so every local
    # current-project hit got qualified as `@{project}:{node.id}` and could
    # resolve to an external root with the same alias on paste-back.
    here_project = get_active_project(environment) if all_roots_flag else search_targets[0][2]

    def _display_id(alias: str, project: str, node: Node) -> str:
        if alias == LOCAL_ROOT_ALIAS and project == here_project:
            return node.id
        if alias == LOCAL_ROOT_ALIAS:
            return f"@{project}:{node.id}"
        return f"@{alias}:{node.id}"

    if output_json:
        print(
            json.dumps(
                [
                    {
                        **_node_summary(node, project=proj, root=alias),
                        "id": _display_id(alias, proj, node),
                        "matched_on": reason,
                    }
                    for alias, proj, node, reason in matches
                ],
                indent=2,
            )
        )
        return

    if len(matches) == 1 or not show_all:
        alias, proj, node, reason = matches[0]
        console.print(f"[green]{_display_id(alias, proj, node)}[/green]  ({reason})")
        if len(matches) > 1:
            console.print(f"[dim]{len(matches) - 1} more match(es). Use --all to see all.[/dim]")
    else:
        table = Table(title=f"Nodes matching '{query}'")
        table.add_column("ID", style="cyan")
        table.add_column("Name")
        table.add_column("Match Reason")
        for alias, proj, node, reason in matches:
            table.add_row(_display_id(alias, proj, node), node.name, reason)
        console.print(table)


@node_app.command("list")
def node_list(
    node_type: Annotated[NodeType | None, typer.Option("--type", "-t", help="Filter by node type")] = None,
    all_projects: Annotated[
        bool,
        typer.Option(
            "--all-projects",
            "-A",
            help="List nodes from all projects (and all configured external roots)",
        ),
    ] = False,
    root_filter: Annotated[
        str | None,
        typer.Option(
            "--root",
            help="Filter to one root by alias. Use '' for the local root.",
        ),
    ] = None,
    output_json: Annotated[bool, typer.Option("--json", help="Output nodes as JSON")] = False,
) -> None:
    """List all nodes."""
    from infracontext.federation import LOCAL_ROOT_ALIAS, all_roots

    environment = require_environment()

    if all_projects:
        # Build (root_alias, environment, label) tuples to walk.
        roots = all_roots(environment)
        if root_filter is not None:
            if root_filter not in roots:
                console.print(f"[red]Unknown root alias: '{root_filter}'[/red]")
                raise typer.Exit(1)
            roots = {root_filter: roots[root_filter]}

        has_external = any(a != LOCAL_ROOT_ALIAS for a in roots)

        # (root_alias, project, node) for every node in scope, gathered once so
        # the JSON and table paths share a single filesystem walk.
        entries: list[tuple[str, str, Node]] = []
        for alias, resolved in roots.items():
            env = resolved.environment
            for project_slug in list_projects(env):
                try:
                    paths = ProjectPaths.for_project(project_slug, env)
                except Exception:
                    continue
                if not paths.nodes_dir.exists():
                    continue

                for type_dir in sorted(paths.nodes_dir.iterdir()):
                    if not type_dir.is_dir():
                        continue
                    if node_type and type_dir.name != node_type:
                        continue

                    for node_file in sorted(type_dir.glob("*.yaml")):
                        node = read_node_with_overrides(node_file, env, project_slug)
                        if node:
                            entries.append((alias, project_slug, node))

        if output_json:
            print(
                json.dumps(
                    [_node_summary(node, project=proj, root=alias) for alias, proj, node in entries],
                    indent=2,
                )
            )
            return

        if not entries:
            console.print("[dim]No nodes found.[/dim]")
            return

        table = Table(title="Nodes across all projects")
        if has_external:
            table.add_column("Root", style="magenta")
        table.add_column("Project", style="dim")
        table.add_column("ID", style="cyan")
        table.add_column("Name")
        table.add_column("Type")
        for alias, project_slug, node in entries:
            row = [project_slug, node.id, node.name, str(node.type)]
            if has_external:
                row = [alias or "(local)", *row]
            table.add_row(*row)
        console.print(table)
        return

    project = require_project()
    paths = ProjectPaths.for_project(project, environment)

    nodes: list[Node] = []
    if paths.nodes_dir.exists():
        for type_dir in sorted(paths.nodes_dir.iterdir()):
            if not type_dir.is_dir():
                continue
            if node_type and type_dir.name != node_type:
                continue
            for node_file in sorted(type_dir.glob("*.yaml")):
                node = read_node_with_overrides(node_file, environment, project)
                if node:
                    nodes.append(node)

    if output_json:
        print(json.dumps([_node_summary(node, project=project) for node in nodes], indent=2))
        return

    if not nodes:
        console.print("[dim]No nodes found.[/dim]")
        return

    table = Table(title=f"Nodes in {project}")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Type")
    for node in nodes:
        table.add_row(node.id, node.name, node.type)
    console.print(table)


@node_app.command("show")
def node_show(
    node_id: Annotated[
        str,
        typer.Argument(
            help="Node ID (type:slug), fuzzy query, or qualified @alias:type:slug",
            autocompletion=complete_node_id,
        ),
    ],
    output_json: Annotated[bool, typer.Option("--json", help="Output the full node as JSON")] = False,
) -> None:
    """Show details for a node.

    Accepts a fuzzy query (name/slug/IP/domain/ssh_alias), plain ``type:slug``
    (current project), or qualified ``@alias:type:slug`` (external root or
    local cross-project).
    """
    target = resolve_node_or_exit(node_id)
    node_file = _node_file_from_id_or_exit(target.paths, target.node_id)

    if not node_file.exists():
        console.print(f"[red]Node '{node_id}' not found.[/red]")
        raise typer.Exit(1)

    node = read_node_with_overrides(node_file, target.environment, target.project)
    if not node:
        console.print(f"[red]Failed to read node '{node_id}'.[/red]")
        raise typer.Exit(1)

    if output_json:
        print(json.dumps(node.model_dump(mode="json"), indent=2))
        return

    console.print(f"[bold cyan]{node.name}[/bold cyan] ({node.id})")
    console.print()
    console.print(f"  [dim]Type:[/dim] {node.type}")
    if node.ip_addresses:
        console.print(f"  [dim]IPs:[/dim] {', '.join(node.ip_addresses)}")
    if node.domains:
        console.print(f"  [dim]Domains:[/dim] {', '.join(node.domains)}")
    if node.description:
        console.print()
        console.print(f"  {node.description}")

    if node.endpoints:
        console.print()
        console.print("  [bold]Endpoints:[/bold]")
        for ep in node.endpoints:
            console.print(f"    - {ep.name}: {ep.protocol}:{ep.port} ({ep.direction})")

    if node.functions:
        console.print()
        console.print("  [bold]Functions:[/bold]")
        for fn in node.functions:
            console.print(f"    - {fn.name}")

    if node.observability:
        console.print()
        console.print("  [bold]Observability:[/bold]")
        for obs in node.observability:
            console.print(f"    - {obs.type}: {obs.name} ({obs.url})")


def _write_new_node(
    paths: ProjectPaths,
    *,
    node_type: NodeType,
    slug: str,
    name: str,
    description: str | None = None,
    ip: list[str] | None = None,
    domain: list[str] | None = None,
    ssh_alias: str | None = None,
    collision_hint: str | None = None,
) -> tuple[Node, Path]:
    """Build, validate, and write a new node file, or exit with an error.

    Shared by ``node create`` and ``node add`` so both go through one path for
    slug validation, collision detection, and serialization. Returns the
    created :class:`Node` and its file path.
    """
    node_id = Node.make_id(node_type, slug)
    try:
        node_file = paths.node_file(node_type, slug)
    except ValueError as e:
        console.print(f"[red]Invalid slug '{slug}': {e}[/red]")
        raise typer.Exit(1) from None

    if node_file.exists():
        console.print(f"[red]Node '{node_id}' already exists.[/red]")
        if collision_hint:
            console.print(f"[dim]{collision_hint}[/dim]")
        raise typer.Exit(1)

    try:
        node = Node(
            id=node_id,
            slug=slug,
            type=node_type,
            name=name,
            description=description,
            ip_addresses=ip or [],
            domains=domain or [],
            ssh_alias=ssh_alias,
        )
    except ValidationError as e:
        console.print(f"[red]Invalid node '{node_id}': {e}[/red]")
        raise typer.Exit(1) from None

    paths.node_type_dir(node_type).mkdir(parents=True, exist_ok=True)
    write_model(node_file, node, header_comment=f"Node: {name}")
    return node, node_file


@node_app.command("create")
def node_create(
    node_type: Annotated[NodeType, typer.Option("--type", "-t", help="Node type")],
    name: Annotated[str, typer.Option("--name", "-n", help="Node name")],
    slug: Annotated[
        str | None, typer.Option("--slug", "-s", help="URL-safe slug (auto-generated if not provided)")
    ] = None,
    description: Annotated[str | None, typer.Option("--description", "-d", help="Node description")] = None,
    ip: Annotated[list[str] | None, typer.Option("--ip", help="IP addresses (can be repeated)")] = None,
    domain: Annotated[list[str] | None, typer.Option("--domain", help="Domains (can be repeated)")] = None,
) -> None:
    """Create a new node."""
    project = require_project()
    paths = ProjectPaths.for_project(project)

    node_slug = slug or slugify(name)
    node, _ = _write_new_node(
        paths,
        node_type=node_type,
        slug=node_slug,
        name=name,
        description=description,
        ip=ip,
        domain=domain,
    )
    console.print(f"[green]Created node '{node.id}'[/green]")


@node_app.command("add")
def node_add(
    ssh_alias: Annotated[
        str,
        typer.Argument(help="SSH alias / host to add as a node (from ~/.ssh/config)"),
    ],
    node_type: Annotated[NodeType, typer.Option("--type", "-t", help="Node type")] = NodeType.VM,
    name: Annotated[
        str | None, typer.Option("--name", "-n", help="Human-readable name (default: the alias)")
    ] = None,
    slug: Annotated[
        str | None, typer.Option("--slug", "-s", help="Slug override (default: derived from the alias)")
    ] = None,
) -> None:
    """Add a node from an SSH alias in one step.

    Derives a slug from the alias (``s.myserver`` -> ``s-myserver``), sets
    ``ssh_alias`` so ``ic ssh`` works immediately, and defaults the type to vm.

    Examples:
        ic describe node add web-prod
        ic describe node add db.internal --type vm --name "Primary DB"
    """
    project = require_project()
    paths = ProjectPaths.for_project(project)

    node_slug = slug or slugify(ssh_alias)
    node, node_file = _write_new_node(
        paths,
        node_type=node_type,
        slug=node_slug,
        name=name or ssh_alias,
        ssh_alias=ssh_alias,
        collision_hint=f"Pick a different slug: ic describe node add {ssh_alias} --slug <slug>",
    )
    console.print(f"[green]Created {node.id}[/green]  [dim]{node_file}[/dim]")
    console.print(f"[dim]next: ic ssh {node.slug}[/dim]")


@node_app.command("edit")
def node_edit(
    node_id: Annotated[
        str,
        typer.Argument(
            help="Node ID (type:slug), fuzzy query, or qualified @alias:type:slug",
            autocompletion=complete_node_id,
        ),
    ],
) -> None:
    """Edit a node in your default editor.

    Editing a node in an external root requires that root be configured
    as ``mode: read-write`` in ``external_roots``.
    """
    import os
    import subprocess

    target = resolve_node_or_exit(node_id, require_writable=True)
    node_file = _node_file_from_id_or_exit(target.paths, target.node_id)

    if not node_file.exists():
        console.print(f"[red]Node '{node_id}' not found.[/red]")
        raise typer.Exit(1)

    editor = os.environ.get("EDITOR", "vi")
    subprocess.run(shlex.split(editor) + [str(node_file)])


@node_app.command("delete")
def node_delete(
    node_id: Annotated[
        str,
        typer.Argument(
            help="Node ID (type:slug), fuzzy query, or qualified @alias:type:slug",
            autocompletion=complete_node_id,
        ),
    ],
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation")] = False,
) -> None:
    """Delete a node."""
    target = resolve_node_or_exit(node_id, require_writable=True)
    node_file = _node_file_from_id_or_exit(target.paths, target.node_id)

    if not node_file.exists():
        console.print(f"[red]Node '{node_id}' not found.[/red]")
        raise typer.Exit(1)

    if not force:
        confirm = typer.confirm(f"Delete node '{node_id}'?")
        if not confirm:
            raise typer.Abort()

    node_file.unlink()
    console.print(f"[green]Deleted node '{node_id}'[/green]")


def _build_node_context(
    node: Node,
    project: str,
    include_relationships: bool,
    include_learnings: bool,
    environment: EnvironmentPaths | None = None,
    root_alias: str = "",
) -> dict:
    """Build node context as a dictionary for serialization.

    ``environment`` and ``root_alias`` scope the project-config and graph
    lookups to the resolved root. When omitted, both default to the local
    root (preserves the pre-federation single-root path). Without this the
    project config and graph for an external-root node would be read from
    the *local* repo, silently mixing in unrelated dependencies and the
    wrong access tier — exactly the LLM-context corruption Codex flagged.
    """
    from infracontext.config import load_project_config
    from infracontext.graph.loader import load_node_neighborhood
    from infracontext.graph.query import get_downstream, get_upstream
    from infracontext.models.tier import AccessTier
    from infracontext.tier import get_collector_script, get_effective_tier, get_tier_capabilities

    # Core identity
    context: dict = {
        "id": node.id,
        "name": node.name,
        "type": node.type,
    }

    # SSH connection info - CRITICAL for triage
    if node.ssh_alias:
        context["ssh"] = {"alias": node.ssh_alias, "command": f"ssh {node.ssh_alias}"}
    elif node.ip_addresses:
        context["ssh"] = {"ip": node.ip_addresses[0], "command": f"ssh {node.ip_addresses[0]}"}
    elif node.domains:
        context["ssh"] = {"domain": node.domains[0], "command": f"ssh {node.domains[0]}"}

    # Triage capability
    can_triage = node.type in COMPUTE_NODE_TYPES
    context["triage_capable"] = can_triage

    # Network
    if node.ip_addresses:
        context["ip_addresses"] = node.ip_addresses
    if node.domains:
        context["domains"] = node.domains

    # Documentation
    if node.description:
        context["description"] = node.description
    if node.notes:
        context["notes"] = node.notes
    if node.source_paths:
        context["source_paths"] = node.source_paths

    # Triage hints
    if node.triage:
        triage_dict: dict = {}
        if node.triage.services:
            triage_dict["services"] = node.triage.services
        if node.triage.context:
            triage_dict["context"] = node.triage.context
        if triage_dict:
            context["triage"] = triage_dict

    # Access tier information — read from the resolved root's project config
    # so an external-root node uses *its* configured tier and collector,
    # not whatever happens to live in a local project of the same slug.
    project_config = load_project_config(project, environment)
    effective_tier = get_effective_tier(project_config, node)
    access_section: dict = {
        "tier": effective_tier.name.lower(),
        "tier_level": int(effective_tier),
        "capabilities": get_tier_capabilities(effective_tier),
    }
    # Include collector script path when tier is COLLECTOR
    if effective_tier == AccessTier.COLLECTOR:
        access_section["collector_script"] = get_collector_script(project_config, node)
    context["access"] = access_section

    # Endpoints
    if node.endpoints:
        context["endpoints"] = [
            {"name": ep.name, "protocol": ep.protocol, "port": ep.port, "direction": ep.direction}
            for ep in node.endpoints
        ]

    # Functions
    if node.functions:
        context["functions"] = [fn.name for fn in node.functions]

    # Observability
    if node.observability:
        context["observability"] = [{"type": obs.type, "name": obs.name, "url": obs.url} for obs in node.observability]

    # Relationships — load just the 2-hop neighborhood rooted in the *node's*
    # root so its real dependencies are surfaced without parsing the whole
    # project. load_node_neighborhood transparently falls back to the full
    # graph when cross-root ``@``-references are present, so an external node
    # still sees its real (possibly cross-repo) dependencies.
    if include_relationships:
        graph = load_node_neighborhood(project, node.id, depth=2, root_alias=root_alias)
        if node.id in graph:
            upstream = get_upstream(graph, node.id, max_depth=2)
            downstream = get_downstream(graph, node.id, max_depth=2)

            if upstream or downstream:
                deps: dict = {}
                if upstream:
                    deps["depends_on"] = [
                        {"id": dep_id, "name": graph.nodes.get(dep_id, {}).get("name", dep_id)}
                        for dep_id in sorted(upstream)
                    ]
                if downstream:
                    deps["depended_on_by"] = [
                        {"id": dep_id, "name": graph.nodes.get(dep_id, {}).get("name", dep_id)}
                        for dep_id in sorted(downstream)
                    ]
                context["dependencies"] = deps

    # Learnings
    if include_learnings and node.learnings:
        context["learnings"] = [
            {"date": ln.date, "source": ln.source, "context": ln.context, "finding": ln.finding}
            for ln in node.learnings
        ]

    return context


def _format_output(data: dict, fmt: OutputFormat) -> str:
    """Format data for output."""
    import io
    import json

    from ruamel.yaml import YAML

    # Convert enums to their values for serialization
    def convert_enums(obj: object) -> object:
        if isinstance(obj, dict):
            return {k: convert_enums(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [convert_enums(v) for v in obj]
        if hasattr(obj, "value"):  # Enum
            return obj.value
        return obj

    data = convert_enums(data)  # type: ignore[assignment]

    match fmt:
        case OutputFormat.yaml:
            yaml = YAML()
            yaml.default_flow_style = False
            stream = io.StringIO()
            yaml.dump(data, stream)
            return stream.getvalue()
        case OutputFormat.json:
            return json.dumps(data, indent=2, ensure_ascii=False)


def run_node_context(
    node_id: str,
    *,
    include_relationships: bool = True,
    include_learnings: bool = True,
    fmt: OutputFormat = OutputFormat.yaml,
) -> None:
    """Resolve a node (fuzzily) and print its full triage context.

    Shared by ``ic describe node context`` and the top-level ``ic ctx`` alias.
    """
    target = resolve_node_or_exit(node_id)
    node_file = _node_file_from_id_or_exit(target.paths, target.node_id)

    if not node_file.exists():
        console.print(f"[red]Node '{target.node_id}' not found.[/red]")
        raise typer.Exit(1)

    node = read_node_with_overrides(node_file, target.environment, target.project)
    if not node:
        console.print(f"[red]Failed to read node '{target.node_id}'.[/red]")
        raise typer.Exit(1)

    context = _build_node_context(
        node,
        target.project,
        include_relationships,
        include_learnings,
        environment=target.environment,
        root_alias=target.root_alias,
    )
    print(_format_output(context, fmt))


@node_app.command("context")
def node_context(
    node_id: Annotated[
        str,
        typer.Argument(
            help="Node ID (type:slug), fuzzy query, or qualified @alias:type:slug",
            autocompletion=complete_node_id,
        ),
    ],
    include_relationships: Annotated[bool, typer.Option("--relationships", "-r", help="Include relationships")] = True,
    include_learnings: Annotated[bool, typer.Option("--learnings", "-l", help="Include learnings")] = True,
    fmt: Annotated[OutputFormat, typer.Option("--format", "-f", help="Output format")] = OutputFormat.yaml,
    json_out: Annotated[bool, typer.Option("--json", help="Shorthand for --format json")] = False,
) -> None:
    """Output full node context for triage (used by Claude).

    This command outputs everything Claude needs to know about a node
    for troubleshooting, in a format optimized for LLM consumption.

    Accepts a fuzzy query or qualified ``@alias:type:slug`` so triage skills
    can fetch context for upstream nodes that live in an external (e.g. fleet)
    root.
    """
    run_node_context(
        node_id,
        include_relationships=include_relationships,
        include_learnings=include_learnings,
        fmt=OutputFormat.json if json_out else fmt,
    )


def append_learning(target: _NodeTarget, *, finding: str, context: str, source: str) -> None:
    """Append a learning to a resolved node target, or exit with an error.

    Shared by ``ic describe node learning`` (agent default) and the top-level
    ``ic learn`` shortcut (human default) so both write learnings identically.
    """
    from datetime import date

    node_file = _node_file_from_id_or_exit(target.paths, target.node_id)
    if not node_file.exists():
        console.print(f"[red]Node '{target.node_id}' not found.[/red]")
        raise typer.Exit(1)

    learning = Learning(
        date=date.today().isoformat(),
        context=context,
        finding=finding,
        source=source,
    )
    append_to_list(node_file, "learnings", learning.model_dump(mode="json"))
    console.print(f"[green]Added learning to '{target.node_id}'[/green]")


@node_app.command("learning")
def node_learning_add(
    node_id: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or qualified @alias:type:slug", autocompletion=complete_node_id),
    ],
    finding: Annotated[str, typer.Argument(help="What was discovered")],
    context: Annotated[str, typer.Option("--context", "-c", help="What was being investigated")] = "triage",
    source: Annotated[str, typer.Option("--source", "-s", help="Who added this: 'agent' or 'human'")] = "agent",
) -> None:
    """Add a learning to a node.

    Learnings are discovered knowledge that helps future troubleshooting.
    Claude can use this command to record findings during triage.

    Writing a learning to an external-root node requires that root be
    configured as ``mode: read-write``.
    """
    target = resolve_node_or_exit(node_id, require_writable=True)
    append_learning(target, finding=finding, context=context, source=source)


@node_app.command("consolidate")
def node_consolidate(
    dest: Annotated[
        str,
        typer.Argument(
            help="Destination node kept after the merge (type:slug or fuzzy query)",
            autocompletion=complete_node_id,
        ),
    ],
    src: Annotated[
        str,
        typer.Argument(
            help="Duplicate node merged into DEST and then deleted (type:slug or fuzzy query)",
            autocompletion=complete_node_id,
        ),
    ],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the merge plan without changing anything")
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Proceed despite a managed_by/source_id ownership conflict"),
    ] = False,
) -> None:
    """Merge duplicate node SRC into DEST and rewrite all references.

    Scalar fields are fill-only (DEST wins), lists are unioned or appended
    with dedupe, and ``first_seen`` keeps the earlier date. Every edge in
    relationships.yaml, every chain member in chains.yaml (including inbound
    ``@project:...`` references from other local projects), and every local
    override key (both ``type:slug`` and ``project/type:slug`` forms) pointing
    at SRC is rewritten to DEST; then the SRC node file is deleted.

    Refuses to consolidate across roots or projects, and refuses when the
    merged node's source binding would misbehave on the next sync -- either
    both nodes are owned by different sources (two sources would fight over
    the merged node), or SRC is source-managed and DEST is not (DEST would
    adopt SRC's binding and the next sync would rename or re-create it) --
    unless ``--force`` is given.

    Examples:
        ic describe node consolidate vm:web-prod vm:web-prod-2
        ic describe node consolidate web-prod web-prod-2 --dry-run
    """
    from infracontext.consolidate import (
        ChainRewrite,
        RelationshipRewrite,
        apply_override_remap,
        merge_nodes,
        plan_override_remap,
        rewrite_chain_members,
        rewrite_relationship_refs,
    )
    from infracontext.federation import LOCAL_ROOT_ALIAS
    from infracontext.models.chain import ChainFile
    from infracontext.models.relationship import RelationshipFile

    dest_target = resolve_node_or_exit(dest)
    src_target = resolve_node_or_exit(src)

    for label, target in (("Destination", dest_target), ("Source", src_target)):
        if target.root_alias != LOCAL_ROOT_ALIAS:
            console.print(
                f"[red]{label} node '@{target.root_alias}:{target.node_id}' lives in external root "
                f"'{target.root_alias}'; consolidate only works on local nodes.[/red]"
            )
            raise typer.Exit(1)

    if dest_target.project != src_target.project:
        console.print(
            f"[red]Nodes live in different projects ('{dest_target.project}' vs "
            f"'{src_target.project}'); consolidate works within one project.[/red]"
        )
        raise typer.Exit(1)

    if dest_target.node_id == src_target.node_id:
        console.print("[red]Source and destination are the same node; nothing to consolidate.[/red]")
        raise typer.Exit(1)

    paths = dest_target.paths
    project = dest_target.project
    dest_file = _node_file_from_id_or_exit(paths, dest_target.node_id)
    src_file = _node_file_from_id_or_exit(paths, src_target.node_id)

    if not dest_file.exists():
        console.print(f"[red]Destination node '{dest_target.node_id}' not found.[/red]")
        raise typer.Exit(1)
    if not src_file.exists():
        console.print(f"[red]Source node '{src_target.node_id}' not found.[/red]")
        raise typer.Exit(1)

    # Read the raw files (no local overrides): the merge must not bake one
    # operator's machine-specific ssh_alias into the shared node file.
    dest_node = read_model(dest_file, Node)
    src_node = read_model(src_file, Node)
    if dest_node is None or src_node is None:
        broken = dest_target.node_id if dest_node is None else src_target.node_id
        console.print(f"[red]Failed to read node '{broken}'.[/red]")
        raise typer.Exit(1)

    # Source-ownership conflict: two sources would fight over the merged node.
    conflicts = []
    if src_node.managed_by and dest_node.managed_by and src_node.managed_by != dest_node.managed_by:
        conflicts.append(f"managed_by '{dest_node.managed_by}' vs '{src_node.managed_by}'")
    if src_node.source_id and dest_node.source_id and src_node.source_id != dest_node.source_id:
        conflicts.append(f"source_id '{dest_node.source_id}' vs '{src_node.source_id}'")
    if conflicts:
        detail = "; ".join(conflicts)
        if not force:
            console.print(
                f"[red]Refusing to consolidate: both nodes are bound to different sources "
                f"({detail}). The next sync of either source would fight over the merged node.[/red]"
            )
            console.print("[dim]Re-run with --force to merge anyway (dest's ownership wins).[/dim]")
            raise typer.Exit(1)
        console.print(
            f"[yellow]Warning: consolidating across source ownership ({detail}); the next sync of "
            f"the losing source may re-create '{src_target.node_id}'.[/yellow]"
        )

    # One-sided ownership: the fill-only merge would make DEST adopt SRC's
    # source binding, and the next sync of that source would rename the merged
    # node back to the source-derived slug (Proxmox) or re-create the
    # duplicate (ssh-config), undoing the consolidation.
    adopted = [
        f"{name} '{getattr(src_node, name)}'"
        for name in ("managed_by", "source_id")
        if getattr(src_node, name) and getattr(dest_node, name) is None
    ]
    if adopted:
        detail = "; ".join(adopted)
        if not force:
            console.print(
                f"[red]Refusing to consolidate: '{src_target.node_id}' is source-managed ({detail}) "
                f"but '{dest_target.node_id}' is not. The merged node would adopt that binding, and "
                f"the next sync of the source would rename or re-create it, undoing the "
                f"consolidation.[/red]"
            )
            console.print(
                f"[dim]Swap the arguments to keep the source-managed node "
                f"(ic describe node consolidate {src_target.node_id} {dest_target.node_id}), "
                f"or re-run with --force to let '{dest_target.node_id}' adopt the binding.[/dim]"
            )
            raise typer.Exit(1)
        console.print(
            f"[yellow]Warning: '{dest_target.node_id}' adopts {detail} from '{src_target.node_id}'; "
            f"the next sync of that source may rename or re-create the merged node.[/yellow]"
        )

    merged, merged_fields = merge_nodes(dest_node, src_node)

    rel_file = read_model(paths.relationships_yaml, RelationshipFile)
    rel_result = RelationshipRewrite()
    if rel_file is not None:
        rel_result = rewrite_relationship_refs(
            rel_file, project=project, src_id=src_target.node_id, dest_id=dest_target.node_id
        )

    chain_file = read_model(paths.chains_yaml, ChainFile)
    chain_result = ChainRewrite()
    if chain_file is not None:
        chain_result = rewrite_chain_members(
            chain_file, project=project, src_id=src_target.node_id, dest_id=dest_target.node_id
        )

    # Inbound references from other local projects (`@<project>:type:slug` is
    # first-class in relationships and chains) would dangle once the src file
    # is deleted -- rewrite them too. Also note whether another project has a
    # node with the src ID: the *global* override key form applies to every
    # project, so it must then be copied, not moved.
    def _read_sibling_or_warn(path: Path, model_cls):
        try:
            return read_model(path, model_cls)
        except (StorageError, ValidationError):
            console.print(f"[yellow]Warning: {path} is malformed; skipping its reference rewrite.[/yellow]")
            return None

    src_in_other_projects = False
    dest_in_other_projects = False
    sibling_changes: list[tuple[str, ProjectPaths, RelationshipFile | None, RelationshipRewrite, ChainFile | None, ChainRewrite]] = []
    for sibling in list_projects(dest_target.environment):
        if sibling == project:
            continue
        sibling_paths = ProjectPaths.for_project(sibling, dest_target.environment)
        if sibling_paths.node_file(*src_target.node_id.split(":", 1)).exists():
            src_in_other_projects = True
        if sibling_paths.node_file(*dest_target.node_id.split(":", 1)).exists():
            dest_in_other_projects = True
        sib_rel = _read_sibling_or_warn(sibling_paths.relationships_yaml, RelationshipFile)
        sib_rel_result = RelationshipRewrite()
        if sib_rel is not None:
            sib_rel_result = rewrite_relationship_refs(
                sib_rel,
                project=project,
                src_id=src_target.node_id,
                dest_id=dest_target.node_id,
                file_project=sibling,
            )
        sib_chains = _read_sibling_or_warn(sibling_paths.chains_yaml, ChainFile)
        sib_chain_result = ChainRewrite()
        if sib_chains is not None:
            sib_chain_result = rewrite_chain_members(
                sib_chains,
                project=project,
                src_id=src_target.node_id,
                dest_id=dest_target.node_id,
                file_project=sibling,
            )
        if sib_rel_result.rewritten or sib_chain_result.members_rewritten:
            sibling_changes.append(
                (sibling, sibling_paths, sib_rel, sib_rel_result, sib_chains, sib_chain_result)
            )

    overrides_path = dest_target.environment.local_overrides
    remap: list[tuple[str, str, str | None]] = []
    if overrides_path.exists():
        try:
            raw_overrides = read_yaml(overrides_path)
        except StorageError:
            console.print(
                f"[yellow]Warning: {overrides_path} is malformed; skipping override remap.[/yellow]"
            )
            raw_overrides = {}
        nodes_map = raw_overrides.get("nodes")
        if isinstance(nodes_map, dict):
            remap = plan_override_remap(
                nodes_map,
                project=project,
                src_id=src_target.node_id,
                dest_id=dest_target.node_id,
                src_in_other_projects=src_in_other_projects,
                dest_in_other_projects=dest_in_other_projects,
            )

    if not dry_run:
        if merged_fields:
            write_model(dest_file, merged, header_comment=f"Node: {merged.name}")
        rel_touched = rel_result.rewritten or rel_result.self_edges_dropped or rel_result.duplicates_removed
        if rel_file is not None and rel_touched:
            write_model(paths.relationships_yaml, rel_file)
        if chain_file is not None and (chain_result.members_rewritten or chain_result.chains_removed):
            write_model(paths.chains_yaml, chain_file)
        for _, sib_paths, sib_rel, sib_rel_result, sib_chains, sib_chain_result in sibling_changes:
            if sib_rel is not None and sib_rel_result.rewritten:
                write_model(sib_paths.relationships_yaml, sib_rel)
            if sib_chains is not None and sib_chain_result.members_rewritten:
                write_model(sib_paths.chains_yaml, sib_chains)
        if remap:

            def _remap_overrides(cm: dict) -> None:
                nodes_section = cm.get("nodes")
                if nodes_section is not None:
                    apply_override_remap(nodes_section, remap)

            update_yaml(overrides_path, _remap_overrides)
        src_file.unlink()

    action = "Would consolidate" if dry_run else "Consolidated"
    console.print(
        f"[green]{action} '{src_target.node_id}' into '{dest_target.node_id}'[/green] "
        f"[dim](project '{project}')[/dim]"
    )
    if merged_fields:
        console.print(f"  merged fields: {', '.join(merged_fields)}")
    else:
        console.print("  merged fields: none (destination already has everything)")
    console.print(
        f"  relationships: {rel_result.rewritten} rewritten, "
        f"{rel_result.duplicates_removed} duplicate(s) removed, "
        f"{rel_result.self_edges_dropped} self-edge(s) dropped"
    )
    console.print(
        f"  chains: {chain_result.members_rewritten} member(s) rewritten, "
        f"{chain_result.chains_removed} chain(s) removed"
    )
    for sibling, _, _, sib_rel_result, _, sib_chain_result in sibling_changes:
        console.print(
            f"  inbound refs from '{sibling}': {sib_rel_result.rewritten} edge(s) and "
            f"{sib_chain_result.members_rewritten} chain member(s) rewritten"
        )
    console.print(f"  overrides: {len(remap)} key(s) remapped")
    if any(action == "copy" and old == src_target.node_id for action, old, _ in remap):
        console.print(
            f"  [dim]global override key '{src_target.node_id}' copied, not moved: "
            f"another project has a node with that ID[/dim]"
        )
    if any(old == src_target.node_id and new is not None and "/" in new for _, old, new in remap):
        console.print(
            f"  [dim]global override entry written project-scoped ('{project}/{dest_target.node_id}'): "
            f"another project has its own node with the destination ID[/dim]"
        )
    if any(action == "delete" for action, _, _ in remap):
        console.print(
            f"  [dim]global override key '{src_target.node_id}' removed: it was fully shadowed by "
            f"the project-scoped entry and no other project has a node with that ID[/dim]"
        )
    console.print(f"  {'would delete' if dry_run else 'deleted'} {src_file}")


# ============================================
# Relationship Commands
# ============================================


@relationship_app.command("list")
def relationship_list() -> None:
    """List all relationships.

    Cross-project references are displayed with their @project prefix.
    """
    from infracontext.models.relationship import RelationshipFile, is_cross_project_ref

    project = require_project()
    paths = ProjectPaths.for_project(project)

    rel_file = read_model(paths.relationships_yaml, RelationshipFile)
    if not rel_file or not rel_file.relationships:
        console.print("[dim]No relationships defined.[/dim]")
        return

    table = Table(title=f"Relationships in {project}")
    table.add_column("Source", style="cyan")
    table.add_column("Type", style="yellow")
    table.add_column("Target", style="cyan")
    table.add_column("Description")

    for rel in rel_file.relationships:
        source_display = rel.source
        target_display = rel.target

        # Highlight cross-project refs
        if is_cross_project_ref(rel.source):
            source_display = f"[bold]{rel.source}[/bold]"
        if is_cross_project_ref(rel.target):
            target_display = f"[bold]{rel.target}[/bold]"

        table.add_row(
            source_display,
            rel.type,
            target_display,
            (rel.description or "")[:50],
        )

    console.print(table)


@relationship_app.command("create")
def relationship_create(
    source: Annotated[str, typer.Option("--source", "-s", help="Source node ID or @project:type:slug")],
    target: Annotated[str, typer.Option("--target", "-t", help="Target node ID or @project:type:slug")],
    rel_type: Annotated[str, typer.Option("--type", "-r", help="Relationship type")],
    description: Annotated[str | None, typer.Option("--description", "-d", help="Description")] = None,
) -> None:
    """Create a relationship between two nodes.

    Supports cross-project references using @project:type:slug format.
    Example: --target @vagt/dev:vm:qoncept-proxy-01
    """
    from infracontext.models.relationship import (
        Relationship,
        RelationshipFile,
        RelationshipType,
        get_valid_relationship_types,
    )

    project = require_project()
    paths = ProjectPaths.for_project(project)

    # Validate relationship type
    try:
        rel_type_enum = RelationshipType(rel_type)
    except ValueError:
        valid_types = ", ".join(t.value for t in RelationshipType)
        console.print(f"[red]Invalid relationship type '{rel_type}'. Valid types: {valid_types}[/red]")
        raise typer.Exit(1) from None

    # Validate source and target nodes exist. Cross-root refs (@<alias>:...)
    # are resolved through the federation layer so external roots work too.
    resolved_refs = [_resolve_existing_node_ref(ref, project, paths) for ref in (source, target)]

    # Extract node types for constraint validation
    source_node_id = resolved_refs[0].node_id
    target_node_id = resolved_refs[1].node_id
    source_type = source_node_id.split(":")[0]
    target_type = target_node_id.split(":")[0]
    valid_types = get_valid_relationship_types(source_type, target_type)

    if not valid_types:
        console.print(f"[red]No valid relationships between {source_type} and {target_type}.[/red]")
        raise typer.Exit(1)

    if rel_type not in valid_types:
        console.print(f"[red]'{rel_type}' is not valid for {source_type} -> {target_type}.[/red]")
        console.print(f"[yellow]Valid types: {', '.join(valid_types)}[/yellow]")
        raise typer.Exit(1)

    # Load existing relationships
    rel_file = read_model(paths.relationships_yaml, RelationshipFile) or RelationshipFile()

    # Check for duplicate
    for existing in rel_file.relationships:
        if existing.source == source and existing.target == target and existing.type == rel_type_enum:
            console.print("[yellow]Relationship already exists.[/yellow]")
            raise typer.Exit(0)

    # Add new relationship (store the raw ref including @project prefix)
    rel = Relationship(source=source, target=target, type=rel_type_enum, description=description)
    rel_file.relationships.append(rel)

    write_model(paths.relationships_yaml, rel_file)
    console.print(f"[green]Created relationship: {source} --[{rel_type}]--> {target}[/green]")


@relationship_app.command("delete")
def relationship_delete(
    source: Annotated[str, typer.Option("--source", "-s", help="Source node ID")],
    target: Annotated[str, typer.Option("--target", "-t", help="Target node ID")],
    rel_type: Annotated[
        str | None, typer.Option("--type", "-r", help="Relationship type (delete all if not specified)")
    ] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation")] = False,
) -> None:
    """Delete a relationship."""
    from infracontext.models.relationship import RelationshipFile

    project = require_project()
    paths = ProjectPaths.for_project(project)

    rel_file = read_model(paths.relationships_yaml, RelationshipFile)
    if not rel_file:
        console.print("[red]No relationships file found.[/red]")
        raise typer.Exit(1)

    original_count = len(rel_file.relationships)
    rel_file.relationships = [
        r
        for r in rel_file.relationships
        if not (r.source == source and r.target == target and (rel_type is None or r.type == rel_type))
    ]

    removed = original_count - len(rel_file.relationships)
    if removed == 0:
        console.print("[yellow]No matching relationships found.[/yellow]")
        raise typer.Exit(0)

    if not force:
        confirm = typer.confirm(f"Delete {removed} relationship(s)?")
        if not confirm:
            raise typer.Abort()

    write_model(paths.relationships_yaml, rel_file)
    console.print(f"[green]Deleted {removed} relationship(s).[/green]")


@relationship_app.command("wizard")
def relationship_wizard() -> None:
    """Interactive wizard for creating relationships."""
    from infracontext.cli.relationships import run_wizard

    run_wizard()


# ============================================
# Chain Commands (request-path chains, chains.yaml)
# ============================================


@chain_app.command("add")
def chain_add(
    name: Annotated[str, typer.Argument(help="Chain name (slug-like, unique per project)")],
    members: Annotated[
        list[str],
        typer.Option(
            "--member",
            "-m",
            help="Node ref (type:slug or @scope:type:slug) in path order; repeat two or more times",
        ),
    ],
    rel_type: Annotated[
        str, typer.Option("--type", "-r", help="Edge type applied to each consecutive pair")
    ] = "routes_to",
    description: Annotated[str | None, typer.Option("--description", "-d", help="Description")] = None,
) -> None:
    """Add a request-path chain: one ordered entry describing lb -> app -> db.

    Stored in chains.yaml (never relationships.yaml) and expanded into
    consecutive pairwise edges at load time, so 'ic graph' and 'ic ctx' see
    ordinary relationships.
    """
    from infracontext.models.chain import Chain, ChainFile
    from infracontext.models.relationship import RelationshipType

    project = require_project()
    paths = ProjectPaths.for_project(project)

    try:
        rel_type_enum = RelationshipType(rel_type)
    except ValueError:
        valid_types = ", ".join(t.value for t in RelationshipType)
        console.print(f"[red]Invalid relationship type '{rel_type}'. Valid types: {valid_types}[/red]")
        raise typer.Exit(1) from None

    if len(members) < 2:
        console.print("[red]A chain needs at least two --member entries.[/red]")
        raise typer.Exit(1)

    for ref in members:
        _resolve_existing_node_ref(ref, project, paths)

    try:
        chain = Chain(name=name, description=description, type=rel_type_enum, members=list(members))
    except ValidationError as e:
        first = e.errors()[0] if e.errors() else {"msg": "validation error"}
        console.print(f"[red]Invalid chain: {first['msg']}[/red]")
        raise typer.Exit(1) from None

    chain_file = read_model(paths.chains_yaml, ChainFile) or ChainFile()
    if any(existing.name == name for existing in chain_file.chains):
        console.print(f"[red]Chain '{name}' already exists in project '{project}'.[/red]")
        raise typer.Exit(1)

    chain_file.chains.append(chain)
    write_model(paths.chains_yaml, chain_file)
    console.print(f"[green]Created chain '{name}': {' -> '.join(members)} ({rel_type})[/green]")


@chain_app.command("list")
def chain_list() -> None:
    """List request-path chains."""
    from infracontext.models.chain import ChainFile

    project = require_project()
    paths = ProjectPaths.for_project(project)

    chain_file = read_model(paths.chains_yaml, ChainFile)
    if not chain_file or not chain_file.chains:
        console.print("[dim]No chains defined.[/dim]")
        return

    table = Table(title=f"Chains in {project}")
    table.add_column("Name", style="cyan")
    table.add_column("Type", style="yellow")
    table.add_column("Path", style="cyan")
    table.add_column("Description")

    for chain in chain_file.chains:
        path_display = " -> ".join(
            member.id + (f" ({member.via})" if member.via else "") for member in chain.members
        )
        table.add_row(chain.name, str(chain.type), path_display, (chain.description or "")[:50])

    console.print(table)


# ============================================
# Source Commands (stubs for now)
# ============================================


@source_app.command("list")
def source_list() -> None:
    """List configured infrastructure sources."""
    project = require_project()
    paths = ProjectPaths.for_project(project)

    if not paths.sources_dir.exists():
        console.print("[dim]No sources configured.[/dim]")
        return

    source_files = list(paths.sources_dir.glob("*.yaml"))
    if not source_files:
        console.print("[dim]No sources configured.[/dim]")
        return

    table = Table(title=f"Sources in {project}")
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Status")

    for sf in sorted(source_files):
        data = read_yaml(sf)
        table.add_row(
            sf.stem,
            data.get("type", "unknown"),
            data.get("status", "unknown"),
        )

    console.print(table)


@source_app.command("add")
def source_add(
    name: Annotated[str, typer.Argument(help="Source name")],
    source_type: Annotated[str, typer.Option("--type", "-t", help="Source type (proxmox, manual)")] = "manual",
) -> None:
    """Add a new infrastructure source."""
    project = require_project()
    paths = ProjectPaths.for_project(project)

    source_file = _source_file_or_exit(paths, name)
    if source_file.exists():
        console.print(f"[red]Source '{name}' already exists.[/red]")
        raise typer.Exit(1)

    paths.sources_dir.mkdir(exist_ok=True)

    source_data: dict = {
        "version": "2.0",
        "name": name,
        "type": source_type,
        "status": "configured",
    }

    if source_type == "proxmox":
        source_data["api_url"] = ""
        source_data["api_token_id"] = ""
        source_data["verify_ssl"] = True
        source_data["exclusion_rules"] = {}

    write_yaml(source_file, source_data)
    console.print(f"[green]Added source '{name}' ({source_type})[/green]")
    console.print(f"[dim]Edit configuration at: {source_file}[/dim]")


@source_app.command("sync")
def source_sync(
    name: Annotated[str, typer.Argument(help="Source name to sync")],
) -> None:
    """Synchronize nodes from an infrastructure source."""
    import infracontext.sources  # noqa: F401 - triggers plugin registration
    from infracontext.sources.registry import get_plugin_instance

    project = require_project()
    paths = ProjectPaths.for_project(project)

    source_file = _source_file_or_exit(paths, name)
    if not source_file.exists():
        console.print(f"[red]Source '{name}' not found.[/red]")
        raise typer.Exit(1)

    config = read_yaml(source_file)
    source_type = config.get("type", "unknown")

    plugin = get_plugin_instance(source_type)
    if plugin is None:
        console.print(f"[red]Unknown source type: {source_type}[/red]")
        raise typer.Exit(1)

    console.print(f"[cyan]Syncing from {name}...[/cyan]")

    try:
        result = plugin.sync(project, name)
    except Exception as e:
        # Plugins normally return a FAILED result rather than raising, but an
        # unexpected error shouldn't escape as a raw traceback.
        console.print(f"[red]Sync failed unexpectedly ({type(e).__name__}): {e}[/red]")
        raise typer.Exit(1) from None

    if result.status == "success":
        console.print("[green]Sync completed successfully[/green]")
    elif result.status == "partial":
        console.print("[yellow]Sync completed with warnings[/yellow]")
    else:
        console.print(f"[red]Sync failed: {result.message}[/red]")
        raise typer.Exit(1)

    console.print(f"  Nodes created: {result.nodes_created}")
    console.print(f"  Nodes updated: {result.nodes_updated}")
    console.print(f"  Nodes unchanged: {result.nodes_unchanged}")
    console.print(f"  Relationships: {result.relationships_created}")
    console.print(f"  Duration: {result.duration_ms}ms")
    for warning in result.warnings:
        console.print(f"  [yellow]{warning}[/yellow]")


@source_app.command("configure")
def source_configure(
    name: Annotated[str, typer.Argument(help="Source name to configure")],
) -> None:
    """Interactively configure a source."""
    import os
    import subprocess

    project = require_project()
    paths = ProjectPaths.for_project(project)

    source_file = _source_file_or_exit(paths, name)
    if not source_file.exists():
        console.print(f"[red]Source '{name}' not found.[/red]")
        raise typer.Exit(1)

    editor = os.environ.get("EDITOR", "vi")
    subprocess.run(shlex.split(editor) + [str(source_file)])


@source_app.command("remove")
def source_remove(
    name: Annotated[str, typer.Argument(help="Source name to remove")],
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation")] = False,
) -> None:
    """Remove a source configuration."""
    project = require_project()
    paths = ProjectPaths.for_project(project)

    source_file = _source_file_or_exit(paths, name)
    if not source_file.exists():
        console.print(f"[red]Source '{name}' not found.[/red]")
        raise typer.Exit(1)

    if not force:
        confirm = typer.confirm(f"Remove source '{name}'? This does not delete synced nodes.")
        if not confirm:
            raise typer.Abort()

    source_file.unlink()
    console.print(f"[green]Removed source '{name}'[/green]")
