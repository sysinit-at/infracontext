"""Tests for infracontext.graph — analysis, query, and loader."""

import networkx as nx

from infracontext.graph.analysis import calculate_impact, find_cycles, find_orphans, find_spofs
from infracontext.graph.query import get_downstream, get_upstream
from infracontext.models.node import Node, NodeType
from infracontext.models.relationship import Relationship, RelationshipFile, RelationshipType
from infracontext.paths import ProjectPaths
from infracontext.storage import write_model

# ── get_upstream / get_downstream ─────────────────────────────────


class TestUpstreamDownstream:
    """Edge convention: source -> target = 'source depends on target'.

    sample_graph: web -> db -> host
      web depends on db, db runs on host.
    """

    def test_upstream_follows_outgoing_edges(self, sample_graph):
        # web depends on db and (transitively) host
        upstream = get_upstream(sample_graph, "vm:web-01")
        assert "vm:db-01" in upstream
        assert "physical_host:host-01" in upstream

    def test_upstream_leaf_returns_empty(self, sample_graph):
        # host has no outgoing edges — depends on nothing
        assert get_upstream(sample_graph, "physical_host:host-01") == set()

    def test_downstream_follows_incoming_edges(self, sample_graph):
        # host is depended on by db and (transitively) web
        downstream = get_downstream(sample_graph, "physical_host:host-01")
        assert "vm:db-01" in downstream
        assert "vm:web-01" in downstream

    def test_downstream_root_returns_empty(self, sample_graph):
        # nothing depends on web
        assert get_downstream(sample_graph, "vm:web-01") == set()

    def test_upstream_depth_limit(self, sample_graph):
        # web at depth 1: only direct dependency (db), not host
        upstream = get_upstream(sample_graph, "vm:web-01", max_depth=1)
        assert "vm:db-01" in upstream
        assert "physical_host:host-01" not in upstream  # too deep

    def test_downstream_depth_limit(self, sample_graph):
        # host at depth 1: only direct dependent (db), not web
        downstream = get_downstream(sample_graph, "physical_host:host-01", max_depth=1)
        assert "vm:db-01" in downstream
        assert "vm:web-01" not in downstream  # too deep

    def test_nonexistent_node_returns_empty(self, sample_graph):
        assert get_upstream(sample_graph, "nonexistent") == set()
        assert get_downstream(sample_graph, "nonexistent") == set()


# ── find_spofs ────────────────────────────────────────────────────


