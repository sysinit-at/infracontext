"""NetBox DCIM source plugin (added in ic 0.4.0).

NetBox is the de-facto open-source source of truth for datacenter inventory
(DCIM). It exposes a paginated, token-authenticated REST API under ``/api/``,
so no vendor SDK is needed: this plugin speaks plain ``requests`` and reuses
the shared HTTP helpers in :mod:`infracontext.query.base`.

Config (in the source YAML):

.. code-block:: yaml

    type: netbox
    url: https://netbox.example.com   # NetBox base URL
    credential: netbox:prod           # ic keychain account holding the API token
    verify_ssl: true                  # default true; tls_skip_verify forces off
    site: dc1                         # optional; restrict the sync to one site slug
    max_devices: 500                  # optional; per-sync device cap (default 500)
    role_map:                         # optional; NetBox role slug -> ic node type
      core-router: network_device

The sync walks three DCIM collections (following the paginated ``next`` link):

* ``/api/dcim/sites/``   -> ``site`` nodes
* ``/api/dcim/racks/``   -> ``rack`` nodes, plus a ``located_in`` edge rack->site
* ``/api/dcim/devices/`` -> ``physical_host`` / ``network_device`` / ``pdu`` /
  ``ups`` nodes (type inferred from the device role, see :func:`_default_role_type`
  and the ``role_map`` override), plus a ``located_in`` edge device->rack (or
  device->site when the device is unracked). Each device carries
  ``attributes.hardware`` (manufacturer/model/serial/asset_tag/u_height/
  rack_position/rack_face) and its ``primary_ip`` as an IP address.

NetBox primary keys are stable, so a node's ``source_id`` is
``netbox:<source>:<object-type>:<pk>``: a renamed object (its slug changes)
is matched by PK and relocated rather than duplicated. Node ownership,
manual-field preservation, the sync guard, run records, and duplicate-detection
warnings all follow the same contract as the Proxmox and Redfish plugins.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlparse

from infracontext.models.node import Node, NodeType, slugify
from infracontext.models.relationship import Relationship, RelationshipFile, RelationshipType
from infracontext.paths import EnvironmentPaths, ProjectPaths
from infracontext.query.base import describe_http_error, resolve_verify_ssl
from infracontext.sources.base import (
    NodeChange,
    PlannedNodeWrite,
    SourcePlugin,
    SyncResult,
    SyncStatus,
    apply_node_writes,
    merge_synced_node,
    record_sync_run,
    remap_edge_ids,
    rewrite_reference_ids,
)
from infracontext.sources.dedup import find_duplicate_candidates, load_existing_nodes, overlap_warning
from infracontext.sources.registry import register_plugin
from infracontext.storage import read_model, read_yaml, write_model, write_yaml

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

# Generous connect/read timeout for a sync (paginates several collections).
_SYNC_TIMEOUT: tuple[float, float] = (5.0, 20.0)

# Default per-sync cap on the number of devices imported. Sites and racks are
# never capped (there are far fewer of them); devices are the collection that
# can run into the thousands on a large fleet.
DEFAULT_MAX_DEVICES = 500

# Device-role tokens that map to a network_device when no explicit role_map
# entry applies. Matched against the whitespace/hyphen tokens of the role slug
# so "core-router" matches but "backups" never matches "ups".
_NETWORK_ROLE_TOKENS = frozenset({"switch", "router", "firewall", "bmc"})
_ROLE_TOKEN_RE = re.compile(r"[a-z0-9]+")


class NetBoxError(RuntimeError):
    """A NetBox HTTP request failed (transport, auth, or malformed body)."""


# ── low-level client ───────────────────────────────────────────────


class NetBoxClient:
    """Minimal token-authenticated NetBox REST client over ``requests``.

    A caller may inject an existing session (tests pass a fake); otherwise one
    is created lazily so the module stays importable without ``requests``
    loaded.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        verify: bool = True,
        timeout: tuple[float, float] = _SYNC_TIMEOUT,
        session: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
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
        """Resolve a NetBox path (or absolute ``next`` link) against the base."""
        if path.startswith(("http://", "https://")):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        return headers

    def get(self, path: str) -> dict:
        """GET a NetBox resource and return the parsed JSON object.

        Raises :class:`NetBoxError` on any transport error, a ``>= 400`` status
        (shaped via :func:`describe_http_error`), or a body that is not a JSON
        object.
        """
        import requests

        url = self._url(path)
        try:
            response = self.session.get(
                url,
                headers=self._headers(),
                verify=self.verify,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise NetBoxError(f"Request to {url} failed: {e}") from e
        if response.status_code >= 400:
            raise NetBoxError(describe_http_error(url, response))
        try:
            data = response.json()
        except ValueError as e:
            raise NetBoxError(f"Invalid JSON from {url}") from e
        if not isinstance(data, dict):
            raise NetBoxError(f"Unexpected JSON shape from {url} (expected object)")
        return data

    def get_all(self, path: str, *, max_items: int | None = None) -> tuple[list[dict], int | None]:
        """Collect every ``results`` item across a paginated list endpoint.

        Follows the absolute ``next`` link NetBox returns until it is null, or
        until ``max_items`` results have been collected (in which case later
        pages are never fetched -- the device cap must not pull the whole
        fleet). Returns ``(items, total_count)`` where ``total_count`` is
        NetBox's reported ``count`` (or None if the server omitted it).
        """
        results: list[dict] = []
        total: int | None = None
        url = self._url(path)
        while url:
            page = self.get(url)
            if total is None and isinstance(page.get("count"), int):
                total = page["count"]
            page_results = page.get("results")
            if not isinstance(page_results, list):
                break
            results.extend(item for item in page_results if isinstance(item, dict))
            if max_items is not None and len(results) >= max_items:
                return results[:max_items], total
            next_link = page.get("next")
            url = next_link if isinstance(next_link, str) and next_link else None
        return results, total


# ── small pure helpers ─────────────────────────────────────────────


def _host(url: str) -> str:
    """Host portion of a URL (no scheme, no port), for messages."""
    return urlparse(url).hostname or url


def _choice_value(value: Any) -> str | None:
    """Value of a NetBox choice field ({value,label} dict, or a bare string)."""
    if isinstance(value, dict):
        inner = value.get("value")
        return inner if isinstance(inner, str) and inner else None
    if isinstance(value, str):
        return value or None
    return None


def _drop_empty(data: dict) -> dict:
    """Copy of ``data`` without ``None`` or empty-string values."""
    return {key: val for key, val in data.items() if val is not None and val != ""}


def _default_role_type(role_slug: str) -> NodeType:
    """Infer a node type from a device-role slug (no explicit mapping given).

    ``ups``/``pdu`` role tokens win first (power gear); then the network-gear
    tokens (switch/router/firewall/bmc); everything else is a physical host.
    Matching is on whole slug tokens, so "backups" never reads as a UPS.
    """
    tokens = set(_ROLE_TOKEN_RE.findall(role_slug.lower()))
    if "ups" in tokens:
        return NodeType.UPS
    if "pdu" in tokens:
        return NodeType.PDU
    if tokens & _NETWORK_ROLE_TOKENS:
        return NodeType.NETWORK_DEVICE
    return NodeType.PHYSICAL_HOST


def _device_role_slug(device: dict) -> str:
    """Role slug of a device, tolerating NetBox <3.6's ``device_role`` name."""
    role = device.get("role") or device.get("device_role") or {}
    if isinstance(role, dict):
        slug = role.get("slug")
        if isinstance(slug, str):
            return slug
    return ""


def _primary_ip(device: dict) -> str | None:
    """Bare address of a device's ``primary_ip`` (mask stripped), or None."""
    primary = device.get("primary_ip")
    if isinstance(primary, dict):
        address = primary.get("address")
        if isinstance(address, str) and address:
            return address.split("/")[0]
    return None


@dataclass
class SyncStats:
    """Statistics from a NetBox sync."""

    nodes_created: int = 0
    nodes_updated: int = 0
    nodes_unchanged: int = 0
    relationships_created: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@register_plugin
class NetBoxSource(SourcePlugin):
    """NetBox DCIM (REST/JSON over HTTPS) infrastructure source plugin."""

    source_type = "netbox"

    # Fakeable in tests: when set, requests are issued over this session.
    _session: Any = None

    def validate_config(self, config: dict) -> list[str]:
        """Validate NetBox source configuration."""
        errors: list[str] = []
        if not config.get("url"):
            errors.append("'url' is required (NetBox base URL, e.g. https://netbox.example.com)")

        role_map = config.get("role_map")
        if role_map is not None:
            if not isinstance(role_map, dict):
                errors.append("'role_map' must be a mapping of {netbox-role-slug: node-type}")
            else:
                for slug, node_type in role_map.items():
                    try:
                        NodeType(node_type)
                    except ValueError:
                        errors.append(f"role_map[{slug!r}] is not a valid node type: {node_type!r}")

        max_devices = config.get("max_devices")
        if max_devices is not None and (
            not isinstance(max_devices, int) or isinstance(max_devices, bool) or max_devices < 1
        ):
            errors.append(f"'max_devices' must be a positive integer (got {max_devices!r})")

        site = config.get("site")
        if site is not None and not isinstance(site, str):
            errors.append("'site' must be a string (a NetBox site slug)")

        return errors

    async def test_connection(self, config: dict) -> tuple[bool, str]:
        """Reach the NetBox API status endpoint (``/api/status/``)."""
        errors = self.validate_config(config)
        if errors:
            return False, "; ".join(errors)
        token = self._resolve_token(config)
        verify = resolve_verify_ssl(config)
        client = self._client(str(config["url"]), token, verify)
        try:
            status = client.get("/api/status/")
        except NetBoxError as e:
            return False, str(e)
        version = status.get("netbox-version", "?")
        return True, f"Connected to NetBox {version} at {_host(str(config['url']))}"

    # ── transport ─────────────────────────────────────────────────

    def _resolve_token(self, config: dict) -> str | None:
        """Resolve the API token, preferring the keychain over inline config.

        ``credential`` names an ``ic`` keychain account holding the raw token;
        a plaintext ``token`` in the config is honoured as a fallback (parity
        with the other plugins' inline-credential convenience).
        """
        account = config.get("credential")
        if account:
            from infracontext.credentials.keychain import get_credential

            token = get_credential(account)
            if token:
                return token
        return config.get("token")

    def _client(self, url: str, token: str | None, verify: bool) -> NetBoxClient:
        return NetBoxClient(url, token=token, verify=verify, timeout=_SYNC_TIMEOUT, session=self._session)

    def _collection_path(self, path: str, params: dict | None) -> str:
        """Build a list-endpoint path with an optional (non-empty) query string."""
        clean = {key: val for key, val in (params or {}).items() if val}
        return f"{path}?{urlencode(clean)}" if clean else path

    # ── node construction ─────────────────────────────────────────

    def _build_site_node(self, site: dict, source_name: str) -> Node:
        name = site.get("name") or f"site-{site.get('id')}"
        slug = slugify(str(name))
        attributes: dict = {}
        netbox = _drop_empty({"status": _choice_value(site.get("status")), "facility": site.get("facility")})
        if netbox:
            attributes["netbox"] = netbox
        return Node(
            id=Node.make_id(NodeType.SITE, slug),
            slug=slug,
            type=NodeType.SITE,
            name=str(name),
            source_id=f"netbox:{source_name}:site:{site.get('id')}",
            source=source_name,
            managed_by=source_name,
            attributes=attributes,
        )

    def _build_rack_node(self, rack: dict, source_name: str) -> Node:
        name = rack.get("name") or f"rack-{rack.get('id')}"
        slug = slugify(str(name))
        attributes: dict = {}
        netbox = _drop_empty(
            {
                "status": _choice_value(rack.get("status")),
                "u_height": rack.get("u_height"),
                "facility_id": rack.get("facility_id"),
            }
        )
        if netbox:
            attributes["netbox"] = netbox
        return Node(
            id=Node.make_id(NodeType.RACK, slug),
            slug=slug,
            type=NodeType.RACK,
            name=str(name),
            source_id=f"netbox:{source_name}:rack:{rack.get('id')}",
            source=source_name,
            managed_by=source_name,
            attributes=attributes,
        )

    def _build_device_node(self, device: dict, source_name: str, role_map: dict) -> Node:
        name = device.get("name") or f"device-{device.get('id')}"
        slug = slugify(str(name))
        role_slug = _device_role_slug(device)
        node_type = self._map_node_type(role_slug, role_map)

        device_type = device.get("device_type") or {}
        manufacturer = (device_type.get("manufacturer") or {}).get("name")
        hardware = _drop_empty(
            {
                "manufacturer": manufacturer,
                "model": device_type.get("model"),
                "serial": device.get("serial"),
                "asset_tag": device.get("asset_tag"),
                "u_height": device_type.get("u_height"),
                "rack_position": device.get("position"),
                "rack_face": _choice_value(device.get("face")),
            }
        )

        ip_addresses: list[str] = []
        primary = _primary_ip(device)
        if primary:
            ip_addresses.append(primary)

        attributes: dict = {}
        if hardware:
            attributes["hardware"] = hardware
        netbox = _drop_empty({"status": _choice_value(device.get("status")), "role": role_slug or None})
        if netbox:
            attributes["netbox"] = netbox

        return Node(
            id=Node.make_id(node_type, slug),
            slug=slug,
            type=node_type,
            name=str(name),
            source_id=f"netbox:{source_name}:device:{device.get('id')}",
            source=source_name,
            managed_by=source_name,
            ip_addresses=ip_addresses,
            attributes=attributes,
        )

    def _map_node_type(self, role_slug: str, role_map: dict) -> NodeType | str:
        """Resolve a device role slug to a node type (explicit map wins)."""
        override = role_map.get(role_slug)
        if override:
            return str(override)
        return _default_role_type(role_slug)

    # ── planning (shared shape with the Redfish plugin) ────────────

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

    def _plan_node(
        self,
        node: Node,
        paths: ProjectPaths,
        existing_nodes: list[Node],
        source_id_index: dict[str, tuple[Node, Path]],
        planned_paths: dict[Path, str],
        today: str,
        stats: SyncStats,
        id_renames: dict[str, str],
    ) -> PlannedNodeWrite | None:
        """Plan the write for one node, merging with an existing on-disk node.

        Handles the rename case (same source_id, different slug: relocate and
        delete the stale file, recording the old->new id in ``id_renames`` so
        references get rewritten) and refuses to overwrite a node bound to a
        foreign source_id (guarded slug collision). Returns None when the node
        must be skipped (a collision was recorded as an error).
        """
        node_file = paths.node_file(node.type, node.slug)
        if node_file in planned_paths:
            stats.errors.append(
                f"Slug collision within sync: objects '{planned_paths[node_file]}' and "
                f"'{node.source_id}' both map to {node.id} -- rename one in NetBox."
            )
            return None
        planned_paths[node_file] = node.source_id or node.id

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
                # A second file for this object lingers at the old slug -- merge
                # into the current slot and remove the stale duplicate.
                old_file_to_delete = prior[1]
                id_renames[prior[0].id] = node.id
                stats.warnings.append(f"Removed stale duplicate {prior[0].id} of {node.id}")
        elif prior_elsewhere and prior is not None:
            # The object was renamed in NetBox (new slug). Reuse the existing
            # node and drop the old file so no orphan is left behind.
            existing = prior[0]
            old_file_to_delete = prior[1]
            id_renames[prior[0].id] = node.id
            stats.warnings.append(f"Renamed: {prior[0].id} -> {node.id} (source_id {node.source_id})")

        if existing is not None:
            node = merge_synced_node(node, existing, preserve_ssh_alias=True)
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

    def _save_relationships(
        self,
        paths: ProjectPaths,
        source_name: str,
        new_relationships: list[Relationship],
        stats: SyncStats,
    ) -> None:
        """Replace this source's managed edges with the freshly computed set.

        Relationships not managed by this source are kept verbatim; this
        source's previous edges are dropped and the current ones re-added, so a
        relocated object drops its stale edge and gains the new one without any
        id rewriting (mirrors the Proxmox/Redfish replace-on-sync behaviour).
        The file is only rewritten when the resulting edge set actually changes
        (no churn on unchanged resyncs), mirroring the SNMP plugin's guard.
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

    def _located_in(self, source_id: str, target_id: str, source_name: str) -> Relationship:
        return Relationship(
            source=source_id,
            target=target_id,
            type=RelationshipType.LOCATED_IN,
            managed_by=source_name,
        )

    # ── sync ──────────────────────────────────────────────────────

    def sync(self, project_slug: str, source_name: str) -> SyncResult:
        """Synchronize DCIM nodes from NetBox into local YAML.

        Writes (site/rack/device nodes and located_in edges) are planned first
        and applied only when the run is a non-empty success (sync guard).
        Every run appends a record under ``.infracontext/runs/``.
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
            token = self._resolve_token(config)
            verify = resolve_verify_ssl(config)
            base_url = str(config["url"]).rstrip("/")
            site_filter = config.get("site")
            role_map = config.get("role_map") or {}
            max_devices = int(config.get("max_devices", DEFAULT_MAX_DEVICES))
            today = time.strftime("%Y-%m-%d", time.gmtime())

            client = self._client(base_url, token, verify)

            existing_nodes = load_existing_nodes(paths)
            source_id_index = self._build_source_id_index(paths)
            plans: list[PlannedNodeWrite] = []
            planned_paths: dict[Path, str] = {}
            id_renames: dict[str, str] = {}
            relationships: list[Relationship] = []
            site_nodes: dict[Any, Node] = {}  # NetBox site pk -> planned node
            rack_nodes: dict[Any, Node] = {}  # NetBox rack pk -> planned node

            # -- Sites (a transport failure here is fatal -> outer except) --
            site_params = {"slug": site_filter} if site_filter else None
            sites, _ = client.get_all(self._collection_path("/api/dcim/sites/", site_params))
            for site in sites:
                plan = self._plan_object(
                    self._build_site_node, site, source_name, paths, existing_nodes,
                    source_id_index, planned_paths, today, stats, id_renames,
                )
                if plan is None:
                    continue
                plans.append(plan)
                if site.get("id") is not None:
                    site_nodes[site["id"]] = plan.node

            # -- Racks (+ located_in rack -> site) --
            rack_params = {"site": site_filter} if site_filter else None
            racks, _ = client.get_all(self._collection_path("/api/dcim/racks/", rack_params))
            for rack in racks:
                plan = self._plan_object(
                    self._build_rack_node, rack, source_name, paths, existing_nodes,
                    source_id_index, planned_paths, today, stats, id_renames,
                )
                if plan is None:
                    continue
                plans.append(plan)
                if rack.get("id") is not None:
                    rack_nodes[rack["id"]] = plan.node
                site_ref = rack.get("site") or {}
                target = site_nodes.get(site_ref.get("id"))
                if target is not None:
                    relationships.append(self._located_in(plan.node.id, target.id, source_name))

            # -- Devices (capped; + located_in device -> rack or site) --
            device_params = {"site": site_filter} if site_filter else None
            devices, total = client.get_all(
                self._collection_path("/api/dcim/devices/", device_params), max_items=max_devices
            )
            if total is not None and total > len(devices):
                stats.warnings.append(
                    f"Device cap reached: imported {len(devices)} of {total} devices; "
                    f"raise 'max_devices' (currently {max_devices}) to import the rest."
                )
            for device in devices:
                plan = self._plan_object(
                    lambda d, s: self._build_device_node(d, s, role_map),
                    device, source_name, paths, existing_nodes,
                    source_id_index, planned_paths, today, stats, id_renames,
                )
                if plan is None:
                    continue
                plans.append(plan)
                rack_ref = device.get("rack") or {}
                target = rack_nodes.get(rack_ref.get("id"))
                if target is None:
                    site_ref = device.get("site") or {}
                    target = site_nodes.get(site_ref.get("id"))
                if target is not None:
                    relationships.append(self._located_in(plan.node.id, target.id, source_name))

            # Sync guard: only a non-empty, error-free run may touch disk.
            status = SyncStatus.SUCCESS if not stats.errors else SyncStatus.PARTIAL
            guarded = status is not SyncStatus.SUCCESS or not plans
            if not guarded:
                apply_node_writes(paths, plans)
                # Relocations delete the old node file -- repoint manual
                # edges/chain members at the new ids, and remap this run's
                # own planned edges the same way (a target resolved before
                # its relocation would otherwise dangle).
                rewrite_reference_ids(paths, id_renames, stats.warnings)
                relationships = remap_edge_ids(relationships, id_renames)
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
                message = f"Sync found {len(stats.errors)} error(s); no node files were written (sync guard)"
            elif not plans:
                message = "NetBox reported no objects to import; no node files were written (empty-sync guard)"
            else:
                written = stats.nodes_created + stats.nodes_updated
                message = (
                    f"Synced {written} node(s) from NetBox "
                    f"({len(sites)} site(s), {len(racks)} rack(s), {len(devices)} device(s))"
                )
                if stats.relationships_created:
                    message += f", {stats.relationships_created} located_in edge(s)"
                if stats.nodes_unchanged:
                    message += f" ({stats.nodes_unchanged} unchanged)"
            if stats.warnings:
                message += f" ({len(stats.warnings)} warnings)"

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

    def _plan_object(
        self,
        builder,
        obj: dict,
        source_name: str,
        paths: ProjectPaths,
        existing_nodes: list[Node],
        source_id_index: dict[str, tuple[Node, Path]],
        planned_paths: dict[Path, str],
        today: str,
        stats: SyncStats,
        id_renames: dict[str, str],
    ) -> PlannedNodeWrite | None:
        """Build a node with ``builder`` and plan its write, isolating errors.

        A malformed single object (bad type, validation failure) records an
        error and is skipped -- one bad row must not abort the whole sync (it
        only holds the guard, exactly like a slug collision).
        """
        try:
            node = builder(obj, source_name)
        except Exception as e:  # noqa: BLE001 - one bad object shouldn't abort the run
            stats.errors.append(f"Skipped NetBox object id={obj.get('id')!r}: {e}")
            return None
        return self._plan_node(
            node, paths, existing_nodes, source_id_index, planned_paths, today, stats, id_renames
        )
