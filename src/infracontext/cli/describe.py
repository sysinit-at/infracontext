"""System description commands: project, node, relationship, source management."""

import shlex
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from infracontext.cli import require_environment, require_project
from infracontext.config import get_active_project, set_active_project
from infracontext.models.node import COMPUTE_NODE_TYPES, Learning, Node, NodeType
from infracontext.overrides import get_node_overrides
from infracontext.paths import (
    EnvironmentPaths,
    InvalidProjectSlugError,
    ProjectPaths,
    list_projects,
    project_exists,
    validate_project_slug,
)
from infracontext.storage import append_to_list, read_model, read_yaml, write_model, write_yaml


class OutputFormat(StrEnum):
    """Output format for LLM-facing commands."""

    yaml = "yaml"
    json = "json"
    toon = "toon"


app = typer.Typer(no_args_is_help=True)
console = Console()

# Sub-apps for different entity types
project_app = typer.Typer(help="Manage projects")
node_app = typer.Typer(help="Manage infrastructure nodes")
relationship_app = typer.Typer(help="Manage relationships between nodes")
source_app = typer.Typer(help="Manage infrastructure sources")

app.add_typer(project_app, name="project")
app.add_typer(node_app, name="node")
app.add_typer(relationship_app, name="relationship")
app.add_typer(source_app, name="source")


def read_node_with_overrides(node_file: Path, environment: EnvironmentPaths | None = None) -> Node | None:
    """Read a node from file and apply local overrides.

    Local overrides from .infracontext.local.yaml are applied for:
    - ssh_alias
    - source_paths
    """
    node = read_model(node_file, Node)
    if node is None:
        return None

    # Apply local overrides
    overrides = get_node_overrides(node.id, environment)
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
        console.print(f"[red]Project '{slug}' not found.[/red]")
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

    paths = ProjectPaths.for_project(slug)
    shutil.rmtree(paths.root)

    if get_active_project() == slug:
        set_active_project(None)

    console.print(f"[green]Deleted project '{slug}'[/green]")


# ============================================
# Node Commands
# ============================================


def _iter_all_nodes(paths: ProjectPaths, environment: EnvironmentPaths | None = None) -> list[Node]:
    """Iterate all nodes in the project, applying local overrides."""
    nodes = []
    if not paths.nodes_dir.exists():
        return nodes
    for type_dir in sorted(paths.nodes_dir.iterdir()):
        if not type_dir.is_dir():
            continue
        for node_file in sorted(type_dir.glob("*.yaml")):
            node = read_node_with_overrides(node_file, environment)
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


@node_app.command("find")
def node_find(
    query: Annotated[str, typer.Argument(help="Search query (domain, IP, name, or node ID)")],
    show_all: Annotated[bool, typer.Option("--all", "-a", help="Show all matches, not just first")] = False,
) -> None:
    """Find nodes by domain, IP, name, or ID.

    Searches across node domains, endpoint domains, IP addresses, names, and slugs.
    Useful for resolving "which server handles example.com?" type queries.

    Examples:
        ic describe node find kimai.example.com
        ic describe node find 192.168.1.100
        ic describe node find proxy
    """
    project = require_project()
    environment = require_environment()
    paths = ProjectPaths.for_project(project, environment)

    nodes = _iter_all_nodes(paths, environment)
    if not nodes:
        console.print("[dim]No nodes found.[/dim]")
        return

    matches: list[tuple[Node, str]] = []
    for node in nodes:
        matched, reason = _node_matches_query(node, query)
        if matched:
            matches.append((node, reason))

    if not matches:
        console.print(f"[yellow]No nodes found matching '{query}'[/yellow]")
        return

    if len(matches) == 1 or not show_all:
        # Single match or just show first
        node, reason = matches[0]
        console.print(f"[green]{node.id}[/green]  ({reason})")
        if len(matches) > 1:
            console.print(f"[dim]{len(matches) - 1} more match(es). Use --all to see all.[/dim]")
    else:
        table = Table(title=f"Nodes matching '{query}'")
        table.add_column("ID", style="cyan")
        table.add_column("Name")
        table.add_column("Match Reason")

        for node, reason in matches:
            table.add_row(node.id, node.name, reason)

        console.print(table)


