"""SNMP source plugin (added in ic 0.4.0).

Walks a small, well-supported set of standard MIBs on each configured target
and turns every reachable device into a ``network_device`` node, plus
``connects_to`` topology edges wherever an LLDP neighbor resolves to a node we
already know.

MIB coverage (the conceptual reference is scanopy's discovery walk):

- **SNMPv2-MIB** system group (``1.3.6.1.2.1.1``): sysName / sysDescr /
  sysLocation / sysUpTime — identity and the node slug.
- **ENTITY-MIB** ``entPhysicalTable`` (``1.3.6.1.2.1.47.1.1.1.1``): the best
  physical entity (class chassis > stack > module) yields
  manufacturer / model / serial into ``attributes.hardware``.
- **IF-MIB** ``ifTable`` + ``ifXTable``: an interface summary (name, admin /
  oper status, speed, MAC) into ``attributes.snmp.interfaces`` — capped
  (default 64) with a truncation note so a large chassis can't bloat the file.
- **LLDP-MIB** ``lldpRemTable``: the remote-neighbor list. A neighbor whose
  ``lldpRemSysName`` matches an existing node becomes a ``connects_to`` edge;
  everything else is recorded under ``attributes.snmp.unmatched_neighbors`` and
  surfaced as a sync warning (we never auto-create a node from an LLDP string).

Config (in the source YAML):

.. code-block:: yaml

    type: snmp
    snmp_version: "2c"          # 2c | 3
    targets:                    # explicit host list (v1 — no CIDR expansion)
      - host: 10.0.0.1
        name: core-sw-01        # optional; else sysName, else host
      - 10.0.0.2                # bare host string is also accepted
    port: 161
    timeout: 5
    retries: 1
    max_interfaces: 64
    default_node_type: network_device
    # v3 only:
    v3_user: monitor
    v3_auth_protocol: sha       # md5 | sha
    v3_priv_protocol: aes       # des | aes

Credentials live in the system keychain (never in the YAML), keyed by source
name — mirroring the Proxmox plugin:

- v2c: ``snmp:<source>:community``
- v3 : ``snmp:<source>:auth`` and (optionally) ``snmp:<source>:priv``

set with ``ic config credential set``.

Guards: each target is collected independently. A target whose walk raises
mid-way is marked partial — nothing derived from it is written or removed
(its node and edges are left exactly as they were), the run is classified
``partial``, and other targets still sync. A run that plans zero writes hits
the standard empty-sync guard.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from infracontext.models.node import Node, NodeType, Observability, slugify
from infracontext.models.relationship import Relationship, RelationshipFile, RelationshipType
from infracontext.paths import EnvironmentPaths, ProjectPaths
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
    remap_edge_ids,
    rewrite_reference_ids,
)
from infracontext.sources.dedup import find_duplicate_candidates, load_existing_nodes, overlap_warning
from infracontext.sources.registry import register_plugin
from infracontext.sources.ssh_config import is_ip_address
from infracontext.storage import read_model, read_yaml, write_model, write_yaml

log = logging.getLogger(__name__)

# ── MIB base OIDs (entry rows; scalars sit at column.0) ────────────
SYS = "1.3.6.1.2.1.1"  # SNMPv2-MIB system group
ENTITY = "1.3.6.1.2.1.47.1.1.1.1"  # ENTITY-MIB entPhysicalEntry
IF_TABLE = "1.3.6.1.2.1.2.2.1"  # IF-MIB ifEntry
IF_X_TABLE = "1.3.6.1.2.1.31.1.1.1"  # IF-MIB ifXEntry
LLDP_REM = "1.0.8802.1.1.2.1.4.1.1"  # LLDP-MIB lldpRemEntry

DEFAULT_PORT = 161
DEFAULT_TIMEOUT = 5
DEFAULT_RETRIES = 1
DEFAULT_MAX_INTERFACES = 64
DEFAULT_NODE_TYPE = "network_device"

_SNMP_VERSIONS = {"2c", "3"}
# puresnmp exposes auth/priv plugins by these short names; kept permissive so
# a device that speaks a variant our puresnmp build ships can still be used.
_V3_AUTH_PROTOCOLS = {"md5", "sha", "sha1", "sha224", "sha256", "sha384", "sha512"}
_V3_PRIV_PROTOCOLS = {"des", "aes", "aes128", "aes192", "aes256"}

# entPhysicalClass -> selection priority (lower is better). Everything else
# (power supply, fan, port, …) ranks last so a device that only exposes those
# still contributes hardware data, but a chassis always wins.
_ENTITY_CLASS_PRIORITY = {3: 0, 11: 1, 9: 2}  # chassis, stack, module

# ifAdminStatus / ifOperStatus code -> label (RFC 2863).
_IF_STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}


class SNMPError(RuntimeError):
    """An SNMP walk against a target failed (connection, timeout, protocol)."""


# ── credentials (shared with the query plugin) ─────────────────────


def snmp_credentials(config: dict):  # noqa: ANN201 - puresnmp type imported lazily
    """Build a puresnmp credential object from source config + the keychain.

    v2c reads ``snmp:<name>:community``; v3 reads ``snmp:<name>:auth`` and
    (optionally) ``snmp:<name>:priv``. Shared by the SNMP *source* plugin
    (discovery/sync) and the SNMP *query* plugin (live status) so both honor
    the same keychain-backed scheme, keyed by source name.
    """
    from infracontext.credentials.keychain import get_credential

    name = config["name"]
    version = str(config.get("snmp_version", "2c"))
    if version == "2c":
        from puresnmp import V2C

        community = get_credential(f"snmp:{name}:community")
        if not community:
            raise SNMPError(
                f"No SNMP community for '{name}'. Set it with "
                f"'ic config credential set snmp:{name}:community'."
            )
        return V2C(community)

    from puresnmp import V3, Auth, Priv

    auth_key = get_credential(f"snmp:{name}:auth")
    priv_key = get_credential(f"snmp:{name}:priv")
    auth = Auth(auth_key.encode(), str(config.get("v3_auth_protocol", "sha")).lower()) if auth_key else None
    priv = Priv(priv_key.encode(), str(config.get("v3_priv_protocol", "aes")).lower()) if priv_key else None
    return V3(str(config["v3_user"]), auth=auth, priv=priv)


# ── value coercion ─────────────────────────────────────────────────


def _text(value: object) -> str:
    """Best-effort text from an SNMP value (OCTET STRINGs arrive as bytes)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        for enc in ("utf-8", "latin-1"):
            try:
                return value.decode(enc).replace("\x00", "").strip()
            except UnicodeDecodeError:
                continue
        return value.hex()
    return str(value).strip()