class TestFindSPOFs:
    def test_bridge_node_detected(self):
        """vm:db is a SPOF: app depends on it, no alternative."""
        g = nx.DiGraph()
        g.add_node("vm:app", name="App", type="vm")
        g.add_node("vm:db", name="DB", type="vm")
        g.add_node("physical_host:h1", name="Host", type="physical_host")
        g.add_edge("vm:app", "vm:db", type="depends_on")
        g.add_edge("vm:db", "physical_host:h1", type="runs_on")

        spofs = find_spofs(g, min_affected=1)
        spof_ids = [s.node_id for s in spofs]
        assert "vm:db" in spof_ids

    def test_redundant_dependencies_not_spofs(self):
        """db1/db2 are redundant so neither is a SPOF, but host is."""
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
        spof_ids = [s.node_id for s in spofs]
        # db1 and db2 are redundant — app has alternatives
        assert "vm:db1" not in spof_ids
        assert "vm:db2" not in spof_ids
        # host IS a SPOF — both db1 and db2 depend on it with no alternative
        assert "physical_host:h1" in spof_ids


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
    def test_host_failure_affects_all(self, sample_graph):
        # If host fails, db and web are affected (both depend on it transitively)
        impact = calculate_impact(sample_graph, "physical_host:host-01")
        assert impact["total_affected"] == 2  # web and db

    def test_leaf_dependent_no_impact(self, sample_graph):
        # Nothing depends on web, so its failure affects nothing
        impact = calculate_impact(sample_graph, "vm:web-01")
        assert impact["total_affected"] == 0

    def test_middle_node_partial_impact(self, sample_graph):
        # If db fails, only web is affected (web depends on db)
        impact = calculate_impact(sample_graph, "vm:db-01")
        assert impact["total_affected"] == 1
        assert "vm:web-01" in impact["affected_nodes"]

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

    def test_corrupt_node_file_skipped_not_fatal(
        self, tmp_project, tmp_environment, monkeypatch_environment, monkeypatch, caplog
    ):
        """A single malformed node YAML must not abort the whole graph load.

        The valid node should still appear; the corrupt one is skipped with a
        warning pointing at `ic doctor`.
        """
        import logging

        from infracontext.graph.loader import load_graph

        good = Node(id="vm:good", slug="good", type=NodeType.VM, name="Good")
        tmp_project.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(tmp_project.node_file("vm", "good"), good)

        # A sibling file with invalid YAML (unclosed bracket)
        (tmp_project.node_file("vm", "bad")).write_text("foo: [unclosed\n")

        monkeypatch.setattr(
            "infracontext.graph.loader.ProjectPaths.for_project",
            lambda _slug, _env=None: tmp_project,
        )

        with caplog.at_level(logging.WARNING):
            graph = load_graph("testproject")

        # Good node survived
        assert graph.has_node("vm:good")
        # Bad node was skipped, not added
        assert not graph.has_node("vm:bad")
        # A warning mentioning the file + doctor was emitted
        assert "bad" in caplog.text
        assert "doctor" in caplog.text.lower()

    def test_invalid_schema_node_skipped(
        self, tmp_project, tmp_environment, monkeypatch_environment, monkeypatch, caplog
    ):
        """A node that parses as YAML but fails Pydantic validation is skipped."""
        import logging

        from infracontext.graph.loader import load_graph

        good = Node(id="vm:good", slug="good", type=NodeType.VM, name="Good")
        tmp_project.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(tmp_project.node_file("vm", "good"), good)

        # Valid YAML, but missing required 'name' field
        (tmp_project.node_file("vm", "bad")).write_text(
            "version: '2.0'\nid: vm:bad\nslug: bad\ntype: vm\n"
        )

        monkeypatch.setattr(
            "infracontext.graph.loader.ProjectPaths.for_project",
            lambda _slug, _env=None: tmp_project,
        )

        with caplog.at_level(logging.WARNING):
            graph = load_graph("testproject")

        assert graph.has_node("vm:good")
        assert not graph.has_node("vm:bad")
        assert "bad" in caplog.text
        assert "doctor" in caplog.text.lower()

    def test_corrupt_relationships_degrades_to_nodes_only(
        self, tmp_project, tmp_environment, monkeypatch_environment, monkeypatch, caplog
    ):
        """A malformed relationships.yaml must not abort the graph load —
        nodes still appear (edges are lost), with a warning pointing at
        `ic doctor`. Same skip-and-warn contract as corrupt node files.
        """
        import logging

        from infracontext.graph.loader import load_graph

        good = Node(id="vm:good", slug="good", type=NodeType.VM, name="Good")
        tmp_project.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(tmp_project.node_file("vm", "good"), good)

        tmp_project.relationships_yaml.write_text("relationships: [unclosed\n")

        monkeypatch.setattr(
            "infracontext.graph.loader.ProjectPaths.for_project",
            lambda _slug, _env=None: tmp_project,
        )

        with caplog.at_level(logging.WARNING):
            graph = load_graph("testproject")

        assert graph.has_node("vm:good")
        assert graph.number_of_edges() == 0
        assert "relationships" in caplog.text.lower()
        assert "doctor" in caplog.text.lower()

    def test_load_node_corrupt_file_returns_none(
        self, tmp_project, tmp_environment, monkeypatch_environment, monkeypatch, caplog
    ):
        """A targeted load of a corrupt node yields None + warning, not a
        traceback — this path is also hit when resolving cross-project refs
        during a graph load, which must stay resilient end to end.
        """
        import logging

        from infracontext.graph.loader import load_node

        tmp_project.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        (tmp_project.node_file("vm", "bad")).write_text("foo: [unclosed\n")

        monkeypatch.setattr(
            "infracontext.graph.loader.ProjectPaths.for_project",
            lambda _slug, _env=None: tmp_project,
        )

        with caplog.at_level(logging.WARNING):
            node = load_node("testproject", "vm:bad")

        assert node is None
        assert "doctor" in caplog.text.lower()


# ── load_merged_graph ────────────────────────────────────────────


def _patch_merged_graph(monkeypatch, tmp_environment, project_slugs):
    """Patch list_projects and ProjectPaths.for_project for merged graph tests."""
    _orig_for_project = ProjectPaths.for_project.__func__

    monkeypatch.setattr(
        "infracontext.graph.loader.list_projects",
        lambda **_kw: project_slugs,
    )
    monkeypatch.setattr(
        "infracontext.graph.loader.ProjectPaths.for_project",
        lambda slug, _env=None: _orig_for_project(ProjectPaths, slug, tmp_environment),
    )