@node_app.command("list")
def node_list(
    node_type: Annotated[NodeType | None, typer.Option("--type", "-t", help="Filter by node type")] = None,
) -> None:
    """List all nodes."""
    project = require_project()
    environment = require_environment()
    paths = ProjectPaths.for_project(project, environment)

    if not paths.nodes_dir.exists():
        console.print("[dim]No nodes found.[/dim]")
        return

    table = Table(title=f"Nodes in {project}")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Type")

    for type_dir in sorted(paths.nodes_dir.iterdir()):
        if not type_dir.is_dir():
            continue
        if node_type and type_dir.name != node_type:
            continue

        for node_file in sorted(type_dir.glob("*.yaml")):
            node = read_node_with_overrides(node_file, environment)
            if not node:
                continue

            table.add_row(
                node.id,
                node.name,
                node.type,
            )

    console.print(table)


@node_app.command("show")
def node_show(
    node_id: Annotated[str, typer.Argument(help="Node ID (type:slug)")],
) -> None:
    """Show details for a node."""
    project = require_project()
    paths = ProjectPaths.for_project(project)

    node_file = _node_file_from_id_or_exit(paths, node_id)

    if not node_file.exists():
        console.print(f"[red]Node '{node_id}' not found.[/red]")
        raise typer.Exit(1)

    node = read_node_with_overrides(node_file)
    if not node:
        console.print(f"[red]Failed to read node '{node_id}'.[/red]")
        raise typer.Exit(1)

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

    # Generate slug if not provided
    if not slug:
        slug = name.lower().replace(" ", "-").replace("_", "-")
        # Remove non-alphanumeric characters except hyphens
        slug = "".join(c for c in slug if c.isalnum() or c == "-")

    node_id = Node.make_id(node_type, slug)
    try:
        node_file = paths.node_file(node_type, slug)
    except ValueError as e:
        console.print(f"[red]Invalid slug '{slug}': {e}[/red]")
        raise typer.Exit(1) from None

    if node_file.exists():
        console.print(f"[red]Node '{node_id}' already exists.[/red]")
        raise typer.Exit(1)

    node = Node(
        id=node_id,
        slug=slug,
        type=node_type,
        name=name,
        description=description,
        ip_addresses=ip or [],
        domains=domain or [],
    )

    paths.node_type_dir(node_type).mkdir(parents=True, exist_ok=True)
    write_model(node_file, node, header_comment=f"Node: {name}")

    console.print(f"[green]Created node '{node_id}'[/green]")


@node_app.command("edit")
def node_edit(
    node_id: Annotated[str, typer.Argument(help="Node ID (type:slug)")],
) -> None:
    """Edit a node in your default editor."""
    import os
    import subprocess

    project = require_project()
    paths = ProjectPaths.for_project(project)

    node_file = _node_file_from_id_or_exit(paths, node_id)

    if not node_file.exists():
        console.print(f"[red]Node '{node_id}' not found.[/red]")
        raise typer.Exit(1)

    editor = os.environ.get("EDITOR", "vi")
    subprocess.run(shlex.split(editor) + [str(node_file)])


