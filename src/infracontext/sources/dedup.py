"""Import-time duplicate detection shared by the node importers.

When an importer (ssh-config sync, Proxmox sync, SOS import, kubectl import)
is about to *create* a node, it asks this module whether the incoming node's
identifiers (IPs, domains, ssh_alias) already belong to an existing node.
Matches are surfaced as warnings that point the operator at
``ic describe node consolidate`` -- detection only, importers never
auto-attach across source boundaries.

False-positive guards:

- Loopback IPs are ignored (every box has 127.0.0.1 / ::1).
- An identifier already shared by more than one existing node is ignored:
  a floating IP, VIP, or shared jump alias means shared identity, and shared
  identity never implies the same box.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from ipaddress import ip_address

from infracontext.models.node import Node
from infracontext.paths import ProjectPaths
from infracontext.storage import read_model


@dataclass(frozen=True)
class IdentifierOverlap:
    """An incoming identifier that already belongs to exactly one existing node."""

    existing_id: str  # node ID (type:slug) of the existing node
    identifier: str  # the shared identifier value
    kind: str  # "ip", "domain", or "ssh_alias"


def _is_loopback(value: str) -> bool:
    """True for loopback IPs (127.0.0.0/8, ::1); False for anything unparsable."""
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def load_existing_nodes(paths: ProjectPaths) -> list[Node]:
    """Load every readable node in the project (unreadable files are skipped).

    Importers call this once per run and pass the result to
    :func:`find_duplicate_candidates` for each node they are about to create.
    """
    nodes: list[Node] = []
    if not paths.nodes_dir.exists():
        return nodes
    for type_dir in sorted(paths.nodes_dir.iterdir()):
        if not type_dir.is_dir():
            continue
        for node_file in sorted(type_dir.glob("*.yaml")):
            try:
                node = read_model(node_file, Node)
            except Exception:
                continue  # a broken file must not abort duplicate detection
            if node:
                nodes.append(node)
    return nodes


def find_duplicate_candidates(
    existing_nodes: Sequence[Node],
    *,
    ips: Iterable[str] = (),
    domains: Iterable[str] = (),
    ssh_alias: str | None = None,
) -> list[IdentifierOverlap]:
    """Find existing nodes that share an identifier with an incoming node.

    Returns one :class:`IdentifierOverlap` per (identifier, existing node)
    pair. Loopback IPs and identifiers owned by more than one existing node
    (floating IP / VIP / shared jump alias) are ignored -- see module
    docstring.
    """
    # (kind, identifier) -> unique owning node IDs, in discovery order.
    owners: dict[tuple[str, str], list[str]] = {}

    def _add(kind: str, value: str, node_id: str) -> None:
        ids = owners.setdefault((kind, value), [])
        if node_id not in ids:
            ids.append(node_id)

    for node in existing_nodes:
        for ip in node.ip_addresses:
            _add("ip", ip, node.id)
        for domain in node.domains:
            _add("domain", domain, node.id)
        if node.ssh_alias:
            _add("ssh_alias", node.ssh_alias, node.id)

    queries: list[tuple[str, str]] = [("ip", ip) for ip in ips if ip and not _is_loopback(ip)]
    queries += [("domain", domain) for domain in domains if domain]
    if ssh_alias:
        queries.append(("ssh_alias", ssh_alias))

    overlaps: list[IdentifierOverlap] = []
    seen: set[tuple[str, str, str]] = set()
    for kind, value in queries:
        ids = owners.get((kind, value), [])
        if len(ids) != 1:
            continue  # unknown identifier, or shared identity -- never a duplicate signal
        key = (kind, value, ids[0])
        if key in seen:
            continue
        seen.add(key)
        overlaps.append(IdentifierOverlap(existing_id=ids[0], identifier=value, kind=kind))
    return overlaps


def overlap_warning(incoming_id: str, overlap: IdentifierOverlap) -> str:
    """Human-readable warning for one overlap, with the consolidate hint."""
    return (
        f"incoming '{incoming_id}' overlaps {overlap.existing_id} on "
        f"{overlap.identifier} ({overlap.kind}) -- consider: "
        f"ic describe node consolidate {overlap.existing_id} {incoming_id}"
    )
