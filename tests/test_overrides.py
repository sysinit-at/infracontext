"""Tests for infracontext.overrides — local override loading and application."""

import pytest
from pydantic import ValidationError

from infracontext.overrides import (
    NodeOverrides,
    apply_overrides_to_node,
    get_node_overrides,
    load_local_overrides,
)
from infracontext.storage import write_yaml

# ── load_local_overrides ──────────────────────────────────────────


class TestLoadLocalOverrides:
    def test_no_file(self, tmp_environment):
        overrides = load_local_overrides(tmp_environment)
        assert overrides.nodes == {}

    def test_with_data(self, tmp_environment):
        write_yaml(
            tmp_environment.local_overrides,
            {
                "nodes": {
                    "vm:web-01": {
                        "ssh_alias": "my-alias",
                        "source_paths": ["/abs/path"],
                    }
                }
            },
        )
        overrides = load_local_overrides(tmp_environment)
        assert "vm:web-01" in overrides.nodes

    def test_empty_file(self, tmp_environment):
        tmp_environment.local_overrides.write_text("")
        overrides = load_local_overrides(tmp_environment)
        assert overrides.nodes == {}


# ── get_node_overrides ────────────────────────────────────────────


class TestGetNodeOverrides:
    def test_exists(self, tmp_environment):
        write_yaml(
            tmp_environment.local_overrides,
            {"nodes": {"vm:web-01": {"ssh_alias": "override-alias"}}},
        )
        ov = get_node_overrides("vm:web-01", tmp_environment)
        assert ov.ssh_alias == "override-alias"

    def test_missing_node(self, tmp_environment):
        write_yaml(
            tmp_environment.local_overrides,
            {"nodes": {"vm:other": {"ssh_alias": "x"}}},
        )
        ov = get_node_overrides("vm:nonexistent", tmp_environment)
        assert ov.ssh_alias is None
        assert ov.source_paths is None


# ── apply_overrides_to_node ───────────────────────────────────────


class TestApplyOverrides:
    def test_full_override(self, tmp_environment):
        write_yaml(
            tmp_environment.local_overrides,
            {
                "nodes": {
                    "vm:web-01": {
                        "ssh_alias": "new-alias",
                        "source_paths": ["/new/path"],
                    }
                }
            },
        )
        data = {"ssh_alias": "old", "source_paths": ["/old"]}
        result = apply_overrides_to_node(data, "vm:web-01", tmp_environment)
        assert result["ssh_alias"] == "new-alias"
        assert result["source_paths"] == ["/new/path"]

    def test_partial_override_ssh_only(self, tmp_environment):
        write_yaml(
            tmp_environment.local_overrides,
            {"nodes": {"vm:web-01": {"ssh_alias": "partial"}}},
        )
        data = {"ssh_alias": "old", "source_paths": ["/keep"]}
        result = apply_overrides_to_node(data, "vm:web-01", tmp_environment)
        assert result["ssh_alias"] == "partial"
        assert result["source_paths"] == ["/keep"]  # unchanged


# ── Validation ────────────────────────────────────────────────────


class TestNodeOverridesValidation:
    def test_relative_paths_rejected(self):
        with pytest.raises(ValueError, match="absolute"):
            NodeOverrides(source_paths=["relative/path"])

    def test_absolute_paths_accepted(self):
        ov = NodeOverrides(source_paths=["/absolute/path"])
        assert ov.source_paths == ["/absolute/path"]

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            NodeOverrides.model_validate({"ssh_alias": "ok", "description": "not allowed"})
