"""Tests for cross-project node reference support."""

import pytest

from infracontext.models.node import Node, NodeType
from infracontext.models.relationship import (
    Relationship,
    RelationshipFile,
    RelationshipType,
    format_node_ref,
    is_cross_project_ref,
    parse_node_ref,
)
from infracontext.paths import ProjectPaths
from infracontext.storage import write_model

# ── parse_node_ref ────────────────────────────────────────────────


class TestParseNodeRef:
    def test_unqualified_ref(self):
        project, node_id = parse_node_ref("vm:web-01", "myproject")
        assert project == "myproject"
        assert node_id == "vm:web-01"

    def test_qualified_ref(self):
        project, node_id = parse_node_ref("@vagt/dev:vm:qoncept-proxy-01", "vagt/test")
        assert project == "vagt/dev"
        assert node_id == "vm:qoncept-proxy-01"

    def test_qualified_ref_simple_project(self):
        project, node_id = parse_node_ref("@prod:service:nginx", "dev")
        assert project == "prod"
        assert node_id == "service:nginx"

    def test_unqualified_ref_with_no_colon_raises(self):
        with pytest.raises(ValueError, match="Invalid node reference"):
            parse_node_ref("invalid", "myproject")

    def test_qualified_ref_missing_slug_raises(self):
        with pytest.raises(ValueError, match="Expected format"):
            parse_node_ref("@vagt/dev:vm", "myproject")

    def test_qualified_ref_empty_project_raises(self):
        with pytest.raises(ValueError, match="Empty project"):
            parse_node_ref("@:vm:web", "myproject")

    def test_qualified_ref_preserves_slug_with_dashes(self):
        project, node_id = parse_node_ref("@acme/prod:physical_host:db-server-01", "acme/dev")
        assert project == "acme/prod"
        assert node_id == "physical_host:db-server-01"


# ── is_cross_project_ref ─────────────────────────────────────────


class TestIsCrossProjectRef:
    def test_qualified_is_cross_project(self):
        assert is_cross_project_ref("@vagt/dev:vm:proxy") is True

    def test_unqualified_is_not_cross_project(self):
        assert is_cross_project_ref("vm:proxy") is False

    def test_at_in_middle_is_not_cross_project(self):
        # Only leading @ counts
        assert is_cross_project_ref("vm:proxy@thing") is False


# ── format_node_ref ───────────────────────────────────────────────


class TestFormatNodeRef:
    def test_same_project_unqualified(self):
        result = format_node_ref("vagt/dev", "vm:proxy", "vagt/dev")
        assert result == "vm:proxy"

    def test_different_project_qualified(self):
        result = format_node_ref("vagt/dev", "vm:proxy", "vagt/test")
        assert result == "@vagt/dev:vm:proxy"


# ── load_graph with cross-project refs ────────────────────────────