def _mac(value: object) -> str:
    """Format an ifPhysAddress / chassis-id OCTET STRING as colon-hex."""
    if isinstance(value, bytes):
        return ":".join(f"{b:02x}" for b in value) if value else ""
    return _text(value)


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _index_sort_key(index: str) -> tuple[int, object]:
    """Sort table indices numerically when possible, lexically otherwise."""
    return (0, int(index)) if index.isdigit() else (1, index)


# ── OID table parsing (pure) ───────────────────────────────────────


def _split_suffix(oid: str, base: str) -> str | None:
    """Return the portion of ``oid`` below ``base`` (``None`` if not under it)."""
    prefix = base + "."
    return oid[len(prefix) :] if oid.startswith(prefix) else None


def _tabulate(rows: list[tuple[str, object]], base: str) -> dict[str, dict[int, object]]:
    """Group walked ``(oid, value)`` pairs into ``{row_index: {column: value}}``.

    The first sub-identifier below ``base`` is the column; the remainder is the
    row index (which for conceptual/LLDP tables is a compound like
    ``timeMark.localPort.remIndex``). Scalars land under row index ``"0"``.
    """
    table: dict[str, dict[int, object]] = {}
    for oid, value in rows:
        suffix = _split_suffix(oid, base)
        if suffix is None:
            continue
        head, _, index = suffix.partition(".")
        col = _as_int(head)
        if col is None or not index:
            continue
        table.setdefault(index, {})[col] = value
    return table


def _speed_mbps(if_high_speed: object, if_speed: object) -> int | None:
    """Prefer ifHighSpeed (Mbps); fall back to ifSpeed (bits/sec)."""
    high = _as_int(if_high_speed)
    if high:
        return high
    low = _as_int(if_speed)
    return low // 1_000_000 if low else None


def parse_system(rows: list[tuple[str, object]]) -> dict[str, object]:
    """Extract the system-group fields we care about from a system walk."""
    scalars = _tabulate(rows, SYS).get("0", {})
    return {
        "sys_descr": _text(scalars.get(1)),
        "sys_uptime": _as_int(scalars.get(3)),
        "sys_name": _text(scalars.get(5)),
        "sys_location": _text(scalars.get(6)),
    }


