"""CheckMK source plugin.

Imports hosts from a CheckMK monitoring site via Livestatus. The v1 transport
is SSH: the query is piped through ``unixcat`` to the site's live socket on
the monitoring host itself — read-only, and no API credentials or automation
user required. Monitoring is usually the most complete host inventory an
environment has, which makes it a natural first sync source.

Config (in the source YAML):

.. code-block:: yaml

    type: checkmk
    ssh_alias: monitor          # SSH alias of the CheckMK server
    site: mysite                # OMD site name
    exclude_patterns:           # host-name regexes to skip
      - "^[0-9a-f]{12}$"        # default: docker piggyback container IDs
    strip_domain_suffixes:      # optional, e.g. [".example.com"] — shortens slugs
      - ".example.com"
    default_node_type: vm
    type_patterns:              # optional overrides, first match wins
      network_device: ["^switch-", "^fw-"]

Node type inference order: ``type_patterns`` (explicit config), then the
CheckMK ``cmk/device_type`` label, then ``default_node_type``.
"""

import json
import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from infracontext.models.node import Node, NodeType, Observability, slugify
from infracontext.paths import EnvironmentPaths, ProjectPaths
from infracontext.sources.base import (
    NodeChange,
    PlannedNodeWrite,
    SourcePlugin,
    SyncResult,
    SyncStatus,
    apply_node_writes,
    merge_synced_node,
    record_sync_run,
    rewrite_reference_ids,
)
from infracontext.sources.dedup import find_duplicate_candidates, load_existing_nodes, overlap_warning
from infracontext.sources.registry import register_plugin
from infracontext.sources.ssh_config import is_ip_address
from infracontext.storage import read_model, read_yaml, write_yaml

log = logging.getLogger(__name__)

# OMD site names: conservative charset because the value lands in a remote
# shell command line (defense in depth on top of shlex quoting).
_SITE_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Docker piggyback hosts show up in CheckMK as bare 12-hex-digit container
# IDs. They churn on every redeploy and make poor nodes; excluded by default.
DEFAULT_EXCLUDE_PATTERNS = [r"^[0-9a-f]{12}$"]

# cmk/device_type label value -> NodeType. Anything unmapped falls back to
# the source's default_node_type.
_DEVICE_TYPE_MAP: dict[str, NodeType] = {
    "vm": NodeType.VM,
    "container": NodeType.OCI_CONTAINER,
    "switch": NodeType.NETWORK_DEVICE,
    "router": NodeType.NETWORK_DEVICE,
    "firewall": NodeType.NETWORK_DEVICE,
    "appliance": NodeType.NETWORK_DEVICE,
    "bmc": NodeType.NETWORK_DEVICE,
}

_HOSTS_QUERY = "GET hosts\nColumns: name address alias labels groups\nOutputFormat: json\n\n"
_STATUS_QUERY = "GET status\nColumns: program_version\nOutputFormat: json\n\n"


class LivestatusError(RuntimeError):
    """A Livestatus query over SSH failed."""


@dataclass
class CheckMKHost:
    """One host row from the Livestatus ``hosts`` table."""

    name: str
    address: str
    alias: str
    labels: dict[str, str] = field(default_factory=dict)
    groups: list[str] = field(default_factory=list)


@dataclass
class SyncStats:
    """Statistics from a CheckMK sync."""

    nodes_created: int = 0
    nodes_updated: int = 0
    nodes_unchanged: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@register_plugin
