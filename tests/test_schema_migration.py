"""Tests for schema migration and backward compatibility.

Covers:
- Config key renames (active_tenant -> active_project)
- Unknown fields in node YAML files (owner, tags, etc.)
- Legacy tenants/ directory detection
"""

import logging

import pytest
from pydantic import BaseModel, ValidationError

from infracontext.config import _migrate_config_keys, load_config
from infracontext.models.node import Node
from infracontext.paths import INFRACONTEXT_DIR, ProjectPaths, list_projects
from infracontext.storage import read_model, write_yaml

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


# ── Legacy tenants/ directory detection ──────────────────────────────


class TestLegacyTenantsDetection:
    def test_warns_on_tenants_dir(self, tmp_environment, caplog):
        """list_projects should warn when tenants/ exists."""
        tenants_dir = tmp_environment.root / INFRACONTEXT_DIR / "tenants"
        tenants_dir.mkdir()
        # Also create a project in projects/ so list_projects has something
        p = ProjectPaths.for_project("prod", tmp_environment)
        p.ensure_dirs()

        with caplog.at_level(logging.WARNING):
            projects = list_projects(tmp_environment)
        assert "prod" in projects
        assert "tenants" in caplog.text
        assert "rename" in caplog.text.lower() or "migrate" in caplog.text.lower()

    def test_no_warning_without_tenants_dir(self, tmp_environment, caplog):
        """No warning when only projects/ exists."""
        p = ProjectPaths.for_project("prod", tmp_environment)
        p.ensure_dirs()

        with caplog.at_level(logging.WARNING):
            projects = list_projects(tmp_environment)
        assert "prod" in projects
        assert "tenants" not in caplog.text