def parse_hardware(rows: list[tuple[str, object]]) -> dict[str, str]:
    """Pick the best physical entity's manufacturer / model / serial."""
    best: dict[str, str] | None = None
    best_rank: tuple[int, int] | None = None
    for cols in _tabulate(rows, ENTITY).values():
        cls = _as_int(cols.get(5))
        priority = _ENTITY_CLASS_PRIORITY.get(cls, 99) if cls is not None else 99
        hardware = {
            "manufacturer": _text(cols.get(12)),
            "model": _text(cols.get(13)),
            "serial": _text(cols.get(11)),
        }
        if not any(hardware.values()):
            continue
        rank = (priority, 0 if hardware["serial"] else 1)
        if best_rank is None or rank < best_rank:
            best_rank, best = rank, hardware
    return {k: v for k, v in best.items() if v} if best else {}


def parse_interfaces(
    if_rows: list[tuple[str, object]], ifx_rows: list[tuple[str, object]], cap: int
) -> tuple[list[dict[str, object]], bool, int]:
    """Merge ifTable + ifXTable into a capped interface summary.

    Returns ``(interfaces, truncated, total)``.
    """
    base = _tabulate(if_rows, IF_TABLE)
    ext = _tabulate(ifx_rows, IF_X_TABLE)
    interfaces: list[dict[str, object]] = []
    for index in sorted(base, key=_index_sort_key):
        cols = base[index]
        xcols = ext.get(index, {})
        entry: dict[str, object] = {
            "name": _text(xcols.get(1)) or _text(cols.get(2)) or index,
            "admin": _IF_STATUS.get(_as_int(cols.get(7)) or 0, ""),
            "oper": _IF_STATUS.get(_as_int(cols.get(8)) or 0, ""),
            "speed_mbps": _speed_mbps(xcols.get(15), cols.get(5)),
            "mac": _mac(cols.get(6)),
        }
        interfaces.append({k: v for k, v in entry.items() if v not in ("", None)})
    total = len(interfaces)
    return interfaces[:cap], total > cap, total


def parse_neighbors(rows: list[tuple[str, object]]) -> list[dict[str, str]]:
    """Extract LLDP remote neighbors, one dict per lldpRemTable row.

    The row index is ``timeMark.localPortNum.remIndex``; the middle component is
    the local port. Remote port prefers the human description over the raw id.
    """
    neighbors: list[dict[str, str]] = []
    table = _tabulate(rows, LLDP_REM)
    for index in sorted(table):
        cols = table[index]
        parts = index.split(".")
        local_port = parts[1] if len(parts) >= 2 else index
        neighbors.append(
            {
                "remote_sysname": _text(cols.get(9)),
                "remote_port": _text(cols.get(8)) or _mac(cols.get(7)),
                "local_port": local_port,
            }
        )
    return neighbors


# ── neighbor -> node matching ──────────────────────────────────────


def _identity_keys(node: Node) -> set[str]:
    """Lowercased identifiers by which an LLDP sysName might name this node."""
    keys: set[str] = {node.slug.lower(), slugify(node.name)}
    if node.ssh_alias:
        keys.add(node.ssh_alias.lower())
    for domain in node.domains:
        keys.add(domain.lower())
        keys.add(domain.split(".", 1)[0].lower())  # short hostname
    return {k for k in keys if k}


def build_identity_index(nodes: list[Node]) -> dict[str, set[str]]:
    """Map each identifier to the set of node ids that own it.

    Mirrors ``dedup.py``'s shared-identity guard: a match is only trusted when
    an identifier resolves to exactly one node, so a value two nodes share
    (a duplicated hostname) never produces a wrong edge.
    """
    owners: dict[str, set[str]] = {}
    for node in nodes:
        for key in _identity_keys(node):
            owners.setdefault(key, set()).add(node.id)
    return owners


def match_neighbor(sysname: str, index: dict[str, set[str]]) -> str | None:
    """Resolve an LLDP remote sysName to a single node id, or ``None``.

    Returns ``None`` when the name is unknown or ambiguous (owned by more than
    one node) — the shared-identity guard.
    """
    if not sysname:
        return None
    candidates = {sysname.lower(), slugify(sysname), sysname.split(".", 1)[0].lower()}
    owners: set[str] = set()
    for key in candidates:
        owners |= index.get(key, set())
    return next(iter(owners)) if len(owners) == 1 else None


