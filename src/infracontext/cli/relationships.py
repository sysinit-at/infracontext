"""Interactive relationship wizard using prompt-toolkit."""

from prompt_toolkit import prompt
from prompt_toolkit.completion import FuzzyWordCompleter
from rich.console import Console
from rich.table import Table

from infracontext.config import get_active_project
from infracontext.graph.loader import load_all_nodes
from infracontext.models.node import Node
from infracontext.models.relationship import (
    Relationship,
    RelationshipFile,
    RelationshipType,
    get_valid_relationship_types,
    get_valid_targets_for_source,
)
from infracontext.paths import ProjectPaths
from infracontext.storage import read_model, write_model

console = Console()


def run_wizard() -> bool:
    """Run the interactive relationship creation wizard.

    Returns:
        True if a relationship was created, False otherwise
    """
    project = get_active_project()
    if not project:
        console.print("[red]No active project. Use 'ic describe project switch <name>' first.[/red]")
        return False

    # Load all nodes
    nodes = load_all_nodes(project)
    if not nodes:
        console.print("[yellow]No nodes found. Create some nodes first.[/yellow]")
        return False

    # Build node ID completer
    node_ids = [n.id for n in nodes]
    node_completer = FuzzyWordCompleter(node_ids)

    # Build lookup maps
    nodes_by_id: dict[str, Node] = {n.id: n for n in nodes}

    console.print()
    console.print("[bold]Relationship Wizard[/bold]")
    console.print("[dim]Press Ctrl-C to cancel at any time[/dim]")
    console.print()

    try:
        # Step 1: Select source node
        console.print("[cyan]Step 1:[/cyan] Select the source node")
        console.print("[dim]The source node is the one that has the dependency or connection[/dim]")
        console.print()

        source_id = prompt(
            "Source node: ",
            completer=node_completer,
            complete_while_typing=True,
        ).strip()

        if source_id not in nodes_by_id:
            console.print(f"[red]Node '{source_id}' not found.[/red]")
            return False

        source_node = nodes_by_id[source_id]
        source_type = source_node.type

        # Show valid targets for this source type
        valid_targets = get_valid_targets_for_source(source_type)
        if not valid_targets:
            console.print(f"[yellow]No valid relationship targets for {source_type}.[/yellow]")
            return False

        console.print()
        console.print(f"[green]Selected:[/green] {source_node.name} ({source_id})")
        console.print()

        # Show valid target types
        console.print("[dim]Valid target types for this source:[/dim]")
        for target_type, rel_types in sorted(valid_targets.items()):
            console.print(f"  [cyan]{target_type}[/cyan]: {', '.join(rel_types)}")
        console.print()

        # Step 2: Select target node
        console.print("[cyan]Step 2:[/cyan] Select the target node")
        console.print("[dim]The target node is what the source depends on or connects to[/dim]")
        console.print()

        # Filter nodes to only show valid targets
        valid_target_ids = [n.id for n in nodes if n.type in valid_targets and n.id != source_id]

        if not valid_target_ids:
            console.print("[yellow]No valid target nodes found for this source type.[/yellow]")
            return False

        target_completer = FuzzyWordCompleter(valid_target_ids)
        target_id = prompt(
            "Target node: ",
            completer=target_completer,
            complete_while_typing=True,
        ).strip()

        if target_id not in nodes_by_id:
            console.print(f"[red]Node '{target_id}' not found.[/red]")
            return False

        target_node = nodes_by_id[target_id]
        target_type = target_node.type

        # Validate the combination
        valid_rel_types = get_valid_relationship_types(source_type, target_type)
        if not valid_rel_types:
            console.print(f"[red]No valid relationships between {source_type} and {target_type}.[/red]")
            return False

        console.print()
        console.print(f"[green]Selected:[/green] {target_node.name} ({target_id})")
        console.print()

        # Step 3: Select relationship type
        console.print("[cyan]Step 3:[/cyan] Select the relationship type")
        console.print()

        # Show options with descriptions
        table = Table(show_header=True)
        table.add_column("#", style="cyan")
        table.add_column("Type")
        table.add_column("Description")

        rel_descriptions = {
            "depends_on": "Source requires target to function",
            "uses": "Source uses services provided by target",
            "runs_on": "Source executes on target infrastructure",
            "hosted_by": "Source is hosted on target",
            "member_of": "Source belongs to target group/cluster",
            "contains": "Source contains/manages target",
            "connects_to": "Source has network connection to target",
            "fronted_by": "Source is fronted/proxied by target",
            "resolves_to": "Source DNS resolves to target",
            "routes_to": "Source routes traffic to target",
            "uses_storage": "Source uses storage from target",
            "mounts": "Source mounts filesystem from target",
            "reads_from": "Source reads data from target",
            "writes_to": "Source writes data to target",
            "replicates_to": "Source replicates data to target",
        }

        for i, rel_type in enumerate(valid_rel_types, 1):
            table.add_row(str(i), rel_type, rel_descriptions.get(rel_type, ""))

        console.print(table)
        console.print()

        rel_type_completer = FuzzyWordCompleter(valid_rel_types)
        selected_type = prompt(
            "Relationship type (name or number): ",
            completer=rel_type_completer,
            complete_while_typing=True,
        ).strip()

        # Handle numeric selection
        if selected_type.isdigit():
            idx = int(selected_type) - 1
            if 0 <= idx < len(valid_rel_types):
                selected_type = valid_rel_types[idx]
            else:
                console.print("[red]Invalid selection.[/red]")
                return False

        if selected_type not in valid_rel_types:
            console.print(f"[red]Invalid relationship type '{selected_type}'.[/red]")
            return False

        rel_type_enum = RelationshipType(selected_type)

        console.print()
        console.print(f"[green]Selected:[/green] {selected_type}")
        console.print()

        # Step 4: Optional description
        console.print("[cyan]Step 4:[/cyan] Add a description (optional)")
        console.print()

        description = prompt("Description (press Enter to skip): ").strip() or None

        console.print()

        # Confirm
        console.print("[bold]Summary:[/bold]")
        console.print(
            f"  {source_node.name} ({source_id})\n    --[{selected_type}]-->\n  {target_node.name} ({target_id})"
        )
        if description:
            console.print(f"  Description: {description}")
        console.print()

        confirm = prompt("Create this relationship? [Y/n]: ").strip().lower()
        if confirm and confirm != "y":
            console.print("[yellow]Cancelled.[/yellow]")
            return False

        # Create the relationship
        paths = ProjectPaths.for_project(project)
        rel_file = read_model(paths.relationships_yaml, RelationshipFile) or RelationshipFile()

        # Check for duplicate
        for existing in rel_file.relationships:
            if existing.source == source_id and existing.target == target_id and existing.type == rel_type_enum:
                console.print("[yellow]This relationship already exists.[/yellow]")
                return False

        rel = Relationship(
            source=source_id,
            target=target_id,
            type=rel_type_enum,
            description=description,
        )
        rel_file.relationships.append(rel)

        write_model(paths.relationships_yaml, rel_file)
        console.print("[green]Created relationship![/green]")
        return True

    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return False
    except EOFError:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return False
