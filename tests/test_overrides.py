"""Tests for infracontext.overrides — local override loading and application."""

import pytest
from pydantic import ValidationError

from infracontext import overrides as overrides_mod
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

    def test_malformed_yaml_returns_empty(self, tmp_environment):
        tmp_environment.local_overrides.write_text("nodes: [invalid: yaml: {")
        overrides = load_local_overrides(tmp_environment)
        assert overrides.nodes == {}

    def test_invalid_schema_returns_empty(self, tmp_environment):
        write_yaml(
            tmp_environment.local_overrides,
            {"nodes": {"vm:web-01": {"not_a_field": "value"}}},
        )
        overrides = load_local_overrides(tmp_environment)
        assert overrides.nodes == {}

    def test_relative_path_returns_empty(self, tmp_environment):
        write_yaml(
            tmp_environment.local_overrides,
            {"nodes": {"vm:web-01": {"source_paths": ["relative/path"]}}},
        )
        overrides = load_local_overrides(tmp_environment)
        assert overrides.nodes == {}


# ── caching ───────────────────────────────────────────────────────


class TestLoadLocalOverridesCache:
    def test_repeated_reads_parse_once(self, tmp_environment, monkeypatch):
        """Two loads of an unchanged file parse it exactly once."""
        write_yaml(
            tmp_environment.local_overrides,
            {"nodes": {"vm:web-01": {"ssh_alias": "alpha"}}},
        )
        # Drop any cache entry left by earlier calls on this path.
        overrides_mod._overrides_cache.pop(tmp_environment.local_overrides.resolve(), None)

        calls = {"n": 0}
        real_parse = overrides_mod._parse_local_overrides

        def counting_parse(path):
            calls["n"] += 1
            return real_parse(path)

        monkeypatch.setattr(overrides_mod, "_parse_local_overrides", counting_parse)

        first = load_local_overrides(tmp_environment)
        second = load_local_overrides(tmp_environment)

        assert calls["n"] == 1  # second call served from cache
        assert first.nodes["vm:web-01"].ssh_alias == "alpha"
        assert second.nodes["vm:web-01"].ssh_alias == "alpha"

    def test_file_change_busts_cache(self, tmp_environment, monkeypatch):
        """Editing the file (changing its size) forces a re-parse and fresh data."""
        write_yaml(
            tmp_environment.local_overrides,
            {"nodes": {"vm:web-01": {"ssh_alias": "alpha"}}},
        )
        overrides_mod._overrides_cache.pop(tmp_environment.local_overrides.resolve(), None)

        calls = {"n": 0}
        real_parse = overrides_mod._parse_local_overrides

        def counting_parse(path):
            calls["n"] += 1
            return real_parse(path)

        monkeypatch.setattr(overrides_mod, "_parse_local_overrides", counting_parse)

        load_local_overrides(tmp_environment)
        load_local_overrides(tmp_environment)
        assert calls["n"] == 1

        # A different-length value changes the file size, so the (mtime_ns, size)
        # key differs regardless of filesystem timestamp granularity.
        write_yaml(
            tmp_environment.local_overrides,
            {"nodes": {"vm:web-01": {"ssh_alias": "bravo-a-much-longer-alias"}}},
        )

        fresh = load_local_overrides(tmp_environment)
        assert calls["n"] == 2  # cache busted, parsed again
        assert fresh.nodes["vm:web-01"].ssh_alias == "bravo-a-much-longer-alias"


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
