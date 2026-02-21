"""Tests for infracontext.graph — analysis, query, and loader."""

import networkx as nx

from infracontext.graph.analysis import calculate_impact, find_cycles, find_orphans, find_spofs
from infracontext.graph.query import get_downstream, get_upstream
from infracontext.models.node import Node, NodeType
from infracontext.models.relationship import Relationship, RelationshipFile, RelationshipType
from infracontext.storage import write_model

# ── get_upstream / get_downstream ─────────────────────────────────


class TestUpstreamDownstream:
    def test_upstream_chain(self, sample_graph):
        upstream = get_upstream(sample_graph, "physical_host:host-01")
        assert "vm:db-01" in upstream
        assert "vm:web-01" in upstream

    def test_downstream_chain(self, sample_graph):
        downstream = get_downstream(sample_graph, "vm:web-01")
        assert "vm:db-01" in downstream
        assert "physical_host:host-01" in downstream

    def test_upstream_depth_limit(self, sample_graph):
        upstream = get_upstream(sample_graph, "physical_host:host-01", max_depth=1)
        assert "vm:db-01" in upstream
        assert "vm:web-01" not in upstream  # too deep

    def test_downstream_depth_limit(self, sample_graph):
        downstream = get_downstream(sample_graph, "vm:web-01", max_depth=1)
        assert "vm:db-01" in downstream
        assert "physical_host:host-01" not in downstream

    def test_nonexistent_node_returns_empty(self, sample_graph):
        assert get_upstream(sample_graph, "nonexistent") == set()
        assert get_downstream(sample_graph, "nonexistent") == set()


# ── find_spofs ────────────────────────────────────────────────────


class TestFindSPOFs:
    def test_bridge_node_detected(self):
        """vm:db is a SPOF: removing it disconnects vm:app from host."""
        g = nx.DiGraph()
        g.add_node("vm:app", name="App", type="vm")
        g.add_node("vm:db", name="DB", type="vm")
        g.add_node("physical_host:h1", name="Host", type="physical_host")
        g.add_edge("vm:app", "vm:db", type="depends_on")
        g.add_edge("vm:db", "physical_host:h1", type="runs_on")

        spofs = find_spofs(g, min_affected=1)
        spof_ids = [s.node_id for s in spofs]
        assert "vm:db" in spof_ids

    def test_redundant_graph_no_spofs(self):
        """Two paths to host = no SPOF."""
        g = nx.DiGraph()
        g.add_node("vm:app", name="App", type="vm")
        g.add_node("vm:db1", name="DB1", type="vm")
        g.add_node("vm:db2", name="DB2", type="vm")
        g.add_node("physical_host:h1", name="Host", type="physical_host")
        g.add_edge("vm:app", "vm:db1", type="depends_on")
        g.add_edge("vm:app", "vm:db2", type="depends_on")
        g.add_edge("vm:db1", "physical_host:h1", type="runs_on")
        g.add_edge("vm:db2", "physical_host:h1", type="runs_on")

        spofs = find_spofs(g, min_affected=2)
        # db1 and db2 are redundant, so neither is a SPOF with min_affected=2
        spof_ids = [s.node_id for s in spofs]
        assert "vm:db1" not in spof_ids
        assert "vm:db2" not in spof_ids


# ── find_cycles ───────────────────────────────────────────────────


class TestFindCycles:
    def test_cycle_detected(self):
        g = nx.DiGraph()
        g.add_node("a", name="A", type="vm")
        g.add_node("b", name="B", type="vm")
        g.add_edge("a", "b")
        g.add_edge("b", "a")

        cycles = find_cycles(g)
        assert len(cycles) >= 1
        cycle_nodes = set()
        for c in cycles:
            cycle_nodes.update(c.cycle)
        assert "a" in cycle_nodes
        assert "b" in cycle_nodes

    def test_dag_no_cycles(self, sample_graph):
        cycles = find_cycles(sample_graph)
        assert cycles == []


# ── find_orphans ──────────────────────────────────────────────────


class TestFindOrphans:
    def test_isolated_node_found(self):
        g = nx.DiGraph()
        g.add_node("vm:lonely", name="Lonely", type="vm")
        g.add_node("vm:connected", name="Connected", type="vm")
        g.add_edge("vm:connected", "vm:lonely-target", type="depends_on")
        g.add_node("vm:lonely-target", name="Target", type="vm")

        orphans = find_orphans(g)
        orphan_ids = [o.node_id for o in orphans]
        assert "vm:lonely" in orphan_ids

    def test_application_type_excluded(self):
        g = nx.DiGraph()
        g.add_node("application:myapp", name="MyApp", type="application")

        orphans = find_orphans(g)
        orphan_ids = [o.node_id for o in orphans]
        assert "application:myapp" not in orphan_ids

    def test_connected_node_not_orphan(self, sample_graph):
        orphans = find_orphans(sample_graph)
        orphan_ids = [o.node_id for o in orphans]
        assert "vm:web-01" not in orphan_ids


# ── calculate_impact ──────────────────────────────────────────────


class TestCalculateImpact:
    def test_transitive_dependents(self, sample_graph):
        impact = calculate_impact(sample_graph, "vm:web-01")
        assert impact["total_affected"] == 2  # db-01 and host-01

    def test_leaf_node_no_impact(self, sample_graph):
        impact = calculate_impact(sample_graph, "physical_host:host-01")
        assert impact["total_affected"] == 0

    def test_nonexistent_node(self, sample_graph):
        impact = calculate_impact(sample_graph, "nonexistent")
        assert "error" in impact


# ── load_graph (end-to-end from YAML) ────────────────────────────


class TestLoadGraph:
    def test_load_from_yaml(self, tmp_project, tmp_environment, monkeypatch_environment, monkeypatch):
        """End-to-end: write nodes + relationships, then load graph."""
        from infracontext.graph.loader import load_graph

        # Write two nodes
        web = Node(id="vm:web", slug="web", type=NodeType.VM, name="Web")
        db = Node(id="vm:db", slug="db", type=NodeType.VM, name="DB")

        tmp_project.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(tmp_project.node_file("vm", "web"), web)
        write_model(tmp_project.node_file("vm", "db"), db)

        # Write a relationship
        rel = Relationship(
            source="vm:web",
            target="vm:db",
            type=RelationshipType.DEPENDS_ON,
            description="DB connection",
        )
        rel_file = RelationshipFile(relationships=[rel])
        write_model(tmp_project.relationships_yaml, rel_file)

        # Patch ProjectPaths.for_project to return our tmp_project
        monkeypatch.setattr(
            "infracontext.graph.loader.ProjectPaths.for_project",
            lambda _slug, _env=None: tmp_project,
        )

        graph = load_graph("testproject")
        assert graph.number_of_nodes() == 2
        assert graph.has_edge("vm:web", "vm:db")
