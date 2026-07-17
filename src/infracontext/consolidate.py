"""Duplicate-node consolidation: merge one node into another, rewrite references.

Pure logic behind ``ic describe node consolidate <dest> <src>``. The CLI verb
in :mod:`infracontext.cli.describe` handles resolution, refusals, writes, and
output; everything here operates on in-memory models/dicts so it is directly
unit-testable and trivially supports ``--dry-run``.

Merge semantics (src merged into dest):

- Scalar fields are fill-only: dest wins, src only fills empty fields.
- ``first_seen`` keeps the earlier ISO date.
- ``ip_addresses`` / ``domains`` / ``source_paths`` are unioned (dest order
  first), ``attributes`` fill-only per key, ``triage.services`` unioned.
- ``learnings`` append with dedupe by (date, finding); ``observability``,
  ``endpoints``, and ``functions`` append with dedupe by full equality.

Reference rewrite: every edge in relationships.yaml and every chain member in
chains.yaml pointing at src is rewritten to dest (both the bare ``type:slug``
and the self-qualified ``@project:type:slug`` forms match); inbound
``@project:...`` references from *other* local projects are rewritten too
(``file_project``). Only rewrites that produce self-edges or duplicate edges
are dropped -- pre-existing duplicates elsewhere in the file are left for
``ic doctor``. Within touched chains only duplicates the rewrite CREATED are
collapsed; chains (and hops) the rewrite never touched are never altered.
Local override keys transfer by *effective entry*: the scoped form wins
wholly at lookup, so exactly one src entry (scoped if present, else global)
moves to dest, confined to the project-scoped dest key whenever the global
form would leak onto another project's same-ID node (see
:func:`plan_override_remap`).
"""

import copy
from collections.abc import MutableMapping
from dataclasses import dataclass

from infracontext.models.chain import ChainFile
from infracontext.models.node import Node
from infracontext.models.relationship import RelationshipFile, parse_node_ref

# Fields merged fill-only: dest wins, src fills only empty (None) fields.
_FILL_ONLY_SCALARS = ("description", "notes", "ssh_alias", "source_id", "source", "managed_by")
# List-of-string fields merged by order-preserving union (dest entries first).
_UNION_LISTS = ("ip_addresses", "domains", "source_paths")
# Model-list fields appended with dedupe by full equality.
_APPEND_MODEL_LISTS = ("endpoints", "functions", "observability")
# Overridable fields in .infracontext.local.yaml entries (see overrides.NodeOverrides).
_OVERRIDE_FIELDS = ("ssh_alias", "source_paths")


def _union(dest: list[str], src: list[str]) -> list[str]:
    """Order-preserving union: dest entries first, then new src entries."""
    return [*dest, *(item for item in src if item not in dest)]


def merge_nodes(dest: Node, src: Node) -> tuple[Node, list[str]]:
    """Merge ``src`` into ``dest`` (see module docstring for the semantics).

    Returns the merged node and a list of human-readable descriptions of the
    fields that actually changed (empty when dest already had everything).
    Identity fields (``id``/``slug``/``type``/``name``/``version``) always
    stay dest's.
    """
    updates: dict = {}
    changed: list[str] = []

    for field_name in _FILL_ONLY_SCALARS:
        if getattr(dest, field_name) is None and getattr(src, field_name) is not None:
            updates[field_name] = getattr(src, field_name)
            changed.append(field_name)

    # first_seen: keep the earlier date (ISO strings compare lexicographically).
    if src.first_seen and (dest.first_seen is None or src.first_seen < dest.first_seen):
        updates["first_seen"] = src.first_seen
        changed.append("first_seen")

    for field_name in _UNION_LISTS:
        merged = _union(getattr(dest, field_name), getattr(src, field_name))
        if merged != getattr(dest, field_name):
            updates[field_name] = merged
            changed.append(f"{field_name} (+{len(merged) - len(getattr(dest, field_name))})")

    new_attrs = {key: value for key, value in src.attributes.items() if key not in dest.attributes}
    if new_attrs:
        updates["attributes"] = {**dest.attributes, **new_attrs}
        changed.append(f"attributes (+{len(new_attrs)})")

    # learnings: append with dedupe by (date, finding).
    seen_learnings = {(ln.date, ln.finding) for ln in dest.learnings}
    added_learnings = []
    for learning in src.learnings:
        key = (learning.date, learning.finding)
        if key in seen_learnings:
            continue
        seen_learnings.add(key)
        added_learnings.append(learning)
    if added_learnings:
        updates["learnings"] = [*dest.learnings, *added_learnings]
        changed.append(f"learnings (+{len(added_learnings)})")

    for field_name in _APPEND_MODEL_LISTS:
        dest_items = getattr(dest, field_name)
        added = [item for item in getattr(src, field_name) if item not in dest_items]
        if added:
            updates[field_name] = [*dest_items, *added]
            changed.append(f"{field_name} (+{len(added)})")

    if src.triage is not None:
        if dest.triage is None:
            updates["triage"] = src.triage.model_copy(deep=True)
            changed.append("triage")
        else:
            triage_updates: dict = {}
            services = _union(dest.triage.services, src.triage.services)
            if services != dest.triage.services:
                triage_updates["services"] = services
            for field_name in ("context", "tier", "collector_script"):
                if getattr(dest.triage, field_name) is None and getattr(src.triage, field_name) is not None:
                    triage_updates[field_name] = getattr(src.triage, field_name)
            if triage_updates:
                updates["triage"] = dest.triage.model_copy(update=triage_updates)
                changed.append(f"triage ({', '.join(sorted(triage_updates))})")

    return dest.model_copy(update=updates), changed


