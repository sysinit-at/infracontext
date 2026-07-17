"""Tests for schema migration and backward/forward compatibility.

Covers:
- Config key renames (active_tenant -> active_project)
- Unknown fields in node YAML files (owner, tags, etc.), top-level and nested
- Unknown enum variants (NodeType/RelationshipType from newer versions)
- Round-trip preservation of stripped fields (read -> edit -> write)
- Doctor reporting of schema drift (warnings, not errors)
- Legacy tenants/ directory detection
"""

import logging

import pytest
from pydantic import BaseModel, ValidationError

from infracontext.cli.doctor import Severity, run_doctor
from infracontext.config import _migrate_config_keys, load_config
from infracontext.models.node import COMPUTE_NODE_TYPES, Node, NodeType
from infracontext.models.relationship import RelationshipFile, RelationshipType
from infracontext.paths import INFRACONTEXT_DIR, ProjectPaths, list_projects
from infracontext.storage import append_to_list, read_model, read_yaml, write_model, write_yaml

# ── Config key migration ────────────────────────────────────────────


class TestMigrateConfigKeys:
    def test_active_tenant_migrated(self, caplog):
        data = {"active_tenant": "vagt/dev"}
        with caplog.at_level(logging.WARNING):
            result = _migrate_config_keys(data)
        assert result == {"active_project": "vagt/dev"}
        assert "active_tenant" in caplog.text
        assert "renamed" in caplog.text

    def test_active_project_not_overwritten_by_tenant(self, caplog):
        """When both old and new keys exist, new key wins."""
        data = {"active_tenant": "old-value", "active_project": "new-value"}
        with caplog.at_level(logging.WARNING):
            result = _migrate_config_keys(data)
        assert result == {"active_project": "new-value"}

    def test_unknown_key_stripped(self, caplog):
        data = {"active_project": "prod", "some_future_key": True}
        with caplog.at_level(logging.WARNING):
            result = _migrate_config_keys(data)
        assert result == {"active_project": "prod"}
        assert "some_future_key" in caplog.text

    def test_clean_data_unchanged(self, caplog):
        data = {"active_project": "prod"}
        with caplog.at_level(logging.WARNING):
            result = _migrate_config_keys(data)
        assert result == {"active_project": "prod"}
        assert caplog.text == ""

    def test_empty_data(self):
        assert _migrate_config_keys({}) == {}


class TestLoadConfigMigration:
    def test_stale_active_tenant_loads(self, tmp_environment, caplog):
        """Config with active_tenant should load without crashing."""
        write_yaml(tmp_environment.config_yaml, {"active_tenant": "vagt/dev"})
        with caplog.at_level(logging.WARNING):
            config = load_config(tmp_environment)
        assert config.active_project == "vagt/dev"

    def test_unknown_keys_ignored(self, tmp_environment, caplog):
        """Config with unknown keys should load without crashing."""
        write_yaml(tmp_environment.config_yaml, {"active_project": "prod", "theme": "dark"})
        with caplog.at_level(logging.WARNING):
            config = load_config(tmp_environment)
        assert config.active_project == "prod"
        assert "theme" in caplog.text


# ── Node unknown fields ─────────────────────────────────────────────


