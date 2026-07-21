"""Import commands for infracontext."""

import json
import subprocess
import time
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel, ValidationError
from rich.console import Console

import infracontext.sources  # noqa: F401 - triggers plugin registration
from infracontext.cli import require_project
from infracontext.cli.resolve import resolve_node_or_exit
from infracontext.models.node import Learning, Node, NodeType, slugify
from infracontext.models.relationship import Relationship, RelationshipFile, RelationshipType
from infracontext.paths import ProjectPaths
from infracontext.sources.dedup import find_duplicate_candidates, load_existing_nodes, overlap_warning
from infracontext.sources.registry import get_plugin_instance
from infracontext.sources.ssh_config import derive_config_path_from_project
from infracontext.storage import StorageError, read_model, read_yaml, write_model, write_yaml

app = typer.Typer(
    name="import",
    help="Import infrastructure from various sources",
    no_args_is_help=True,
)

console = Console()


@app.command("ssh-config")
def import_ssh_config(
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Explicit path to SSH config file"),
    ] = None,
    source_name: Annotated[
        str,
        typer.Option("--name", "-n", help="Name for the source (default: ssh-config)"),
    ] = "ssh-config",
) -> None:
    """Import hosts from SSH config file.

    If no path is provided, auto-discovers based on project hierarchy:
    Project <customer>/<project> → ~/.ssh/conf.d/<customer>/<project>.conf
    """
    project = require_project()
    paths = ProjectPaths.for_project(project)

    # Determine config path
    if path:
        config_path = path.expanduser()
    else:
        config_path = derive_config_path_from_project(project)
        if not config_path:
            console.print("[red]Cannot derive SSH config path from project.[/red]")
            console.print(f"[dim]Project '{project}' is not hierarchical (needs customer/project format).[/dim]")
            console.print("[dim]Use --path to specify the SSH config file explicitly.[/dim]")
            raise typer.Exit(1)

    if not config_path.exists():
        console.print(f"[red]SSH config file not found: {config_path}[/red]")
        raise typer.Exit(1)

    console.print(f"[cyan]Importing from {config_path}...[/cyan]")

    # Create or update source configuration
    try:
        source_file = paths.source_file(source_name)
    except ValueError as e:
        console.print(f"[red]Invalid source name '{source_name}': {e}[/red]")
        raise typer.Exit(1) from None
    paths.sources_dir.mkdir(exist_ok=True)

    if source_file.exists():
        config = read_yaml(source_file)
        console.print(f"[dim]Using existing source '{source_name}'[/dim]")
    else:
        config = {
            "version": "2.0",
            "name": source_name,
            "type": "ssh_config",
            "status": "configured",
            "config_path": str(config_path) if path else None,  # Only store if explicit
            "default_node_type": "vm",
            "type_patterns": {
                "physical_host": ["^pve-", "^proxmox-"],
                "lxc_container": ["^ct-", "^lxc-"],
            },
        }
        write_yaml(source_file, config)
        console.print(f"[green]Created source '{source_name}'[/green]")

    # Run sync
    plugin = get_plugin_instance("ssh_config")
    if not plugin:
        console.print("[red]SSH config plugin not found.[/red]")
        raise typer.Exit(1)

    try:
        result = plugin.sync(project, source_name)
    except Exception as e:
        console.print(f"[red]Import failed unexpectedly ({type(e).__name__}): {e}[/red]")
        raise typer.Exit(1) from None

    if result.status == "success":
        console.print("[green]Import completed successfully[/green]")
    elif result.status == "partial":
        console.print("[yellow]Import completed with warnings[/yellow]")
    else:
        console.print(f"[red]Import failed: {result.message}[/red]")
        raise typer.Exit(1)

    console.print(f"  Nodes created: {result.nodes_created}")
    console.print(f"  Nodes updated: {result.nodes_updated}")
    console.print(f"  Duration: {result.duration_ms}ms")
    for warning in result.warnings:
        console.print(f"  [yellow]{warning}[/yellow]")


@app.command("ssh", hidden=True, deprecated=True)
def import_ssh_deprecated(
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Explicit path to SSH config file"),
    ] = None,
    source_name: Annotated[
        str,
        typer.Option("--name", "-n", help="Name for the source (default: ssh-config)"),
    ] = "ssh-config",
) -> None:
    """Deprecated alias for 'ic import ssh-config'."""
    console.print("[yellow]'ic import ssh' is deprecated; use 'ic import ssh-config'.[/yellow]")
    import_ssh_config(path=path, source_name=source_name)