class TestLoadMergedGraph:
    def test_nodes_qualified_with_project(self, tmp_environment, monkeypatch_environment, monkeypatch):
        """Nodes from different projects get project-qualified IDs."""
        from infracontext.graph.loader import load_merged_graph

        proj_a = ProjectPaths.for_project("proj-a", tmp_environment)
        proj_a.ensure_dirs()
        proj_b = ProjectPaths.for_project("proj-b", tmp_environment)
        proj_b.ensure_dirs()

        web = Node(id="vm:web", slug="web", type=NodeType.VM, name="Web")
        db = Node(id="vm:db", slug="db", type=NodeType.VM, name="DB")

        proj_a.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(proj_a.node_file("vm", "web"), web)

        proj_b.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(proj_b.node_file("vm", "db"), db)

        _patch_merged_graph(monkeypatch, tmp_environment, ["proj-a", "proj-b"])

        graph = load_merged_graph()
        assert graph.number_of_nodes() == 2
        assert graph.has_node("proj-a/vm:web")
        assert graph.has_node("proj-b/vm:db")
        assert graph.nodes["proj-a/vm:web"]["project"] == "proj-a"
        assert graph.nodes["proj-b/vm:db"]["project"] == "proj-b"

    def test_same_node_id_different_projects_no_collision(
        self, tmp_environment, monkeypatch_environment, monkeypatch
    ):
        """Two projects can have the same node ID without collision."""
        from infracontext.graph.loader import load_merged_graph

        proj_a = ProjectPaths.for_project("proj-a", tmp_environment)
        proj_a.ensure_dirs()
        proj_b = ProjectPaths.for_project("proj-b", tmp_environment)
        proj_b.ensure_dirs()

        node_a = Node(id="vm:db-01", slug="db-01", type=NodeType.VM, name="DB Alpha")
        node_b = Node(id="vm:db-01", slug="db-01", type=NodeType.VM, name="DB Beta")

        proj_a.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(proj_a.node_file("vm", "db-01"), node_a)

        proj_b.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(proj_b.node_file("vm", "db-01"), node_b)

        _patch_merged_graph(monkeypatch, tmp_environment, ["proj-a", "proj-b"])

        graph = load_merged_graph()
        assert graph.number_of_nodes() == 2
        assert graph.nodes["proj-a/vm:db-01"]["name"] == "DB Alpha"
        assert graph.nodes["proj-b/vm:db-01"]["name"] == "DB Beta"

    def test_relationships_qualified(self, tmp_environment, monkeypatch_environment, monkeypatch):
        """Edges use qualified node IDs, including cross-project refs."""
        from infracontext.graph.loader import load_merged_graph

        proj_a = ProjectPaths.for_project("proj-a", tmp_environment)
        proj_a.ensure_dirs()
        proj_b = ProjectPaths.for_project("proj-b", tmp_environment)
        proj_b.ensure_dirs()

        web = Node(id="vm:web", slug="web", type=NodeType.VM, name="Web")
        db = Node(id="vm:db", slug="db", type=NodeType.VM, name="DB")

        proj_a.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(proj_a.node_file("vm", "web"), web)
        proj_b.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(proj_b.node_file("vm", "db"), db)

        # proj-a: web depends on proj-b's db (cross-project ref)
        rel = Relationship(
            source="vm:web",
            target="@proj-b:vm:db",
            type=RelationshipType.DEPENDS_ON,
            description="Remote DB",
        )
        write_model(proj_a.relationships_yaml, RelationshipFile(relationships=[rel]))

        _patch_merged_graph(monkeypatch, tmp_environment, ["proj-a", "proj-b"])

        graph = load_merged_graph()
        assert graph.has_edge("proj-a/vm:web", "proj-b/vm:db")
        edge_data = graph.edges["proj-a/vm:web", "proj-b/vm:db"]
        assert edge_data["project"] == "proj-a"

    def test_edge_skipped_when_target_missing(self, tmp_environment, monkeypatch_environment, monkeypatch):
        """Edges referencing nonexistent nodes are silently skipped."""
        from infracontext.graph.loader import load_merged_graph

        proj = ProjectPaths.for_project("proj-a", tmp_environment)
        proj.ensure_dirs()

        web = Node(id="vm:web", slug="web", type=NodeType.VM, name="Web")
        proj.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(proj.node_file("vm", "web"), web)

        # Relationship to a node that doesn't exist
        rel = Relationship(
            source="vm:web",
            target="vm:ghost",
            type=RelationshipType.DEPENDS_ON,
            description="Missing",
        )
        write_model(proj.relationships_yaml, RelationshipFile(relationships=[rel]))

        _patch_merged_graph(monkeypatch, tmp_environment, ["proj-a"])

        graph = load_merged_graph()
        assert graph.number_of_nodes() == 1
        assert graph.number_of_edges() == 0


# ── unqualify_node_id ────────────────────────────────────────────


class TestUnqualifyNodeId:
    def test_qualified_id(self):
        from infracontext.graph.loader import unqualify_node_id

        assert unqualify_node_id("customer-acme/vm:web-01") == ("customer-acme", "vm:web-01")

    def test_unqualified_id(self):
        from infracontext.graph.loader import unqualify_node_id

        assert unqualify_node_id("vm:web-01") == ("", "vm:web-01")

    def test_hierarchical_project(self):
        from infracontext.graph.loader import unqualify_node_id

        # Handles hierarchical project slugs (org/team)
        assert unqualify_node_id("org/team/vm:web") == ("org/team", "vm:web")

    def test_external_root_qualified(self):
        # External-root form '@alias:project/type:slug' must not be split at
        # the first colon (which would lose the root prefix and group the
        # node under an empty project label).
        from infracontext.graph.loader import unqualify_node_id

        assert unqualify_node_id("@fleet:prod/vm:web") == ("@fleet:prod", "vm:web")
        # Hierarchical project inside an external root.
        assert unqualify_node_id("@fleet:org/team/vm:web") == ("@fleet:org/team", "vm:web")
        # Round-trip: scope + '/' + node_id == original qualified ID.
        for qid in ("@fleet:prod/vm:web", "@fleet:org/team/vm:web"):
            scope, nid = unqualify_node_id(qid)
            assert f"{scope}/{nid}" == qid