def _warn_legacy_slug_observability(
    node: Node, existing: Node, fresh: Observability, warnings: list[str]
) -> None:
    """Flag (never delete) the artifact the pre-0.4.0 adoption path wrote.

    That path recorded ``Observability(type="snmp", instance=<slug>)`` -- no
    ``source`` field, every other field at its default. Live queries against
    such an entry target a often-unresolvable sysName slug, so it deserves a
    loud migration hint.

    It must stay a WARNING because there is no reliable provenance separating
    the artifact from a genuinely manual entry of the same shape: adoption
    itself sets ``managed_by``, so one sync after adopting a manual node an
    ownership-gated deletion would misclassify the operator's own entry and
    destroy manual configuration. Detection therefore only reports, with the
    exact remediation; :func:`ensure_source_observability`'s contract
    (source-less entries are never touched) holds unconditionally.

    Detection stays narrow to keep the warning honest: the pre-sync on-disk
    node was owned by this source, and the entry equals the artifact
    field-for-field against ``existing.slug`` (the slug at the time the old
    code wrote it, so a relocation in this same sync still matches). An entry
    an operator has touched (notes, url, anything -- including unknown fields
    stashed by a newer ic, which pydantic equality ignores) never matches.
    """
    if existing.managed_by != fresh.source:
        return
    artifact = Observability(type="snmp", instance=existing.slug)
    if any(
        o == artifact and not getattr(o, "_ic_unknown_fields", None) for o in node.observability
    ):
        # The remediation must name BOTH fields: instance alone would leave
        # the entry source-less, so queries in a multi-source project could
        # still pick another source's credentials -- and when the configured
        # target host equals the slug, an instance-only edit is a no-op and
        # this warning would repeat forever. Setting source makes the entry
        # source-owned, which also provably silences this warning.
        warnings.append(
            f"{node.id}: snmp observability entry points at the node slug '{existing.slug}' "
            f"with no source field -- likely a pre-0.4.0 artifact; live queries may not resolve "
            f"or may use another source's credentials. Fix: set instance to '{fresh.instance}' "
            f"AND source to '{fresh.source}' via 'ic describe node edit {node.id}', or delete "
            f"the entry so source '{fresh.source}' can manage it."
        )


# ── collected-target payload ───────────────────────────────────────


@dataclass
class TargetData:
    """Everything one successful SNMP walk of a target yielded."""

    host: str
    sys_name: str = ""
    sys_descr: str = ""
    sys_location: str = ""
    sys_uptime: int | None = None
    hardware: dict[str, str] = field(default_factory=dict)
    interfaces: list[dict[str, object]] = field(default_factory=list)
    interfaces_truncated: bool = False
    interfaces_total: int = 0
    neighbors: list[dict[str, str]] = field(default_factory=list)


@dataclass
class _Collected:
    """A target that walked cleanly, with its preliminary node."""

    host: str
    data: TargetData
    node: Node


@dataclass
class SyncStats:
    """Statistics from an SNMP sync."""

    nodes_created: int = 0
    nodes_updated: int = 0
    nodes_unchanged: int = 0
    relationships_created: int = 0
    partial_targets: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@register_plugin
