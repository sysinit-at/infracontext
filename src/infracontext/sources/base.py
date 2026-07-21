"""Base class for infrastructure source plugins."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from infracontext.models.node import Node, Observability
from infracontext.models.relationship import Relationship
from infracontext.paths import EnvironmentPaths, ProjectPaths
from infracontext.runs import write_run_record
from infracontext.storage import update_yaml, write_model

log = logging.getLogger(__name__)


class SyncStatus(StrEnum):
    """Status of a sync operation."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass
class SyncResult:
    """Result of a sync operation."""

    status: SyncStatus
    message: str = ""
    nodes_created: int = 0
    nodes_updated: int = 0
    nodes_deleted: int = 0
    nodes_unchanged: int = 0
    relationships_created: int = 0
    relationships_deleted: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0


class NodeChange(StrEnum):
    """How a sync run classified one node it saw (run-record vocabulary)."""

    CREATED = "created"
    UPDATED = "updated"
    CONFIRMED_UNCHANGED = "confirmed_unchanged"


@dataclass
class PlannedNodeWrite:
    """A node write a sync plugin intends to perform.

    Plugins plan all writes first and apply them only when the whole run
    succeeded and was non-empty (the sync guard): a failed, partial, or empty
    sync must never rewrite node files, so incomplete source data can't
    clobber good on-disk data.
    """

    node: Node
    node_file: Path
    change: NodeChange
    old_file_to_delete: Path | None = None  # rename case: stale file at the old slug


def apply_node_writes(paths: ProjectPaths, plans: list[PlannedNodeWrite]) -> None:
    """Apply planned node writes; CONFIRMED_UNCHANGED plans touch nothing."""
    for plan in plans:
        if plan.change is NodeChange.CONFIRMED_UNCHANGED:
            continue
        paths.node_type_dir(plan.node.type).mkdir(parents=True, exist_ok=True)
        write_model(plan.node_file, plan.node)
        if plan.old_file_to_delete and plan.old_file_to_delete.exists():
            plan.old_file_to_delete.unlink()


def record_sync_run(
    environment: EnvironmentPaths,
    project: str,
    source: str,
    status: SyncStatus,
    plans: list[PlannedNodeWrite],
) -> None:
    """Append a run record for a completed sync (never raises).

    The record lists the node IDs the source *reported*, classified by
    ``NodeChange`` -- for guarded runs (failed/partial/empty) it documents the
    observation even though nothing was written. A failing record write must
    not mask the sync result, so errors are logged and swallowed.
    """
    by_change: dict[NodeChange, list[str]] = {change: [] for change in NodeChange}
    for plan in plans:
        by_change[plan.change].append(plan.node.id)
    try:
        write_run_record(
            environment,
            project=project,
            source=source,
            status=str(status),
            created=by_change[NodeChange.CREATED],
            updated=by_change[NodeChange.UPDATED],
            confirmed_unchanged=by_change[NodeChange.CONFIRMED_UNCHANGED],
        )
    except Exception as e:
        log.warning("Could not write run record for source '%s' (project '%s'): %s", source, project, e)


class SourceConfig(BaseModel):
    """Base configuration for a source plugin."""

    version: str = "2.0"
    name: str
    type: str
    status: str = "configured"

    model_config = {"extra": "allow"}