class TestLoadGraphCrossProject:
    def test_cross_project_node_loaded(self, tmp_environment, monkeypatch_environment, monkeypatch):
        """A cross-project ref loads the referenced node into the graph."""
        from infracontext.graph.loader import load_graph

        # Set up "dev" project with a proxy node
        dev_paths = ProjectPaths.for_project("dev", tmp_environment)
        dev_paths.ensure_dirs()
        proxy = Node(id="vm:proxy-01", slug="proxy-01", type=NodeType.VM, name="Proxy")
        dev_paths.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(dev_paths.node_file("vm", "proxy-01"), proxy)

        # Set up "test" project with a web node that references dev's proxy
        test_paths = ProjectPaths.for_project("test", tmp_environment)
        test_paths.ensure_dirs()
        web = Node(id="vm:web-01", slug="web-01", type=NodeType.VM, name="Web")
        test_paths.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(test_paths.node_file("vm", "web-01"), web)

        # Relationship: web-01 fronted_by @dev:vm:proxy-01
        rel = Relationship(
            source="vm:web-01",
            target="@dev:vm:proxy-01",
            type=RelationshipType.FRONTED_BY,
        )
        rel_file = RelationshipFile(relationships=[rel])
        write_model(test_paths.relationships_yaml, rel_file)

        # Patch ProjectPaths.for_project to route correctly
        original_for_project = ProjectPaths.for_project

        def patched_for_project(slug, env=None):
            return original_for_project(slug, tmp_environment)

        monkeypatch.setattr(
            "infracontext.graph.loader.ProjectPaths.for_project",
            patched_for_project,
        )

        graph = load_graph("test")

        # Both nodes should be in the graph
        assert graph.has_node("vm:web-01")
        assert graph.has_node("vm:proxy-01")
        # The edge should exist
        assert graph.has_edge("vm:web-01", "vm:proxy-01")
        # The cross-project node should be tagged with its source project
        assert graph.nodes["vm:proxy-01"]["project"] == "dev"

    def test_missing_cross_project_node_skipped(self, tmp_environment, monkeypatch_environment, monkeypatch):
        """A cross-project ref to a non-existent node is silently skipped."""
        from infracontext.graph.loader import load_graph

        # Set up "test" project with a web node referencing non-existent cross-project node
        test_paths = ProjectPaths.for_project("test", tmp_environment)
        test_paths.ensure_dirs()
        web = Node(id="vm:web-01", slug="web-01", type=NodeType.VM, name="Web")
        test_paths.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(test_paths.node_file("vm", "web-01"), web)

        # Set up empty "dev" project so the path resolves but node doesn't exist
        dev_paths = ProjectPaths.for_project("dev", tmp_environment)
        dev_paths.ensure_dirs()

        rel = Relationship(
            source="vm:web-01",
            target="@dev:vm:nonexistent",
            type=RelationshipType.FRONTED_BY,
        )
        rel_file = RelationshipFile(relationships=[rel])
        write_model(test_paths.relationships_yaml, rel_file)

        original_for_project = ProjectPaths.for_project

        def patched_for_project(slug, env=None):
            return original_for_project(slug, tmp_environment)

        monkeypatch.setattr(
            "infracontext.graph.loader.ProjectPaths.for_project",
            patched_for_project,
        )

        graph = load_graph("test")

        assert graph.has_node("vm:web-01")
        assert not graph.has_node("vm:nonexistent")
        assert graph.number_of_edges() == 0

    def test_local_refs_still_work(self, tmp_project, tmp_environment, monkeypatch_environment, monkeypatch):
        """Unqualified refs continue to work as before (backwards compatible)."""
        from infracontext.graph.loader import load_graph

        web = Node(id="vm:web", slug="web", type=NodeType.VM, name="Web")
        db = Node(id="vm:db", slug="db", type=NodeType.VM, name="DB")

        tmp_project.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(tmp_project.node_file("vm", "web"), web)
        write_model(tmp_project.node_file("vm", "db"), db)

        rel = Relationship(
            source="vm:web",
            target="vm:db",
            type=RelationshipType.DEPENDS_ON,
        )
        rel_file = RelationshipFile(relationships=[rel])
        write_model(tmp_project.relationships_yaml, rel_file)

        monkeypatch.setattr(
            "infracontext.graph.loader.ProjectPaths.for_project",
            lambda _slug, _env=None: tmp_project,
        )

        graph = load_graph("testproject")
        assert graph.number_of_nodes() == 2
        assert graph.has_edge("vm:web", "vm:db")


# ── doctor cross-project validation ──────────────────────────────