@node_app.command("delete")
def node_delete(
    node_id: Annotated[str, typer.Argument(help="Node ID (type:slug)")],
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation")] = False,
) -> None:
    """Delete a node."""
    project = require_project()
    paths = ProjectPaths.for_project(project)

    node_file = _node_file_from_id_or_exit(paths, node_id)

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
) -> dict:
    """Build node context as a dictionary for serialization."""
    from infracontext.config import load_project_config
    from infracontext.graph.loader import load_graph
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

    # Access tier information
    project_config = load_project_config(project)
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

    # Relationships
    if include_relationships:
        graph = load_graph(project)
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
    import subprocess

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
        case OutputFormat.toon:
            # Use npx to run toon CLI (Python implementation is incomplete)
            json_str = json.dumps(data, ensure_ascii=False)
            result = subprocess.run(
                ["npx", "--yes", "@toon-format/cli", "--encode"],
                input=json_str,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            return result.stdout


@node_app.command("context")
def node_context(
    node_id: Annotated[str, typer.Argument(help="Node ID (type:slug)")],
    include_relationships: Annotated[bool, typer.Option("--relationships", "-r", help="Include relationships")] = True,
    include_learnings: Annotated[bool, typer.Option("--learnings", "-l", help="Include learnings")] = True,
    fmt: Annotated[OutputFormat, typer.Option("--format", "-f", help="Output format")] = OutputFormat.yaml,
) -> None:
    """Output full node context for triage (used by Claude).

    This command outputs everything Claude needs to know about a node
    for troubleshooting, in a format optimized for LLM consumption.

    Use --format toon for token-efficient output when feeding to LLMs.
    """
    project = require_project()
    paths = ProjectPaths.for_project(project)

    node_file = _node_file_from_id_or_exit(paths, node_id)

    if not node_file.exists():
        console.print(f"[red]Node '{node_id}' not found.[/red]")
        raise typer.Exit(1)

    node = read_node_with_overrides(node_file)
    if not node:
        console.print(f"[red]Failed to read node '{node_id}'.[/red]")
        raise typer.Exit(1)

    context = _build_node_context(node, project, include_relationships, include_learnings)
    print(_format_output(context, fmt))


@node_app.command("learning")
def node_learning_add(
    node_id: Annotated[str, typer.Argument(help="Node ID (type:slug)")],
    finding: Annotated[str, typer.Argument(help="What was discovered")],
    context: Annotated[str, typer.Option("--context", "-c", help="What was being investigated")] = "triage",
    source: Annotated[str, typer.Option("--source", "-s", help="Who added this: 'agent' or 'human'")] = "agent",
) -> None:
    """Add a learning to a node.

    Learnings are discovered knowledge that helps future troubleshooting.
    Claude can use this command to record findings during triage.
    """
    from datetime import date

    project = require_project()
    paths = ProjectPaths.for_project(project)

    node_file = _node_file_from_id_or_exit(paths, node_id)

    if not node_file.exists():
        console.print(f"[red]Node '{node_id}' not found.[/red]")
        raise typer.Exit(1)

    learning = Learning(
        date=date.today().isoformat(),
        context=context,
        finding=finding,
        source=source,
    )

    append_to_list(node_file, "learnings", learning.model_dump(mode="json"))
    console.print(f"[green]Added learning to '{node_id}'[/green]")


# ============================================
# Relationship Commands
# ============================================


@relationship_app.command("list")
def relationship_list() -> None:
    """List all relationships."""
    from infracontext.models.relationship import RelationshipFile

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
        table.add_row(
            rel.source,
            rel.type,
            rel.target,
            (rel.description or "")[:50],
        )

    console.print(table)


@relationship_app.command("create")
def relationship_create(
    source: Annotated[str, typer.Option("--source", "-s", help="Source node ID")],
    target: Annotated[str, typer.Option("--target", "-t", help="Target node ID")],
    rel_type: Annotated[str, typer.Option("--type", "-r", help="Relationship type")],
    description: Annotated[str | None, typer.Option("--description", "-d", help="Description")] = None,
) -> None:
    """Create a relationship between two nodes."""
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

    # Validate source and target nodes exist
    for node_id in [source, target]:
        node_file = _node_file_from_id_or_exit(paths, node_id)
        if not node_file.exists():
            console.print(f"[red]Node '{node_id}' not found.[/red]")
            raise typer.Exit(1)

    # Validate relationship constraint
    source_type = source.split(":")[0]
    target_type = target.split(":")[0]
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

    # Add new relationship
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

    result = plugin.sync(project, name)

    if result.status == "success":
        console.print("[green]Sync completed successfully[/green]")
    elif result.status == "partial":
        console.print("[yellow]Sync completed with warnings[/yellow]")
    else:
        console.print(f"[red]Sync failed: {result.message}[/red]")
        raise typer.Exit(1)

    console.print(f"  Nodes created: {result.nodes_created}")
    console.print(f"  Nodes updated: {result.nodes_updated}")
    console.print(f"  Relationships: {result.relationships_created}")
    console.print(f"  Duration: {result.duration_ms}ms")


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
