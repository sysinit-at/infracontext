"""Load nodes and relationships into a NetworkX graph."""

import networkx as nx

from infracontext.models.node import Node
from infracontext.models.relationship import (
    Relationship,
    RelationshipFile,
    is_cross_project_ref,
    parse_node_ref,
)
from infracontext.paths import ProjectPaths, list_projects
from infracontext.storage import read_model


def _resolve_cross_project_node(
    ref: str,
    project_slug: str,
    graph: nx.DiGraph,
) -> str | None:
    """Resolve a potentially cross-project node reference.

    If the ref is a cross-project reference (@project:type:slug), load that
    specific node from the other project and add it to the graph. Returns
    the local node_id used in the graph, or None if the node cannot be found.

    For same-project refs, returns the node_id unchanged (assumes it's
    already loaded in the graph or will be checked separately).
    """
    project, node_id = parse_node_ref(ref, project_slug)

    if project == project_slug:
        # Same project, node should already be in the graph
        return node_id

    # Cross-project: load the specific node if not already in the graph
    if not graph.has_node(node_id):
        node = load_node(project, node_id)
        if node is None:
            return None
        graph.add_node(
            node.id,
            node=node,
            name=node.name,
            type=node.type,
            project=project,
        )

    return node_id


def load_graph(project_slug: str) -> nx.DiGraph:
    """Load all nodes and relationships for a project into a directed graph.

    Nodes are stored as graph nodes with their full data as attributes.
    Relationships are stored as edges with relationship type and metadata.

    Cross-project references (using @project:type:slug format) are resolved
    by loading only the specific referenced nodes from other projects.

    Args:
        project_slug: The project to load

    Returns:
        A NetworkX DiGraph with nodes and edges
    """
    paths = ProjectPaths.for_project(project_slug)
    graph = nx.DiGraph()

    # Load all nodes from this project
    if paths.nodes_dir.exists():
        for type_dir in paths.nodes_dir.iterdir():
            if not type_dir.is_dir():
                continue
            for node_file in type_dir.glob("*.yaml"):
                node = read_model(node_file, Node)
                if node:
                    graph.add_node(
                        node.id,
                        node=node,
                        name=node.name,
                        type=node.type,
                    )

    # Load relationships, resolving cross-project refs
    rel_file = read_model(paths.relationships_yaml, RelationshipFile)
    if rel_file:
        for rel in rel_file.relationships:
            # Resolve source and target, handling cross-project refs
            if is_cross_project_ref(rel.source) or is_cross_project_ref(rel.target):
                source_id = _resolve_cross_project_node(rel.source, project_slug, graph)
                target_id = _resolve_cross_project_node(rel.target, project_slug, graph)
                if source_id is None or target_id is None:
                    continue
            else:
                source_id = rel.source
                target_id = rel.target

            # Only add edge if both nodes exist
            if graph.has_node(source_id) and graph.has_node(target_id):
                graph.add_edge(
                    source_id,
                    target_id,
                    relationship=rel,
                    type=rel.type,
                    description=rel.description,
                )

    return graph


def load_node(project_slug: str, node_id: str) -> Node | None:
    """Load a single node by ID.

    Args:
        project_slug: The project
        node_id: Node ID in format type:slug

    Returns:
        The Node or None if not found
    """
    if ":" not in node_id:
        return None

    paths = ProjectPaths.for_project(project_slug)
    node_type, slug = node_id.split(":", 1)
    try:
        node_file = paths.node_file(node_type, slug)
    except ValueError:
        return None

    if not node_file.exists():
        return None

    return read_model(node_file, Node)


def load_all_nodes(project_slug: str) -> list[Node]:
    """Load all nodes for a project.

    Args:
        project_slug: The project

    Returns:
        List of all nodes
    """
    paths = ProjectPaths.for_project(project_slug)
    nodes = []

    if not paths.nodes_dir.exists():
        return nodes

    for type_dir in paths.nodes_dir.iterdir():
        if not type_dir.is_dir():
            continue
        for node_file in type_dir.glob("*.yaml"):
            node = read_model(node_file, Node)
            if node:
                nodes.append(node)

    return nodes


def load_relationships(project_slug: str) -> list[Relationship]:
    """Load all relationships for a project.

    Args:
        project_slug: The project

    Returns:
        List of all relationships
    """
    paths = ProjectPaths.for_project(project_slug)
    rel_file = read_model(paths.relationships_yaml, RelationshipFile)
    return rel_file.relationships if rel_file else []


def load_merged_graph() -> nx.DiGraph:
    """Load all nodes and relationships from all projects into a single graph.

    Node IDs are qualified with their project slug to avoid collisions
    across projects (e.g., two projects both having vm:db-01).

    Qualified format: "project_slug/node_id" (e.g., "customer-acme/vm:web-01")

    Cross-project references are resolved to their qualified form.

    Returns:
        A NetworkX DiGraph containing all projects' nodes and edges.
    """
    graph = nx.DiGraph()
    projects = list_projects()

    # Pass 1: load all nodes from all projects
    for project_slug in projects:
        paths = ProjectPaths.for_project(project_slug)
        if paths.nodes_dir.exists():
            for type_dir in paths.nodes_dir.iterdir():
                if not type_dir.is_dir():
                    continue
                for node_file in type_dir.glob("*.yaml"):
                    node = read_model(node_file, Node)
                    if node:
                        qualified_id = f"{project_slug}/{node.id}"
                        graph.add_node(
                            qualified_id,
                            node=node,
                            name=node.name,
                            type=node.type,
                            project=project_slug,
                        )

    # Pass 2: load relationships (all nodes must exist before resolving refs)
    for project_slug in projects:
        paths = ProjectPaths.for_project(project_slug)
        rel_file = read_model(paths.relationships_yaml, RelationshipFile)
        if rel_file:
            for rel in rel_file.relationships:
                source_project, source_node_id = parse_node_ref(rel.source, project_slug)
                target_project, target_node_id = parse_node_ref(rel.target, project_slug)

                qualified_source = f"{source_project}/{source_node_id}"
                qualified_target = f"{target_project}/{target_node_id}"

                if graph.has_node(qualified_source) and graph.has_node(qualified_target):
                    graph.add_edge(
                        qualified_source,
                        qualified_target,
                        relationship=rel,
                        type=rel.type,
                        description=rel.description,
                        project=project_slug,
                    )

    return graph


def unqualify_node_id(qualified_id: str) -> tuple[str, str]:
    """Split a qualified node ID into (project_slug, node_id).

    The node_id always contains ":" (type:slug), so the boundary is the
    last "/" before the first ":". This handles hierarchical project slugs
    like "org/team".

    Examples:
        "customer-acme/vm:web-01" -> ("customer-acme", "vm:web-01")
        "org/team/vm:web-01"      -> ("org/team", "vm:web-01")
        "vm:web-01"               -> ("", "vm:web-01")
    """
    colon_pos = qualified_id.find(":")
    if colon_pos == -1:
        return "", qualified_id

    # Last "/" before the ":" is the project/node boundary
    slash_pos = qualified_id.rfind("/", 0, colon_pos)
    if slash_pos == -1:
        return "", qualified_id

    return qualified_id[:slash_pos], qualified_id[slash_pos + 1 :]
