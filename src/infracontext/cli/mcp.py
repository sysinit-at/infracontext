"""``ic mcp serve`` -- run the infracontext MCP server over stdio.

Kept deliberately thin at import time: the module top pulls in nothing heavier
than Typer so ``import infracontext.cli.main`` (and thus every ``ic`` startup)
stays fast. The ``mcp`` SDK, its ``anyio``/``starlette`` dependency graph, and
:mod:`infracontext.mcp_server` are all imported lazily inside ``serve`` -- and
only after the optional dependency is confirmed present.
"""

from __future__ import annotations

import os
from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True)

_MISSING_MCP_MESSAGE = (
    "MCP support requires the 'mcp' extra.\n"
    "  installed as a uv tool:  uv tool install --force '<path-to-checkout>[mcp]'\n"
    "  running from a checkout: uv sync --extra mcp"
)


@app.command()
# Raw docstring + \[ escapes: Rich would otherwise parse the TOML section
# header [mcp_servers.infracontext] as markup and strip it from --help.
def serve(
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            "-p",
            help="Project to serve context for (sets IC_PROJECT for the server process)",
            envvar="IC_PROJECT",
        ),
    ] = None,
    http: Annotated[
        bool,
        typer.Option(
            "--http",
            help="Serve over streamable HTTP instead of stdio (for web MCP "
            "clients like Open WebUI). The endpoint is http://HOST:PORT/mcp.",
        ),
    ] = False,
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Bind address for --http. Unless a bearer token is "
            "configured (IC_MCP_AUTH_TOKEN / --auth-token-file) the endpoint "
            "is unauthenticated: bind only to loopback or a trusted container "
            "network, never a public interface.",
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", help="TCP port for --http."),
    ] = 8722,
    allow_host: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-host",
            help="Extra Host header to accept over --http (repeatable). "
            "DNS-rebinding protection allows only localhost by default; a "
            "client reaching the server by a container/DNS name is rejected "
            "(HTTP 421) until that name is listed, e.g. --allow-host "
            "'infra-mcp:*'. Loopback stays allowed.",
        ),
    ] = None,
    auth_token_file: Annotated[
        str | None,
        typer.Option(
            "--auth-token-file",
            help="File holding the HTTP bearer token (a mounted secret). "
            "Otherwise IC_MCP_AUTH_TOKEN is used. There is deliberately no "
            "--auth-token flag: argv is world-readable via ps.",
        ),
    ] = None,
    tls_cert: Annotated[
        str | None,
        typer.Option(
            "--tls-cert",
            help="PEM certificate to serve --http over TLS (with --tls-key). "
            "Without it the bearer token crosses the network in cleartext.",
        ),
    ] = None,
    tls_key: Annotated[
        str | None,
        typer.Option("--tls-key", help="PEM private key matching --tls-cert."),
    ] = None,
    require_auth: Annotated[
        bool,
        typer.Option(
            "--require-auth",
            help="Refuse to start over --http when no bearer token is "
            "configured. Use in deployments so a missing or misspelled "
            "IC_MCP_AUTH_TOKEN fails loudly instead of silently serving the "
            "whole map unauthenticated.",
        ),
    ] = False,
) -> None:
    r"""Serve infracontext as an MCP server over stdio (default) or HTTP.

    Exposes typed tools (find_node, get_context, query_status, add_learning,
    plus parked_* read tools for oversized query payloads) so agents get
    structured infrastructure context instead of shelling out to ``ic`` and
    parsing YAML. Environment/project resolution matches the CLI
    (IC_ROOT -> cwd walk-up -> registered default).

    Register with your agent -- Claude Code:

        claude mcp add infracontext -- uv run --project /path/to/repo ic mcp serve

    OpenAI Codex (~/.codex/config.toml):

        \[mcp_servers.infracontext]
        command = "ic"
        args = \["mcp", "serve"]

    OpenCode (opencode.json): add an entry under "mcp" with the same command.

    Web clients (Open WebUI "MCP (streamable HTTP)" external tool): run with
    ``--http`` and point the client at ``http://HOST:PORT/mcp``.

    Set ``IC_MCP_AUTH_TOKEN`` (or ``--auth-token-file``) to require an
    ``Authorization: Bearer <token>`` header, and add ``--require-auth`` so a
    missing token aborts startup instead of silently serving the whole map to
    anyone who can reach the port.
    """
    if project:
        # Propagate to the server process (and every reused CLI code path,
        # which reads IC_PROJECT) exactly as the top-level ``-p`` flag does.
        os.environ["IC_PROJECT"] = project

    # Probe the optional dependency before importing anything that needs it, so
    # a base install fails with an actionable message instead of an ImportError
    # traceback. Lazy on purpose: never load ``mcp`` at CLI startup.
    try:
        import mcp  # noqa: F401
    except ImportError:
        typer.echo(_MISSING_MCP_MESSAGE, err=True)
        raise typer.Exit(1) from None

    if http:
        from infracontext.mcp_server import resolve_auth_token, run_streamable_http

        try:
            token = resolve_auth_token(auth_token_file)
        except (OSError, ValueError) as e:
            typer.echo(f"Could not read the auth token: {e}", err=True)
            raise typer.Exit(1) from None
        if require_auth and not token:
            typer.echo(
                "--require-auth was given but no bearer token is configured "
                "(set IC_MCP_AUTH_TOKEN or pass --auth-token-file). Refusing "
                "to serve the infrastructure map unauthenticated.",
                err=True,
            )
            raise typer.Exit(1)
        if bool(tls_cert) != bool(tls_key):
            typer.echo("--tls-cert and --tls-key must be given together.", err=True)
            raise typer.Exit(1)
        run_streamable_http(
            host=host,
            port=port,
            allowed_hosts=allow_host,
            auth_token=token,
            tls_cert=tls_cert,
            tls_key=tls_key,
        )
    else:
        from infracontext.mcp_server import run_stdio

        run_stdio()