class TestReadModelUnknownFields:
    def test_node_with_owner_and_tags(self, tmp_path, caplog):
        """Node files from older schema with owner/tags should load."""
        node_file = tmp_path / "web-01.yaml"
        write_yaml(node_file, {
            "id": "vm:web-01",
            "slug": "web-01",
            "type": "vm",
            "name": "Web Server 01",
            "owner": "ops-team",
            "tags": ["production", "web"],
        })
        with caplog.at_level(logging.WARNING):
            node = read_model(node_file, Node)
        assert node is not None
        assert node.id == "vm:web-01"
        assert node.name == "Web Server 01"
        assert "owner" in caplog.text
        assert "tags" in caplog.text

    def test_unquoted_first_seen_date_loads(self, tmp_path):
        """Hand-edited YAML naturally reads `first_seen: 2026-07-16` unquoted;
        the loader resolves that to datetime.date, which must coerce to the
        ISO string instead of failing validation and dropping the whole node
        from the graph."""
        node_file = tmp_path / "web-01.yaml"
        node_file.write_text(
            "version: '2.0'\n"
            "id: vm:web-01\n"
            "slug: web-01\n"
            "type: vm\n"
            "name: Web Server 01\n"
            "first_seen: 2026-07-16\n"
        )
        node = read_model(node_file, Node)
        assert node is not None
        assert node.first_seen == "2026-07-16"

    def test_unquoted_learning_date_loads(self, tmp_path):
        node_file = tmp_path / "web-01.yaml"
        node_file.write_text(
            "version: '2.0'\n"
            "id: vm:web-01\n"
            "slug: web-01\n"
            "type: vm\n"
            "name: Web Server 01\n"
            "learnings:\n"
            "  - date: 2026-01-05\n"
            "    context: cpu spike\n"
            "    finding: cron overlap\n"
        )
        node = read_model(node_file, Node)
        assert node is not None
        assert node.learnings[0].date == "2026-01-05"

    def test_node_without_extras_loads_clean(self, tmp_path, caplog):
        """Node without extra fields should not trigger warnings."""
        node_file = tmp_path / "db-01.yaml"
        write_yaml(node_file, {
            "id": "vm:db-01",
            "slug": "db-01",
            "type": "vm",
            "name": "Database 01",
        })
        with caplog.at_level(logging.WARNING):
            node = read_model(node_file, Node)
        assert node is not None
        assert node.name == "Database 01"
        assert caplog.text == ""

    def test_model_with_extra_ignore_not_stripped(self, tmp_path):
        """Models that use extra='ignore' should bypass stripping."""

        class Flexible(BaseModel):
            name: str
            model_config = {"extra": "ignore"}

        f = tmp_path / "flex.yaml"
        write_yaml(f, {"name": "test", "unknown": "value"})
        obj = read_model(f, Flexible)
        assert obj is not None
        assert obj.name == "test"

    def test_real_validation_error_still_raises(self, tmp_path):
        """Missing required fields should still raise ValidationError."""
        node_file = tmp_path / "bad.yaml"
        # Missing required 'id', 'slug', 'type', 'name'
        write_yaml(node_file, {"description": "incomplete node"})
        with pytest.raises(ValidationError):
            read_model(node_file, Node)


# ── Nested unknown fields ───────────────────────────────────────────


def _drifted_node_data() -> dict:
    """A node as a newer infracontext might write it: unknown fields at the
    top level and inside nested models."""
    return {
        "id": "vm:web-01",
        "slug": "web-01",
        "type": "vm",
        "name": "Web Server 01",
        "owner": "ops-team",
        "triage": {"services": ["nginx"], "gpu_hints": "check nvidia-smi"},
        "observability": [{"type": "prometheus", "instance": "web:9100", "scrape_interval": 30}],
        "learnings": [
            {"date": "2026-01-01", "context": "cpu", "finding": "ok", "source": "human", "confidence": 0.9},
        ],
    }


class TestNestedUnknownFields:
    def test_nested_unknowns_stripped_with_warning(self, tmp_path, caplog):
        node_file = tmp_path / "web-01.yaml"
        write_yaml(node_file, _drifted_node_data())
        with caplog.at_level(logging.WARNING):
            node = read_model(node_file, Node)
        assert node is not None
        assert node.triage is not None and node.triage.services == ["nginx"]
        assert node.observability[0].instance == "web:9100"
        assert node.learnings[0].finding == "ok"
        # Warnings carry the dotted path of each stripped field.
        assert "owner" in caplog.text
        assert "triage.gpu_hints" in caplog.text
        assert "observability.0.scrape_interval" in caplog.text
        assert "learnings.0.confidence" in caplog.text

    def test_clean_nested_models_stay_quiet(self, tmp_path, caplog):
        data = _drifted_node_data()
        for key in ("owner",):
            del data[key]
        del data["triage"]["gpu_hints"]
        del data["observability"][0]["scrape_interval"]
        del data["learnings"][0]["confidence"]
        node_file = tmp_path / "web-01.yaml"
        write_yaml(node_file, data)
        with caplog.at_level(logging.WARNING):
            node = read_model(node_file, Node)
        assert node is not None
        assert caplog.text == ""


