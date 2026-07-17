"""Tests for the infracontext MCP server (``ic mcp serve``).

No real stdio client is needed: the FastMCP server object is introspected for
its registered tools, and each tool function is called directly against the
``hotpath_env`` fixture (a tmp ``.infracontext`` environment with an active
``prod`` project). The ``serve`` command's ``--help`` and the missing-extra
guard are exercised through Typer's ``CliRunner``.
"""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner

from infracontext.mcp_server import (
    TOOL_NAMES,
    ToolError,
    _park_oversized_sources,
    add_learning,
    build_server,
    find_node,
    get_context,
    parked_get,
    parked_grep,
    parked_schema,
    parked_slice,
    query_status,
)
from infracontext.models.node import Node
from infracontext.paths import ProjectPaths
from infracontext.storage import read_model


def _read(env, node_id: str) -> Node:
    node_type, slug = node_id.split(":", 1)
    node = read_model(ProjectPaths.for_project("prod", env).node_file(node_type, slug), Node)
    assert node is not None
    return node


class TestToolRegistration:
    def test_registers_all_named_tools(self):
        server = build_server()
        tools = {t.name: t for t in server._tool_manager.list_tools()}
        assert set(tools) == set(TOOL_NAMES)

    def test_schemas_and_descriptions(self):
        server = build_server()
        tools = {t.name: t for t in server._tool_manager.list_tools()}

        # Every tool carries a non-empty description (its LLM-facing docstring).
        for tool in tools.values():
            assert tool.description and tool.description.strip()

        # Required parameters match the tool signatures.
        assert tools["find_node"].parameters["required"] == ["query"]
        assert tools["get_context"].parameters["required"] == ["node_id"]
        assert tools["query_status"].parameters["required"] == ["node_id"]
        assert sorted(tools["add_learning"].parameters["required"]) == ["finding", "node_id"]
        assert tools["parked_schema"].parameters["required"] == ["file"]
        assert sorted(tools["parked_grep"].parameters["required"]) == ["file", "pattern"]
        assert sorted(tools["parked_slice"].parameters["required"]) == ["end", "file", "start"]
        assert sorted(tools["parked_get"].parameters["required"]) == ["file", "path"]

        # Optional parameters expose their defaults.
        find_props = tools["find_node"].parameters["properties"]
        assert find_props["all_roots"]["default"] is False

    def test_registered_fns_are_the_module_functions(self):
        # The tool the client calls is exactly the function tested directly.
        server = build_server()
        tools = {t.name: t for t in server._tool_manager.list_tools()}
        assert tools["find_node"].fn is find_node
        assert tools["get_context"].fn is get_context
        assert tools["query_status"].fn is query_status
        assert tools["add_learning"].fn is add_learning
        assert tools["parked_schema"].fn is parked_schema
        assert tools["parked_grep"].fn is parked_grep
        assert tools["parked_slice"].fn is parked_slice
        assert tools["parked_get"].fn is parked_get


class TestFindNode:
    def test_finds_matching_node(self, hotpath_env):
        results = find_node("web")
        assert len(results) == 1
        match = results[0]
        assert match["id"] == "vm:web-01"
        assert match["name"] == "Web Server 01"
        assert match["type"] == "vm"
        assert match["ssh_alias"] == "web-prod"
        assert match["project"] == "prod"
        assert match["root"] == ""  # local root
        assert match["matched_on"]

    def test_matches_by_ssh_alias(self, hotpath_env):
        # web-prod is the ssh_alias, not the slug -- exercises the matcher reuse.
        results = find_node("prod")
        assert {m["id"] for m in results} == {"vm:web-01"}

    def test_no_match_returns_empty_list(self, hotpath_env):
        assert find_node("does-not-exist") == []


class TestGetContext:
    def test_full_context_roundtrip(self, hotpath_env):
        ctx = get_context("web-01")
        assert ctx["id"] == "vm:web-01"
        assert ctx["name"] == "Web Server 01"
        assert ctx["ssh"]["alias"] == "web-prod"
        assert ctx["triage"]["services"] == ["nginx", "php-fpm"]
        assert "learnings" in ctx and ctx["learnings"][0]["finding"] == "pool misconfigured"

    def test_exclude_learnings(self, hotpath_env):
        ctx = get_context("web-01", include_learnings=False)
        assert "learnings" not in ctx

    def test_unknown_node_raises_clean_error(self, hotpath_env):
        with pytest.raises(ToolError) as exc:
            get_context("no-such-node")
        # The error names the query and reads as a message, not a traceback.
        assert "no-such-node" in str(exc.value)


class TestQueryStatus:
    def test_node_without_sources_returns_clean_structure(self, hotpath_env):
        # db-01 is bare: no observability, no ssh target -> zero sources.
        status = query_status("vm:db-01")
        assert status == {"node": "vm:db-01", "sources": []}

    def test_unknown_node_raises_clean_error(self, hotpath_env):
        with pytest.raises(ToolError) as exc:
            query_status("ghost")
        assert "ghost" in str(exc.value)


