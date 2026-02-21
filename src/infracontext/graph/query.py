"""Graph query utilities for dependency analysis."""

from collections.abc import Iterator

import networkx as nx

from infracontext.models.relationship import RelationshipType


def get_upstream(graph: nx.DiGraph, node_id: str, max_depth: int | None = None) -> set[str]:
    """Get all nodes that this node depends on (predecessors).

    Follows edges in reverse direction to find what the node depends on.

    Args:
        graph: The infrastructure graph
        node_id: Starting node ID
        max_depth: Maximum traversal depth (None for unlimited)

    Returns:
        Set of node IDs that are upstream dependencies
    """
    if node_id not in graph:
        return set()

    if max_depth is not None:
        # BFS with depth limit
        visited = set()
        current_level = {node_id}
        for _ in range(max_depth):
            next_level = set()
            for n in current_level:
                for pred in graph.predecessors(n):
                    if pred not in visited:
                        visited.add(pred)
                        next_level.add(pred)
            current_level = next_level
        return visited
    else:
        # All ancestors
        return nx.ancestors(graph, node_id)


def get_downstream(graph: nx.DiGraph, node_id: str, max_depth: int | None = None) -> set[str]:
    """Get all nodes that depend on this node (successors).

    Follows edges in forward direction to find what depends on the node.

    Args:
        graph: The infrastructure graph
        node_id: Starting node ID
        max_depth: Maximum traversal depth (None for unlimited)

    Returns:
        Set of node IDs that are downstream dependents
    """
    if node_id not in graph:
        return set()

    if max_depth is not None:
        # BFS with depth limit
        visited = set()
        current_level = {node_id}
        for _ in range(max_depth):
            next_level = set()
            for n in current_level:
                for succ in graph.successors(n):
                    if succ not in visited:
                        visited.add(succ)
                        next_level.add(succ)
            current_level = next_level
        return visited
    else:
        # All descendants
        return nx.descendants(graph, node_id)


def get_all_paths(
    graph: nx.DiGraph,
    source: str,
    target: str,
    max_paths: int = 10,
) -> list[list[str]]:
    """Find all simple paths between two nodes.

    Args:
        graph: The infrastructure graph
        source: Starting node ID
        target: Ending node ID
        max_paths: Maximum number of paths to return

    Returns:
        List of paths (each path is a list of node IDs)
    """
    if source not in graph or target not in graph:
        return []

    paths = []
    try:
        for path in nx.all_simple_paths(graph, source, target):
            paths.append(path)
            if len(paths) >= max_paths:
                break
    except nx.NetworkXNoPath:
        pass

    return paths


def get_shortest_path(graph: nx.DiGraph, source: str, target: str) -> list[str] | None:
    """Find the shortest path between two nodes.

    Args:
        graph: The infrastructure graph
        source: Starting node ID
        target: Ending node ID

    Returns:
        List of node IDs in the path, or None if no path exists
    """
    if source not in graph or target not in graph:
        return None

    try:
        return nx.shortest_path(graph, source, target)
    except nx.NetworkXNoPath:
        return None


def get_neighbors(
    graph: nx.DiGraph,
    node_id: str,
    relationship_type: RelationshipType | None = None,
    direction: str = "both",
) -> Iterator[tuple[str, str, dict]]:
    """Get neighboring nodes with optional filtering.

    Args:
        graph: The infrastructure graph
        node_id: Node to find neighbors for
        relationship_type: Optional filter by relationship type
        direction: "in" (predecessors), "out" (successors), or "both"

    Yields:
        Tuples of (neighbor_id, direction, edge_data)
    """
    if node_id not in graph:
        return

    if direction in ("in", "both"):
        for pred in graph.predecessors(node_id):
            edge_data = graph.edges[pred, node_id]
            if relationship_type is None or edge_data.get("type") == relationship_type:
                yield pred, "in", edge_data

    if direction in ("out", "both"):
        for succ in graph.successors(node_id):
            edge_data = graph.edges[node_id, succ]
            if relationship_type is None or edge_data.get("type") == relationship_type:
                yield succ, "out", edge_data


def get_nodes_by_type(graph: nx.DiGraph, node_type: str) -> list[str]:
    """Get all node IDs of a specific type.

    Args:
        graph: The infrastructure graph
        node_type: Node type to filter by

    Returns:
        List of matching node IDs
    """
    return [n for n, data in graph.nodes(data=True) if data.get("type") == node_type]