# ── Unknown enum variants (forward compat) ──────────────────────────


class TestUnknownEnumVariants:
    def test_unknown_node_type_loads(self, tmp_path):
        node_file = tmp_path / "q-01.yaml"
        write_yaml(node_file, {"id": "quantum_host:q-01", "slug": "q-01", "type": "quantum_host", "name": "Q"})
        node = read_model(node_file, Node)
        assert node is not None
        assert node.type == "quantum_host"
        assert not isinstance(node.type, NodeType)
        assert node.type not in COMPUTE_NODE_TYPES

    def test_known_node_type_stays_enum_member(self, tmp_path):
        node_file = tmp_path / "web-01.yaml"
        write_yaml(node_file, {"id": "vm:web-01", "slug": "web-01", "type": "vm", "name": "Web"})
        node = read_model(node_file, Node)
        assert node is not None
        assert isinstance(node.type, NodeType)
        assert node.type is NodeType.VM

    def test_unknown_node_type_round_trip_preserves_string(self, tmp_path):
        node_file = tmp_path / "q-01.yaml"
        write_yaml(node_file, {"id": "quantum_host:q-01", "slug": "q-01", "type": "quantum_host", "name": "Q"})
        node = read_model(node_file, Node)
        write_model(node_file, node)
        assert read_yaml(node_file)["type"] == "quantum_host"
        assert "type: quantum_host" in node_file.read_text()

    def test_unknown_relationship_type_loads_and_round_trips(self, tmp_path):
        rel_path = tmp_path / "relationships.yaml"
        write_yaml(
            rel_path,
            {"relationships": [{"source": "vm:a", "target": "vm:b", "type": "quantum_entangled_with"}]},
        )
        rel_file = read_model(rel_path, RelationshipFile)
        assert rel_file is not None
        assert rel_file.relationships[0].type == "quantum_entangled_with"
        assert not isinstance(rel_file.relationships[0].type, RelationshipType)
        write_model(rel_path, rel_file)
        assert read_yaml(rel_path)["relationships"][0]["type"] == "quantum_entangled_with"

    def test_unknown_type_node_usable_in_graph(self, monkeypatch_environment):
        from infracontext.graph.loader import load_graph

        paths = ProjectPaths.for_project("prod", monkeypatch_environment)
        paths.ensure_dirs()
        paths.node_type_dir("quantum_host").mkdir(parents=True, exist_ok=True)
        write_yaml(
            paths.node_file("quantum_host", "q-01"),
            {"id": "quantum_host:q-01", "slug": "q-01", "type": "quantum_host", "name": "Q"},
        )
        graph = load_graph("prod")
        assert "quantum_host:q-01" in graph
        assert graph.nodes["quantum_host:q-01"]["type"] == "quantum_host"

    def test_non_string_type_still_rejected(self, tmp_path):
        node_file = tmp_path / "bad.yaml"
        write_yaml(node_file, {"id": "vm:bad", "slug": "bad", "type": 42, "name": "Bad"})
        with pytest.raises(ValidationError):
            read_model(node_file, Node)


# ── Edit round-trip preservation of stripped fields ─────────────────