def _ref_key(ref: str, project: str) -> tuple[str, str]:
    """Canonical (project, node_id) identity of a ref; raw fallback if malformed."""
    try:
        return parse_node_ref(ref, project)
    except ValueError:
        return ("", ref)


@dataclass
class RelationshipRewrite:
    """Counts from rewriting relationships.yaml."""

    rewritten: int = 0  # edges with at least one endpoint rewritten
    self_edges_dropped: int = 0  # edges collapsed onto dest by the rewrite
    duplicates_removed: int = 0  # (source, type, target) duplicates after the rewrite


def rewrite_relationship_refs(
    rel_file: RelationshipFile, *, project: str, src_id: str, dest_id: str, file_project: str | None = None
) -> RelationshipRewrite:
    """Rewrite every edge endpoint pointing at ``src_id`` to ``dest_id``.

    Mutates ``rel_file`` in place. ``project`` is the project the src/dest
    nodes live in; ``file_project`` is the project owning this
    relationships.yaml (defaults to ``project``). When they differ, matches
    are inbound cross-project references and the rewrite keeps the qualified
    ``@project:...`` form. Refs pointing at other projects/roots never match.

    Dropping is scoped to rewritten edges only: an edge the rewrite collapsed
    onto itself (an existing src<->dest edge) or made identical to another
    edge by (source, type, target) is dropped -- pre-existing duplicates
    between untouched edges are left alone (``ic doctor`` reports those).
    """
    file_project = file_project if file_project is not None else project
    dest_ref = dest_id if file_project == project else f"@{project}:{dest_id}"
    target = (project, src_id)

    result = RelationshipRewrite()
    touched_flags: list[bool] = []
    for rel in rel_file.relationships:
        touched = False
        if _ref_key(rel.source, file_project) == target:
            rel.source = dest_ref
            touched = True
        if _ref_key(rel.target, file_project) == target:
            rel.target = dest_ref
            touched = True
        if touched:
            result.rewritten += 1
        touched_flags.append(touched)

    untouched_keys = {
        (rel.source, str(rel.type), rel.target)
        for rel, touched in zip(rel_file.relationships, touched_flags, strict=True)
        if not touched
    }
    kept = []
    seen_rewritten: set[tuple[str, str, str]] = set()
    for rel, touched in zip(rel_file.relationships, touched_flags, strict=True):
        if not touched:
            kept.append(rel)
            continue
        if _ref_key(rel.source, file_project) == _ref_key(rel.target, file_project):
            result.self_edges_dropped += 1
            continue
        key = (rel.source, str(rel.type), rel.target)
        if key in untouched_keys or key in seen_rewritten:
            result.duplicates_removed += 1
            continue
        seen_rewritten.add(key)
        kept.append(rel)
    rel_file.relationships = kept
    return result


@dataclass
class ChainRewrite:
    """Counts from rewriting chains.yaml."""

    members_rewritten: int = 0
    chains_removed: int = 0  # chains that collapsed below two members