class CheckMKSource(SourcePlugin):
    """CheckMK (Livestatus over SSH) infrastructure source plugin."""

    source_type = "checkmk"

    def validate_config(self, config: dict) -> list[str]:
        """Validate CheckMK source configuration."""
        errors = []
        if not config.get("ssh_alias"):
            errors.append("'ssh_alias' is required (SSH alias of the CheckMK server)")
        site = config.get("site", "")
        if not site:
            errors.append("'site' is required (OMD site name)")
        elif not _SITE_RE.match(site):
            errors.append(f"'site' contains invalid characters: {site!r}")
        default_type = config.get("default_node_type", "vm")
        try:
            NodeType(default_type)
        except ValueError:
            errors.append(f"'default_node_type' is not a valid node type: {default_type!r}")
        for pattern in self._exclude_patterns(config):
            try:
                re.compile(pattern)
            except re.error as e:
                errors.append(f"invalid exclude_patterns regex {pattern!r}: {e}")
        return errors

    async def test_connection(self, config: dict) -> tuple[bool, str]:
        """Query the site's Livestatus status table over SSH."""
        errors = self.validate_config(config)
        if errors:
            return False, "; ".join(errors)
        try:
            rows = self._run_livestatus(config, _STATUS_QUERY)
        except LivestatusError as e:
            return False, str(e)
        version = rows[0][0] if rows and rows[0] else "unknown"
        return True, f"Connected to CheckMK site '{config['site']}' (version {version})"

    # ── transport ─────────────────────────────────────────────────

    def _run_livestatus(self, config: dict, query: str) -> list:
        """Run a Livestatus query on the monitoring host via SSH and parse JSON.

        The query travels on stdin; ``unixcat`` connects it to the site's live
        socket. Read-only by construction — Livestatus commands (COMMAND ...)
        are never issued here.
        """
        site = config["site"]
        socket_path = config.get("livestatus_socket") or f"/omd/sites/{site}/tmp/run/live"
        unixcat = config.get("unixcat_path") or f"/omd/sites/{site}/bin/unixcat"
        timeout = int(config.get("ssh_timeout", 30))
        remote_cmd = f"{shlex.quote(unixcat)} {shlex.quote(socket_path)}"
        cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={timeout}",
            config["ssh_alias"],
            remote_cmd,
        ]
        try:
            proc = subprocess.run(
                cmd, input=query, capture_output=True, text=True, timeout=timeout + 30
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LivestatusError(f"Livestatus query via '{config['ssh_alias']}' failed: {e}") from e
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip().splitlines()
            raise LivestatusError(
                f"Livestatus query via '{config['ssh_alias']}' failed "
                f"(exit {proc.returncode}): {detail[-1] if detail else 'no stderr'}"
            )
        if not proc.stdout.strip():
            return []
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LivestatusError(f"Livestatus returned invalid JSON: {e}") from e

    # ── host parsing / classification ─────────────────────────────

    @staticmethod
    def _exclude_patterns(config: dict) -> list[str]:
        patterns = config.get("exclude_patterns")
        return DEFAULT_EXCLUDE_PATTERNS if patterns is None else list(patterns)

    def _fetch_hosts(self, config: dict) -> list[CheckMKHost]:
        hosts = []
        for row in self._run_livestatus(config, _HOSTS_QUERY):
            try:
                name, address, alias, labels, groups = row
            except (TypeError, ValueError):
                raise LivestatusError(f"Unexpected hosts row shape: {row!r}") from None
            hosts.append(
                CheckMKHost(
                    name=str(name),
                    address=str(address or ""),
                    alias=str(alias or ""),
                    labels=dict(labels or {}),
                    groups=list(groups or []),
                )
            )
        return hosts

    def _determine_node_type(self, host: CheckMKHost, config: dict) -> NodeType:
        """type_patterns (explicit config) > cmk/device_type label > default."""
        for node_type_str, patterns in (config.get("type_patterns") or {}).items():
            for pattern in patterns:
                if re.match(pattern, host.name):
                    try:
                        return NodeType(node_type_str)
                    except ValueError:
                        log.warning("type_patterns names unknown node type %r", node_type_str)
        device_type = host.labels.get("cmk/device_type", "").lower()
        if device_type in _DEVICE_TYPE_MAP:
            return _DEVICE_TYPE_MAP[device_type]
        try:
            return NodeType(config.get("default_node_type", "vm"))
        except ValueError:
            return NodeType.VM

    @staticmethod
    def _strip_suffixes(name: str, config: dict) -> str:
        for suffix in config.get("strip_domain_suffixes") or []:
            if name.endswith(suffix) and len(name) > len(suffix):
                return name[: -len(suffix)]
        return name

    def _build_node(self, host: CheckMKHost, config: dict, source_name: str) -> Node:
        node_type = self._determine_node_type(host, config)
        slug = slugify(self._strip_suffixes(host.name, config))
        ip_addresses: list[str] = []
        domains: list[str] = []
        if host.address:
            if is_ip_address(host.address):
                ip_addresses.append(host.address)
            else:
                domains.append(host.address)
        if (
            "." in host.name
            and host.name != host.address
            and not is_ip_address(host.name)
            and host.name not in domains
        ):
            domains.append(host.name)

        checkmk_attrs: dict = {"address": host.address}
        if host.alias and host.alias != host.name:
            checkmk_attrs["alias"] = host.alias
        if host.groups:
            checkmk_attrs["groups"] = host.groups
        labels = {k.removeprefix("cmk/"): v for k, v in host.labels.items() if k != "cmk/site"}
        if labels:
            checkmk_attrs["labels"] = labels

        return Node(
            id=Node.make_id(node_type, slug),
            slug=slug,
            type=node_type,
            name=host.name,
            source_id=f"checkmk:{source_name}:{host.name}",
            source=source_name,
            managed_by=source_name,
            ip_addresses=ip_addresses,
            domains=domains,
            attributes={"checkmk": checkmk_attrs},
            observability=[
                Observability(type="checkmk", host_name=host.name),
            ],
        )

    @staticmethod
    def _build_source_id_index(paths: ProjectPaths) -> dict[str, tuple[Node, Path]]:
        """Map source_id -> (node, file) for every readable node in the project.

        Used to detect relocations (type/slug changes for the same CheckMK
        host). Broken files are skipped — they must not abort the sync.
        """
        index: dict[str, tuple[Node, Path]] = {}
        if not paths.nodes_dir.exists():
            return index
        for type_dir in sorted(paths.nodes_dir.iterdir()):
            if not type_dir.is_dir():
                continue
            for node_file in sorted(type_dir.glob("*.yaml")):
                try:
                    node = read_model(node_file, Node)
                except Exception:
                    continue
                if node.source_id:
                    index[node.source_id] = (node, node_file)
        return index

    # ── sync ──────────────────────────────────────────────────────

    def sync(self, project_slug: str, source_name: str) -> SyncResult:
        """Synchronize hosts from CheckMK Livestatus into local YAML files.

        Mirrors the other sources: writes are planned first and applied only
        when the run is a non-empty success (sync guard), and every run
        appends a record under ``.infracontext/runs/``.
        """
        environment = EnvironmentPaths.current()
        paths = ProjectPaths.for_project(project_slug, environment)
        try:
            source_file = paths.source_file(source_name)
        except ValueError as e:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=f"Invalid source name '{source_name}': {e}",
            )
        if not source_file.exists():
            return SyncResult(status=SyncStatus.FAILED, message=f"Source '{source_name}' not found")

        config = read_yaml(source_file)
        start_time = time.monotonic()
        stats = SyncStats()

        config_errors = self.validate_config(config)
        if config_errors:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="Invalid source config: " + "; ".join(config_errors),
                errors=config_errors,
            )

        try:
            hosts = self._fetch_hosts(config)
            today = time.strftime("%Y-%m-%d", time.gmtime())
            excludes = [re.compile(p) for p in self._exclude_patterns(config)]

            existing_nodes = load_existing_nodes(paths)
            source_id_index = self._build_source_id_index(paths)
            plans: list[PlannedNodeWrite] = []
            planned_paths: dict[Path, str] = {}
            id_renames: dict[str, str] = {}
            for host in hosts:
                if any(rx.match(host.name) for rx in excludes):
                    stats.skipped += 1
                    continue
                try:
                    node = self._build_node(host, config, source_name)
                    node_file = paths.node_file(node.type, node.slug)
                    if node_file in planned_paths:
                        stats.errors.append(
                            f"Slug collision within sync: hosts '{planned_paths[node_file]}' and "
                            f"'{host.name}' both map to {node.id} — rename one or adjust "
                            "strip_domain_suffixes/type_patterns."
                        )
                        continue
                    planned_paths[node_file] = host.name

                    # Relocation: the same host (source_id) may land at a new
                    # type/slug when its cmk/device_type label appears, or when
                    # type_patterns / strip_domain_suffixes change. Reuse that
                    # node and delete the old file — otherwise every
                    # reclassification would strand a stale duplicate.
                    existing: Node | None = None
                    old_file_to_delete = None
                    prior = source_id_index.get(node.source_id or "")
                    prior_elsewhere = prior is not None and prior[1] != node_file
                    if node_file.exists():
                        at_dest = read_model(node_file, Node)
                        if at_dest.source_id is not None and at_dest.source_id != node.source_id:
                            stats.errors.append(
                                f"Slug collision for '{node.slug}' ({node.type}): existing node "
                                f"'{at_dest.id}' is bound to source_id '{at_dest.source_id}', "
                                "refusing to overwrite."
                            )
                            continue
                        if prior_elsewhere and prior is not None:
                            if at_dest.source_id is None:
                                # The relocation target is a user-owned manual
                                # node AND this host already has a node
                                # elsewhere — collapsing them would destroy
                                # one of the two. Leave both; the operator
                                # decides with `ic describe node consolidate`.
                                stats.errors.append(
                                    f"Relocation of {prior[0].id} to {node.id} blocked: the target "
                                    "is a manually created node — merge them with "
                                    f"'ic describe node consolidate {node.id} {prior[0].id}'."
                                )
                                continue
                            # Destination already carries this source_id: the
                            # file elsewhere is a stale duplicate — clean it up.
                            existing = at_dest
                            old_file_to_delete = prior[1]
                            id_renames[prior[0].id] = node.id
                            stats.warnings.append(
                                f"Removed stale duplicate {prior[0].id} of {node.id} "
                                f"(source_id {node.source_id})"
                            )
                        else:
                            # Same-path update; a manual node (source_id None)
                            # at its own slug is adopted/enriched, matching
                            # the other sync sources.
                            existing = at_dest
                    elif prior_elsewhere and prior is not None:
                        existing = prior[0]
                        old_file_to_delete = prior[1]
                        id_renames[prior[0].id] = node.id
                        stats.warnings.append(
                            f"Relocated: {prior[0].id} → {node.id} (source_id {node.source_id})"
                        )

                    if existing:
                        # CheckMK doesn't manage SSH connectivity — keep the
                        # operator's ssh_alias and other manual fields.
                        node = merge_synced_node(node, existing, preserve_ssh_alias=True)
                        if not any(o.type == "checkmk" for o in node.observability):
                            node = node.model_copy(
                                update={
                                    "observability": [
                                        *node.observability,
                                        Observability(type="checkmk", host_name=host.name),
                                    ]
                                }
                            )
                        change = (
                            NodeChange.CONFIRMED_UNCHANGED if node == existing else NodeChange.UPDATED
                        )
                        if old_file_to_delete is not None and change is NodeChange.CONFIRMED_UNCHANGED:
                            # Content-equal, but the on-disk state still
                            # changes (a stale file gets deleted) — and
                            # apply_node_writes skips CONFIRMED_UNCHANGED
                            # plans entirely, deletion included.
                            change = NodeChange.UPDATED
                    else:
                        node = node.model_copy(update={"first_seen": today})
                        change = NodeChange.CREATED
                        for overlap in find_duplicate_candidates(
                            existing_nodes,
                            ips=node.ip_addresses,
                            domains=node.domains,
                            ssh_alias=node.ssh_alias,
                        ):
                            stats.warnings.append(overlap_warning(node.id, overlap))

                    plans.append(
                        PlannedNodeWrite(
                            node=node,
                            node_file=node_file,
                            change=change,
                            old_file_to_delete=old_file_to_delete,
                        )
                    )
                except Exception as e:
                    stats.errors.append(f"Error processing host '{host.name}': {e}")

            # Sync guard: only a non-empty, error-free run may touch node files.
            status = SyncStatus.SUCCESS if not stats.errors else SyncStatus.PARTIAL
            guarded = status is not SyncStatus.SUCCESS or not plans
            if not guarded:
                apply_node_writes(paths, plans)
                rewrite_reference_ids(paths, id_renames, stats.warnings)
                for plan in plans:
                    if plan.change is NodeChange.CREATED:
                        stats.nodes_created += 1
                    elif plan.change is NodeChange.UPDATED:
                        stats.nodes_updated += 1
                    else:
                        stats.nodes_unchanged += 1

            duration_ms = int((time.monotonic() - start_time) * 1000)
            config["last_sync_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            config["last_sync_status"] = str(status)
            config["last_sync_stats"] = {
                "nodes_created": stats.nodes_created,
                "nodes_updated": stats.nodes_updated,
                "nodes_unchanged": stats.nodes_unchanged,
                "skipped": stats.skipped,
                "errors": stats.errors,
                "warnings": stats.warnings,
            }
            write_yaml(source_file, config)

            record_sync_run(environment, project_slug, source_name, status, plans)

            if stats.errors:
                message = (
                    f"Sync found {len(stats.errors)} error(s); no node files were written (sync guard)"
                )
            elif not plans:
                message = (
                    "CheckMK reported 0 importable hosts; no node files were written (empty-sync guard)"
                )
            else:
                message = f"Synced {stats.nodes_created + stats.nodes_updated} nodes from {len(hosts)} CheckMK hosts"
                if stats.nodes_unchanged:
                    message += f" ({stats.nodes_unchanged} unchanged)"
                if stats.skipped:
                    message += f", {stats.skipped} excluded"

            return SyncResult(
                status=status,
                message=message,
                nodes_created=stats.nodes_created,
                nodes_updated=stats.nodes_updated,
                nodes_unchanged=stats.nodes_unchanged,
                errors=stats.errors,
                warnings=stats.warnings,
                duration_ms=duration_ms,
            )

        except LivestatusError as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            config["last_sync_status"] = "failed"
            config["last_sync_message"] = str(e)
            write_yaml(source_file, config)
            record_sync_run(environment, project_slug, source_name, SyncStatus.FAILED, [])
            return SyncResult(
                status=SyncStatus.FAILED, message=str(e), errors=[str(e)], duration_ms=duration_ms
            )
