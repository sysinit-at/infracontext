"""MCP (Model Context Protocol) server exposing infracontext as typed tools.

Agents (Claude Code sessions, skills) get infrastructure context as structured
tools instead of shelling out to ``ic`` and string-parsing YAML/JSON. Eight
tools are served over stdio: ``find_node``, ``get_context``, ``query_status``,
``add_learning``, and the four ``parked_*`` read tools.

Oversized-output parking
------------------------
Observability payloads (Loki logs, CheckMK service lists, SOS findings) can
dwarf a context window. ``query_status`` therefore parks any per-source
``data`` above a byte threshold (:mod:`infracontext.parking`) and returns a
compact pointer in its place; the ``parked_schema`` / ``parked_grep`` /
``parked_slice`` / ``parked_get`` tools then pull bounded slices on demand.
Parking is MCP-only -- CLI ``--json`` output stays complete for scripts.

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
TOOL_NAMES = (
    "find_node",
    "get_context",
    "query_status",
    "add_learning",
    "parked_schema",
    "parked_grep",
    "parked_slice",
    "parked_get",
)


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
        configured. A source ``data`` too large for context is replaced by a
        pointer dict (``"_parked": true``) naming a parked file -- explore it
        with the ``parked_schema``/``parked_grep``/``parked_slice``/
        ``parked_get`` tools instead of re-querying.
    """
    from infracontext.cli.query import query_status as cli_query_status

    output = _captured(
        lambda: cli_query_status(node_id, output_json=True),
        on_exit=f"Could not query status for {node_id!r}.",
    )
    return _park_oversized_sources(_parse_json(output, what="query_status"))


def _park_oversized_sources(doc: Any) -> Any:
    """Replace oversized per-source ``data`` payloads with parked pointers.

    Per-source (not whole-document) granularity keeps small sources inline
    and parks only the offender, so a healthy Prometheus summary isn't hidden
    behind a pointer just because Loki returned a log flood.
    """
    from infracontext.parking import maybe_park

    if not isinstance(doc, dict):
        return doc
    label_base = str(doc.get("node", "node")).replace(":", "-")
    for source in doc.get("sources", []):
        if isinstance(source, dict) and source.get("data") is not None:
            source["data"] = maybe_park(
                source["data"], label=f"{label_base}-{source.get('type', 'source')}"
            )
    return doc


def parked_schema(file: str) -> dict:
    """Show the structure of a parked query payload.

    Returns a recursive outline (keys, types, array lengths, string sizes) of
    a payload that ``query_status`` parked on disk, at the deepest depth that
    fits the per-call size cap. Start here to decide what to extract.

    Args:
        file: The parked file reference exactly as returned in a pointer's
            ``file`` field (a bare filename, never a path).

    Returns:
        ``{"file", "bytes", "lines", "depth", "schema"}``.
    """
    from infracontext.parking import ParkingError, schema_parked

    try:
        return schema_parked(file)
    except ParkingError as err:
        raise ToolError(str(err)) from err


def parked_grep(file: str, pattern: str, context: int = 2, max_matches: int = 50) -> dict:
    """Search a parked query payload with a regex.

    Scans the pretty-printed JSON line by line and returns matching lines with
    surrounding context, bounded by ``max_matches`` and a per-call size cap
    (trailing matches are dropped first; check ``truncated``).

    Args:
        file: Parked file reference from a pointer's ``file`` field.
        pattern: Python regex to search for (max 512 chars).
        context: Context lines around each match (0-10).
        max_matches: Maximum matches to return (1-50).

    Returns:
        ``{"file", "pattern", "total_matches", "returned", "truncated",
        "matches": [{"line", "excerpt"}, ...]}`` with 1-based line numbers
        that feed directly into ``parked_slice``.
    """
    from infracontext.parking import ParkingError, grep_parked

    try:
        return grep_parked(file, pattern, context=context, max_matches=max_matches)
    except ParkingError as err:
        raise ToolError(str(err)) from err


def parked_slice(file: str, start: int, end: int) -> dict:
    """Read a line range from a parked query payload.

    Args:
        file: Parked file reference from a pointer's ``file`` field.
        start: First line to read (1-based, inclusive).
        end: Last line to read (inclusive; capped at 400 lines per call and
            shrunk further if the content exceeds the per-call size cap --
            check the returned ``end`` and ``truncated``).

    Returns:
        ``{"file", "start", "end", "total_lines", "truncated", "content"}``
        where ``content`` is numbered lines.
    """
    from infracontext.parking import ParkingError, slice_parked

    try:
        return slice_parked(file, start, end)
    except ParkingError as err:
        raise ToolError(str(err)) from err


def parked_get(file: str, path: str, offset: int = 0, limit: int = 0) -> dict:
    """Extract a nested value from a parked query payload by dotted path.

    Args:
        file: Parked file reference from a pointer's ``file`` field.
        path: Dotted path with array indices, e.g. ``logs[0].line`` or
            ``summary.failed``.
        offset: For string values, character offset; for arrays, element
            offset. Ignored otherwise.
        limit: Window size -- characters for string values, elements for
            arrays. 0 (the default) means a 4000-char window for strings and
            everything from ``offset`` for arrays. Either window shrinks
            further to honor the per-call size cap: always compare the
            returned ``returned`` against ``length`` and page with ``offset``.

    Returns:
        ``{"file", "path", "type", "value", ...}`` plus ``length``/``offset``/
        ``returned`` for windowed strings and arrays. Dict or scalar values
        over the per-call cap fail with the value's structure so the path can
        be narrowed.
    """
    from infracontext.parking import ParkingError, get_parked

    try:
        return get_parked(file, path, offset=offset, limit=limit)
    except ParkingError as err:
        raise ToolError(str(err)) from err


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
    """Construct the FastMCP server with all infracontext tools registered.

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
    server.add_tool(parked_schema)
    server.add_tool(parked_grep)
    server.add_tool(parked_slice)
    server.add_tool(parked_get)
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
