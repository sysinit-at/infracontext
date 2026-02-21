"""Query monitoring sources for node status."""

import json
from typing import Annotated

import typer
from rich.console import Console

from infracontext.cli import require_project
from infracontext.models.node import Node
from infracontext.paths import ProjectPaths
from infracontext.storage import read_model, read_yaml

app = typer.Typer(no_args_is_help=True)
console = Console()


def get_source_config(project: str, source_type: str, source_name: str | None = None) -> dict | None:
    """Find source config of given type, optionally by name.

    Args:
        project: Project slug
        source_type: Source type (prometheus, loki, checkmk)
        source_name: Optional specific source name (file stem). If not provided,
                     returns first source of the given type.
    """
    paths = ProjectPaths.for_project(project)
    if not paths.sources_dir.exists():
        return None

    # If specific name requested, look for that file
    if source_name:
        try:
            source_file = paths.source_file(source_name)
        except ValueError:
            return None
        if source_file.exists():
            config = read_yaml(source_file)
            if config.get("type") == source_type:
                config["name"] = source_file.stem
                return config
        return None

    # Otherwise find first source of type
    for source_file in sorted(paths.sources_dir.glob("*.yaml")):
        config = read_yaml(source_file)
        if config.get("type") == source_type:
            config["name"] = source_file.stem
            return config
    return None


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


def get_node_observability(project: str, node_id: str, obs_type: str) -> dict | None:
    """Get observability config for a node."""
    paths = ProjectPaths.for_project(project)

    if ":" not in node_id:
        return None

    node_type, slug = node_id.split(":", 1)
    try:
        node_file = paths.node_file(node_type, slug)
    except ValueError:
        return None

    if not node_file.exists():
        return None

    node = read_model(node_file, Node)
    if not node or not node.observability:
        return None

    for obs in node.observability:
        if obs.type == obs_type:
            return obs.model_dump()
    return None


def get_node_ssh_target(project: str, node_id: str) -> str | None:
    """Get SSH target for a node (ssh_alias, domain, or IP)."""
    from infracontext.cli.describe import read_node_with_overrides

    paths = ProjectPaths.for_project(project)

    if ":" not in node_id:
        return None

    node_type, slug = node_id.split(":", 1)
    try:
        node_file = paths.node_file(node_type, slug)
    except ValueError:
        return None

    if not node_file.exists():
        return None

    node = read_node_with_overrides(node_file)
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


@app.command("prometheus")
def query_prometheus(
    node_id: Annotated[str, typer.Argument(help="Node ID (type:slug)")],
    query_type: Annotated[
        str, typer.Option("--type", "-t", help="Query type: status, cpu, memory, disk, load")
    ] = "status",
    promql: Annotated[str | None, typer.Option("--promql", "-q", help="Custom PromQL query")] = None,
    raw: Annotated[bool, typer.Option("--raw", "-r", help="Output raw JSON")] = False,
) -> None:
    """Query Prometheus metrics for a node.

    Examples:
        ic query prometheus vm:web-server
        ic query prometheus vm:web-server -t cpu
        ic query prometheus vm:web-server --promql 'up{instance="web:9100"}'
    """
    from infracontext.query.prometheus import PrometheusPlugin

    project = require_project()
    node = require_node(project, node_id)

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

    if raw:
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
    node_id: Annotated[str, typer.Argument(help="Node ID (type:slug)")],
    since: Annotated[str, typer.Option("--since", "-s", help="Time range (e.g., 1h, 30m, 2d)")] = "1h",
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max log entries")] = 50,
    grep: Annotated[str | None, typer.Option("--grep", "-g", help="Filter pattern")] = None,
    logql: Annotated[str | None, typer.Option("--logql", "-q", help="Custom LogQL query")] = None,
    labels: Annotated[bool, typer.Option("--labels", help="List available labels")] = False,
    raw: Annotated[bool, typer.Option("--raw", "-r", help="Output raw JSON")] = False,
) -> None:
    """Query Loki logs for a node.

    Examples:
        ic query loki vm:web-server
        ic query loki vm:web-server --grep error --since 2h
        ic query loki vm:web-server --logql '{service_name="web"} |= "error"'
    """
    from infracontext.query.loki import LokiPlugin

    project = require_project()
    node = require_node(project, node_id)

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

    if raw:
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
    node_id: Annotated[str, typer.Argument(help="Node ID (type:slug)")],
    query_type: Annotated[str, typer.Option("--type", "-t", help="Query type: status, services, alerts")] = "status",
    raw: Annotated[bool, typer.Option("--raw", "-r", help="Output raw JSON")] = False,
) -> None:
    """Query CheckMK for node status.

    Examples:
        ic query checkmk vm:web-server
        ic query checkmk vm:web-server -t services
        ic query checkmk vm:web-server -t alerts
    """
    from infracontext.query.checkmk import CheckMKPlugin

    project = require_project()
    node = require_node(project, node_id)

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

    if raw:
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