class TestOversizedOutputParking:
    """Per-source parking on the query_status MCP path, and the parked_* tools."""

    @pytest.fixture(autouse=True)
    def isolated_scratch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IC_SCRATCH_DIR", str(tmp_path / "parked"))
        monkeypatch.setenv("IC_PARK_THRESHOLD", "200")

    @staticmethod
    def _doc():
        return {
            "node": "vm:web-01",
            "sources": [
                {"source": "Prometheus", "type": "prometheus", "success": True,
                 "error": None, "data": {"up": 1}},
                {"source": "Loki (recent errors)", "type": "loki", "success": True,
                 "error": None,
                 "data": {"logs": [{"line": "error " + "x" * 50} for _ in range(20)]}},
                {"source": "CheckMK", "type": "checkmk", "success": False,
                 "error": "unreachable", "data": None},
            ],
        }

    def test_query_status_tool_parks_through_the_real_path(self, monkeypatch):
        # Pin the wiring itself: the MCP query_status tool must route its
        # parsed document through parking (a mutation dropping the
        # _park_oversized_sources call must fail here).
        doc = self._doc()

        def fake_cli_query_status(node_id, output_json=False):
            print(json.dumps(doc))

        monkeypatch.setattr("infracontext.cli.query.query_status", fake_cli_query_status)
        result = query_status("vm:web-01")
        assert result["sources"][1]["data"]["_parked"] is True
        assert result["sources"][0]["data"] == {"up": 1}

    def test_get_context_never_parks(self, hotpath_env, monkeypatch):
        # Parking is deliberately query_status-only; get_context must return
        # its full document even when it exceeds the threshold.
        monkeypatch.setenv("IC_PARK_THRESHOLD", "1")
        ctx = get_context("web-01")
        assert "_parked" not in json.dumps(ctx)
        assert ctx["learnings"][0]["finding"] == "pool misconfigured"

    def test_small_sources_stay_inline_large_ones_park(self):
        doc = _park_oversized_sources(self._doc())

        prom, loki, cmk = doc["sources"]
        assert prom["data"] == {"up": 1}  # under threshold: untouched
        assert cmk["data"] is None  # failed source: untouched

        pointer = loki["data"]
        assert pointer["_parked"] is True
        # Label carries node and source type for traceability on disk.
        assert pointer["file"].startswith("vm-web-01-loki-")

    def test_parked_source_roundtrips_through_read_tools(self):
        doc = _park_oversized_sources(self._doc())
        file = doc["sources"][1]["data"]["file"]

        schema = parked_schema(file)
        assert schema["schema"]["logs"]["__array__"] == 20

        grep = parked_grep(file, "error", max_matches=3)
        assert grep["total_matches"] == 20 and grep["returned"] == 3

        line_no = grep["matches"][0]["line"]
        sliced = parked_slice(file, line_no, line_no)
        assert "error" in sliced["content"]

        got = parked_get(file, "logs[0].line")
        assert got["value"].startswith("error")

    def test_read_tools_translate_parking_errors(self):
        with pytest.raises(ToolError):
            parked_schema("../escape.json")
        with pytest.raises(ToolError):
            parked_grep("missing-file.json", "x")
        with pytest.raises(ToolError):
            parked_slice("missing-file.json", 1, 2)
        with pytest.raises(ToolError):
            parked_get("missing-file.json", "a")

    def test_non_dict_doc_passes_through(self):
        assert _park_oversized_sources(["not", "a", "doc"]) == ["not", "a", "doc"]


class TestAddLearning:
    def test_append_roundtrip(self, hotpath_env):
        before = len(_read(hotpath_env, "vm:db-01").learnings)
        result = add_learning("db", "cache pool tuned", context="triage", source="agent")

        assert result["node_id"] == "vm:db-01"
        assert result["ok"] is True
        assert result["context"] == "triage"
        assert result["source"] == "agent"
        assert result["date"]  # ISO date string

        node = _read(hotpath_env, "vm:db-01")
        assert len(node.learnings) == before + 1
        assert node.learnings[-1].finding == "cache pool tuned"
        assert node.learnings[-1].source == "agent"
        assert node.learnings[-1].context == "triage"

    def test_default_context_and_source(self, hotpath_env):
        result = add_learning("db", "observed a thing")
        assert result["context"] == "mcp"
        assert result["source"] == "agent"

    def test_empty_finding_rejected(self, hotpath_env):
        before = len(_read(hotpath_env, "vm:db-01").learnings)
        with pytest.raises(ToolError):
            add_learning("db", "   ")
        assert len(_read(hotpath_env, "vm:db-01").learnings) == before

    def test_unknown_node_raises_clean_error(self, hotpath_env):
        with pytest.raises(ToolError) as exc:
            add_learning("phantom", "finding")
        assert "phantom" in str(exc.value)


class TestServeCommand:
    def test_help_works(self):
        from infracontext.cli.main import app

        result = CliRunner().invoke(app, ["mcp", "serve", "--help"])
        assert result.exit_code == 0
        assert "--project" in result.output

    def test_missing_mcp_extra_message(self, monkeypatch):
        from infracontext.cli.main import app

        # Force `import mcp` inside serve() to fail, simulating a base install
        # without the optional extra.
        monkeypatch.setitem(sys.modules, "mcp", None)
        result = CliRunner().invoke(app, ["mcp", "serve"])
        assert result.exit_code == 1
        # The remedy must cover both install modes: a uv tool install (extras
        # baked in at install time) and a dev checkout (uv sync).
        assert "uv tool install" in result.output
        assert "[mcp]" in result.output
        assert "uv sync --extra mcp" in result.output


def test_build_server_lazy_import_guarded_from_cli_startup():
    """Importing the CLI entrypoint must not pull in the mcp SDK.

    Guards the startup-latency budget: ``ic`` runs on every incident command,
    so ``mcp``/``anyio``/``starlette`` must stay behind the lazy import in
    ``serve``. mcp_server itself is what imports them, so it must be absent
    after a bare ``import infracontext.cli.main``.
    """
    import subprocess

    check = (
        "import infracontext.cli.main, sys; "
        "assert 'infracontext.mcp_server' not in sys.modules, 'mcp_server eagerly imported'; "
        "assert 'mcp' not in sys.modules, 'mcp SDK eagerly imported'; "
        "print('ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", check],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"
