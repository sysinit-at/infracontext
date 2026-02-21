"""Graph analysis utilities for infrastructure health."""

from collections import Counter
from dataclasses import dataclass, field

import networkx as nx


@dataclass
class SPOFResult:
    """Single Point of Failure analysis result."""

    node_id: str
    node_name: str
    node_type: str
    affected_count: int
    affected_nodes: list[str] = field(default_factory=list)


@dataclass
class CycleResult:
    """Circular dependency detection result."""

    cycle: list[str]
    node_names: list[str]


@dataclass
class OrphanResult:
    """Orphaned node detection result."""

    node_id: str
    node_name: str
    node_type: str
    has_incoming: bool
    has_outgoing: bool


@dataclass
class HealthReport:
    """Overall infrastructure health report."""

    total_nodes: int
    total_relationships: int
    orphan_count: int
    cycle_count: int
    spof_count: int
    node_type_distribution: dict[str, int]
    relationship_type_distribution: dict[str, int]


def find_spofs(
    graph: nx.DiGraph,
    min_affected: int = 2,
) -> list[SPOFResult]:
    """Find single points of failure in the infrastructure.

    A SPOF is a node whose removal would disconnect or significantly
    impact other nodes. We identify these by:
    1. Finding articulation points (nodes whose removal increases components)
    2. Calculating how many nodes would be affected

    Args:
        graph: The infrastructure graph
        min_affected: Minimum number of affected nodes to report

    Returns:
        List of SPOFResult sorted by affected count descending
    """
    spofs = []

    for node_id in graph.nodes():
        node_data = graph.nodes[node_id]

        # Create a copy without this node
        test_graph = graph.copy()
        test_graph.remove_node(node_id)

        # Find all descendants that would lose their path to roots
        # (nodes with no predecessors in original graph)
        affected = set()
        for n in graph.successors(node_id):
            # Check if this successor still has a path from any root
            has_alternative = False
            for pred in graph.predecessors(n):
                if pred != node_id and nx.has_path(test_graph, pred, n):
                    has_alternative = True
                    break
            if not has_alternative:
                affected.add(n)
                affected.update(nx.descendants(graph, n))

        if len(affected) < min_affected:
            continue

        spofs.append(
            SPOFResult(
                node_id=node_id,
                node_name=node_data.get("name", node_id),
                node_type=node_data.get("type", "unknown"),
                affected_count=len(affected),
                affected_nodes=sorted(affected)[:10],  # Limit for display
            )
        )

    return sorted(spofs, key=lambda s: s.affected_count, reverse=True)


def find_cycles(graph: nx.DiGraph) -> list[CycleResult]:
    """Find all circular dependencies in the infrastructure.

    Args:
        graph: The infrastructure graph

    Returns:
        List of CycleResult, each containing the nodes in the cycle
    """
    cycles = []

    try:
        # Find all simple cycles
        for cycle in nx.simple_cycles(graph):
            if len(cycle) > 1:  # Ignore self-loops
                node_names = [graph.nodes[n].get("name", n) for n in cycle]
                cycles.append(CycleResult(cycle=cycle, node_names=node_names))
    except nx.NetworkXNoCycle:
        pass

    return cycles


def find_orphans(
    graph: nx.DiGraph,
    exclude_types: set[str] | None = None,
) -> list[OrphanResult]:
    """Find nodes with no relationships.

    Args:
        graph: The infrastructure graph
        exclude_types: Node types to exclude (e.g., applications are naturally roots)

    Returns:
        List of OrphanResult
    """
    exclude = exclude_types or {"application"}
    orphans = []

    for node_id, node_data in graph.nodes(data=True):
        node_type = node_data.get("type", "unknown")
        if node_type in exclude:
            continue

        in_degree = graph.in_degree(node_id)
        out_degree = graph.out_degree(node_id)

        if in_degree == 0 and out_degree == 0:
            orphans.append(
                OrphanResult(
                    node_id=node_id,
                    node_name=node_data.get("name", node_id),
                    node_type=node_type,
                    has_incoming=False,
                    has_outgoing=False,
                )
            )

    return orphans


def calculate_impact(graph: nx.DiGraph, node_id: str) -> dict:
    """Calculate the impact if a node fails.

    Args:
        graph: The infrastructure graph
        node_id: Node to analyze

    Returns:
        Dictionary with impact analysis
    """
    if node_id not in graph:
        return {"error": "Node not found"}

    node_data = graph.nodes[node_id]

    # Direct dependents
    direct_dependents = set(graph.successors(node_id))

    # All transitive dependents
    all_dependents = nx.descendants(graph, node_id)

    # Applications affected (trace to application layer)
    apps_affected = [n for n in all_dependents if graph.nodes[n].get("type") == "application"]

    return {
        "node_id": node_id,
        "node_name": node_data.get("name", node_id),
        "node_type": node_data.get("type"),
        "direct_dependents": len(direct_dependents),
        "total_affected": len(all_dependents),
        "applications_affected": len(apps_affected),
        "affected_nodes": sorted(all_dependents)[:20],
    }


def generate_health_report(graph: nx.DiGraph) -> HealthReport:
    """Generate an overall health report for the infrastructure.

    Args:
        graph: The infrastructure graph

    Returns:
        HealthReport with summary statistics
    """
    # Node type distribution
    node_types = Counter(data.get("type", "unknown") for _, data in graph.nodes(data=True))

    # Relationship type distribution
    rel_types = Counter(data.get("type", "unknown") for _, _, data in graph.edges(data=True))

    # Find issues
    orphans = find_orphans(graph)
    cycles = find_cycles(graph)
    spofs = find_spofs(graph, min_affected=3)

    return HealthReport(
        total_nodes=graph.number_of_nodes(),
        total_relationships=graph.number_of_edges(),
        orphan_count=len(orphans),
        cycle_count=len(cycles),
        spof_count=len(spofs),
        node_type_distribution=dict(node_types),
        relationship_type_distribution=dict(rel_types),
    )