@app.command("status")
def query_status(
    node_id: Annotated[str, typer.Argument(help="Node ID (type:slug)")],
) -> None:
    """Query all configured monitoring sources for a node.

    Runs prometheus, loki (errors), and checkmk queries in sequence.
    """
    project = require_project()
    node = require_node(project, node_id)

    console.print(f"[bold]Querying monitoring for {node_id}[/bold]")
    console.print()

    # Prometheus
    if get_source_config(project, "prometheus"):
        console.print("[cyan]Prometheus[/cyan]")
        try:
            from infracontext.query.prometheus import PrometheusPlugin

            obs = get_node_observability(project, node_id, "prometheus")
            node_selector = obs.get("instance") if obs else f"{node.slug}:9100"
            result = PrometheusPlugin().query(get_source_config(project, "prometheus"), node_selector, "status")
            if result.success:
                _print_prometheus_result(result.data, "status")
            else:
                console.print(f"  [red]{result.error}[/red]")
        except Exception as e:
            console.print(f"  [red]Error: {e}[/red]")
        console.print()

    # CheckMK
    if get_source_config(project, "checkmk"):
        console.print("[cyan]CheckMK[/cyan]")
        try:
            from infracontext.query.checkmk import CheckMKPlugin

            obs = get_node_observability(project, node_id, "checkmk")
            node_selector = obs.get("host_name") if obs else node.slug
            result = CheckMKPlugin().query(get_source_config(project, "checkmk"), node_selector, "alerts")
            if result.success:
                _print_checkmk_result(result.data, "alerts")
            else:
                console.print(f"  [red]{result.error}[/red]")
        except Exception as e:
            console.print(f"  [red]Error: {e}[/red]")
        console.print()

    # Loki (errors only)
    if get_source_config(project, "loki"):
        console.print("[cyan]Loki (recent errors)[/cyan]")
        try:
            from infracontext.query.loki import LokiPlugin

            obs = get_node_observability(project, node_id, "loki")
            node_selector = obs.get("selector") if obs else f'{{host="{node.slug}"}}'
            result = LokiPlugin().query(
                get_source_config(project, "loki"), node_selector, grep="error", since="1h", limit=10
            )
            if result.success:
                logs = result.data.get("logs", [])
                if logs:
                    console.print(f"  [yellow]Found {len(logs)} error entries[/yellow]")
                    for entry in logs[:5]:
                        line = entry.get("line", str(entry))[:100]
                        console.print(f"  [dim]{line}[/dim]")
                else:
                    console.print("  [green]No errors in last hour[/green]")
            else:
                console.print(f"  [red]{result.error}[/red]")
        except Exception as e:
            console.print(f"  [red]Error: {e}[/red]")
        console.print()

    # Monit (if node has SSH access or direct URL)
    obs = get_node_observability(project, node_id, "monit")
    monit_url = obs.get("monit_url") if obs else None
    ssh_target = get_node_ssh_target(project, node_id)

    if monit_url or ssh_target:
        console.print("[cyan]Monit[/cyan]")
        try:
            from infracontext.query.monit import MonitPlugin

            if monit_url:
                credential = obs.get("credential_hint") if obs else None
                result = MonitPlugin().query(url=monit_url, credential=credential)
            else:
                port = obs.get("monit_port", 2812) if obs else 2812
                result = MonitPlugin().query(ssh_target=ssh_target, port=port)
            if result.success:
                _print_monit_result(result.data)
            else:
                console.print(f"  [dim]{result.error}[/dim]")
        except Exception as e:
            console.print(f"  [red]Error: {e}[/red]")


@app.command("monit")
def query_monit(
    node_id: Annotated[str, typer.Argument(help="Node ID (type:slug)")],
    service: Annotated[str | None, typer.Option("--service", "-s", help="Specific service to query")] = None,
    port: Annotated[int, typer.Option("--port", "-p", help="Monit HTTP port (SSH mode)")] = 2812,
    url: Annotated[str | None, typer.Option("--url", "-u", help="Direct Monit HTTP URL")] = None,
    raw: Annotated[bool, typer.Option("--raw", "-r", help="Output raw JSON")] = False,
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

    project = require_project()
    require_node(project, node_id)

    # Check node's observability config for monit settings
    obs = get_node_observability(project, node_id, "monit")
    monit_url = url or (obs.get("monit_url") if obs else None)
    monit_port = obs.get("monit_port", port) if obs else port
    credential = obs.get("credential_hint") if obs else None

    plugin = MonitPlugin()

    if monit_url:
        # Direct HTTP mode
        result = plugin.query(url=monit_url, credential=credential, service=service)
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

    if raw:
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
