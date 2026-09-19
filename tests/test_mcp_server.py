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

    def test_attributes_and_components_flow_through(self, hotpath_env):
        # The structured layer (hardware, netbox_components) must reach MCP
        # clients -- it was silently absent while notes prose got through.
        ctx = get_context("web-01")
        assert ctx["attributes"]["hardware"]["serial"] == "SN-1"
        components = ctx["attributes"]["netbox_components"]
        assert components["interfaces"][0]["name"] == "eth0"
        assert components["inventory_items"][0]["role"] == "Disk"

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


class TestStreamableHttp:
    """The HTTP runner builds the ASGI app itself and drives uvicorn, so the
    auth middleware has somewhere to wrap; these pin that wiring."""

    @staticmethod
    def _fake_server(recorder):
        class _Settings:
            host = port = stateless_http = None
            transport_security = "UNTOUCHED"
            log_level = "INFO"

        class _FakeServer:
            settings = _Settings()

            def streamable_http_app(self):
                return "INNER_APP"

        return _FakeServer()

    def _run(self, monkeypatch, **kwargs):
        import infracontext.mcp_server as mod

        seen = {}
        server = self._fake_server(seen)
        monkeypatch.setattr(mod, "build_server", lambda: server)

        class _FakeUvicorn:
            @staticmethod
            def run(app, host, port, log_level, ssl_certfile=None, ssl_keyfile=None):
                seen.update(app=app, host=host, port=port, cert=ssl_certfile)

        monkeypatch.setitem(sys.modules, "uvicorn", _FakeUvicorn)
        mod.run_streamable_http(**kwargs)
        seen["settings"] = server.settings
        return seen

    def test_help_shows_http_options(self):
        from infracontext.cli.main import app

        result = CliRunner().invoke(app, ["mcp", "serve", "--help"])
        assert result.exit_code == 0
        assert "--http" in result.output
        assert "--port" in result.output

    def test_configures_host_port_and_stateless(self, monkeypatch):
        seen = self._run(monkeypatch, host="127.0.0.1", port=9001)
        assert (seen["host"], seen["port"]) == ("127.0.0.1", 9001)
        assert seen["settings"].stateless_http is True
        assert seen["app"] == "INNER_APP"  # unwrapped when no token

    def test_auth_token_wraps_the_app(self, monkeypatch):
        from infracontext.mcp_server import BearerAuthMiddleware

        seen = self._run(monkeypatch, host="0.0.0.0", port=8722, auth_token="tok")
        assert isinstance(seen["app"], BearerAuthMiddleware)

    def test_allow_host_extends_allowlist_keeping_loopback(self, monkeypatch):
        seen = self._run(monkeypatch, host="0.0.0.0", port=8722, allowed_hosts=["infra-mcp:*"])
        ts = seen["settings"].transport_security
        assert ts.enable_dns_rebinding_protection is True
        assert "infra-mcp:*" in ts.allowed_hosts
        assert "localhost:*" in ts.allowed_hosts  # loopback preserved
        assert "http://infra-mcp:*" in ts.allowed_origins

    def test_no_allow_host_leaves_secure_default(self, monkeypatch):
        seen = self._run(monkeypatch, host="127.0.0.1", port=8722)
        assert seen["settings"].transport_security == "UNTOUCHED"

    def test_allow_host_covers_both_schemes(self, monkeypatch):
        """Once TLS is on, the same name arrives as an https Origin."""
        seen = self._run(monkeypatch, host="0.0.0.0", port=8722, allowed_hosts=["infra-mcp:*"])
        origins = seen["settings"].transport_security.allowed_origins
        assert "http://infra-mcp:*" in origins
        assert "https://infra-mcp:*" in origins
        assert "https://localhost:*" in origins


class TestTls:
    def test_cert_and_key_reach_uvicorn(self, monkeypatch):
        import infracontext.mcp_server as mod

        seen = {}

        class _Settings:
            host = port = stateless_http = transport_security = None
            log_level = "INFO"

        class _FakeServer:
            settings = _Settings()

            def streamable_http_app(self):
                return "INNER"

        monkeypatch.setattr(mod, "build_server", lambda: _FakeServer())

        class _FakeUvicorn:
            @staticmethod
            def run(app, host, port, log_level, ssl_certfile, ssl_keyfile):
                seen.update(cert=ssl_certfile, key=ssl_keyfile)

        monkeypatch.setitem(sys.modules, "uvicorn", _FakeUvicorn)
        mod.run_streamable_http(host="0.0.0.0", port=1, tls_cert="/c.pem", tls_key="/k.pem")
        assert seen == {"cert": "/c.pem", "key": "/k.pem"}

    def test_cert_without_key_is_rejected(self):
        from infracontext.cli.main import app

        result = CliRunner().invoke(app, ["mcp", "serve", "--http", "--tls-cert", "/c.pem"])
        assert result.exit_code == 1
        assert "must be given together" in result.output

    def test_key_without_cert_is_rejected(self):
        from infracontext.cli.main import app

        result = CliRunner().invoke(app, ["mcp", "serve", "--http", "--tls-key", "/k.pem"])
        assert result.exit_code == 1
        assert "must be given together" in result.output