class TestEditRoundTripPreservation:
    def test_unknowns_survive_attribute_edit(self, tmp_path):
        node_file = tmp_path / "web-01.yaml"
        write_yaml(node_file, _drifted_node_data())
        node = read_model(node_file, Node)
        node.name = "Renamed"
        write_model(node_file, node)

        data = read_yaml(node_file)
        assert data["name"] == "Renamed"
        assert data["owner"] == "ops-team"
        assert data["triage"]["gpu_hints"] == "check nvidia-smi"
        assert data["observability"][0]["scrape_interval"] == 30
        assert data["learnings"][0]["confidence"] == 0.9

    def test_unknowns_survive_model_copy_update(self, tmp_path):
        """Mirrors the source-sync flow: read_model -> model_copy(update) -> write_model."""
        node_file = tmp_path / "web-01.yaml"
        write_yaml(node_file, _drifted_node_data())
        node = read_model(node_file, Node)
        updated = node.model_copy(update={"ssh_alias": "web-prod"})
        write_model(node_file, updated)

        data = read_yaml(node_file)
        assert data["ssh_alias"] == "web-prod"
        assert data["owner"] == "ops-team"
        assert data["triage"]["gpu_hints"] == "check nvidia-smi"

    def test_unknowns_follow_surviving_list_items(self, tmp_path):
        """Stashes anchor to the owning item, not its list position: deleting
        a sibling must not shift an unknown field onto the wrong entry."""
        rel_path = tmp_path / "relationships.yaml"
        write_yaml(
            rel_path,
            {
                "relationships": [
                    {"source": "vm:a", "target": "vm:b", "type": "depends_on"},
                    {"source": "vm:b", "target": "vm:c", "type": "runs_on", "weight": 5},
                ]
            },
        )
        rel_file = read_model(rel_path, RelationshipFile)
        rel_file.relationships = [r for r in rel_file.relationships if r.source != "vm:a"]
        write_model(rel_path, rel_file)

        data = read_yaml(rel_path)
        assert len(data["relationships"]) == 1
        assert data["relationships"][0]["source"] == "vm:b"
        assert data["relationships"][0]["weight"] == 5

    def test_replaced_list_items_drop_their_stash(self, tmp_path):
        """A freshly constructed item is new data -- restoring old unknowns
        onto it would be mangling, so they are (correctly) dropped. Copies
        (model_copy) of a read item keep their stash instead."""
        from infracontext.models.relationship import Relationship

        rel_path = tmp_path / "relationships.yaml"
        write_yaml(
            rel_path,
            {"relationships": [{"source": "vm:a", "target": "vm:b", "type": "depends_on", "weight": 5}]},
        )
        rel_file = read_model(rel_path, RelationshipFile)
        rel_file.relationships = [
            Relationship(source=r.source, target=r.target, type=r.type) for r in rel_file.relationships
        ]
        write_model(rel_path, rel_file)
        assert "weight" not in read_yaml(rel_path)["relationships"][0]

    def test_append_to_list_leaves_unknown_fields_untouched(self, tmp_path):
        """`ic learn` appends via update_yaml/append_to_list (ruamel round-trip,
        no model validation) -- unknown fields must survive as-is."""
        node_file = tmp_path / "web-01.yaml"
        write_yaml(node_file, _drifted_node_data())
        append_to_list(
            node_file,
            "learnings",
            {"date": "2026-07-16", "context": "disk", "finding": "inode exhaustion", "source": "human"},
        )
        data = read_yaml(node_file)
        assert data["owner"] == "ops-team"
        assert data["triage"]["gpu_hints"] == "check nvidia-smi"
        assert data["learnings"][1]["finding"] == "inode exhaustion"


# ── Doctor reporting of schema drift ────────────────────────────────