class SNMPSource(SourcePlugin):
    """SNMP (v2c / v3) network-device discovery source plugin."""

    source_type = "snmp"

    # ── config ────────────────────────────────────────────────────

    @staticmethod
    def _targets(config: dict) -> list[tuple[str, str | None]]:
        """Normalize ``targets`` to ``(host, explicit_name | None)`` pairs."""
        out: list[tuple[str, str | None]] = []
        for raw in config.get("targets") or []:
            if isinstance(raw, str):
                out.append((raw.strip(), None))
            elif isinstance(raw, dict) and raw.get("host"):
                out.append((str(raw["host"]).strip(), raw.get("name")))
        return out

    def validate_config(self, config: dict) -> list[str]:
        """Validate SNMP source configuration."""
        errors: list[str] = []

        version = str(config.get("snmp_version", "2c"))
        if version not in _SNMP_VERSIONS:
            errors.append(f"'snmp_version' must be one of {sorted(_SNMP_VERSIONS)}, got {version!r}")

        targets_raw = config.get("targets")
        if not targets_raw or not isinstance(targets_raw, list):
            errors.append("'targets' is required (a non-empty list of hosts)")
        else:
            for raw in targets_raw:
                if isinstance(raw, str):
                    if not raw.strip():
                        errors.append("'targets' contains an empty host")
                elif isinstance(raw, dict):
                    if not raw.get("host"):
                        errors.append(f"target {raw!r} is missing 'host'")
                else:
                    errors.append(f"target {raw!r} must be a host string or a mapping with 'host'")

        node_type = config.get("default_node_type", DEFAULT_NODE_TYPE)
        try:
            NodeType(node_type)
        except ValueError:
            errors.append(f"'default_node_type' is not a valid node type: {node_type!r}")

        for key in ("port", "timeout", "retries", "max_interfaces"):
            if key in config and _as_int(config[key]) is None:
                errors.append(f"'{key}' must be an integer, got {config[key]!r}")

        if version == "3":
            if not config.get("v3_user"):
                errors.append("'v3_user' is required for SNMPv3")
            auth = str(config.get("v3_auth_protocol", "sha")).lower()
            if auth not in _V3_AUTH_PROTOCOLS:
                errors.append(f"'v3_auth_protocol' {auth!r} is not recognized")
            priv = str(config.get("v3_priv_protocol", "aes")).lower()
            if priv not in _V3_PRIV_PROTOCOLS:
                errors.append(f"'v3_priv_protocol' {priv!r} is not recognized")
        return errors

    async def test_connection(self, config: dict) -> tuple[bool, str]:
        """Probe sysName on the first target (walk runs off the event loop)."""
        errors = self.validate_config(config)
        if errors:
            return False, "; ".join(errors)
        host = self._targets(config)[0][0]
        try:
            rows = await asyncio.to_thread(self._walk, config, host, SYS)
        except SNMPError as e:
            return False, str(e)
        name = parse_system(rows).get("sys_name") or "(no sysName)"
        return True, f"Connected to SNMP target {host} (sysName={name})"

    # ── transport (the single real-network seam; faked in tests) ───

    def _credentials(self, config: dict):  # noqa: ANN202 - puresnmp type imported lazily
        return snmp_credentials(config)

    def _walk(self, config: dict, host: str, base_oid: str) -> list[tuple[str, object]]:
        """Blocking SNMP walk of ``base_oid`` on ``host`` (raises SNMPError).

        This is the only method that touches the network — every test replaces
        it with a fake, so the plugin's logic is exercised without a device.
        """
        try:
            return asyncio.run(self._awalk(config, host, base_oid))
        except SNMPError:
            raise
        except Exception as e:  # noqa: BLE001 - puresnmp raises a wide variety
            raise SNMPError(f"SNMP walk of {base_oid} on {host} failed: {e}") from e

    async def _awalk(self, config: dict, host: str, base_oid: str) -> list[tuple[str, object]]:
        from puresnmp import Client, PyWrapper

        client = Client(host, self._credentials(config), port=int(config.get("port", DEFAULT_PORT)))
        wrapper = PyWrapper(client)
        timeout = int(config.get("timeout", DEFAULT_TIMEOUT))
        retries = int(config.get("retries", DEFAULT_RETRIES))
        out: list[tuple[str, object]] = []
        with client.reconfigure(timeout=timeout, retries=retries):
            async for varbind in wrapper.walk(base_oid):
                out.append((str(varbind.oid), varbind.value))
        return out

    # ── collection ────────────────────────────────────────────────

    def _collect_target(self, config: dict, host: str) -> TargetData:
        """Walk every MIB for one target. Any walk error marks it partial."""
        cap = int(config.get("max_interfaces", DEFAULT_MAX_INTERFACES))
        system = parse_system(self._walk(config, host, SYS))
        hardware = parse_hardware(self._walk(config, host, ENTITY))
        interfaces, truncated, total = parse_interfaces(
            self._walk(config, host, IF_TABLE), self._walk(config, host, IF_X_TABLE), cap
        )
        neighbors = parse_neighbors(self._walk(config, host, LLDP_REM))
        return TargetData(
            host=host,
            sys_name=str(system["sys_name"]),
            sys_descr=str(system["sys_descr"]),
            sys_location=str(system["sys_location"]),
            sys_uptime=system["sys_uptime"],  # type: ignore[arg-type]
            hardware=hardware,
            interfaces=interfaces,
            interfaces_truncated=truncated,
            interfaces_total=total,
            neighbors=neighbors,
        )

    def _build_node(self, config: dict, source_name: str, host: str, name: str | None, data: TargetData) -> Node:
        """Build the preliminary node (all attributes except LLDP results)."""
        try:
            node_type = NodeType(config.get("default_node_type", DEFAULT_NODE_TYPE))
        except ValueError:
            node_type = NodeType.NETWORK_DEVICE
        slug = slugify(data.sys_name or host)
        display_name = name or data.sys_name or host

        ip_addresses: list[str] = []
        domains: list[str] = []
        if is_ip_address(host):
            ip_addresses.append(host)
        else:
            domains.append(host)
        if (
            data.sys_name
            and "." in data.sys_name
            and not is_ip_address(data.sys_name)
            and data.sys_name not in domains
        ):
            domains.append(data.sys_name)

        snmp_attrs: dict[str, object] = {}
        if data.sys_descr:
            snmp_attrs["sys_descr"] = data.sys_descr
        if data.sys_location:
            snmp_attrs["sys_location"] = data.sys_location
        # sysUpTime is a live counter that ticks on every poll: persisting it
        # would force an UPDATED write on every otherwise-unchanged resync
        # (defeating the no-churn design). It is a query-time metric instead --
        # the SNMP query plugin exposes it live as sys_uptime_ticks.
        if data.interfaces:
            snmp_attrs["interfaces"] = data.interfaces
            if data.interfaces_truncated:
                snmp_attrs["interfaces_truncated"] = True
                snmp_attrs["interfaces_total"] = data.interfaces_total

        attributes: dict[str, object] = {"snmp": snmp_attrs}
        if data.hardware:
            attributes["hardware"] = data.hardware

        return Node(
            id=Node.make_id(node_type, slug),
            slug=slug,
            type=node_type,
            name=display_name,
            source_id=f"snmp:{source_name}:{host}",
            source=source_name,
            managed_by=source_name,
            ip_addresses=ip_addresses,
            domains=domains,
            attributes=attributes,  # type: ignore[arg-type]
            observability=[Observability(type="snmp", instance=host, source=source_name)],
        )

    # ── relocation bookkeeping (ported from the CheckMK plugin) ────

    @staticmethod
    def _build_source_id_index(paths: ProjectPaths) -> dict[str, tuple[Node, Path]]:
        """Map source_id -> (node, file) for every readable node (drift-safe)."""
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

    def _plan_device(
        self,
        paths: ProjectPaths,
        node: Node,
        source_id_index: dict[str, tuple[Node, Path]],
        planned_paths: dict[Path, str],
        id_renames: dict[str, str],
        existing_nodes: list[Node],
        stats: SyncStats,
    ) -> PlannedNodeWrite | None:
        """Merge with any existing node and produce a write plan, or ``None``.

        Returns ``None`` on a slug/relocation conflict (recorded as an error);
        the caller then also drops any edges derived from this node. Mirrors the
        CheckMK relocation logic: same source_id at a new slug relocates (old
        file deleted), a foreign source_id or a manual node at the target is
        never overwritten.
        """
        node_file = paths.node_file(node.type, node.slug)
        if node_file in planned_paths:
            stats.errors.append(
                f"Slug collision within sync: {planned_paths[node_file]} and "
                f"{node.source_id} both map to {node.id} (two devices share a sysName)."
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
                    f"'{at_dest.id}' is bound to source_id '{at_dest.source_id}', refusing to overwrite."
                )
                return None
            if prior_elsewhere and prior is not None:
                if at_dest.source_id is None:
                    stats.errors.append(
                        f"Relocation of {prior[0].id} to {node.id} blocked: the target is a "
                        f"manually created node — merge them with "
                        f"'ic describe node consolidate {node.id} {prior[0].id}'."
                    )
                    return None
                existing = at_dest
                old_file_to_delete = prior[1]
                id_renames[prior[0].id] = node.id
                stats.warnings.append(
                    f"Removed stale duplicate {prior[0].id} of {node.id} (source_id {node.source_id})"
                )
            else:
                existing = at_dest
        elif prior_elsewhere and prior is not None:
            existing = prior[0]
            old_file_to_delete = prior[1]
            id_renames[prior[0].id] = node.id
            stats.warnings.append(f"Relocated: {prior[0].id} → {node.id} (source_id {node.source_id})")

        if existing:
            # _build_node's own entry carries the configured target host (the
            # working address) and this source's name; capture it before the
            # merge replaces observability with the existing node's list.
            fresh_obs = next((o for o in node.observability if o.type == "snmp"), None)
            node = merge_synced_node(node, existing, preserve_ssh_alias=True)
            if fresh_obs is not None:
                _warn_legacy_slug_observability(node, existing, fresh_obs, stats.warnings)
                node = ensure_source_observability(node, fresh_obs)
            change = NodeChange.CONFIRMED_UNCHANGED if node == existing else NodeChange.UPDATED
            if old_file_to_delete is not None and change is NodeChange.CONFIRMED_UNCHANGED:
                change = NodeChange.UPDATED  # a stale file still needs deleting
        else:
            node = node.model_copy(update={"first_seen": time.strftime("%Y-%m-%d", time.gmtime())})
            change = NodeChange.CREATED
            for overlap in find_duplicate_candidates(
                existing_nodes, ips=node.ip_addresses, domains=node.domains, ssh_alias=node.ssh_alias
            ):
                stats.warnings.append(overlap_warning(node.id, overlap))

        return PlannedNodeWrite(node=node, node_file=node_file, change=change, old_file_to_delete=old_file_to_delete)

    def _save_relationships(
        self,
        paths: ProjectPaths,
        source_name: str,
        synced_source_ids: set[str],
        new_edges: list[Relationship],
        stats: SyncStats,
    ) -> None:
        """Replace this source's edges for freshly-synced devices only.

        Edges managed by this source whose *source* node was synced this run are
        rebuilt from ``new_edges``; edges belonging to a partial (skipped) target
        are kept, so a flaky device never loses its known topology. Other
        sources' and user edges are always kept. The file is only rewritten when
        the managed edge set actually changes (no churn on unchanged resyncs).
        """
        existing = read_model(paths.relationships_yaml, RelationshipFile) or RelationshipFile()
        original = existing.relationships
        kept = [
            r for r in original if not (r.managed_by == source_name and r.source in synced_source_ids)
        ]
        to_add = [
            rel
            for rel in new_edges
            if not any(k.source == rel.source and k.target == rel.target and str(k.type) == str(rel.type) for k in kept)
        ]

        def _sig(r: Relationship) -> tuple:
            return (r.source, r.target, str(r.type), r.managed_by, tuple(sorted((r.attributes or {}).items())))

        final = kept + to_add
        if {_sig(r) for r in final} != {_sig(r) for r in original}:
            existing.relationships = final
            stats.relationships_created += len(to_add)
            write_model(paths.relationships_yaml, existing)

    # ── sync ──────────────────────────────────────────────────────

    def sync(self, project_slug: str, source_name: str) -> SyncResult:  # noqa: C901 - staged but linear
        """Synchronize devices and LLDP topology from SNMP into local YAML."""
        environment = EnvironmentPaths.current()
        paths = ProjectPaths.for_project(project_slug, environment)
        try:
            source_file = paths.source_file(source_name)
        except ValueError as e:
            return SyncResult(status=SyncStatus.FAILED, message=f"Invalid source name '{source_name}': {e}")
        if not source_file.exists():
            return SyncResult(status=SyncStatus.FAILED, message=f"Source '{source_name}' not found")

        config = read_yaml(source_file)
        config_errors = self.validate_config(config)
        if config_errors:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="Invalid source config: " + "; ".join(config_errors),
                errors=config_errors,
            )

        start_time = time.monotonic()
        stats = SyncStats()

        try:
            existing_nodes = load_existing_nodes(paths)
            source_id_index = self._build_source_id_index(paths)

            # Pass 1: collect each target; a partial walk is skipped whole.
            collected: list[_Collected] = []
            for host, name in self._targets(config):
                try:
                    data = self._collect_target(config, host)
                except SNMPError as e:
                    stats.partial_targets += 1
                    stats.errors.append(f"SNMP target '{host}' partially walked, left untouched: {e}")
                    continue
                node = self._build_node(config, source_name, host, name, data)
                collected.append(_Collected(host=host, data=data, node=node))

            # Resolve LLDP neighbors against everything we know (on disk + this
            # run's devices), then finalize each node and plan its write.
            identity_index = build_identity_index(existing_nodes + [c.node for c in collected])
            planned_paths: dict[Path, str] = {}
            id_renames: dict[str, str] = {}
            plans: list[PlannedNodeWrite] = []
            synced_source_ids: set[str] = set()
            all_edges: list[Relationship] = []

            for c in collected:
                edges, unmatched = self._resolve_neighbors(c, identity_index, source_name, stats)
                if unmatched:
                    c.node.attributes["snmp"]["unmatched_neighbors"] = unmatched  # type: ignore[index]
                plan = self._plan_device(
                    paths, c.node, source_id_index, planned_paths, id_renames, existing_nodes, stats
                )
                if plan is None:
                    continue  # conflict: skip this node AND its edges
                plans.append(plan)
                synced_source_ids.add(plan.node.id)
                all_edges.extend(edges)

            if not plans and stats.errors:
                status = SyncStatus.FAILED
            elif stats.errors:
                status = SyncStatus.PARTIAL
            else:
                status = SyncStatus.SUCCESS

            if plans:  # empty-sync guard
                apply_node_writes(paths, plans)
                rewrite_reference_ids(paths, id_renames, stats.warnings)
                # A neighbor may resolve to a node that this same run relocated
                # (identity_index was built over the pre-relocation on-disk node,
                # whose file apply_node_writes just deleted). Repoint those edges
                # at the relocated ids so we never persist a dangling edge.
                all_edges = remap_edge_ids(all_edges, id_renames)
                self._save_relationships(paths, source_name, synced_source_ids, all_edges, stats)
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
                "relationships_created": stats.relationships_created,
                "partial_targets": stats.partial_targets,
                "errors": stats.errors,
                "warnings": stats.warnings,
            }
            write_yaml(source_file, config)
            record_sync_run(environment, project_slug, source_name, status, plans)

            message = self._message(stats, plans, len(collected))
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
        except Exception as e:  # noqa: BLE001 - any unexpected failure is a failed run
            duration_ms = int((time.monotonic() - start_time) * 1000)
            config["last_sync_status"] = "failed"
            config["last_sync_message"] = str(e)
            write_yaml(source_file, config)
            record_sync_run(environment, project_slug, source_name, SyncStatus.FAILED, [])
            return SyncResult(status=SyncStatus.FAILED, message=str(e), errors=[str(e)], duration_ms=duration_ms)

    def _resolve_neighbors(
        self, collected: _Collected, identity_index: dict[str, set[str]], source_name: str, stats: SyncStats
    ) -> tuple[list[Relationship], list[dict[str, str]]]:
        """Split a device's LLDP neighbors into matched edges and residue."""
        edges: list[Relationship] = []
        unmatched: list[dict[str, str]] = []
        for neighbor in collected.data.neighbors:
            match_id = match_neighbor(neighbor["remote_sysname"], identity_index)
            if match_id and match_id != collected.node.id:
                attrs: dict[str, str | int | bool] = {}
                if neighbor.get("local_port"):
                    attrs["local_port"] = neighbor["local_port"]
                if neighbor.get("remote_port"):
                    attrs["remote_port"] = neighbor["remote_port"]
                edges.append(
                    Relationship(
                        source=collected.node.id,
                        target=match_id,
                        type=RelationshipType.CONNECTS_TO,
                        managed_by=source_name,
                        attributes=attrs,
                    )
                )
            else:
                unmatched.append(neighbor)
        if unmatched:
            names = sorted({u["remote_sysname"] for u in unmatched if u["remote_sysname"]})
            hint = ", ".join(names) if names else "unnamed neighbor(s)"
            stats.warnings.append(
                f"{collected.node.id}: {len(unmatched)} LLDP neighbor(s) unmatched ({hint}); "
                "add them with 'ic describe node add' or /ic-collect, then re-sync to draw the edges."
            )
        return edges, unmatched

    @staticmethod
    def _message(stats: SyncStats, plans: list[PlannedNodeWrite], target_count: int) -> str:
        if not plans and stats.errors:
            return f"All SNMP targets failed to walk ({len(stats.errors)} error(s)); nothing written"
        if not plans:
            return "SNMP reported 0 importable devices; no node files were written (empty-sync guard)"
        written = stats.nodes_created + stats.nodes_updated
        message = f"Synced {written} device(s) from {target_count} SNMP target(s)"
        if stats.relationships_created:
            message += f", {stats.relationships_created} topology edge(s)"
        if stats.nodes_unchanged:
            message += f" ({stats.nodes_unchanged} unchanged)"
        if stats.partial_targets:
            message += f"; {stats.partial_targets} target(s) partial"
        return message
