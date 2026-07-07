"""Graph analysis utilities for infrastructure health."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
    """Find single points of failure.

    Edge convention: ``source -> target`` means "source depends on target."
    A predecessor ``p`` of ``node_id`` is *dependency-orphaned* by removing
    ``node_id`` iff ``p`` has no other successor — i.e. ``node_id`` is its
    only dependency. We then propagate via :func:`nx.ancestors` to count
    everything transitively depending on those orphans.

    Previous implementation copied the graph per node and ran
    ``nx.has_path`` per outgoing edge of every predecessor, which is
    O(V * (V + E + deg_in * deg_out * (V + E))). It was also semantically
    weak: ``has_path(p, succ)`` after removing ``node_id`` is trivially
    true when ``succ != node_id`` (the direct edge ``p -> succ`` still
    exists), so the "alternative" check collapsed to "does ``p`` have
    any successor besides ``node_id``" — exactly the simple out-degree
    check we now do directly.

    Args:
        graph: The infrastructure graph.
        min_affected: Minimum transitively-affected nodes to report.

    Returns:
        SPOFResult list sorted by affected count descending.
    """
    import networkx as nx

    spofs = []
    # Cache ancestors so each subtree is walked at most once.
    ancestors_cache: dict[str, set[str]] = {}

    def _ancestors(n: str) -> set[str]:
        cached = ancestors_cache.get(n)
        if cached is None:
            cached = nx.ancestors(graph, n)
            ancestors_cache[n] = cached
        return cached

    for node_id in graph.nodes():
        orphaned = {
            p
            for p in graph.predecessors(node_id)
            if graph.out_degree(p) == 1  # only successor is node_id
        }
        if not orphaned:
            continue

        affected: set[str] = set(orphaned)
        for p in orphaned:
            affected.update(_ancestors(p))

        if len(affected) < min_affected:
            continue

        node_data = graph.nodes[node_id]
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
    import networkx as nx

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

    Edge convention: source -> target means "source depends on target".
    So nodes affected by a failure are predecessors (things that depend on this node).

    Args:
        graph: The infrastructure graph
        node_id: Node to analyze

    Returns:
        Dictionary with impact analysis
    """
    import networkx as nx

    if node_id not in graph:
        return {"error": "Node not found"}

    node_data = graph.nodes[node_id]

    # Direct dependents (nodes that depend on this node = predecessors)
    direct_dependents = set(graph.predecessors(node_id))

    # All transitive dependents (ancestors in the "depends on" graph)
    all_dependents = nx.ancestors(graph, node_id)

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
