"""MCP (Model Context Protocol) server exposing infracontext as typed tools.

Agents (Claude Code sessions, skills) get infrastructure context as structured
tools instead of shelling out to ``ic`` and string-parsing YAML/JSON. Four
tools are served over stdio: ``find_node``, ``get_context``, ``query_status``,
and ``add_learning``.

Reuse, not reimplementation
---------------------------
Every tool adapts an existing CLI code path through its stable public entry
point (``node_find``, ``run_node_context``, ``query_status``,
``resolve_node_or_exit`` + ``append_learning``). Those paths emit their result
to a Rich console / ``print`` and signal failure with ``typer.Exit``; the
adaptation here is exactly to *capture* that output, parse the machine-readable
JSON they already produce, and translate ``typer.Exit`` into a clean tool
error. Depending only on documented signatures and JSON contracts keeps this
module resilient to internal refactors of ``describe.py`` / ``query.py``.

Stdout discipline
-----------------
The stdio transport owns stdout for the JSON-RPC protocol. It binds
``sys.stdout.buffer`` once at startup and writes framed messages there.
``contextlib.redirect_stdout`` swaps only the Python-level ``sys.stdout`` text
object, never that captured buffer -- so capturing a reused CLI path's output
during a tool call cannot interleave with or corrupt the protocol stream. The
startup banner is written to stderr for the same reason.

Tool handlers are synchronous. An MCP stdio client drives one request at a
time, so the brief stdout-capture windows never overlap; and even if they did,
the transport's buffer is untouched, so the worst case is a bounded, in-process
capture -- never a corrupted wire.
"""

from __future__ import annotations

import contextlib
import io
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # Import only for type checkers; never at runtime module load.
    from mcp.server.fastmcp import FastMCP

SERVER_NAME = "infracontext"
TOOL_NAMES = ("find_node", "get_context", "query_status", "add_learning")


class ToolError(RuntimeError):
    """A tool could not complete. FastMCP maps this to a clean tool error."""


def _captured(call: Any, *, on_exit: str) -> str:
    """Run a stdout-emitting CLI ``call`` and return what it wrote.

    ``call`` is a zero-arg thunk wrapping a reused CLI function that prints its
    result to stdout and raises ``typer.Exit`` on failure. Its stdout is
    captured (see the module docstring on why this is protocol-safe). On
    ``typer.Exit`` the captured text -- Rich markup renders to plain text on a
    non-tty stream -- becomes the error message, falling back to ``on_exit``.
    """
    import typer

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        try:
            call()
        except typer.Exit as exit_signal:
            detail = buffer.getvalue().strip()
            raise ToolError(detail or on_exit) from exit_signal
    return buffer.getvalue().strip()


def _parse_json(payload: str, *, what: str) -> Any:
    """Parse a captured JSON payload, or raise a clear ToolError."""
    if not payload:
        raise ToolError(f"{what} produced no output.")
    try:
        return json.loads(payload)
    except json.JSONDecodeError as err:
        raise ToolError(f"{what} produced unparseable output: {payload[:200]}") from err


def find_node(query: str, all_roots: bool = False) -> list[dict]:
    """Find infrastructure nodes by name, domain, IP, SSH alias, or ID.

    Fuzzy-searches the active project (or, with ``all_roots``, every local
    project and every configured external root). Use it to answer "which node
    is X?" before calling ``get_context`` or ``query_status``.

    Args:
        query: Search term -- a node ID (``type:slug``), slug, name, domain,
            IP address, or SSH alias. Substring matches are honored.
        all_roots: Search across the local root and all external roots instead
            of just the active project. Cross-root matches are returned with
            qualified ``@alias:type:slug`` IDs.

    Returns:
        A list of match dicts, each with ``id``, ``name``, ``type``,
        ``ssh_alias``, ``project``, ``root`` ("" for the local root), and
        ``matched_on`` (why it matched). Empty list if nothing matches.
    """
    from infracontext.cli.describe import node_find

    output = _captured(
        lambda: node_find(
            query=query, show_all=True, all_roots_flag=all_roots, output_json=True
        ),
        on_exit=f"Could not search for {query!r}.",
    )
    return _parse_json(output, what="find_node")


