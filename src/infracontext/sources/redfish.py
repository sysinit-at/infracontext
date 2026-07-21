"""Redfish source plugin (added in ic 0.4.0).

Redfish is a RESTful, JSON-over-HTTPS management standard (DMTF). Every BMC
(iDRAC, iLO, XClarity, OpenBMC, ...) exposes the same resource tree under
``/redfish/v1/`` -- so no vendor SDK or extra dependency is needed: this
plugin speaks plain ``requests`` and reuses the shared HTTP helpers in
:mod:`infracontext.query.base`.

Config (in the source YAML):

.. code-block:: yaml

    type: redfish
    endpoints:                       # one BMC per entry
      - url: https://bmc-web-01.example.com
        name: web-01-bmc             # optional; otherwise derived from the
                                     # system HostName or the URL host
      - url: https://10.0.0.51
    credential: redfish:prod         # ic keychain account holding "user:password"
    verify_ssl: true                 # default true; tls_skip_verify forces off

For each endpoint the plugin walks the service root, the Systems collection
(Manufacturer/Model/SerialNumber/SKU/UUID/BiosVersion), and the Managers (BMC
firmware). It records one ``network_device`` node -- the BMC -- per endpoint.
Live power draw is *not* inventory: it is read on demand by the Redfish query
plugin (``ic query redfish -t power``), never persisted into the node file.

The BMC's ``manages`` edge to the host it controls is inferred by matching the
system SerialNumber against existing nodes' ``attributes.hardware.serial``
(case-insensitive, exact). A single match yields the edge; zero or multiple
matches only warn (with the candidates) and never guess.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from infracontext.models.node import Node, NodeType, Observability, slugify
from infracontext.models.relationship import Relationship, RelationshipFile, RelationshipType
from infracontext.paths import EnvironmentPaths, ProjectPaths
from infracontext.query.base import describe_http_error, resolve_basic_auth, resolve_verify_ssl
from infracontext.sources.base import (
    NodeChange,
    PlannedNodeWrite,
    SourcePlugin,
    SyncResult,
    SyncStatus,
    apply_node_writes,
    ensure_source_observability,
    merge_synced_node,
    record_sync_run,
    rewrite_reference_ids,
)
from infracontext.sources.dedup import find_duplicate_candidates, load_existing_nodes, overlap_warning
from infracontext.sources.registry import register_plugin
from infracontext.sources.ssh_config import is_ip_address
from infracontext.storage import read_model, read_yaml, write_model, write_yaml

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

# Generous connect/read timeout for a sync (walks several resources per BMC).
_SYNC_TIMEOUT: tuple[float, float] = (5.0, 20.0)


class RedfishError(RuntimeError):
    """A Redfish HTTP request failed (transport, auth, or malformed body)."""


# ── low-level client (shared with the query plugin) ────────────────


class RedfishClient:
    """Minimal Redfish GET client over a shared ``requests.Session``.

    A caller may inject an existing session (the query plugin passes its
    pooled :attr:`QueryPlugin.session`); otherwise one is created lazily so
    the module stays importable without ``requests`` loaded.
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth: tuple[str, str] | None = None,
        verify: bool = True,
        timeout: tuple[float, float] = _SYNC_TIMEOUT,
        session: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.verify = verify
        self.timeout = timeout
        self._session = session

    @property
    def session(self) -> Any:
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _url(self, path: str) -> str:
        """Resolve a Redfish path/@odata.id against the base URL."""
        if path.startswith(("http://", "https://")):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def get(self, path: str) -> dict:
        """GET a Redfish resource and return the parsed JSON object.

        Raises :class:`RedfishError` on any transport error, a ``>= 400``
        status (shaped via :func:`describe_http_error`), or a body that is
        not a JSON object.
        """
        import requests

        url = self._url(path)
        try:
            response = self.session.get(
                url,
                auth=self.auth,
                verify=self.verify,
                timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
        except requests.RequestException as e:
            raise RedfishError(f"Request to {url} failed: {e}") from e
        if response.status_code >= 400:
            raise RedfishError(describe_http_error(url, response))
        try:
            data = response.json()
        except ValueError as e:
            raise RedfishError(f"Invalid JSON from {url}") from e
        if not isinstance(data, dict):
            raise RedfishError(f"Unexpected JSON shape from {url} (expected object)")
        return data


def odata_id(link: Any) -> str | None:
    """Extract an ``@odata.id`` from a Redfish link object (or None)."""
    if isinstance(link, dict):
        value = link.get("@odata.id")
        if isinstance(value, str) and value:
            return value
    return None


def collection_member_links(collection: dict) -> list[str]:
    """Return the ``@odata.id`` of every member in a Redfish collection."""
    links: list[str] = []
    for member in collection.get("Members") or []:
        link = odata_id(member)
        if link:
            links.append(link)
    return links


def parse_system(system: dict) -> dict:
    """Pull the identity fields we care about from a ComputerSystem."""
    status = system.get("Status") or {}
    return {
        "manufacturer": system.get("Manufacturer"),
        "model": system.get("Model"),
        "serial": system.get("SerialNumber"),
        "sku": system.get("SKU"),
        "uuid": system.get("UUID"),
        "bios_version": system.get("BiosVersion"),
        "hostname": system.get("HostName"),
        "health": status.get("Health"),
        "state": status.get("State"),
    }


def read_power(client: RedfishClient, chassis: dict) -> float | None:
    """Read live consumed power (watts) for a chassis, or None if unavailable.

    Prefers the legacy ``Power`` resource (``PowerControl[].PowerConsumedWatts``,
    summed across controls). When only the modern ``PowerSubsystem`` is present,
    the consumed reading lives on the chassis ``EnvironmentMetrics``
    (``PowerWatts.Reading``); that fallback is used instead.
    """
    power_link = odata_id(chassis.get("Power"))
    if power_link:
        power = client.get(power_link)
        total = 0.0
        found = False
        for control in power.get("PowerControl") or []:
            watts = control.get("PowerConsumedWatts") if isinstance(control, dict) else None
            if isinstance(watts, (int, float)) and not isinstance(watts, bool):
                total += float(watts)
                found = True
        if found:
            return total
    if chassis.get("PowerSubsystem"):
        env_link = odata_id(chassis.get("EnvironmentMetrics"))
        if env_link:
            env = client.get(env_link)
            reading = (env.get("PowerWatts") or {}).get("Reading")
            if isinstance(reading, (int, float)) and not isinstance(reading, bool):
                return float(reading)
    return None


def collect_inventory(client: RedfishClient) -> dict:
    """Walk one BMC and collect the (inventory) data needed to build its node.

    Returns ``{"systems": [parsed system, ...], "bmc_firmware": str | None}``.
    Live power draw is deliberately excluded -- it is a query-time metric, not
    inventory (see :func:`read_power` / the Redfish query plugin), so walking it
    here would only add HTTP round-trips for a value we would not persist.
    """
    root = client.get("/redfish/v1/")
    result: dict = {"systems": [], "bmc_firmware": None}

    systems_link = odata_id(root.get("Systems"))
    if systems_link:
        for link in collection_member_links(client.get(systems_link)):
            result["systems"].append(parse_system(client.get(link)))

    managers_link = odata_id(root.get("Managers"))
    if managers_link:
        for link in collection_member_links(client.get(managers_link)):
            firmware = client.get(link).get("FirmwareVersion")
            if firmware:
                result["bmc_firmware"] = firmware
                break

    return result


# ── small pure helpers ─────────────────────────────────────────────


def _endpoint_host(url: str) -> str:
    """Host portion of an endpoint URL (no scheme, no port)."""
    parsed = urlparse(url)
    return parsed.hostname or url


def _primary_serial(inventory: dict) -> str | None:
    """SerialNumber of the first system reporting one (trimmed), else None."""
    for system in inventory.get("systems") or []:
        serial = system.get("serial")
        if isinstance(serial, str) and serial.strip():
            return serial.strip()
    return None


def _node_serial(node: Node) -> str | None:
    """A node's ``attributes.hardware.serial`` (trimmed), or None."""
    hardware = node.attributes.get("hardware")
    if isinstance(hardware, dict):
        serial = hardware.get("serial")
        if isinstance(serial, str) and serial.strip():
            return serial.strip()
    return None


@dataclass
class SyncStats:
    """Statistics from a Redfish sync."""

    nodes_created: int = 0
    nodes_updated: int = 0
    nodes_unchanged: int = 0
    relationships_created: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@register_plugin
class RedfishSource(SourcePlugin):
    """Redfish (REST/JSON over HTTPS) infrastructure source plugin."""

    source_type = "redfish"

    # Fakeable in tests: when set, endpoints are fetched over this session
    # instead of a fresh one (mirrors QueryPlugin's ``_session``).
    _session: Any = None

    def validate_config(self, config: dict) -> list[str]:
        """Validate Redfish source configuration."""
        errors: list[str] = []
        endpoints = config.get("endpoints")
        if not endpoints:
            errors.append("'endpoints' is required (list of {url, name?})")
        elif not isinstance(endpoints, list):
            errors.append("'endpoints' must be a list of {url, name?} mappings")
        else:
            for i, endpoint in enumerate(endpoints):
                if not isinstance(endpoint, dict) or not endpoint.get("url"):
                    errors.append(f"endpoints[{i}] must be a mapping with a 'url'")
        return errors

    async def test_connection(self, config: dict) -> tuple[bool, str]:
        """Reach each endpoint's Redfish service root."""
        errors = self.validate_config(config)
        if errors:
            return False, "; ".join(errors)
        auth = resolve_basic_auth(config)
        verify = resolve_verify_ssl(config)
        reached: list[str] = []
        failed: list[str] = []
        for endpoint in config["endpoints"]:
            url = str(endpoint["url"]).rstrip("/")
            client = self._client(url, auth, verify)
            try:
                root = client.get("/redfish/v1/")
                reached.append(f"{_endpoint_host(url)} (Redfish {root.get('RedfishVersion', '?')})")
            except RedfishError as e:
                failed.append(f"{_endpoint_host(url)}: {e}")
        if reached and not failed:
            return True, f"Reached {len(reached)} endpoint(s): {', '.join(reached)}"
        if reached:
            return True, (
                f"Reached {len(reached)}/{len(reached) + len(failed)} endpoints; "
                f"failures: {'; '.join(failed)}"
            )
        return False, f"No Redfish endpoints reachable: {'; '.join(failed)}"

    # ── transport ─────────────────────────────────────────────────

    def _client(self, url: str, auth: tuple[str, str] | None, verify: bool) -> RedfishClient:
        return RedfishClient(url, auth=auth, verify=verify, timeout=_SYNC_TIMEOUT, session=self._session)

    def _fetch_endpoint(self, url: str, auth: tuple[str, str] | None, verify: bool) -> dict:
        return collect_inventory(self._client(url, auth, verify))

    # ── node construction / planning ──────────────────────────────

    def _build_node(self, endpoint: dict, url: str, inventory: dict, source_name: str) -> Node:
        host = _endpoint_host(url)
        systems = inventory.get("systems") or []
        primary = systems[0] if systems else {}

        label = endpoint.get("name") or primary.get("hostname") or host
        slug = slugify(str(label))

        ip_addresses: list[str] = []
        domains: list[str] = []
        if host:
            if is_ip_address(host):
                ip_addresses.append(host)
            else:
                domains.append(host)

        hardware = {
            key: value
            for key, value in (
                ("manufacturer", primary.get("manufacturer")),
                ("model", primary.get("model")),
                ("serial", primary.get("serial")),
                ("sku", primary.get("sku")),
                ("uuid", primary.get("uuid")),
            )
            if value is not None
        }
        # Live power draw is deliberately NOT persisted: a BMC reading
        # fluctuates continuously, so storing it would rewrite the node file on
        # every resync (no-churn violation). It is a query-time metric -- the
        # Redfish query plugin exposes it live via `ic query redfish -t power`.
        redfish_attrs = {
            key: value
            for key, value in (
                ("bios_version", primary.get("bios_version")),
                ("bmc_firmware", inventory.get("bmc_firmware")),
            )
            if value is not None
        }
        attributes: dict = {}
        if hardware:
            attributes["hardware"] = hardware
        if redfish_attrs:
            attributes["redfish"] = redfish_attrs

        return Node(
            id=Node.make_id(NodeType.NETWORK_DEVICE, slug),
            slug=slug,
            type=NodeType.NETWORK_DEVICE,
            name=str(label),
            source_id=f"redfish:{source_name}:{host}",
            source=source_name,
            managed_by=source_name,
            ip_addresses=ip_addresses,
            domains=domains,
            attributes=attributes,
            observability=[Observability(type="redfish", instance=url, source=source_name)],
        )

    @staticmethod
    def _build_source_id_index(paths: ProjectPaths) -> dict[str, tuple[Node, Path]]:
        """Map source_id -> (node, file) for every readable node (broken skipped)."""
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
                if node and node.source_id:
                    index[node.source_id] = (node, node_file)
        return index

    def _plan_endpoint_node(
        self,
        endpoint: dict,
        url: str,
        inventory: dict,
        source_name: str,
        paths: ProjectPaths,
        existing_nodes: list[Node],
        source_id_index: dict[str, tuple[Node, Path]],
        planned_paths: dict[Path, str],
        today: str,
        stats: SyncStats,
        id_renames: dict[str, str],
    ) -> PlannedNodeWrite | None:
        node = self._build_node(endpoint, url, inventory, source_name)
        node_file = paths.node_file(node.type, node.slug)
        if node_file in planned_paths:
            stats.errors.append(
                f"Slug collision within sync: endpoints '{planned_paths[node_file]}' and "
                f"'{url}' both map to {node.id} -- set a distinct 'name' on one."
            )
            return None
        planned_paths[node_file] = url

        existing: Node | None = None
        old_file_to_delete: Path | None = None
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
                return None
            existing = at_dest
            if prior_elsewhere and prior is not None:
                # A second file for this BMC lingers at the old slug -- merge
                # into the current slot and remove the stale duplicate.
                old_file_to_delete = prior[1]
                id_renames[prior[0].id] = node.id
                stats.warnings.append(f"Removed stale duplicate {prior[0].id} of {node.id}")
        elif prior_elsewhere and prior is not None:
            # Endpoint 'name' changed -> the BMC moved to a new slug. Reuse the
            # existing node and drop the old file so no orphan is left behind.
            existing = prior[0]
            old_file_to_delete = prior[1]
            id_renames[prior[0].id] = node.id
            stats.warnings.append(f"Renamed: {prior[0].id} -> {node.id} (source_id {node.source_id})")

        if existing is not None:
            node = merge_synced_node(node, existing, preserve_ssh_alias=True)
            # merge_synced_node keeps observability from the existing node; the
            # source's own entry must still track the configured URL (a changed
            # scheme/port would otherwise leave ic query on the old endpoint).
            node = ensure_source_observability(
                node, Observability(type="redfish", instance=url, source=source_name)
            )
            change = NodeChange.CONFIRMED_UNCHANGED if node == existing else NodeChange.UPDATED
            if old_file_to_delete is not None and change is NodeChange.CONFIRMED_UNCHANGED:
                # Content-equal, but a stale file still needs deleting, and
                # apply_node_writes skips CONFIRMED_UNCHANGED plans entirely.
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

        return PlannedNodeWrite(
            node=node,
            node_file=node_file,
            change=change,
            old_file_to_delete=old_file_to_delete,
        )

    def _plan_manages_edge(
        self,
        node: Node,
        inventory: dict,
        existing_nodes: list[Node],
        source_name: str,
        relationships: list[Relationship],
        stats: SyncStats,
    ) -> None:
        """Match the system serial to a host node and plan a manages edge.

        Exactly one match -> BMC ``manages`` host. Zero or multiple matches
        warn (with the candidates) and never guess. Nodes managed by this same
        source (other BMCs, and the BMC itself on a re-sync) are excluded --
        a BMC and its host share the serial, so they must not self-match.
        """
        serial = _primary_serial(inventory)
        if not serial:
            return
        matches = [
            other
            for other in existing_nodes
            if other.managed_by != source_name
            and other.id != node.id
            and (other_serial := _node_serial(other)) is not None
            and other_serial.lower() == serial.lower()
        ]
        if len(matches) == 1:
            relationships.append(
                Relationship(
                    source=node.id,
                    target=matches[0].id,
                    type=RelationshipType.MANAGES,
                    managed_by=source_name,
                )
            )
        elif not matches:
            stats.warnings.append(
                f"BMC {node.id}: no host node matches serial {serial!r}; not linking "
                "(create/import the host, then re-sync to add the manages edge)."
            )
        else:
            candidates = ", ".join(sorted(m.id for m in matches))
            stats.warnings.append(
                f"BMC {node.id}: serial {serial!r} matches {len(matches)} nodes "
                f"({candidates}); refusing to guess which host it manages."
            )

    def _save_relationships(
        self,
        paths: ProjectPaths,
        source_name: str,
        new_relationships: list[Relationship],
        stats: SyncStats,
    ) -> None:
        """Replace this source's managed edges with the freshly computed set.

        Relationships not managed by this source are kept verbatim; this
        source's previous edges are dropped and the current ones re-added.
        A renamed BMC therefore drops its stale edge and gains the new one
        without any id rewriting. The file is only rewritten when the resulting
        edge set actually changes (no churn on unchanged resyncs), mirroring the
        SNMP plugin's change-guard.
        """
        existing = read_model(paths.relationships_yaml, RelationshipFile) or RelationshipFile()
        original = existing.relationships
        kept = [r for r in original if r.managed_by != source_name]
        to_add = [
            rel
            for rel in new_relationships
            if not any(r.source == rel.source and r.target == rel.target and r.type == rel.type for r in kept)
        ]

        def _sig(r: Relationship) -> tuple:
            return (r.source, r.target, str(r.type), r.managed_by, tuple(sorted((r.attributes or {}).items())))

        final = kept + to_add
        if {_sig(r) for r in final} != {_sig(r) for r in original}:
            existing.relationships = final
            stats.relationships_created += len(to_add)
            write_model(paths.relationships_yaml, existing)

    # ── sync ──────────────────────────────────────────────────────

    def sync(self, project_slug: str, source_name: str) -> SyncResult:
        """Synchronize BMC nodes from Redfish endpoints into local YAML.

        Writes (nodes and manages edges) are planned first and applied only
        when the run is a non-empty success (sync guard). Every run appends a
        record under ``.infracontext/runs/``.
        """
        environment = EnvironmentPaths.current()
        paths = ProjectPaths.for_project(project_slug, environment)
        try:
            source_file = paths.source_file(source_name)
        except ValueError as e:
            return SyncResult(status=SyncStatus.FAILED, message=f"Invalid source name '{source_name}': {e}")
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
            auth = resolve_basic_auth(config)
            verify = resolve_verify_ssl(config)
            today = time.strftime("%Y-%m-%d", time.gmtime())

            existing_nodes = load_existing_nodes(paths)
            source_id_index = self._build_source_id_index(paths)
            plans: list[PlannedNodeWrite] = []
            planned_paths: dict[Path, str] = {}
            id_renames: dict[str, str] = {}
            relationships: list[Relationship] = []

            for endpoint in config["endpoints"]:
                url = str(endpoint.get("url") or "").rstrip("/")
                if not url:
                    continue
                try:
                    inventory = self._fetch_endpoint(url, auth, verify)
                except RedfishError as e:
                    stats.errors.append(f"Endpoint '{url}' failed: {e}")
                    continue
                try:
                    plan = self._plan_endpoint_node(
                        endpoint,
                        url,
                        inventory,
                        source_name,
                        paths,
                        existing_nodes,
                        source_id_index,
                        planned_paths,
                        today,
                        stats,
                        id_renames,
                    )
                except Exception as e:
                    stats.errors.append(f"Error processing endpoint '{url}': {e}")
                    continue
                if plan is None:
                    continue
                plans.append(plan)
                self._plan_manages_edge(
                    plan.node, inventory, existing_nodes, source_name, relationships, stats
                )

            # Sync guard: only a non-empty, error-free run may touch disk.
            status = SyncStatus.SUCCESS if not stats.errors else SyncStatus.PARTIAL
            guarded = status is not SyncStatus.SUCCESS or not plans
            if not guarded:
                apply_node_writes(paths, plans)
                # Renames delete the old BMC node file -- repoint manual
                # edges/chain members (and any prior manages edges) at the
                # new ids so nothing dangles.
                rewrite_reference_ids(paths, id_renames, stats.warnings)
                for plan in plans:
                    if plan.change is NodeChange.CREATED:
                        stats.nodes_created += 1
                    elif plan.change is NodeChange.UPDATED:
                        stats.nodes_updated += 1
                    else:
                        stats.nodes_unchanged += 1
                self._save_relationships(paths, source_name, relationships, stats)

            duration_ms = int((time.monotonic() - start_time) * 1000)
            config["last_sync_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            config["last_sync_status"] = str(status)
            config["last_sync_stats"] = {
                "nodes_created": stats.nodes_created,
                "nodes_updated": stats.nodes_updated,
                "nodes_unchanged": stats.nodes_unchanged,
                "relationships_created": stats.relationships_created,
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
                message = "Redfish reported 0 endpoints to import; no node files were written (empty-sync guard)"
            else:
                written = stats.nodes_created + stats.nodes_updated
                message = f"Synced {written} BMC node(s) from {len(config['endpoints'])} endpoint(s)"
                if stats.relationships_created:
                    message += f", {stats.relationships_created} manages edge(s)"
                if stats.nodes_unchanged:
                    message += f" ({stats.nodes_unchanged} unchanged)"

            return SyncResult(
                status=status,
                message=message,
                nodes_created=stats.nodes_created,
                nodes_updated=stats.nodes_updated,
                nodes_unchanged=stats.nodes_unchanged,
                relationships_created=stats.relationships_created,
                errors=stats.errors,
                warnings=stats.warnings,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            config["last_sync_status"] = "failed"
            config["last_sync_message"] = str(e)
            write_yaml(source_file, config)
            record_sync_run(environment, project_slug, source_name, SyncStatus.FAILED, [])
            return SyncResult(
                status=SyncStatus.FAILED, message=str(e), errors=[str(e)], duration_ms=duration_ms
            )