class TestDoctorCrossProject:
    def test_valid_cross_project_ref_no_error(self, tmp_environment, monkeypatch_environment, monkeypatch):
        """Doctor should not report errors for valid cross-project refs."""
        from infracontext.cli.doctor import DoctorReport, _check_relationships

        # Set up "dev" project with a proxy node
        dev_paths = ProjectPaths.for_project("dev", tmp_environment)
        dev_paths.ensure_dirs()
        proxy = Node(id="vm:proxy-01", slug="proxy-01", type=NodeType.VM, name="Proxy")
        dev_paths.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(dev_paths.node_file("vm", "proxy-01"), proxy)

        # Monkeypatch to route load_node correctly
        original_for_project = ProjectPaths.for_project

        def patched_for_project(slug, env=None):
            return original_for_project(slug, tmp_environment)

        monkeypatch.setattr(
            "infracontext.graph.loader.ProjectPaths.for_project",
            patched_for_project,
        )

        # Simulate relationships data with cross-project ref
        data = {
            "version": "2.0",
            "relationships": [
                {
                    "source": "vm:web-01",
                    "target": "@dev:vm:proxy-01",
                    "type": "fronted_by",
                },
            ],
        }

        report = DoctorReport()
        local_node_ids = {"vm:web-01"}
        from pathlib import Path

        _check_relationships(Path("/fake/relationships.yaml"), data, local_node_ids, report, project_slug="test")

        errors = [i for i in report.issues if i.severity.value == "error"]
        assert len(errors) == 0

    def test_invalid_cross_project_ref_reports_error(self, tmp_environment, monkeypatch_environment, monkeypatch):
        """Doctor should report an error for cross-project refs to non-existent nodes."""
        from infracontext.cli.doctor import DoctorReport, _check_relationships

        # Set up empty "dev" project
        dev_paths = ProjectPaths.for_project("dev", tmp_environment)
        dev_paths.ensure_dirs()

        original_for_project = ProjectPaths.for_project

        def patched_for_project(slug, env=None):
            return original_for_project(slug, tmp_environment)

        monkeypatch.setattr(
            "infracontext.graph.loader.ProjectPaths.for_project",
            patched_for_project,
        )

        data = {
            "version": "2.0",
            "relationships": [
                {
                    "source": "vm:web-01",
                    "target": "@dev:vm:nonexistent",
                    "type": "fronted_by",
                },
            ],
        }

        report = DoctorReport()
        local_node_ids = {"vm:web-01"}
        from pathlib import Path

        _check_relationships(Path("/fake/relationships.yaml"), data, local_node_ids, report, project_slug="test")

        errors = [i for i in report.issues if i.severity.value == "error"]
        assert len(errors) == 1
        assert "non-existent" in errors[0].message
        assert "dev" in errors[0].message

    def test_malformed_cross_project_ref_reports_error(self, tmp_environment, monkeypatch_environment):
        """Doctor should report an error for malformed cross-project refs."""
        from infracontext.cli.doctor import DoctorReport, _check_relationships

        data = {
            "version": "2.0",
            "relationships": [
                {
                    "source": "vm:web-01",
                    "target": "@badref",
                    "type": "fronted_by",
                },
            ],
        }

        report = DoctorReport()
        local_node_ids = {"vm:web-01"}
        from pathlib import Path

        _check_relationships(Path("/fake/relationships.yaml"), data, local_node_ids, report, project_slug="test")

        errors = [i for i in report.issues if i.severity.value == "error"]
        assert len(errors) == 1
        assert "Invalid cross-project" in errors[0].message

    def test_local_orphan_still_detected(self, tmp_environment, monkeypatch_environment):
        """Doctor should still detect orphaned local refs (backwards compatible)."""
        from infracontext.cli.doctor import DoctorReport, _check_relationships

        data = {
            "version": "2.0",
            "relationships": [
                {
                    "source": "vm:web-01",
                    "target": "vm:nonexistent",
                    "type": "depends_on",
                },
            ],
        }

        report = DoctorReport()
        local_node_ids = {"vm:web-01"}
        from pathlib import Path

        _check_relationships(Path("/fake/relationships.yaml"), data, local_node_ids, report, project_slug="test")

        errors = [i for i in report.issues if i.severity.value == "error"]
        assert len(errors) == 1
        assert "non-existent" in errors[0].message
        assert "vm:nonexistent" in errors[0].message


# ── Relationship model with cross-project refs ───────────────────


class TestRelationshipModelCrossProject:
    def test_relationship_with_cross_project_target(self):
        """Relationship model accepts cross-project refs as strings."""
        rel = Relationship(
            source="vm:web-01",
            target="@vagt/dev:vm:proxy-01",
            type=RelationshipType.FRONTED_BY,
        )
        assert rel.target == "@vagt/dev:vm:proxy-01"
        assert rel.source == "vm:web-01"

    def test_relationship_with_cross_project_source(self):
        rel = Relationship(
            source="@vagt/dev:vm:proxy-01",
            target="vm:web-01",
            type=RelationshipType.ROUTES_TO,
        )
        assert rel.source == "@vagt/dev:vm:proxy-01"

    def test_self_referential_cross_project_still_rejected(self):
        """Even with @ prefix, self-referential relationships are rejected."""
        # This tests that if both resolve to the same string, it's caught
        with pytest.raises(ValueError, match="relationship with itself"):
            Relationship(
                source="vm:web-01",
                target="vm:web-01",
                type=RelationshipType.DEPENDS_ON,
            )

    def test_roundtrip_through_yaml(self, tmp_path):
        """Cross-project refs survive YAML serialization."""
        rel = Relationship(
            source="vm:web-01",
            target="@vagt/dev:vm:proxy-01",
            type=RelationshipType.FRONTED_BY,
            description="Reverse proxy",
        )
        rel_file = RelationshipFile(relationships=[rel])

        yaml_path = tmp_path / "relationships.yaml"
        write_model(yaml_path, rel_file)

        from infracontext.storage import read_model

        loaded = read_model(yaml_path, RelationshipFile)
        assert loaded is not None
        assert len(loaded.relationships) == 1
        assert loaded.relationships[0].target == "@vagt/dev:vm:proxy-01"
        assert loaded.relationships[0].source == "vm:web-01"