def get_context(
    node_id: str,
    include_relationships: bool = True,
    include_learnings: bool = True,
) -> dict:
    """Get full triage context for a node.

    Returns the same structured context ``ic describe node context`` emits:
    identity, SSH connection command, network addresses, triage hints, access
    tier and capabilities, endpoints, observability, dependency graph
    (upstream/downstream), and accumulated learnings. This is the primary tool
    for understanding a node before or during an incident.

    Args:
        node_id: Node ID (``type:slug``), a fuzzy query resolved against the
            active project, or a qualified ``@alias:type:slug`` reference.
        include_relationships: Include the up/downstream dependency graph.
        include_learnings: Include recorded learnings.

    Returns:
        The node context as a JSON-serializable dict.
    """
    from infracontext.cli.describe import OutputFormat, run_node_context

    output = _captured(
        lambda: run_node_context(
            node_id,
            include_relationships=include_relationships,
            include_learnings=include_learnings,
            fmt=OutputFormat.json,
        ),
        on_exit=f"Node {node_id!r} not found.",
    )
    return _parse_json(output, what="get_context")


def query_status(node_id: str) -> dict:
    """Query all configured monitoring sources for a node.

    Fetches Prometheus, CheckMK, Loki (recent errors), Monit, and any imported
    SOS report concurrently, returning one aggregated document. Use it for a
    fast health snapshot during triage.

    Args:
        node_id: Node ID (``type:slug``) or a fuzzy query resolved against the
            active project.

    Returns:
        ``{"node": <id>, "sources": [{"source", "type", "success", "error",
        "data"}, ...]}``. ``sources`` is empty when the node has no monitoring
        configured.
    """
    from infracontext.cli.query import query_status as cli_query_status

    output = _captured(
        lambda: cli_query_status(node_id, output_json=True),
        on_exit=f"Could not query status for {node_id!r}.",
    )
    return _parse_json(output, what="query_status")


def add_learning(
    node_id: str,
    finding: str,
    context: str = "mcp",
    source: str = "agent",
) -> dict:
    """Record a learning on a node.

    Learnings are durable findings that accumulate on a node's living document
    to inform future triage. Call this after discovering something worth
    remembering (a root cause, a gotcha, a fix).

    Args:
        node_id: Node ID (``type:slug``), fuzzy query, or qualified
            ``@alias:type:slug`` reference. Writing to an external root
            requires it be configured ``mode: read-write``.
        finding: What was discovered (non-empty).
        context: What was being investigated (defaults to ``"mcp"``).
        source: Who recorded it -- ``"agent"`` or ``"human"``.

    Returns:
        ``{"node_id", "date", "context", "source", "ok": True}``.
    """
    from datetime import date

    from infracontext.cli.describe import append_learning
    from infracontext.federation import LOCAL_ROOT_ALIAS

    if not finding or not finding.strip():
        raise ToolError("finding must be a non-empty string.")

    target = _resolve_target(node_id, require_writable=True)
    recorded = date.today().isoformat()
    _captured(
        lambda: append_learning(
            target, finding=finding, context=context, source=source
        ),
        on_exit=f"Node {node_id!r} not found.",
    )

    display_id = (
        target.node_id
        if target.root_alias == LOCAL_ROOT_ALIAS
        else f"@{target.root_alias}:{target.node_id}"
    )
    return {
        "node_id": display_id,
        "date": recorded,
        "context": context,
        "source": source,
        "ok": True,
    }


def _resolve_target(query: str, *, require_writable: bool = False):
    """Resolve a node query to a ``describe._NodeTarget`` without printing.

    Reuses the shared CLI resolver (exact ``type:slug``, fuzzy, and qualified
    ``@alias:type:slug``), converting its terminal behavior into a ToolError.
    Unlike ``_captured``, this needs the resolver's *return value*, so the
    capture is inlined rather than delegated.
    """
    import typer

    from infracontext.cli.resolve import resolve_node_or_exit

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        try:
            return resolve_node_or_exit(query, require_writable=require_writable)
        except typer.Exit as exit_signal:
            detail = buffer.getvalue().strip()
            raise ToolError(detail or f"Could not resolve node {query!r}.") from exit_signal


def build_server() -> FastMCP:
    """Construct the FastMCP server with the four infracontext tools registered.

    Importing this function's body pulls in the ``mcp`` package, so callers
    that must tolerate a base install (without the ``mcp`` extra) should guard
    the import -- see ``infracontext.cli.mcp``.
    """
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(SERVER_NAME)
    server.add_tool(find_node)
    server.add_tool(get_context)
    server.add_tool(query_status)
    server.add_tool(add_learning)
    return server


def run_stdio() -> None:
    """Build the server and serve it over stdio (blocking).

    The readiness banner goes to stderr; stdout is reserved for the JSON-RPC
    protocol stream owned by the transport.
    """
    import sys

    server = build_server()
    print(
        f"infracontext MCP server {SERVER_NAME!r} ready on stdio "
        f"(tools: {', '.join(TOOL_NAMES)})",
        file=sys.stderr,
        flush=True,
    )
    server.run(transport="stdio")