class SourcePlugin(ABC):
    """Abstract base class for infrastructure source plugins.

    Source plugins are responsible for:
    1. Connecting to external infrastructure (Proxmox, K8s, cloud APIs, etc.)
    2. Discovering nodes and their relationships
    3. Syncing that data into the local YAML store
    """

    source_type: str  # Subclasses must set this (e.g., source_type = "proxmox")

    @abstractmethod
    def validate_config(self, config: dict) -> list[str]:
        """Validate source configuration.

        Args:
            config: The source configuration dictionary

        Returns:
            List of validation error messages (empty if valid)
        """
        ...

    @abstractmethod
    async def test_connection(self, config: dict) -> tuple[bool, str]:
        """Test connection to the source.

        Args:
            config: The source configuration dictionary

        Returns:
            Tuple of (success, message)
        """
        ...

    def generate_source_id(self, *parts: str) -> str:
        """Generate a stable source ID from parts.

        Example: generate_source_id("cluster1", "qemu", "100") -> "proxmox:cluster1:qemu:100"
        """
        return f"{self.source_type}:{':'.join(parts)}"

    def parse_source_id(self, source_id: str) -> list[str]:
        """Parse a source ID into its parts.

        Example: parse_source_id("proxmox:cluster1:qemu:100") -> ["cluster1", "qemu", "100"]
        """
        parts = source_id.split(":")
        if parts[0] != self.source_type:
            raise ValueError(f"Source ID {source_id} is not from {self.source_type}")
        return parts[1:]


def merge_synced_node(new_node: Node, existing: Node, *, preserve_ssh_alias: bool) -> Node:
    """Merge a freshly-synced node with an existing one, preserving manual edits.

    Source-managed fields come from ``new_node``; manually-managed fields are
    kept from ``existing`` so a re-sync never clobbers operator additions.

    Args:
        new_node: The node as the source currently reports it.
        existing: The node already on disk.
        preserve_ssh_alias: When True, keep the existing ``ssh_alias`` (the
            source doesn't manage SSH connectivity -- e.g. Proxmox). When
            False, take the new ``ssh_alias`` (the source *is* an SSH config,
            so the alias is authoritative from the source).

    The preserved field set (``domains``, ``description``, ``notes``,
    ``source_paths``, ``endpoints``, ``functions``, ``observability``,
    ``triage``, ``learnings``) matches what both the Proxmox and SSH-config
    plugins previously hard-coded, so this is a behaviour-preserving
    consolidation of those two copies.

    The merge is a ``model_copy`` of ``existing`` (never a fresh ``Node``):
    manual fields, ``first_seen`` (write-once -- absent stays absent, no mass
    rewrite), and the unknown-field stash ``read_model`` attached for
    newer-schema round-trips all ride along, so a sync rewrite never deletes
    fields written by a newer infracontext version.
    """
    updates: dict = {
        # Identity + source-managed fields come from the fresh sync.
        "version": new_node.version,
        "id": new_node.id,
        "slug": new_node.slug,
        "type": new_node.type,
        "name": new_node.name,
        "ip_addresses": new_node.ip_addresses,
        "attributes": new_node.attributes,
        "source_id": new_node.source_id,
        "source": new_node.source,
        "managed_by": new_node.managed_by,
    }
    # ssh_alias is source-managed for ssh_config but manual for proxmox.
    if not preserve_ssh_alias:
        updates["ssh_alias"] = new_node.ssh_alias
    return existing.model_copy(update=updates)


def ensure_source_observability(node: Node, fresh: Observability) -> Node:
    """Add or refresh the sync source's own observability entry on ``node``.

    ``merge_synced_node`` preserves ``observability`` from the existing node
    (it is a manual field), so a source that attaches its query endpoint must
    reconcile its entry *after* the merge. Ownership is the ``source`` field:

    - an entry with ``type == fresh.type`` and ``source == fresh.source``
      belongs to this sync source and is REPLACED when it differs (a changed
      BMC URL or target host must not leave ``ic query`` pointed at the old
      endpoint);
    - no entry of that type at all -> ``fresh`` is appended;
    - an entry of that type owned by someone else (different or absent
      ``source``) is manual/foreign and is left alone.

    ``fresh`` must carry ``source`` (the sync source's name); entries written
    without it cannot be distinguished from manual ones and are never touched.
    Returns ``node`` unchanged (same instance) when nothing needs to change.
    """
    if not fresh.source:
        raise ValueError("ensure_source_observability requires fresh.source to be set")
    entries = list(node.observability)
    owned = [
        i for i, o in enumerate(entries) if o.type == fresh.type and o.source == fresh.source
    ]
    if owned:
        i = owned[0]
        if entries[i] == fresh:
            return node
        entries[i] = fresh
        return node.model_copy(update={"observability": entries})
    if any(o.type == fresh.type for o in entries):
        return node
    return node.model_copy(update={"observability": [*entries, fresh]})


