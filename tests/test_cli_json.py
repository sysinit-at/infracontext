"""Tests for machine-readable (``--json``) output across the CLI surface.

Covers ``query status --json`` (aggregated document), the individual
``ic query *`` commands (``--json`` primary, ``--raw`` deprecated alias), and
``node list / find / show`` plus ``ctx`` JSON.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from infracontext.cli.describe import app as describe_app
from infracontext.cli.main import app as main_app
from infracontext.cli.query import app as query_app
from infracontext.models.node import Node, NodeType
from infracontext.query.base import QueryResult

runner = CliRunner()


# ── node list / find / show ────────────────────────────────────────


class TestNodeListJson:
    def test_list_json_is_valid_and_shaped(self, hotpath_env):
        result = runner.invoke(describe_app, ["node", "list", "--json"])
        assert result.exit_code == 0, result.output

        data = json.loads(result.output)
        ids = {n["id"] for n in data}
        assert {"vm:web-01", "vm:db-01"} <= ids

        web = next(n for n in data if n["id"] == "vm:web-01")
        assert web["ssh_alias"] == "web-prod"
        assert web["name"] == "Web Server 01"
        assert web["type"] == "vm"
        assert web["project"] == "prod"

    def test_list_json_filtered_empty_is_array(self, hotpath_env):
        # No 'service' nodes exist -> an empty JSON array, still parseable.
        result = runner.invoke(describe_app, ["node", "list", "--type", "service", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == []


class TestNodeFindShowJson:
    def test_find_json_reports_matched_on(self, hotpath_env):
        result = runner.invoke(describe_app, ["node", "find", "web01.example.com", "--json"])
        assert result.exit_code == 0, result.output

        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["id"] == "vm:web-01"
        assert "domain" in data[0]["matched_on"]

    def test_find_json_no_match_is_empty_array(self, hotpath_env):
        result = runner.invoke(describe_app, ["node", "find", "zzzzz", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == []

    def test_show_json_is_full_dump(self, hotpath_env):
        result = runner.invoke(describe_app, ["node", "show", "vm:web-01", "--json"])
        assert result.exit_code == 0, result.output

        data = json.loads(result.output)
        assert data["id"] == "vm:web-01"
        assert data["slug"] == "web-01"
        assert data["ssh_alias"] == "web-prod"
        # A full model dump carries nested structures the human view collapses.
        assert data["learnings"][0]["finding"] == "pool misconfigured"


# ── ctx / node context ─────────────────────────────────────────────


class TestCtxJson:
    def test_ctx_json_shorthand(self, hotpath_env):
        result = runner.invoke(main_app, ["ctx", "web", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["id"] == "vm:web-01"

    def test_node_context_json_flag(self, hotpath_env):
        result = runner.invoke(describe_app, ["node", "context", "vm:web-01", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["id"] == "vm:web-01"


# ── query status --json ────────────────────────────────────────────


def _patch_query_env(monkeypatch, configs: dict) -> None:
    """Wire query.py helpers so only ``configs`` sources are configured."""
    from infracontext.overrides import NodeOverrides

    monkeypatch.setattr("infracontext.cli.query.require_project", lambda: "demo")
    monkeypatch.setattr(
        "infracontext.cli.query.require_node",
        lambda _p, _n: Node(id="vm:web", slug="web", type=NodeType.VM, name="Web"),
    )
    # query_status reads the node once and passes it (and the pre-read sources
    # index) into the helpers, which the fakes accept and ignore.
    monkeypatch.setattr(
        "infracontext.cli.query.get_node_observability", lambda _p, _n, _t, node=None: None
    )
    monkeypatch.setattr("infracontext.cli.query.get_node_ssh_target", lambda _p, _n, node=None: None)
    monkeypatch.setattr("infracontext.cli.query.get_node_overrides", lambda *_a, **_k: NodeOverrides())
    monkeypatch.setattr(
        "infracontext.cli.query.get_source_config",
        lambda _p, source_type, _name=None, sources=None: configs.get(source_type),
    )


class TestQueryStatusJson:
    def test_json_aggregates_sources(self, monkeypatch):
        _patch_query_env(
            monkeypatch,
            {"prometheus": {"addr": "http://p:9090"}, "checkmk": {"api_url": "http://c"}},
        )

        class _OkProm:
            def query(self, *_a, **_k):
                return QueryResult(success=True, source_type="prometheus", source_name="p", data={"up": 1})

        class _FailCmk:
            def query(self, *_a, **_k):
                return QueryResult(success=False, source_type="checkmk", source_name="c", error="down")

        monkeypatch.setattr("infracontext.query.prometheus.PrometheusPlugin", _OkProm)
        monkeypatch.setattr("infracontext.query.checkmk.CheckMKPlugin", _FailCmk)

        result = runner.invoke(query_app, ["status", "vm:web", "--json"])
        assert result.exit_code == 0, result.output

        doc = json.loads(result.output)
        assert doc["node"] == "vm:web"
        by_type = {s["type"]: s for s in doc["sources"]}
        assert by_type["prometheus"]["success"] is True
        assert by_type["prometheus"]["data"] == {"up": 1}
        assert by_type["checkmk"]["success"] is False
        assert by_type["checkmk"]["error"] == "down"
        # Every entry carries a human-facing source label too.
        assert all("source" in s for s in doc["sources"])

    def test_json_no_sources_is_valid_document(self, monkeypatch):
        _patch_query_env(monkeypatch, {})
        result = runner.invoke(query_app, ["status", "vm:web", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"node": "vm:web", "sources": []}

    def test_json_never_parks_sources(self, monkeypatch, tmp_path):
        # Parking is MCP-only: CLI --json must stay complete for scripts
        # piping to jq, no matter how large a source payload is.
        monkeypatch.setenv("IC_SCRATCH_DIR", str(tmp_path / "parked"))
        monkeypatch.setenv("IC_PARK_THRESHOLD", "100")
        _patch_query_env(monkeypatch, {"prometheus": {"addr": "http://p:9090"}})

        big = {"series": [{"metric": f"node_cpu_{i}", "values": list(range(50))} for i in range(20)]}

        class _BigProm:
            def query(self, *_a, **_k):
                return QueryResult(success=True, source_type="prometheus", source_name="p", data=big)

        monkeypatch.setattr("infracontext.query.prometheus.PrometheusPlugin", _BigProm)

        result = runner.invoke(query_app, ["status", "vm:web", "--json"])
        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert doc["sources"][0]["data"] == big
        assert "_parked" not in result.output

    def test_json_captures_raised_exception(self, monkeypatch):
        _patch_query_env(monkeypatch, {"prometheus": {"addr": "http://p"}})

        class _Boom:
            def query(self, *_a, **_k):
                raise RuntimeError("kaboom")

        monkeypatch.setattr("infracontext.query.prometheus.PrometheusPlugin", _Boom)

        result = runner.invoke(query_app, ["status", "vm:web", "--json"])
        assert result.exit_code == 0, result.output

        source = json.loads(result.output)["sources"][0]
        assert source["success"] is False
        assert "kaboom" in source["error"]


# ── individual query commands: --json primary, --raw deprecated alias ──


class TestIndividualQueryJson:
    def _patch(self, monkeypatch, data: dict) -> None:
        monkeypatch.setattr("infracontext.cli.query.require_project", lambda: "demo")
        monkeypatch.setattr(
            "infracontext.cli.query.require_node",
            lambda _p, _n: Node(id="vm:web", slug="web", type=NodeType.VM, name="Web"),
        )
        monkeypatch.setattr(
            "infracontext.cli.query.get_node_observability",
            lambda _p, _n, _t: {"instance": "web:9100"},
        )
        monkeypatch.setattr(
            "infracontext.cli.query.get_source_config",
            lambda _p, _t, _n=None: {"addr": "http://p:9090"},
        )

        class _Prom:
            def query(self, *_a, **_k):
                return QueryResult(success=True, source_type="prometheus", source_name="p", data=data)

        monkeypatch.setattr("infracontext.query.prometheus.PrometheusPlugin", _Prom)

    def test_json_flag_emits_data(self, monkeypatch):
        self._patch(monkeypatch, {"cpu": 12.5})
        result = runner.invoke(query_app, ["prometheus", "vm:web", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"cpu": 12.5}

    def test_raw_alias_still_works(self, monkeypatch):
        self._patch(monkeypatch, {"cpu": 12.5})
        result = runner.invoke(query_app, ["prometheus", "vm:web", "--raw"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"cpu": 12.5}

    def test_raw_is_hidden_from_help(self, monkeypatch):
        result = runner.invoke(query_app, ["prometheus", "--help"])
        assert result.exit_code == 0
        assert "--json" in result.output
        assert "--raw" not in result.output