class TestDoctorSchemaDrift:
    @staticmethod
    def _project(environment) -> ProjectPaths:
        paths = ProjectPaths.for_project("prod", environment)
        paths.ensure_dirs()
        return paths

    def test_unknown_fields_warn_not_error(self, monkeypatch_environment):
        paths = self._project(monkeypatch_environment)
        paths.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_yaml(paths.node_file("vm", "web-01"), _drifted_node_data())

        report = run_doctor(monkeypatch_environment)

        unknown = [i for i in report.issues if i.category == "unknown_field"]
        assert {i.severity for i in unknown} == {Severity.WARNING}
        messages = " | ".join(i.message for i in unknown)
        assert "owner" in messages
        assert "triage.gpu_hints" in messages
        assert not report.has_errors
        assert report.nodes_checked == 1

    def test_unknown_node_type_flagged(self, monkeypatch_environment):
        paths = self._project(monkeypatch_environment)
        paths.node_type_dir("quantum_host").mkdir(parents=True, exist_ok=True)
        write_yaml(
            paths.node_file("quantum_host", "q-01"),
            {"id": "quantum_host:q-01", "slug": "q-01", "type": "quantum_host", "name": "Q"},
        )

        report = run_doctor(monkeypatch_environment)

        variants = [i for i in report.issues if i.category == "unknown_variant"]
        assert len(variants) == 1
        assert variants[0].severity == Severity.WARNING
        assert "quantum_host" in variants[0].message
        assert not report.has_errors

    def test_unknown_relationship_type_flagged(self, monkeypatch_environment):
        paths = self._project(monkeypatch_environment)
        paths.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        for slug in ("web-01", "db-01"):
            write_yaml(
                paths.node_file("vm", slug),
                {"id": f"vm:{slug}", "slug": slug, "type": "vm", "name": slug, "ssh_alias": slug},
            )
        write_yaml(
            paths.relationships_yaml,
            {"relationships": [{"source": "vm:web-01", "target": "vm:db-01", "type": "quantum_entangled_with"}]},
        )

        report = run_doctor(monkeypatch_environment)

        variants = [i for i in report.issues if i.category == "unknown_variant"]
        assert len(variants) == 1
        assert variants[0].severity == Severity.WARNING
        assert "quantum_entangled_with" in variants[0].message
        assert not report.has_errors

    def test_real_schema_error_still_error(self, monkeypatch_environment):
        paths = self._project(monkeypatch_environment)
        paths.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        # Missing required 'name', plus an unknown field: the unknown field is
        # a warning, the missing field stays an error.
        write_yaml(
            paths.node_file("vm", "web-01"),
            {"id": "vm:web-01", "slug": "web-01", "type": "vm", "owner": "ops-team"},
        )

        report = run_doctor(monkeypatch_environment)

        assert report.has_errors
        schema_errors = [i for i in report.issues if i.category == "schema"]
        assert any("name" in i.message for i in schema_errors)
        unknown = [i for i in report.issues if i.category == "unknown_field"]
        assert len(unknown) == 1 and "owner" in unknown[0].message


# ── Legacy tenants/ directory detection ──────────────────────────────


class TestLegacyTenantsDetection:
    def test_warns_on_tenants_dir(self, tmp_environment, caplog):
        """list_projects should warn when a populated tenants/ exists."""
        tenants_dir = tmp_environment.root / INFRACONTEXT_DIR / "tenants"
        (tenants_dir / "acme").mkdir(parents=True)
        # Also create a project in projects/ so list_projects has something
        p = ProjectPaths.for_project("prod", tmp_environment)
        p.ensure_dirs()

        with caplog.at_level(logging.WARNING):
            projects = list_projects(tmp_environment)
        assert "prod" in projects
        assert "tenants" in caplog.text
        assert "rename" in caplog.text.lower() or "migrate" in caplog.text.lower()

    def test_empty_tenants_dir_does_not_warn(self, tmp_environment, caplog):
        """An empty leftover tenants/ has nothing to migrate — stay quiet."""
        (tmp_environment.root / INFRACONTEXT_DIR / "tenants").mkdir()
        p = ProjectPaths.for_project("prod", tmp_environment)
        p.ensure_dirs()

        with caplog.at_level(logging.WARNING):
            list_projects(tmp_environment)
        assert "tenants" not in caplog.text

    def test_no_warning_without_tenants_dir(self, tmp_environment, caplog):
        """No warning when only projects/ exists."""
        p = ProjectPaths.for_project("prod", tmp_environment)
        p.ensure_dirs()

        with caplog.at_level(logging.WARNING):
            projects = list_projects(tmp_environment)
        assert "prod" in projects
        assert "tenants" not in caplog.text