def remap_edge_ids(edges: list[Relationship], id_renames: dict[str, str]) -> list[Relationship]:
    """Repoint each edge's source/target through ``id_renames`` (relocations).

    A no-op when nothing was relocated. Mirrors :func:`rewrite_reference_ids`'s
    on-disk rewrite for the in-memory edges a sync run resolved, so an edge
    that matched a node relocated in this same sync lands on the new id.
    """
    if not id_renames:
        return edges
    return [
        edge.model_copy(
            update={
                "source": id_renames.get(edge.source, edge.source),
                "target": id_renames.get(edge.target, edge.target),
            }
        )
        for edge in edges
    ]


def rewrite_reference_ids(paths: ProjectPaths, id_renames: dict[str, str], warnings: list[str]) -> None:
    """Repoint relationships/chains at relocated node ids (never raises).

    A relocation changes a node id (rename or ``vm:x`` -> ``network_device:x``);
    without this, manual edges and chain members referencing the old id would
    silently dangle once the old file is deleted. Only this project's
    ``relationships.yaml`` and ``chains.yaml`` are rewritten -- qualified
    cross-project/root references cannot be safely edited from here and are
    left for ``ic doctor`` to flag.

    Safety properties: edits go through :func:`update_yaml` (locked, atomic,
    comment-preserving -- a plain read/rewrite would strip hand-written
    comments); files that don't mention any old id are not touched at all; and
    no exception may escape -- node writes are already applied by the time
    this runs, so a broken reference file must degrade to a warning (stale ids
    are ``ic doctor``'s to flag), never abort the half-recorded run.

    ``warnings`` is the sync's warning sink (e.g. ``stats.warnings``) --
    passed as a list so this helper stays decoupled from the per-plugin
    ``SyncStats`` dataclasses.
    """
    if not id_renames:
        return
    rewritten = 0

    def _rewrite_relationships(data: dict) -> bool:
        nonlocal rewritten
        changed = 0
        for rel in data.get("relationships") or []:
            if not isinstance(rel, dict):
                continue
            for key in ("source", "target"):
                if rel.get(key) in id_renames:
                    rel[key] = id_renames[rel[key]]
                    changed += 1
        rewritten += changed
        # False vetoes the write: the substring gate below is only a
        # heuristic (an id can be a prefix of an unrelated id), and an
        # untouched file must not be reformatted.
        return bool(changed)

    def _rewrite_chains(data: dict) -> bool:
        nonlocal rewritten
        changed = 0
        for chain in data.get("chains") or []:
            if not isinstance(chain, dict):
                continue
            members = chain.get("members")
            if not isinstance(members, list):
                continue
            for i, member in enumerate(members):
                if isinstance(member, str) and member in id_renames:
                    members[i] = id_renames[member]
                    changed += 1
                elif isinstance(member, dict) and member.get("id") in id_renames:
                    member["id"] = id_renames[member["id"]]
                    changed += 1
        rewritten += changed
        return bool(changed)

    try:
        for path, updater in (
            (paths.relationships_yaml, _rewrite_relationships),
            (paths.chains_yaml, _rewrite_chains),
        ):
            if not path.exists():
                continue
            # Cheap gate: skip (and never reformat) files that don't
            # mention any relocated id.
            text = path.read_text(encoding="utf-8")
            if not any(old_id in text for old_id in id_renames):
                continue
            update_yaml(path, updater)
    except Exception as e:
        warnings.append(
            f"Could not rewrite references to relocated node ids ({e}); stale ids may remain in "
            "relationships.yaml/chains.yaml — run 'ic doctor' and fix manually."
        )
        return

    if rewritten:
        warnings.append(f"Rewrote {rewritten} relationship/chain reference(s) to relocated node ids")