class TestBearerAuth:
    """The token is the whole access control once the port is reachable."""

    @staticmethod
    def _scope(header: bytes | None):
        headers = [(b"host", b"x")]
        if header is not None:
            headers.append((b"authorization", header))
        return {"type": "http", "headers": headers}

    @staticmethod
    async def _drive(mw, scope):
        sent = []

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {"type": "http.request"}

        await mw(scope, receive, send)
        return sent

    def _run(self, mw, scope):
        import asyncio

        return asyncio.run(self._drive(mw, scope))

    def _mw(self, token="s3cret", inner=None):
        from infracontext.mcp_server import BearerAuthMiddleware

        calls = []

        async def default_inner(scope, receive, send):
            calls.append(scope["type"])

        return BearerAuthMiddleware(inner or default_inner, token), calls

    def test_correct_token_passes_through(self):
        mw, calls = self._mw()
        sent = self._run(mw, self._scope(b"Bearer s3cret"))
        assert calls == ["http"]
        assert sent == []  # inner app owns the response

    def test_missing_header_is_401(self):
        mw, calls = self._mw()
        sent = self._run(mw, self._scope(None))
        assert calls == []  # inner app never reached
        assert sent[0]["status"] == 401
        assert (b"www-authenticate", b'Bearer realm="infracontext"') in sent[0]["headers"]

    def test_wrong_token_is_401(self):
        mw, calls = self._mw()
        assert self._run(mw, self._scope(b"Bearer wrong"))[0]["status"] == 401
        assert calls == []

    def test_token_prefix_is_not_enough(self):
        """Guards against a truncating/startswith comparison."""
        mw, _ = self._mw(token="s3cret")
        assert self._run(mw, self._scope(b"Bearer s3c"))[0]["status"] == 401

    def test_wrong_scheme_is_401(self):
        mw, _ = self._mw()
        assert self._run(mw, self._scope(b"Basic s3cret"))[0]["status"] == 401

    def test_scheme_is_case_insensitive(self):
        mw, calls = self._mw()
        self._run(mw, self._scope(b"bearer s3cret"))
        assert calls == ["http"]

    def test_lifespan_scope_bypasses_auth(self):
        """Authenticating lifespan would stop the session manager starting."""
        mw, calls = self._mw()
        self._run(mw, {"type": "lifespan", "headers": []})
        assert calls == ["lifespan"]


class TestResolveAuthToken:
    def test_env_var_is_used(self, monkeypatch):
        from infracontext.mcp_server import resolve_auth_token

        monkeypatch.setenv("IC_MCP_AUTH_TOKEN", "  from-env  ")
        assert resolve_auth_token() == "from-env"

    def test_absent_env_is_none(self, monkeypatch):
        from infracontext.mcp_server import resolve_auth_token

        monkeypatch.delenv("IC_MCP_AUTH_TOKEN", raising=False)
        assert resolve_auth_token() is None

    def test_empty_env_is_none(self, monkeypatch):
        from infracontext.mcp_server import resolve_auth_token

        monkeypatch.setenv("IC_MCP_AUTH_TOKEN", "   ")
        assert resolve_auth_token() is None

    def test_file_wins_over_env(self, tmp_path, monkeypatch):
        from infracontext.mcp_server import resolve_auth_token

        monkeypatch.setenv("IC_MCP_AUTH_TOKEN", "from-env")
        f = tmp_path / "tok"
        f.write_text("from-file\n")
        assert resolve_auth_token(str(f)) == "from-file"

    def test_empty_file_raises(self, tmp_path):
        from infracontext.mcp_server import resolve_auth_token

        f = tmp_path / "tok"
        f.write_text("\n")
        with pytest.raises(ValueError):
            resolve_auth_token(str(f))


class TestRequireAuth:
    def test_require_auth_without_token_exits(self, monkeypatch):
        """A misspelled env var must fail loudly, not serve the map openly."""
        from infracontext.cli.main import app

        monkeypatch.delenv("IC_MCP_AUTH_TOKEN", raising=False)
        result = CliRunner().invoke(app, ["mcp", "serve", "--http", "--require-auth"])
        assert result.exit_code == 1
        assert "Refusing" in result.output

    def test_token_is_never_a_cli_option(self):
        """argv is world-readable via ps; only env/file may carry the token.

        Asserted against the declared parameters, not the help text -- the
        help legitimately mentions --auth-token to explain its absence.
        """
        import typing

        from infracontext.cli.mcp import serve

        # get_type_hints(include_extras) -- the module uses `from __future__
        # import annotations`, so raw annotations are strings.
        opts = set()
        for hint in typing.get_type_hints(serve, include_extras=True).values():
            for meta in typing.get_args(hint)[1:]:
                # typer keeps the first declaration in .default and the rest
                # in .param_decls, so both have to be collected.
                first = getattr(meta, "default", None)
                if isinstance(first, str) and first.startswith("-"):
                    opts.add(first)
                opts.update(getattr(meta, "param_decls", None) or ())
        assert "--auth-token-file" in opts
        assert "--auth-token" not in opts

    def test_http_run_receives_resolved_token(self, monkeypatch):
        import infracontext.mcp_server as mod
        from infracontext.cli.main import app

        seen = {}
        monkeypatch.setenv("IC_MCP_AUTH_TOKEN", "env-token")
        monkeypatch.setattr(
            mod, "run_streamable_http", lambda **kw: seen.update(kw)
        )
        CliRunner().invoke(app, ["mcp", "serve", "--http", "--require-auth"])
        assert seen.get("auth_token") == "env-token"


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