def rewrite_chain_members(
    chain_file: ChainFile, *, project: str, src_id: str, dest_id: str, file_project: str | None = None
) -> ChainRewrite:
    """Rewrite chain members pointing at ``src_id`` to ``dest_id``.

    Mutates ``chain_file`` in place. ``file_project`` names the project owning
    this chains.yaml (defaults to ``project``, see
    :func:`rewrite_relationship_refs`). Consecutive duplicate members created
    by the rewrite are collapsed (the first hop keeps its ``via``); a chain
    left with fewer than two members no longer describes a path and is
    removed. Both apply ONLY to chains the rewrite touched: pre-existing
    consecutive duplicates or degenerate chains elsewhere in the file are
    unrelated state and stay exactly as they are (``ic doctor``'s business).
    """
    file_project = file_project if file_project is not None else project
    dest_ref = dest_id if file_project == project else f"@{project}:{dest_id}"
    target = (project, src_id)

    result = ChainRewrite()
    kept_chains = []
    for chain in chain_file.chains:
        rewritten_flags = []
        for member in chain.members:
            was_rewritten = _ref_key(member.id, file_project) == target
            if was_rewritten:
                member.id = dest_ref
                result.members_rewritten += 1
            rewritten_flags.append(was_rewritten)
        if not any(rewritten_flags):
            kept_chains.append(chain)
            continue
        # Collapse only duplicates the rewrite CREATED, judged on ORIGINAL
        # adjacencies: pair (i-1, i) drops member i (the first hop keeps its
        # via) only when the two members are _ref_key-identical AND one of
        # THOSE TWO was rewritten. Rewritten status never propagates across
        # dropped members, so a longer pre-existing canonical-duplicate run
        # bordering the rewrite survives beyond the one pair the rewrite
        # actually created.
        keys = [_ref_key(member.id, file_project) for member in chain.members]
        drop = [False] * len(chain.members)
        for i in range(1, len(chain.members)):
            if (rewritten_flags[i - 1] or rewritten_flags[i]) and keys[i - 1] == keys[i]:
                drop[i] = True
        kept_members = [m for m, dropped in zip(chain.members, drop, strict=True) if not dropped]
        # A drop can glue two STRING-identical members together (e.g.
        # [vm:src, @prod:vm:dest, vm:dest] -> [vm:dest, vm:dest]). That
        # adjacency can never pre-exist -- the Chain validator forbids it --
        # so it is always a rewrite artifact, and writing it would make the
        # file fail validation on the next load. Collapse it.
        deduped = [kept_members[0]]
        for member in kept_members[1:]:
            if member.id == deduped[-1].id:
                continue
            deduped.append(member)
        chain.members = deduped
        if len(chain.members) < 2:
            result.chains_removed += 1
            continue
        kept_chains.append(chain)
    chain_file.chains = kept_chains
    return result


def plan_override_remap(
    nodes: dict,
    *,
    project: str,
    src_id: str,
    dest_id: str,
    src_in_other_projects: bool = False,
    dest_in_other_projects: bool = False,
) -> list[tuple[str, str, str | None]]:
    """Which local-override keys must change, as (action, old_key, new_key) ops.

    Override keys exist in BOTH forms: global (``vm:web-01``) and
    project-scoped (``prod/vm:web-01``); at lookup the scoped entry wins
    WHOLLY (no field-level fallback to the global one, see
    ``overrides.get_node_overrides``). The plan mirrors those semantics --
    exactly ONE src entry transfers to dest: the entry that was *effective*
    for src in this project (scoped if present, else global). Field-merging
    both forms would activate global-only fields that the scoped entry
    shadowed before the consolidation, silently applying another project's
    values here.

    - The effective entry lands on the scoped dest key when it must not leak:
      always for a scoped src entry, and for a global one when another
      project has its own node with ``dest_id`` (``dest_in_other_projects``).
    - A global src entry transfers as a copy when another project still has a
      node with ``src_id`` (``src_in_other_projects``) -- moving it would
      strip that project's override.
    - A global src entry *shadowed* by a scoped one stays untouched when
      other projects still need it, and is deleted otherwise (dead key; its
      fields were inactive here, so transferring them would be a leak).
    - When the transfer lands on a scoped dest key that does not exist while
      a global dest entry does, that entry seeds the scoped key first: dest's
      own values must keep winning under scoped-shadows-global lookup, src
      only fills gaps.

    Keys scoped to *other* projects refer to that project's node of the same
    ID and are always left alone.

    Actions: ``move`` pops old and fill-only merges into new; ``copy`` does
    the same from a deep copy, keeping old; ``delete`` removes old
    (new is None).
    """
    scoped_src = f"{project}/{src_id}"
    scoped_dest = f"{project}/{dest_id}"
    plan: list[tuple[str, str, str | None]] = []

    if scoped_src in nodes:
        plan.append(("move", scoped_src, scoped_dest))
        if src_id in nodes and not src_in_other_projects:
            plan.append(("delete", src_id, None))
    elif src_id in nodes:
        target = scoped_dest if dest_in_other_projects else dest_id
        plan.append(("copy" if src_in_other_projects else "move", src_id, target))

    lands_on_scoped_dest = any(new == scoped_dest for _, _, new in plan)
    if lands_on_scoped_dest and scoped_dest not in nodes and dest_id in nodes:
        plan.insert(0, ("copy", dest_id, scoped_dest))
    return plan


def apply_override_remap(nodes: MutableMapping, remap: list[tuple[str, str, str | None]]) -> None:
    """Apply a remap plan to the raw ``nodes`` mapping of the overrides file.

    ``copy`` uses a deep copy so ruamel never emits YAML anchors for the
    shared entry. When a move/copy destination key already exists, dest wins
    and the incoming entry only fills its missing fields (same fill-only
    semantics as the node merge).
    """
    for action, old, new in remap:
        if action == "delete":
            nodes.pop(old, None)
            continue
        entry = copy.deepcopy(nodes[old]) if action == "copy" else nodes.pop(old)
        existing = nodes.get(new)
        if existing is None:
            nodes[new] = entry
            continue
        if isinstance(entry, MutableMapping) and isinstance(existing, MutableMapping):
            for field_name in _OVERRIDE_FIELDS:
                if existing.get(field_name) is None and entry.get(field_name) is not None:
                    existing[field_name] = entry[field_name]
