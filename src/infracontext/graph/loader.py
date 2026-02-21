"""Load nodes and relationships into a NetworkX graph."""

import networkx as nx

from infracontext.models.node import Node
from infracontext.models.relationship import Relationship, RelationshipFile
from infracontext.paths import ProjectPaths
from infracontext.storage import read_model


def load_graph(project_slug: str) -> nx.DiGraph:
    """Load all nodes and relationships for a project into a directed graph.

    Nodes are stored as graph nodes with their full data as attributes.
    Relationships are stored as edges with relationship type and metadata.

    Args:
        project_slug: The project to load

    Returns:
        A NetworkX DiGraph with nodes and edges
    """
    paths = ProjectPaths.for_project(project_slug)
    graph = nx.DiGraph()

    # Load all nodes
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

    # Load relationships
    rel_file = read_model(paths.relationships_yaml, RelationshipFile)
    if rel_file:
        for rel in rel_file.relationships:
            # Only add edge if both nodes exist
            if graph.has_node(rel.source) and graph.has_node(rel.target):
                graph.add_edge(
                    rel.source,
                    rel.target,
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
