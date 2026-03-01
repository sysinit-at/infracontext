"""Load nodes and relationships into a NetworkX graph."""

import networkx as nx

from infracontext.models.node import Node
from infracontext.models.relationship import (
    Relationship,
    RelationshipFile,
    is_cross_project_ref,
    parse_node_ref,
)
from infracontext.paths import ProjectPaths
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
