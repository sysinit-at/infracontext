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
) -> None:
    r"""Serve infracontext as an MCP server over stdio.

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

    from infracontext.mcp_server import run_stdio

    run_stdio()