def _run_cmd(cmd: list[str], description: str) -> str | None:
    """Run a command and return stdout, or None on failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            console.print(f"[red]{description} failed: {result.stderr.strip()}[/red]")
            return None
        return result.stdout
    except FileNotFoundError:
        console.print(f"[red]{description}: command not found ({cmd[0]})[/red]")
        return None
    except subprocess.TimeoutExpired:
        console.print(f"[red]{description}: timed out[/red]")
        return None


def _find_node_by_hostname(project: str, hostname: str) -> Node | None:
    """Find an existing node whose slug or name matches the hostname.

    If multiple nodes match across different types, prints a warning
    and returns None so the caller can require an explicit --node flag.
    """
    from infracontext.graph.loader import load_all_nodes

    slug = slugify(hostname)
    matches = [
        node for node in load_all_nodes(project)
        if node.slug == slug or node.name.lower() == hostname.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = ", ".join(m.id for m in matches)
        console.print(f"[yellow]Ambiguous match: hostname '{hostname}' matches {len(matches)} nodes: {ids}[/yellow]")
        console.print("[dim]Use --node to specify which node to enrich.[/dim]")
        return None
    return None


@app.command("sos")
def import_sos(
    path: Annotated[Path, typer.Argument(help="Path to SOS report directory or archive")],
    node: Annotated[str | None, typer.Option("--node", "-n", help="Target existing node ID (type:slug)")] = None,
    node_type: Annotated[str, typer.Option("--type", "-t", help="Node type when creating new")] = "vm",
) -> None:
    """Import system data from an SOS report into a node.

    Extracts hostname, OS, CPU, memory, and health findings from the report.
    Matches to an existing node by hostname, or creates a new one.

    Requires sosq to be installed (pip install sosq).

    Examples:
        ic import sos /data/sosreports/web-01/
        ic import sos report.tar.xz --node vm:web-01
        ic import sos report.tar.xz --type physical_host
    """
    project = require_project()
    paths = ProjectPaths.for_project(project)
    report_path = path.resolve()

    if not report_path.exists():
        console.print(f"[red]Path not found: {report_path}[/red]")
        raise typer.Exit(1)

    # Run sosq health --json to get system info + findings
    console.print(f"[cyan]Analyzing SOS report: {report_path}[/cyan]")
    output = _run_cmd(["sosq", "health", "--json", str(report_path)], "sosq health")
    if output is None:
        raise typer.Exit(1)

    try:
        data = json.loads(output)
    except json.JSONDecodeError as e:
        console.print(f"[red]Failed to parse sosq output: {e}[/red]")
        raise typer.Exit(1) from None

    system = data.get("system", {})
    findings = data.get("findings", [])
    hostname = system.get("hostname", "")

    if not hostname:
        console.print("[red]Could not determine hostname from SOS report.[/red]")
        raise typer.Exit(1)

    console.print(f"  Hostname: {hostname}")
    console.print(f"  OS: {system.get('os', 'unknown')}")
    console.print(f"  Kernel: {system.get('kernel', 'unknown')}")
    console.print(f"  CPUs: {system.get('cpus', '?')}, Memory: {system.get('memory_gb', '?')} GB")

    # Resolve target node
    if node:
        # Explicit node ID
        if ":" not in node:
            console.print("[red]Invalid node ID. Use format: type:slug[/red]")
            raise typer.Exit(1)
        ntype, slug = node.split(":", 1)
        node_file = paths.node_file(ntype, slug)
        existing = read_model(node_file, Node) if node_file.exists() else None
        if existing is None:
            console.print(f"[red]Node '{node}' not found.[/red]")
            raise typer.Exit(1)
    else:
        # Match by hostname
        existing = _find_node_by_hostname(project, hostname)
        if existing:
            console.print(f"[green]Matched existing node: {existing.id}[/green]")

    today = time.strftime("%Y-%m-%d")

    if existing:
        # Update existing node
        slug = existing.slug
        ntype = existing.type
        node_file = paths.node_file(ntype, slug)

        # Merge attributes
        attrs = dict(existing.attributes)
        attrs["sos_report_path"] = str(report_path)
        attrs["sos_collected_at"] = today
        if system.get("os"):
            attrs["os"] = system["os"]
        if system.get("kernel"):
            attrs["kernel"] = system["kernel"]
        if system.get("cpus"):
            attrs["cpu_cores"] = system["cpus"]
        if system.get("memory_gb"):
            attrs["memory_gb"] = system["memory_gb"]

        # Merge learnings — add findings as new learnings
        learnings = list(existing.learnings)
        for f in findings:
            if f["severity"] in ("critical", "warning"):
                learnings.append(Learning(
                    date=today,
                    context=f"SOS report: {f['category']}",
                    finding=f["message"],
                    source="agent",
                ))

        updated = existing.model_copy(update={"attributes": attrs, "learnings": learnings})
        write_model(node_file, updated)
        console.print(f"[green]Updated node {existing.id}[/green]")
    else:
        # Create new node
        try:
            ntype_enum = NodeType(node_type)
        except ValueError:
            console.print(f"[red]Invalid node type '{node_type}'. Valid types: {', '.join(t.value for t in NodeType)}[/red]")
            raise typer.Exit(1) from None

        slug = slugify(hostname)
        node_id = Node.make_id(ntype_enum, slug)

        attrs: dict = {
            "sos_report_path": str(report_path),
            "sos_collected_at": today,
        }
        if system.get("os"):
            attrs["os"] = system["os"]
        if system.get("kernel"):
            attrs["kernel"] = system["kernel"]
        if system.get("cpus"):
            attrs["cpu_cores"] = system["cpus"]
        if system.get("memory_gb"):
            attrs["memory_gb"] = system["memory_gb"]

        learnings = []
        for f in findings:
            if f["severity"] in ("critical", "warning"):
                learnings.append(Learning(
                    date=today,
                    context=f"SOS report: {f['category']}",
                    finding=f["message"],
                    source="agent",
                ))

        new_node = Node(
            id=node_id,
            slug=slug,
            type=ntype_enum,
            name=hostname,
            first_seen=today,
            attributes=attrs,
            learnings=learnings,
        )

        # Duplicate detection (detection only, never auto-attach): the report's
        # hostname may already be a domain of an existing node.
        overlaps = find_duplicate_candidates(load_existing_nodes(paths), domains=[hostname])

        node_file = paths.node_file(ntype_enum, slug)
        paths.node_type_dir(ntype_enum).mkdir(parents=True, exist_ok=True)
        write_model(node_file, new_node)
        console.print(f"[green]Created node {node_id}[/green]")
        for overlap in overlaps:
            console.print(f"[yellow]{overlap_warning(node_id, overlap)}[/yellow]")

    crit = sum(1 for f in findings if f["severity"] == "critical")
    warn = sum(1 for f in findings if f["severity"] == "warning")
    if crit or warn:
        console.print(f"  Imported {crit} critical, {warn} warning findings as learnings")


@app.command("kubectl")
def import_kubectl(
    context: Annotated[str | None, typer.Option("--context", "-c", help="Kubernetes context name")] = None,
    cluster_name: Annotated[str | None, typer.Option("--name", "-n", help="Override cluster name")] = None,
) -> None:
    """Import cluster and nodes from kubectl.

    Creates a k8s_cluster node and k8s_node nodes with relationships.
    Populates attributes with capacity, OS, kubelet version, and addresses.

    Examples:
        ic import kubectl
        ic import kubectl --context prod-cluster
        ic import kubectl --name my-cluster
    """
    project = require_project()
    paths = ProjectPaths.for_project(project)

    # Determine context
    ctx_args = ["--context", context] if context else []

    if not context:
        output = _run_cmd(["kubectl", "config", "current-context"], "kubectl current-context")
        if output is None:
            raise typer.Exit(1)
        context = output.strip()

    console.print(f"[cyan]Importing from kubectl context: {context}[/cyan]")

    # Get nodes
    output = _run_cmd(
        ["kubectl", *ctx_args, "get", "nodes", "-o", "json"],
        "kubectl get nodes",
    )
    if output is None:
        raise typer.Exit(1)

    try:
        nodes_data = json.loads(output)
    except json.JSONDecodeError as e:
        console.print(f"[red]Failed to parse kubectl output: {e}[/red]")
        raise typer.Exit(1) from None

    items = nodes_data.get("items", [])
    console.print(f"  Found {len(items)} node(s)")

    # Snapshot existing nodes once for duplicate detection on creations
    # (detection only -- never auto-attach across source boundaries).
    existing_nodes = load_existing_nodes(paths)

    # Create/update cluster node
    c_name = cluster_name or context
    c_slug = slugify(c_name)
    c_id = Node.make_id(NodeType.KUBERNETES_CLUSTER, c_slug)

    cluster_file = paths.node_file(NodeType.KUBERNETES_CLUSTER, c_slug)
    existing_cluster = read_model(cluster_file, Node) if cluster_file.exists() else None

    today = time.strftime("%Y-%m-%d")
    source_name = "kubectl"
    cluster_attrs: dict = {"kubectl_context": context, "imported_at": today}

    # Get cluster version
    ver_output = _run_cmd(["kubectl", *ctx_args, "version", "-o", "json"], "kubectl version")
    if ver_output:
        try:
            ver = json.loads(ver_output)
            sv = ver.get("serverVersion", {})
            cluster_attrs["k8s_version"] = sv.get("gitVersion", "")
            cluster_attrs["platform"] = sv.get("platform", "")
        except json.JSONDecodeError:
            pass

    cluster_attrs["node_count"] = len(items)

    c_source_id = f"kubectl:{context}:cluster"

    if existing_cluster:
        if existing_cluster.source_id is not None and existing_cluster.source_id != c_source_id:
            console.print(
                f"[red]Cluster node {c_id} is owned by source_id '{existing_cluster.source_id}', "
                f"refusing to overwrite.[/red]"
            )
            raise typer.Exit(1)
        updated = existing_cluster.model_copy(update={
            "source_id": c_source_id,
            "source": source_name,
            "managed_by": source_name,
            "attributes": {**existing_cluster.attributes, **cluster_attrs},
        })
        write_model(cluster_file, updated)
        console.print(f"[green]Updated cluster {c_id}[/green]")
    else:
        cluster_node = Node(
            id=c_id,
            slug=c_slug,
            type=NodeType.KUBERNETES_CLUSTER,
            name=c_name,
            source_id=c_source_id,
            source=source_name,
            managed_by=source_name,
            first_seen=today,
            attributes=cluster_attrs,
        )
        paths.node_type_dir(NodeType.KUBERNETES_CLUSTER).mkdir(parents=True, exist_ok=True)
        write_model(cluster_file, cluster_node)
        console.print(f"[green]Created cluster {c_id}[/green]")

    # Create/update k8s_node nodes
    nodes_created = 0
    nodes_updated = 0
    rel_file_path = paths.relationships_yaml
    rel_file = read_model(rel_file_path, RelationshipFile) or RelationshipFile()
    existing_rels = {(r.source, r.target, r.type) for r in rel_file.relationships}

    for item in items:
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        labels = metadata.get("labels", {})
        node_name = metadata.get("name", "unknown")
        n_slug = slugify(node_name)
        n_id = Node.make_id(NodeType.KUBERNETES_NODE, n_slug)

        # Extract addresses
        ip_addresses = []
        domains = []
        for addr in status.get("addresses", []):
            if addr.get("type") in ("InternalIP", "ExternalIP"):
                ip_addresses.append(addr["address"])
            elif addr.get("type") == "Hostname":
                domains.append(addr["address"])

        # Extract capacity and info
        capacity = status.get("capacity", {})
        node_info = status.get("nodeInfo", {})
        n_attrs: dict = {
            "imported_at": today,
            "kubectl_context": context,
        }
        if capacity.get("cpu"):
            n_attrs["cpu_cores"] = capacity["cpu"]
        if capacity.get("memory"):
            n_attrs["memory"] = capacity["memory"]
        if capacity.get("pods"):
            n_attrs["max_pods"] = capacity["pods"]
        if node_info.get("kubeletVersion"):
            n_attrs["kubelet_version"] = node_info["kubeletVersion"]
        if node_info.get("osImage"):
            n_attrs["os_image"] = node_info["osImage"]
        if node_info.get("containerRuntimeVersion"):
            n_attrs["container_runtime"] = node_info["containerRuntimeVersion"]
        if node_info.get("architecture"):
            n_attrs["arch"] = node_info["architecture"]

        # Determine roles from labels
        roles = []
        for label_key in labels:
            if label_key.startswith("node-role.kubernetes.io/"):
                roles.append(label_key.split("/", 1)[1])
        if roles:
            n_attrs["roles"] = roles

        # Check readiness
        for cond in status.get("conditions", []):
            if cond.get("type") == "Ready":
                n_attrs["ready"] = cond.get("status") == "True"

        n_source_id = f"kubectl:{context}:{node_name}"
        node_file = paths.node_file(NodeType.KUBERNETES_NODE, n_slug)
        existing_node = read_model(node_file, Node) if node_file.exists() else None

        if existing_node:
            if existing_node.source_id is not None and existing_node.source_id != n_source_id:
                console.print(
                    f"  [yellow]Skipping {n_id}: owned by source_id '{existing_node.source_id}'[/yellow]"
                )
                continue
            updated = existing_node.model_copy(update={
                "source_id": n_source_id,
                "source": source_name,
                "managed_by": source_name,
                "ip_addresses": ip_addresses or existing_node.ip_addresses,
                "domains": domains or existing_node.domains,
                "attributes": {**existing_node.attributes, **n_attrs},
            })
            write_model(node_file, updated)
            nodes_updated += 1
        else:
            new_node = Node(
                id=n_id,
                slug=n_slug,
                type=NodeType.KUBERNETES_NODE,
                name=node_name,
                source_id=n_source_id,
                source=source_name,
                managed_by=source_name,
                first_seen=today,
                ip_addresses=ip_addresses,
                domains=domains,
                attributes=n_attrs,
            )
            paths.node_type_dir(NodeType.KUBERNETES_NODE).mkdir(parents=True, exist_ok=True)
            write_model(node_file, new_node)
            nodes_created += 1
            for overlap in find_duplicate_candidates(existing_nodes, ips=ip_addresses, domains=domains):
                console.print(f"  [yellow]{overlap_warning(n_id, overlap)}[/yellow]")

        # Add member_of relationship to cluster
        rel_key = (n_id, c_id, RelationshipType.MEMBER_OF)
        if rel_key not in existing_rels:
            rel_file.relationships.append(Relationship(
                source=n_id,
                target=c_id,
                type=RelationshipType.MEMBER_OF,
                managed_by="kubectl",
            ))
            existing_rels.add(rel_key)

    # Write relationships
    write_model(rel_file_path, rel_file)

    console.print(f"  Nodes created: {nodes_created}")
    console.print(f"  Nodes updated: {nodes_updated}")
    console.print(f"  Relationships: {len(items)} member_of → {c_id}")


class DeviceType(BaseModel):
    """Physical-identity subset of a NetBox devicetype-library YAML file.

    The community devicetype-library (github.com/netbox-community/
    devicetype-library) describes a hardware model: manufacturer, model, and
    rack/physical characteristics, followed by long interface, console, and
    power port template lists. We map only the physical-identity subset into a
    node's ``attributes.hardware`` and ignore the port templates
    (``extra="ignore"``) -- infracontext models running hosts, not port
    inventories.

    Field names and enum values track ``contrib/generated_schema.json`` in the
    netbox-community/devicetype-library repository. Optional fields default to
    ``None`` (not the library's own defaults) so that omitted fields are never
    invented during a fill-only merge -- only what the file actually states is
    imported. Enum-valued fields (airflow, weight_unit, subdevice_role) are
    accepted as free strings for forward compatibility with new enum members.

    Added in ic 0.4.0.
    """

    manufacturer: str
    model: str
    part_number: str | None = None
    u_height: float | None = None
    is_full_depth: bool | None = None
    airflow: str | None = None
    weight: float | None = None
    weight_unit: str | None = None
    subdevice_role: str | None = None

    model_config = {"extra": "ignore"}


@app.command("devicetype")
def import_devicetype(
    path: Annotated[Path, typer.Argument(help="Path to a devicetype-library YAML file")],
    node: Annotated[
        str,
        typer.Option("--node", "-n", help="Target node (type:slug, @alias:type:slug, or fuzzy query)"),
    ],
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite existing hardware values instead of only filling gaps"),
    ] = False,
) -> None:
    """Import hardware attributes from a NetBox devicetype-library YAML file.

    Parses the physical-identity subset of a community devicetype file
    (manufacturer, model, part_number, u_height, is_full_depth, airflow,
    weight/weight_unit, subdevice_role) and merges it into the target node's
    ``attributes.hardware``. Interface, console, and power port template lists
    in the file are ignored by design -- infracontext models running hosts,
    not port inventories.

    The merge is fill-only: an existing hardware value always wins, and only
    empty/absent fields are filled. Pass --force to overwrite existing values.
    Added in ic 0.4.0.

    Examples:
        ic import devicetype cisco-catalyst-9300-48p.yaml --node network_device:sw-01
        ic import devicetype dell-poweredge-r750.yaml -n host-01 --force
    """
    devicetype_path = path.expanduser()
    if not devicetype_path.exists():
        console.print(f"[red]File not found: {devicetype_path}[/red]")
        raise typer.Exit(1)

    try:
        data = read_yaml(devicetype_path)
    except StorageError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None

    # Reject anything that isn't recognizably a devicetype file. The library's
    # defining fields are manufacturer + model; without both this is some other
    # YAML and merging it would pollute the node's hardware namespace.
    if not data.get("manufacturer") or not data.get("model"):
        console.print(f"[red]Not a devicetype-library YAML file: {devicetype_path}[/red]")
        console.print(
            "[dim]A devicetype file needs at least 'manufacturer' and 'model' fields. "
            "See github.com/netbox-community/devicetype-library.[/dim]"
        )
        raise typer.Exit(1)

    try:
        device_type = DeviceType.model_validate(data)
    except ValidationError as e:
        console.print(f"[red]Invalid devicetype file {devicetype_path}:[/red]")
        for err in e.errors():
            loc = ".".join(str(part) for part in err["loc"])
            console.print(f"  [red]{loc}: {err['msg']}[/red]")
        raise typer.Exit(1) from None

    target = resolve_node_or_exit(node, require_writable=True)
    if ":" not in target.node_id:
        console.print("[red]Invalid node ID. Use format: type:slug[/red]")
        raise typer.Exit(1)
    ntype, slug = target.node_id.split(":", 1)
    try:
        node_file = target.paths.node_file(ntype, slug)
    except ValueError as e:
        console.print(f"[red]Invalid node ID '{target.node_id}': {e}[/red]")
        raise typer.Exit(1) from None
    existing = read_model(node_file, Node) if node_file.exists() else None
    if existing is None:
        console.print(f"[red]Node '{target.node_id}' not found.[/red]")
        raise typer.Exit(1)

    console.print(f"[cyan]Importing devicetype into {existing.id}[/cyan]")
    console.print(f"  {device_type.manufacturer} {device_type.model}")

    # exclude_none: only the fields the file actually states are candidates.
    values = device_type.model_dump(exclude_none=True)
    hardware = dict(existing.attributes.get("hardware") or {})

    filled: list[tuple[str, object]] = []
    overwritten: list[tuple[str, object, object]] = []
    skipped: list[tuple[str, object]] = []

    # Iterate model fields in declaration order for deterministic reporting.
    for key in DeviceType.model_fields:
        if key not in values:
            continue
        new_val = values[key]
        current = hardware.get(key)
        if current in (None, ""):
            hardware[key] = new_val
            filled.append((key, new_val))
        elif force:
            hardware[key] = new_val
            overwritten.append((key, current, new_val))
        else:
            skipped.append((key, current))

    if filled or overwritten:
        attrs = dict(existing.attributes)
        attrs["hardware"] = hardware
        updated = existing.model_copy(update={"attributes": attrs})
        write_model(node_file, updated)

    for key, val in filled:
        console.print(f"  [green]filled[/green] hardware.{key} = {val}")
    for key, old, new in overwritten:
        console.print(f"  [yellow]overwrote[/yellow] hardware.{key}: {old} → {new}")
    for key, val in skipped:
        console.print(f"  [dim]skipped[/dim] hardware.{key} (keeping {val}; use --force to overwrite)")

    if filled or overwritten:
        console.print(f"[green]Updated {existing.id}[/green]")
    else:
        console.print("[yellow]No hardware fields imported (all present; use --force to overwrite).[/yellow]")

    console.print("[dim]Interface/console/power port template lists are ignored by design.[/dim]")
