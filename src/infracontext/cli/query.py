"""Query monitoring sources for node status."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from infracontext.cli import require_project
from infracontext.cli.completion import complete_node_id
from infracontext.models.node import Node
from infracontext.overrides import get_node_overrides
from infracontext.paths import ProjectPaths
from infracontext.storage import read_model, read_yaml

app = typer.Typer(no_args_is_help=True)
console = Console()

# Strip Rich markup tags (e.g. "[red]...[/red]") when reusing human-facing
# error lines for the machine-readable --json path.
_MARKUP_RE = re.compile(r"\[/?[a-z0-9 #]+\]")


@dataclass
class _StatusSection:
    """One monitoring source within ``ic query status``.

    ``fetch`` runs in a worker thread and returns a plugin-specific value
    (usually a ``QueryResult``, or a raised exception captured as a value).
    ``printer`` renders it to the Rich console; ``to_json`` converts the same
    value into a ``{success, error, data}`` dict for ``--json``.
    """

    title: str
    source_type: str
    fetch: Callable[[], Any]
    printer: Callable[[Any], None]
    to_json: Callable[[Any], dict]


def _plugin_result_json(result: Any) -> dict:
    """Convert a plugin ``QueryResult`` (or captured exception) to JSON fields."""
    if isinstance(result, Exception):
        return {"success": False, "error": str(result), "data": None}
    return {"success": bool(result.success), "error": result.error, "data": result.data}


def _sos_result_json(result: Any) -> dict:
    """Convert the classified SOS fetch outcome to JSON fields."""
    if isinstance(result, Exception):
        return {"success": False, "error": str(result), "data": None}
    kind, payload = result
    if kind == "ok":
        return {"success": True, "error": None, "data": payload}
    if kind == "missing":
        return {"success": False, "error": "sosq not installed", "data": None}
    return {"success": False, "error": payload, "data": None}


def _strip_markup(text: str) -> str:
    """Remove Rich markup tags from a string."""
    return _MARKUP_RE.sub("", text)


def _resolve_query_target(node_id: str) -> tuple[str, str, Node]:
    """Resolve a fuzzy/exact node argument to ``(project, node_id, node)``.

    An argument containing ``:`` is an exact address and takes the original
    fast path (``require_project`` + ``require_node``) unchanged. A bare query
    is fuzzy-matched against the active project's nodes so ``ic query status
    web`` resolves the same way ``ic ssh web`` does.
    """
    if ":" in node_id:
        project = require_project()
        return project, node_id, require_node(project, node_id)

    from infracontext.cli.resolve import resolve_node_or_exit

    target = resolve_node_or_exit(node_id)
    return target.project, target.node_id, require_node(target.project, target.node_id)


def _index_sources(project: str) -> list[tuple[str, dict]]:
    """Read every source config in the project once as ``(stem, config)``.

    ``ic query status`` resolves several source types in one run; globbing and
    reading the sources dir a single time and picking from the result avoids
    the previous per-type re-glob.
    """
    paths = ProjectPaths.for_project(project)
    if not paths.sources_dir.exists():
        return []
    return [(f.stem, read_yaml(f)) for f in sorted(paths.sources_dir.glob("*.yaml"))]


def _pick_source(
    sources: list[tuple[str, dict]], source_type: str, source_name: str | None = None
) -> dict | None:
    """Select a source of ``source_type`` from a pre-read index.

    With ``source_name`` the matching file stem must also carry the right
    ``type``; otherwise the first source of the type (stems are sorted) wins.
    The returned dict carries an added ``name`` (the file stem).
    """
    if source_name:
        for stem, config in sources:
            if stem == source_name:
                if config.get("type") == source_type:
                    return {**config, "name": stem}
                return None
        return None

    for stem, config in sources:
        if config.get("type") == source_type:
            return {**config, "name": stem}
    return None


def get_source_config(
    project: str,
    source_type: str,
    source_name: str | None = None,
    sources: list[tuple[str, dict]] | None = None,
) -> dict | None:
    """Find source config of given type, optionally by name.

    Args:
        project: Project slug
        source_type: Source type (prometheus, loki, checkmk)
        source_name: Optional specific source name (file stem). If not provided,
                     returns first source of the given type.
        sources: Optional pre-read index from :func:`_index_sources`; pass it to
                 avoid re-reading the sources dir (``ic query status`` does).
    """
    if sources is None:
        sources = _index_sources(project)
    return _pick_source(sources, source_type, source_name)


def require_node(project: str, node_id: str) -> Node:
    """Load and validate node, or exit with a user-friendly error."""
    if ":" not in node_id:
        console.print("[red]Invalid node ID. Use format: type:slug[/red]")
        raise typer.Exit(1)

    node_type, slug = node_id.split(":", 1)
    try:
        node_file = ProjectPaths.for_project(project).node_file(node_type, slug)
    except ValueError as e:
        console.print(f"[red]Invalid node ID '{node_id}': {e}[/red]")
        raise typer.Exit(1) from None

    if not node_file.exists():
        console.print(f"[red]Node '{node_id}' not found.[/red]")
        raise typer.Exit(1)

    node = read_model(node_file, Node)
    if node is None:
        console.print(f"[red]Failed to read node '{node_id}'.[/red]")
        raise typer.Exit(1)
    return node


def _load_node_optional(project: str, node_id: str) -> Node | None:
    """Read a node model by ID, or None if the ID is malformed or missing."""
    if ":" not in node_id:
        return None
    node_type, slug = node_id.split(":", 1)
    try:
        node_file = ProjectPaths.for_project(project).node_file(node_type, slug)
    except ValueError:
        return None
    if not node_file.exists():
        return None
    return read_model(node_file, Node)


def get_node_observability(
    project: str, node_id: str, obs_type: str, node: Node | None = None
) -> dict | None:
    """Get observability config for a node.

    Pass a pre-loaded ``node`` to reuse it instead of re-reading the file --
    ``ic query status`` loads the node once and threads it through each source
    lookup rather than re-parsing per observability type.
    """
    if node is None:
        node = _load_node_optional(project, node_id)
    if node is None or not node.observability:
        return None

    for obs in node.observability:
        if obs.type == obs_type:
            return obs.model_dump()
    return None


def get_node_ssh_target(project: str, node_id: str, node: Node | None = None) -> str | None:
    """Get SSH target for a node (ssh_alias, domain, or IP).

    Pass a pre-loaded ``node`` (with local overrides already applied) to avoid
    re-reading the file. When omitted, the node is read *with* local overrides
    so the SSH alias honors ``.infracontext.local.yaml``.
    """
    if node is None:
        from infracontext.cli.describe import read_node_with_overrides

        if ":" not in node_id:
            return None
        node_type, slug = node_id.split(":", 1)
        try:
            node_file = ProjectPaths.for_project(project).node_file(node_type, slug)
        except ValueError:
            return None
        if not node_file.exists():
            return None
        node = read_node_with_overrides(node_file, project=project)
        if not node:
            return None

    # Prefer ssh_alias, then domain, then IP
    if node.ssh_alias:
        return node.ssh_alias
    if node.domains:
        return node.domains[0]
    if node.ip_addresses:
        return node.ip_addresses[0]
    return None


def _apply_local_overrides(node: Node, project: str) -> None:
    """Apply ``.infracontext.local.yaml`` overrides to an in-memory node.

    Mirrors :func:`infracontext.cli.describe.read_node_with_overrides` so
    ``ic query status`` honors ssh_alias / source_paths overrides after reading
    the node file exactly once (environment resolves to the current root, the
    same context ``get_node_ssh_target`` used when it re-read the file).
    """
    overrides = get_node_overrides(node.id, None, project)
    if overrides.ssh_alias is not None:
        node.ssh_alias = overrides.ssh_alias
    if overrides.source_paths is not None:
        node.source_paths = overrides.source_paths


@app.command("prometheus")
def query_prometheus(
    node_id: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or fuzzy query", autocompletion=complete_node_id),
    ],
    query_type: Annotated[
        str, typer.Option("--type", "-t", help="Query type: status, cpu, memory, disk, load")
    ] = "status",
    promql: Annotated[str | None, typer.Option("--promql", "-q", help="Custom PromQL query")] = None,
    output_json: Annotated[bool, typer.Option("--json", help="Output raw JSON")] = False,
    raw: Annotated[bool, typer.Option("--raw", "-r", hidden=True, help="Deprecated alias for --json")] = False,
) -> None:
    """Query Prometheus metrics for a node.

    Examples:
        ic query prometheus vm:web-server
        ic query prometheus vm:web-server -t cpu
        ic query prometheus vm:web-server --promql 'up{instance="web:9100"}'
    """
    from infracontext.query.prometheus import PrometheusPlugin

    project, node_id, node = _resolve_query_target(node_id)

    # Get instance and source name from node's observability config
    obs = get_node_observability(project, node_id, "prometheus")
    source_name = obs.get("source") if obs else None
    source_config = get_source_config(project, "prometheus", source_name)

    if not source_config:
        console.print("[red]No Prometheus source configured.[/red]")
        console.print("[dim]Add one with: ic describe source add prometheus --type prometheus[/dim]")
        raise typer.Exit(1)

    if obs and obs.get("instance"):
        node_selector = obs["instance"]
    else:
        # Fallback: derive from node ID
        node_selector = f"{node.slug}:9100"
        console.print(f"[dim]No Prometheus config in node, using: {node_selector}[/dim]")

    plugin = PrometheusPlugin()
    result = plugin.query(source_config, node_selector, query_type, promql=promql)

    if not result.success:
        console.print(f"[red]Query failed: {result.error}[/red]")
        raise typer.Exit(1)

    if raw or output_json:
        print(json.dumps(result.data, indent=2))
    else:
        _print_prometheus_result(result.data, query_type)


def _print_prometheus_result(data: dict, query_type: str) -> None:
    """Pretty print Prometheus results."""
    if query_type == "status":
        console.print("[bold]Node Metrics[/bold]")
        for metric, value in data.items():
            if value is not None:
                if metric == "up":
                    status = "[green]UP[/green]" if value == 1 else "[red]DOWN[/red]"
                    console.print(f"  Status: {status}")
                elif metric in ("cpu", "memory", "disk"):
                    color = "green" if value < 70 else "yellow" if value < 90 else "red"
                    console.print(f"  {metric.upper()}: [{color}]{value:.1f}%[/{color}]")
                elif metric == "load":
                    console.print(f"  Load 1m: {value:.2f}")
    else:
        console.print(json.dumps(data, indent=2))


@app.command("loki")
def query_loki(
    node_id: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or fuzzy query", autocompletion=complete_node_id),
    ],
    since: Annotated[str, typer.Option("--since", "-s", help="Time range (e.g., 1h, 30m, 2d)")] = "1h",
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max log entries")] = 50,
    grep: Annotated[str | None, typer.Option("--grep", "-g", help="Filter pattern")] = None,
    logql: Annotated[str | None, typer.Option("--logql", "-q", help="Custom LogQL query")] = None,
    labels: Annotated[bool, typer.Option("--labels", help="List available labels")] = False,
    output_json: Annotated[bool, typer.Option("--json", help="Output raw JSON")] = False,
    raw: Annotated[bool, typer.Option("--raw", "-r", hidden=True, help="Deprecated alias for --json")] = False,
) -> None:
    """Query Loki logs for a node.

    Examples:
        ic query loki vm:web-server
        ic query loki vm:web-server --grep error --since 2h
        ic query loki vm:web-server --logql '{service_name="web"} |= "error"'
    """
    from infracontext.query.loki import LokiPlugin

    project, node_id, node = _resolve_query_target(node_id)

    # Get selector and source name from node's observability config
    obs = get_node_observability(project, node_id, "loki")
    source_name = obs.get("source") if obs else None
    source_config = get_source_config(project, "loki", source_name)

    if not source_config:
        console.print("[red]No Loki source configured.[/red]")
        console.print("[dim]Add one with: ic describe source add loki --type loki[/dim]")
        raise typer.Exit(1)

    if labels:
        plugin = LokiPlugin()
        result = plugin.query(source_config, "", query_type="labels")
        if result.success:
            console.print("[bold]Available Labels[/bold]")
            for label in result.data.get("labels", []):
                console.print(f"  {label}")
        else:
            console.print(f"[red]{result.error}[/red]")
        return

    if obs and obs.get("selector"):
        node_selector = obs["selector"]
    else:
        # Fallback: derive from node ID
        node_name = node.slug
        node_selector = f'{{host="{node_name}"}}'
        console.print(f"[dim]No Loki config in node, using: {node_selector}[/dim]")

    plugin = LokiPlugin()
    result = plugin.query(source_config, node_selector, logql=logql, since=since, limit=limit, grep=grep)

    if not result.success:
        console.print(f"[red]Query failed: {result.error}[/red]")
        raise typer.Exit(1)

    if raw or output_json:
        print(json.dumps(result.data, indent=2))
    else:
        logs = result.data.get("logs", [])
        console.print(f"[dim]Found {len(logs)} log entries[/dim]")
        for entry in logs:
            if "line" in entry:
                console.print(entry["line"])
            elif "timestamp" in entry:
                console.print(f"{entry.get('timestamp', '')} {entry.get('line', entry)}")


@app.command("checkmk")
def query_checkmk(
    node_id: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or fuzzy query", autocompletion=complete_node_id),
    ],
    query_type: Annotated[str, typer.Option("--type", "-t", help="Query type: status, services, alerts")] = "status",
    output_json: Annotated[bool, typer.Option("--json", help="Output raw JSON")] = False,
    raw: Annotated[bool, typer.Option("--raw", "-r", hidden=True, help="Deprecated alias for --json")] = False,
) -> None:
    """Query CheckMK for node status.

    Examples:
        ic query checkmk vm:web-server
        ic query checkmk vm:web-server -t services
        ic query checkmk vm:web-server -t alerts
    """
    from infracontext.query.checkmk import CheckMKPlugin

    project, node_id, node = _resolve_query_target(node_id)

    # Get host_name and source name from node's observability config
    obs = get_node_observability(project, node_id, "checkmk")
    source_name = obs.get("source") if obs else None
    source_config = get_source_config(project, "checkmk", source_name)

    if not source_config:
        console.print("[red]No CheckMK source configured.[/red]")
        console.print("[dim]Add one with: ic describe source add checkmk --type checkmk[/dim]")
        raise typer.Exit(1)

    if obs and obs.get("host_name"):
        node_selector = obs["host_name"]
    else:
        # Fallback: derive from node ID
        node_selector = node.slug
        console.print(f"[dim]No CheckMK config in node, using: {node_selector}[/dim]")

    plugin = CheckMKPlugin()
    result = plugin.query(source_config, node_selector, query_type)

    if not result.success:
        console.print(f"[red]Query failed: {result.error}[/red]")
        raise typer.Exit(1)

    if raw or output_json:
        print(json.dumps(result.data, indent=2))
    else:
        _print_checkmk_result(result.data, query_type)


def _print_checkmk_result(data: dict, query_type: str) -> None:
    """Pretty print CheckMK results."""
    if query_type == "status":
        console.print("[bold]Host Status[/bold]")
        state = data.get("state", -1)
        state_text = {0: "[green]UP[/green]", 1: "[red]DOWN[/red]", 2: "[yellow]UNREACHABLE[/yellow]"}.get(
            state, f"[dim]{state}[/dim]"
        )
        console.print(f"  State: {state_text}")
        if data.get("in_downtime"):
            console.print("  [yellow]In scheduled downtime[/yellow]")
        if data.get("acknowledged"):
            console.print("  [dim]Problem acknowledged[/dim]")

    elif query_type == "services":
        summary = data.get("summary", {})
        console.print("[bold]Services Summary[/bold]")
        console.print(f"  [green]OK: {summary.get('ok', 0)}[/green]")
        if summary.get("warn", 0) > 0:
            console.print(f"  [yellow]WARN: {summary['warn']}[/yellow]")
        if summary.get("crit", 0) > 0:
            console.print(f"  [red]CRIT: {summary['crit']}[/red]")
        if summary.get("unknown", 0) > 0:
            console.print(f"  [dim]UNKNOWN: {summary['unknown']}[/dim]")

        # Show non-OK services
        for svc in data.get("services", []):
            if svc.get("state", 0) != 0:
                state_color = {1: "yellow", 2: "red"}.get(svc.get("state"), "dim")
                console.print(
                    f"  [{state_color}]{svc.get('description')}: {svc.get('plugin_output', '')[:60]}[/{state_color}]"
                )

    elif query_type == "alerts":
        alerts = data.get("alerts", [])
        if not alerts:
            console.print("[green]No active alerts[/green]")
        else:
            console.print(f"[bold]Active Alerts ({len(alerts)})[/bold]")
            for alert in alerts:
                state_color = {1: "yellow", 2: "red"}.get(alert.get("state"), "dim")
                console.print(
                    f"  [{state_color}]{alert.get('service')}: {alert.get('output', '')[:60]}[/{state_color}]"
                )


@app.command("redfish")
def query_redfish(
    node_id: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or fuzzy query", autocompletion=complete_node_id),
    ],
    query_type: Annotated[str, typer.Option("--type", "-t", help="Query type: status, power")] = "status",
    output_json: Annotated[bool, typer.Option("--json", help="Output raw JSON")] = False,
    raw: Annotated[bool, typer.Option("--raw", "-r", hidden=True, help="Deprecated alias for --json")] = False,
) -> None:
    """Query a BMC over Redfish for live health or power.

    The node must carry a ``redfish`` observability entry whose ``instance``
    is the BMC base URL; the Redfish source config supplies credentials.

    Examples:
        ic query redfish network_device:web-01-bmc
        ic query redfish web-01-bmc -t power
        ic query redfish web-01-bmc --json
    """
    from infracontext.query.redfish import RedfishQueryPlugin

    project, node_id, _node = _resolve_query_target(node_id)

    obs = get_node_observability(project, node_id, "redfish")
    source_name = obs.get("source") if obs else None
    source_config = get_source_config(project, "redfish", source_name)

    if not source_config:
        console.print("[red]No Redfish source configured.[/red]")
        console.print("[dim]Add one with: ic describe source add redfish --type redfish[/dim]")
        raise typer.Exit(1)

    node_selector = obs.get("instance") if obs else None
    if not node_selector:
        console.print(f"[red]Node '{node_id}' has no Redfish URL (observability instance).[/red]")
        console.print(f"[dim]Add a redfish observability entry: ic describe node edit {node_id}[/dim]")
        raise typer.Exit(1)

    plugin = RedfishQueryPlugin()
    result = plugin.query(source_config, node_selector, query_type)

    if not result.success:
        console.print(f"[red]Query failed: {result.error}[/red]")
        raise typer.Exit(1)

    if raw or output_json:
        print(json.dumps(result.data, indent=2))
    else:
        _print_redfish_result(result.data, query_type)


def _print_redfish_result(data: dict, query_type: str) -> None:
    """Pretty print Redfish query results."""
    if query_type == "power":
        total = data.get("power_watts")
        if total is None:
            console.print("[dim]No power reading available[/dim]")
        else:
            console.print(f"[bold]Power[/bold]: {total:.0f} W")
        for chassis in data.get("chassis", []):
            watts = chassis.get("power_watts")
            watts_str = f"{watts:.0f} W" if isinstance(watts, (int, float)) else "n/a"
            console.print(f"  {chassis.get('id', '?')}: {watts_str}")
        return

    # status
    health = data.get("health", "Unknown")
    color = {"ok": "green", "warning": "yellow", "critical": "red"}.get(str(health).lower(), "dim")
    console.print(f"[bold]Health[/bold]: [{color}]{health}[/{color}]")
    for system in data.get("systems", []):
        sh = system.get("health") or "?"
        sc = {"ok": "green", "warning": "yellow", "critical": "red"}.get(str(sh).lower(), "dim")
        console.print(f"  System {system.get('id', '?')}: [{sc}]{sh}[/{sc}] ({system.get('state') or '?'})")
    thermal = data.get("thermal")
    if thermal:
        th = thermal.get("health") or "?"
        tc = {"ok": "green", "warning": "yellow", "critical": "red"}.get(str(th).lower(), "dim")
        console.print(f"  Thermal: [{tc}]{th}[/{tc}]")


@app.command("snmp")
def query_snmp(
    node_id: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or fuzzy query", autocompletion=complete_node_id),
    ],
    query_type: Annotated[
        str, typer.Option("--type", "-t", help="Query type: status, interfaces")
    ] = "status",
    output_json: Annotated[bool, typer.Option("--json", help="Output raw JSON")] = False,
    raw: Annotated[bool, typer.Option("--raw", "-r", hidden=True, help="Deprecated alias for --json")] = False,
) -> None:
    """Query a network device over SNMP for live status.

    The node must carry an ``snmp`` observability entry (its ``instance`` is the
    device host to walk); the SNMP source config supplies credentials.

    Examples:
        ic query snmp network_device:core-sw
        ic query snmp core-sw -t interfaces
        ic query snmp core-sw --json
    """
    from infracontext.query.snmp import SNMPQueryPlugin

    project, node_id, node = _resolve_query_target(node_id)

    # Get host (instance) and source name from the node's observability config
    obs = get_node_observability(project, node_id, "snmp")
    source_name = obs.get("source") if obs else None
    source_config = get_source_config(project, "snmp", source_name)

    if not source_config:
        console.print("[red]No SNMP source configured.[/red]")
        console.print("[dim]Add one with: ic describe source add snmp --type snmp[/dim]")
        raise typer.Exit(1)

    if obs and obs.get("instance"):
        node_selector = obs["instance"]
    else:
        # Fallback: derive the device host from the node's addresses.
        node_selector = _snmp_fallback_host(node)
        console.print(f"[dim]No SNMP instance in node, using: {node_selector}[/dim]")

    plugin = SNMPQueryPlugin()
    result = plugin.query(source_config, node_selector, query_type)

    if not result.success:
        console.print(f"[red]Query failed: {result.error}[/red]")
        raise typer.Exit(1)

    if raw or output_json:
        print(json.dumps(result.data, indent=2))
    else:
        _print_snmp_result(result.data, query_type)


def _snmp_fallback_host(node: Node) -> str:
    """Best device host for an SNMP walk when no observability instance is set."""
    if node.ip_addresses:
        return node.ip_addresses[0]
    if node.domains:
        return node.domains[0]
    return node.slug


def _print_snmp_result(data: dict, query_type: str) -> None:
    """Pretty print SNMP query results."""
    if query_type == "interfaces":
        interfaces = data.get("interfaces", [])
        console.print(f"[bold]Interfaces ({data.get('total', len(interfaces))})[/bold]")
        for iface in interfaces:
            oper = iface.get("oper", "")
            color = "green" if oper == "up" else "red" if oper == "down" else "dim"
            speed = f" {iface['speed_mbps']}Mb" if iface.get("speed_mbps") else ""
            alias = f" [dim]({iface['alias']})[/dim]" if iface.get("alias") else ""
            console.print(
                f"  {iface.get('name', '?')}: [{color}]{oper or '?'}[/{color}]"
                f"/{iface.get('admin', '?')}{speed}{alias}"
            )
        return

    # status
    console.print("[bold]Device[/bold]")
    if data.get("sys_name"):
        console.print(f"  Name: {data['sys_name']}")
    if data.get("sys_uptime"):
        console.print(f"  Uptime: {data['sys_uptime']}")
    if data.get("sys_location"):
        console.print(f"  Location: {data['sys_location']}")
    counts = data.get("interface_counts", {})
    if counts:
        up = counts.get("up", 0)
        down = counts.get("down", 0)
        total = counts.get("total", 0)
        down_str = f", [red]{down} down[/red]" if down else ""
        console.print(f"  Interfaces: [green]{up} up[/green]{down_str} ({total} total)")


@app.command("status")
def query_status(
    node_id: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or fuzzy query", autocompletion=complete_node_id),
    ],
    output_json: Annotated[
        bool, typer.Option("--json", help="Emit one aggregated JSON document instead of Rich output")
    ] = False,
) -> None:
    """Query all configured monitoring sources for a node.

    Queries prometheus, checkmk, loki (errors), SNMP and Redfish (each when the
    node carries the matching observability entry), monit, and any imported SOS
    report. Sources are fetched concurrently, so total wall time is bounded
    by the slowest source rather than the sum of all timeouts -- during an
    incident with an unreachable monitoring host that's the difference
    between ~1 minute and several.

    With ``--json`` the same results are emitted as a single machine-readable
    document (``{"node": ..., "sources": [{source, type, success, error,
    data}, ...]}``) on stdout, for agents and scripts.
    """
    from concurrent.futures import ThreadPoolExecutor

    project, node_id, node = _resolve_query_target(node_id)
    # Read the node once: apply local overrides in place (so the SSH target
    # honors ssh_alias overrides) and read the sources dir a single time. Every
    # per-source lookup below reuses this node/index instead of re-parsing.
    _apply_local_overrides(node, project)
    sources = _index_sources(project)

    # Each section fetches in a worker thread (must not touch the console) and
    # is rendered in the main thread in registration order. ``to_json`` mirrors
    # the printer for the --json path.
    sections: list[_StatusSection] = []

    def _plugin_printer(print_ok: Callable[[Any], None], error_style: str = "red") -> Callable:
        def _print(result: Any) -> None:
            if isinstance(result, Exception):
                console.print(f"  [red]Error: {result}[/red]")
            elif result.success:
                print_ok(result.data)
            else:
                console.print(f"  [{error_style}]{result.error}[/{error_style}]")

        return _print

    # Prometheus — resolve source from node's observability config
    prom_obs = get_node_observability(project, node_id, "prometheus", node=node)
    prom_config = get_source_config(
        project, "prometheus", prom_obs.get("source") if prom_obs else None, sources=sources
    )
    if prom_config:
        from infracontext.query.prometheus import PrometheusPlugin

        prom_selector = prom_obs.get("instance") if prom_obs else f"{node.slug}:9100"
        sections.append(_StatusSection(
            "Prometheus",
            "prometheus",
            lambda: PrometheusPlugin().query(prom_config, prom_selector, "status"),
            _plugin_printer(lambda data: _print_prometheus_result(data, "status")),
            _plugin_result_json,
        ))

    # CheckMK — resolve source from node's observability config
    cmk_obs = get_node_observability(project, node_id, "checkmk", node=node)
    cmk_config = get_source_config(
        project, "checkmk", cmk_obs.get("source") if cmk_obs else None, sources=sources
    )
    if cmk_config:
        from infracontext.query.checkmk import CheckMKPlugin

        cmk_selector = cmk_obs.get("host_name") if cmk_obs else node.slug
        sections.append(_StatusSection(
            "CheckMK",
            "checkmk",
            lambda: CheckMKPlugin().query(cmk_config, cmk_selector, "alerts"),
            _plugin_printer(lambda data: _print_checkmk_result(data, "alerts")),
            _plugin_result_json,
        ))

    # Loki (errors only) — resolve source from node's observability config
    loki_obs = get_node_observability(project, node_id, "loki", node=node)
    loki_config = get_source_config(
        project, "loki", loki_obs.get("source") if loki_obs else None, sources=sources
    )
    if loki_config:
        from infracontext.query.loki import LokiPlugin

        loki_selector = loki_obs.get("selector") if loki_obs else f'{{host="{node.slug}"}}'

        def _print_loki_logs(data: Any) -> None:
            logs = data.get("logs", [])
            if logs:
                console.print(f"  [yellow]Found {len(logs)} error entries[/yellow]")
                for entry in logs[:5]:
                    line = entry.get("line", str(entry))[:100]
                    console.print(f"  [dim]{line}[/dim]")
            else:
                console.print("  [green]No errors in last hour[/green]")

        sections.append(_StatusSection(
            "Loki (recent errors)",
            "loki",
            lambda: LokiPlugin().query(
                loki_config, loki_selector, grep="error", since="1h", limit=10
            ),
            _plugin_printer(_print_loki_logs),
            _plugin_result_json,
        ))

    # SNMP — only when the node explicitly carries an snmp observability entry.
    # (Unlike prometheus/checkmk, we never probe a device the node didn't
    # declare: there is no safe slug fallback for a host we may not reach.)
    snmp_obs = get_node_observability(project, node_id, "snmp", node=node)
    snmp_instance = snmp_obs.get("instance") if snmp_obs else None
    snmp_config = get_source_config(
        project, "snmp", snmp_obs.get("source") if snmp_obs else None, sources=sources
    )
    if snmp_instance and snmp_config:
        from infracontext.query.snmp import SNMPQueryPlugin

        sections.append(_StatusSection(
            "SNMP",
            "snmp",
            lambda: SNMPQueryPlugin().query(snmp_config, snmp_instance, "status"),
            _plugin_printer(lambda data: _print_snmp_result(data, "status")),
            _plugin_result_json,
        ))

    # Redfish — only when the node carries a redfish observability entry with
    # a URL (there is no slug fallback for a BMC endpoint).
    redfish_obs = get_node_observability(project, node_id, "redfish", node=node)
    redfish_url = redfish_obs.get("instance") if redfish_obs else None
    redfish_config = get_source_config(
        project, "redfish", redfish_obs.get("source") if redfish_obs else None, sources=sources
    )
    if redfish_url and redfish_config:
        from infracontext.query.redfish import RedfishQueryPlugin

        sections.append(_StatusSection(
            "Redfish",
            "redfish",
            lambda: RedfishQueryPlugin().query(redfish_config, redfish_url, "status"),
            _plugin_printer(lambda data: _print_redfish_result(data, "status")),
            _plugin_result_json,
        ))

    # Monit (if node has SSH access or direct URL)
    obs = get_node_observability(project, node_id, "monit", node=node)
    monit_url = obs.get("monit_url") if obs else None
    ssh_target = get_node_ssh_target(project, node_id, node=node)
    if monit_url or ssh_target:
        from infracontext.query.monit import MonitPlugin

        if monit_url:
            credential = obs.get("credential_hint") if obs else None
            skip_verify = bool(obs.get("tls_skip_verify", False)) if obs else False
            monit_fetch = lambda: MonitPlugin().query(  # noqa: E731
                url=monit_url, credential=credential, tls_skip_verify=skip_verify
            )
        else:
            port = obs.get("monit_port", 2812) if obs else 2812
            monit_fetch = lambda: MonitPlugin().query(ssh_target=ssh_target, port=port)  # noqa: E731
        sections.append(_StatusSection(
            "Monit",
            "monit",
            monit_fetch,
            _plugin_printer(_print_monit_result, error_style="dim"),
            _plugin_result_json,
        ))

    # SOS Report (if node has an imported report)
    sos_raw = node.attributes.get("sos_report_path")
    if sos_raw:
        sos_path, sos_errors = _resolve_sos_report_path(sos_raw, node_id)

        if sos_path is None:

            def _print_sos_invalid(_result: Any) -> None:
                for line in sos_errors:
                    console.print(line)

            sections.append(_StatusSection(
                "SOS Report",
                "sos",
                lambda: None,
                _print_sos_invalid,
                lambda _r, errs=sos_errors: {
                    "success": False,
                    "error": " ".join(_strip_markup(line) for line in errs).strip(),
                    "data": None,
                },
            ))
        else:
            sections.append(_StatusSection(
                "SOS Report",
                "sos",
                lambda: _fetch_sos_health(sos_path),
                _print_sos_result,
                _sos_result_json,
            ))

    if not sections:
        if output_json:
            print(json.dumps({"node": node_id, "sources": []}, indent=2))
            return
        console.print(f"[yellow]No monitoring sources configured for {node_id}.[/yellow]")
        console.print(
            "[dim]Add an observability entry to the node YAML "
            f"(ic describe node edit {node_id}) or configure a source "
            "(ic describe source add).[/dim]"
        )
        return

    if output_json:
        with ThreadPoolExecutor(max_workers=len(sections)) as pool:
            futures = [pool.submit(_swallow_exceptions(s.fetch)) for s in sections]
            sources_out = []
            for section, future in zip(sections, futures, strict=True):
                result = future.result()
                sources_out.append({
                    "source": section.title,
                    "type": section.source_type,
                    **section.to_json(result),
                })
        print(json.dumps({"node": node_id, "sources": sources_out}, indent=2))
        return

    console.print(f"[bold]Querying monitoring for {node_id}[/bold]")
    console.print()

    with ThreadPoolExecutor(max_workers=len(sections)) as pool:
        futures = [pool.submit(_swallow_exceptions(s.fetch)) for s in sections]
        # Print in registration order as each result lands; sections that
        # finish early are buffered by their future.
        for section, future in zip(sections, futures, strict=True):
            result = future.result()
            console.print(f"[cyan]{section.title}[/cyan]")
            section.printer(result)
            console.print()


def _swallow_exceptions(fn: Callable[[], Any]) -> Callable[[], Any]:
    """Wrap a fetch so a raising source returns its exception as a value.

    One broken source (bad config, DNS failure, ...) must not cancel the
    other concurrent fetches or escape as a traceback.
    """

    def _safe() -> Any:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 -- deliberate catch-all per source
            return e

    return _safe


def _fetch_sos_health(sos_path: Path) -> tuple[str, Any]:
    """Run ``sosq health`` and classify the outcome for the printer.

    Returns one of ``("ok", parsed_json)``, ``("stderr", message)``, or
    ``("missing", None)`` when sosq is not installed.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["sosq", "health", "--json", str(sos_path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except FileNotFoundError:
        return ("missing", None)
    if proc.returncode == 0:
        return ("ok", json.loads(proc.stdout))
    return ("stderr", proc.stderr.strip())


def _print_sos_result(result: Any) -> None:
    if isinstance(result, Exception):
        console.print(f"  [red]Error: {result}[/red]")
        return
    kind, payload = result
    if kind == "ok":
        _print_sos_health(payload)
    elif kind == "missing":
        console.print("  [dim]sosq not installed[/dim]")
    else:
        console.print(f"  [dim]{payload}[/dim]")


def _resolve_sos_report_path(raw: object, node_id: str) -> tuple[Path | None, list[str]]:
    """Validate an ``sos_report_path`` node attribute before shell-out.

    The value is stored in node YAML and forwarded to ``sosq``. It's passed
    as a list element (not through a shell), so this isn't an injection
    vector, but an unvalidated path produces opaque ``sosq`` failures. This
    resolves it to an absolute :class:`Path` and confirms it exists.

    Returns ``(path, [])`` on success, or ``(None, error_lines)`` where
    ``error_lines`` are rich-markup strings for the caller to print --
    keeping this function print-free lets ``query status`` place the errors
    under its section header.
    """
    if not raw or not isinstance(raw, str):
        return None, [
            f"[red]No SOS report path for node '{node_id}'.[/red]",
            f"[dim]Import one with: ic import sos <path> --node {node_id}[/dim]",
        ]
    resolved = Path(raw).expanduser()
    if not resolved.is_absolute():
        # sos reports are typically absolute; a relative value is suspicious
        # enough to flag rather than resolve against an arbitrary CWD.
        return None, [
            f"[red]SOS report path '{raw}' for node '{node_id}' is not absolute.[/red]",
            f"[dim]Fix it with: ic describe node edit {node_id} "
            "(set attributes.sos_report_path to an absolute path)[/dim]",
        ]
    if not resolved.exists():
        return None, [
            f"[red]SOS report path for node '{node_id}' does not exist: {resolved}[/red]",
            f"[dim]Re-import with: ic import sos <path> --node {node_id}[/dim]",
        ]
    return resolved, []


@app.command("sos")
def query_sos(
    node_id: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or fuzzy query", autocompletion=complete_node_id),
    ],
    query_type: Annotated[
        str, typer.Option("--type", "-t", help="Query type: health, errors, info, search")
    ] = "health",
    grep: Annotated[str | None, typer.Option("--grep", "-g", help="Search pattern (for search type)")] = None,
    output_json: Annotated[bool, typer.Option("--json", help="Output raw JSON (health only)")] = False,
    raw: Annotated[bool, typer.Option("--raw", "-r", hidden=True, help="Deprecated alias for --json")] = False,
) -> None:
    """Query SOS report data for a node.

    Requires the node to have an sos_report_path attribute (set by ic import sos).
    Shells out to sosq CLI.

    Examples:
        ic query sos vm:web-server
        ic query sos vm:web-server -t errors
        ic query sos vm:web-server -t search -g 'OOM'
    """
    import subprocess

    project, node_id, node = _resolve_query_target(node_id)

    report_path, path_errors = _resolve_sos_report_path(
        node.attributes.get("sos_report_path"), node_id
    )
    if report_path is None:
        for line in path_errors:
            console.print(line)
        raise typer.Exit(1)

    if query_type == "health":
        cmd = ["sosq", "health", "--json", str(report_path)]
    elif query_type == "errors":
        cmd = ["sosq", "errors", str(report_path)]
    elif query_type == "info":
        cmd = ["sosq", "info", str(report_path)]
    elif query_type == "search":
        if not grep:
            console.print("[red]--grep pattern required for search type[/red]")
            raise typer.Exit(1)
        cmd = ["sosq", "search", str(report_path), grep]
    else:
        console.print(f"[red]Unknown query type '{query_type}'. Use: health, errors, info, search[/red]")
        raise typer.Exit(1)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except FileNotFoundError:
        console.print("[red]sosq not found. Install with: pip install sosq[/red]")
        raise typer.Exit(1) from None

    if result.returncode != 0:
        console.print(f"[red]sosq failed: {result.stderr.strip()}[/red]")
        raise typer.Exit(1)

    if (raw or output_json) and query_type == "health":
        print(result.stdout)
    elif query_type == "health":
        try:
            data = json.loads(result.stdout)
            _print_sos_health(data)
        except json.JSONDecodeError:
            console.print(result.stdout)
    else:
        # errors, info, search — sosq already formats these nicely
        console.print(result.stdout.rstrip())


def _print_sos_health(data: dict) -> None:
    """Pretty print SOS health check results."""
    system = data.get("system", {})
    findings = data.get("findings", [])

    console.print(f"[bold]SOS Report: {system.get('hostname', 'unknown')}[/bold]")
    console.print(f"  OS: {system.get('os', '?')}  Kernel: {system.get('kernel', '?')}")

    if not findings:
        console.print("  [green]No issues found[/green]")
        return

    severity_style = {"critical": "red", "warning": "yellow", "info": "dim"}
    for f in sorted(findings, key=lambda x: {"critical": 0, "warning": 1, "info": 2}.get(x.get("severity", ""), 3)):
        style = severity_style.get(f["severity"], "dim")
        console.print(f"  [{style}]{f['severity'].upper()}[/{style}] {f['category']}: {f['message']}")

    crit = sum(1 for f in findings if f["severity"] == "critical")
    warn = sum(1 for f in findings if f["severity"] == "warning")
    console.print(f"  [dim]{len(findings)} finding(s): {crit} critical, {warn} warning(s)[/dim]")


@app.command("monit")
def query_monit(
    node_id: Annotated[
        str,
        typer.Argument(help="Node ID (type:slug) or fuzzy query", autocompletion=complete_node_id),
    ],
    service: Annotated[str | None, typer.Option("--service", "-s", help="Specific service to query")] = None,
    port: Annotated[int, typer.Option("--port", "-p", help="Monit HTTP port (SSH mode)")] = 2812,
    url: Annotated[str | None, typer.Option("--url", "-u", help="Direct Monit HTTP URL")] = None,
    output_json: Annotated[bool, typer.Option("--json", help="Output raw JSON")] = False,
    raw: Annotated[bool, typer.Option("--raw", "-r", hidden=True, help="Deprecated alias for --json")] = False,
) -> None:
    """Query Monit service status.

    Two modes:
    - Direct HTTP: Use --url or configure monit_url in node observability
    - SSH mode: Connects via SSH and queries localhost:2812

    Examples:
        ic query monit vm:web-server
        ic query monit vm:web-server --service nginx
        ic query monit vm:web-server --url http://monit.example.com:2812
    """
    from infracontext.query.monit import MonitPlugin

    project, node_id, _node = _resolve_query_target(node_id)

    # Check node's observability config for monit settings
    obs = get_node_observability(project, node_id, "monit")
    monit_url = url or (obs.get("monit_url") if obs else None)
    monit_port = obs.get("monit_port", port) if obs else port
    credential = obs.get("credential_hint") if obs else None
    tls_skip_verify = bool(obs.get("tls_skip_verify", False)) if obs else False

    plugin = MonitPlugin()

    if monit_url:
        # Direct HTTP mode
        result = plugin.query(
            url=monit_url, credential=credential, service=service, tls_skip_verify=tls_skip_verify
        )
    else:
        # SSH mode
        ssh_target = get_node_ssh_target(project, node_id)
        if not ssh_target:
            console.print(f"[red]No SSH target or Monit URL found for node '{node_id}'.[/red]")
            console.print("[dim]Add ssh_alias/domain/IP or set monit_url in observability.[/dim]")
            raise typer.Exit(1)
        result = plugin.query(ssh_target=ssh_target, port=monit_port, service=service)

    if not result.success:
        console.print(f"[red]Query failed: {result.error}[/red]")
        raise typer.Exit(1)

    if raw or output_json:
        print(json.dumps(result.data, indent=2))
    else:
        _print_monit_result(result.data, service)


def _print_monit_result(data: dict, filter_service: str | None = None) -> None:
    """Pretty print Monit results."""
    services = data.get("services", [])
    summary = data.get("summary", {})

    if filter_service:
        # Single service detail
        if not services:
            console.print(f"[yellow]Service '{filter_service}' not found[/yellow]")
            return
        svc = services[0]
        console.print(f"[bold]{svc['name']}[/bold] ({svc['type']})")
        status_color = "green" if svc["status"] == 0 else "red"
        console.print(f"  Status: [{status_color}]{svc['status_text']}[/{status_color}]")
        if "pid" in svc:
            console.print(f"  PID: {svc['pid']}")
        if "uptime" in svc:
            hours = svc["uptime"] // 3600
            console.print(f"  Uptime: {hours}h")
        if "memory_percent" in svc:
            console.print(f"  Memory: {svc['memory_percent']:.1f}%")
        if "cpu_percent" in svc:
            console.print(f"  CPU: {svc['cpu_percent']:.1f}%")
    else:
        # Summary view
        console.print("[bold]Monit Services[/bold]")
        console.print(f"  Total: {summary.get('total', 0)}")
        console.print(f"  [green]Running: {summary.get('running', 0)}[/green]")
        if summary.get("failed", 0) > 0:
            console.print(f"  [red]Failed: {summary['failed']}[/red]")
        if summary.get("not_monitored", 0) > 0:
            console.print(f"  [dim]Not monitored: {summary['not_monitored']}[/dim]")

        # Show failed services
        for svc in services:
            if svc["status"] not in (0, 3):  # Not running and not "not monitored"
                console.print(f"  [red]{svc['name']}: {svc['status_text']}[/red]")
