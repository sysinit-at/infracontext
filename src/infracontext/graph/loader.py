"""Load nodes and relationships into a NetworkX graph."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    import networkx as nx

from infracontext.federation import (
    LOCAL_ROOT_ALIAS,
    all_roots,
    get_root,
    resolve_node_ref,
)
from infracontext.models.node import Node
from infracontext.models.relationship import (
    Relationship,
    RelationshipFile,
    is_cross_project_ref,
    parse_node_ref,
)
from infracontext.paths import EnvironmentPaths, ProjectPaths, list_projects
from infracontext.storage import StorageError, read_model

log = logging.getLogger(__name__)


def _first_error_detail(e: ValidationError) -> str:
    """Summarize a ValidationError by its first error (location + message)."""
    first = e.errors()[0] if e.errors() else None
    if not first:
        return "validation error"
    return f"validation error at {'.'.join(str(p) for p in first['loc'])}: {first['msg']}"


def _load_node_safe(node_file) -> Node | None:  # type: ignore[no-untyped-def]
    """Read a node YAML, returning None and warning on parse/schema errors.

    A single corrupt file shouldn't abort a whole graph/list load -- those
    commands are needed precisely *to diagnose* a broken state. Errors are
    logged with the file path and a pointer to ``ic doctor`` (which reports
    the same failures precisely). The caller treats None as "skip".
    """
    try:
        return read_model(node_file, Node)
    except StorageError as e:
        log.warning("Skipping %s: %s -- run 'ic doctor' for details", node_file, e)
        return None
    except ValidationError as e:
        detail = _first_error_detail(e)
        log.warning("Skipping %s: %s -- run 'ic doctor' for details", node_file, detail)
        return None


def _load_relationships_safe(rel_path) -> RelationshipFile | None:  # type: ignore[no-untyped-def]
    """Read a relationships YAML with the same skip-and-warn semantics as
    :func:`_load_node_safe` -- a corrupt relationships file degrades the graph
    to nodes-only instead of aborting the whole load.
    """
    try:
        return read_model(rel_path, RelationshipFile)
    except StorageError as e:
        log.warning(
            "Skipping relationships in %s: %s -- run 'ic doctor' for details", rel_path, e
        )
        return None
    except ValidationError as e:
        detail = _first_error_detail(e)
        log.warning(
            "Skipping relationships in %s: %s -- run 'ic doctor' for details", rel_path, detail
        )
        return None


def _qualify(root_alias: str, project_slug: str, node_id: str) -> str:
    """Build the in-graph qualified ID for a node.

    Local-root nodes use the existing ``project/type:slug`` format to preserve
    backward compatibility. External-root nodes are prefixed with
    ``@<alias>:`` so the qualified ID is self-describing.
    """
    if root_alias == LOCAL_ROOT_ALIAS:
        return f"{project_slug}/{node_id}"
    return f"@{root_alias}:{project_slug}/{node_id}"


def _resolve_cross_project_node(
    ref: str,
    project_slug: str,
    graph: nx.DiGraph,
    origin_root: str = LOCAL_ROOT_ALIAS,
) -> str | None:
    """Resolve a node reference within a load_graph pass.

    ``origin_root`` is the root the *current* graph load is rooted in. It
    controls how unqualified and ``@project:`` refs are interpreted:

    - Unqualified ``type:slug``       -> origin_root, current project (bare ID).
    - ``@other-project:type:slug``    -> origin_root, that other project.
    - ``@<external-alias>:type:slug`` -> the external root's active project.

    The graph ID format mirrors that distinction:

    - Same-root same-project nodes are stored under their bare ``type:slug``.
    - Same-root cross-project nodes are stored as ``project/type:slug`` (when
      origin is local) or ``@origin:project/type:slug`` (when origin is
      external).
    - Cross-root nodes are stored as ``@alias:project/type:slug``.

    Returns the graph node ID used for the resolved node, or None if it
    cannot be found.
    """
    resolved = resolve_node_ref(ref, default_project=project_slug)

    # Re-interpret "local" scope relative to the origin root. resolve_node_ref
    # always returns LOCAL_ROOT_ALIAS for scopes that aren't external-root
    # aliases, but when load_graph is rooted in an external root, an
    # unqualified or `@project:` ref means "same external root", not "local".
    effective_root = origin_root if resolved.root_alias == LOCAL_ROOT_ALIAS else resolved.root_alias

    # Same root, same project -> bare type:slug to match how the current-project
    # nodes were registered by load_graph.
    if effective_root == origin_root and resolved.project == project_slug:
        return resolved.node_id

    graph_id = _qualify(effective_root, resolved.project, resolved.node_id)
    if not graph.has_node(graph_id):
        node = load_node(
            resolved.project, resolved.node_id, root_alias=effective_root
        )
        if node is None:
            return None
        attrs: dict[str, object] = {
            "node": node,
            "name": node.name,
            "type": node.type,
            "project": resolved.project,
        }
        if effective_root != LOCAL_ROOT_ALIAS:
            attrs["root"] = effective_root
        graph.add_node(graph_id, **attrs)

    return graph_id


def load_graph(project_slug: str, root_alias: str = LOCAL_ROOT_ALIAS) -> nx.DiGraph:
    """Load all nodes and relationships for a (root, project) into a digraph.

    Nodes are stored as graph nodes with their full data as attributes.
    Relationships are stored as edges with relationship type and metadata.

    Args:
        project_slug: The project to load.
        root_alias: Root containing the project. ``""`` (default) is the
            local root; pass an external root alias to load the graph from
            an external repo so node_context / triage of an external node
            sees its real relationships, project config, and access tier.

    Returns:
        A NetworkX DiGraph with nodes and edges.
    """
    import networkx as nx

    paths = _build_paths(project_slug, root_alias)
    graph = nx.DiGraph()
    if paths is None:
        return graph

    # Load all nodes from this project
    if paths.nodes_dir.exists():
        for type_dir in paths.nodes_dir.iterdir():
            if not type_dir.is_dir():
                continue
            for node_file in type_dir.glob("*.yaml"):
                node = _load_node_safe(node_file)
                if node:
                    graph.add_node(
                        node.id,
                        node=node,
                        name=node.name,
                        type=node.type,
                    )

    # Load relationships, resolving cross-project refs in the context of
    # *this* root rather than always falling back to the local root.
    rel_file = _load_relationships_safe(paths.relationships_yaml)
    if rel_file:
        for rel in rel_file.relationships:
            if is_cross_project_ref(rel.source) or is_cross_project_ref(rel.target):
                source_id = _resolve_cross_project_node(
                    rel.source, project_slug, graph, origin_root=root_alias
                )
                target_id = _resolve_cross_project_node(
                    rel.target, project_slug, graph, origin_root=root_alias
                )
                if source_id is None or target_id is None:
                    continue
            else:
                source_id = rel.source
                target_id = rel.target

            if graph.has_node(source_id) and graph.has_node(target_id):
                graph.add_edge(
                    source_id,
                    target_id,
                    relationship=rel,
                    type=rel.type,
                    description=rel.description,
                )

    return graph


def load_node_neighborhood(
    project_slug: str,
    node_id: str,
    depth: int = 2,
    root_alias: str = LOCAL_ROOT_ALIAS,
) -> nx.DiGraph:
    """Load only the nodes within ``depth`` hops of ``node_id`` into a digraph.

    A targeted alternative to :func:`load_graph` for the single-node context
    path (``ic ctx`` / ``ic describe node context``): it reads
    ``relationships.yaml`` once, walks the local edge list to find the node
    IDs within ``depth`` hops in *both* directions (successors for upstream,
    predecessors for downstream -- mirroring :func:`get_upstream` /
    :func:`get_downstream`), and parses only those node files instead of every
    node in the project. On a large project this turns an O(all nodes) parse
    into O(neighborhood).

    The restricted graph yields byte-for-byte identical upstream/downstream
    sets and dependency names to :func:`load_graph` for the same
    ``max_depth``: the collected ball is exactly the set those queries would
    traverse, and every edge among ball members is preserved.

    Cross-project / cross-root ``@``-references can only be resolved by
    loading the other roots' nodes, which is what :func:`load_graph` already
    does. When any relationship in the project carries such a reference this
    falls back to the full load so federated context stays correct --
    correctness beats speed.

    Returns an empty graph if the project or its paths cannot be resolved.
    """
    import networkx as nx

    graph = nx.DiGraph()
    paths = _build_paths(project_slug, root_alias)
    if paths is None:
        return graph

    rel_file = _load_relationships_safe(paths.relationships_yaml)
    relationships = rel_file.relationships if rel_file else []

    # Any cross-project ref means we can't stay local; defer to the full load.
    if any(
        is_cross_project_ref(rel.source) or is_cross_project_ref(rel.target)
        for rel in relationships
    ):
        return load_graph(project_slug, root_alias=root_alias)

    # Directed adjacency from the local edge list. Edge convention mirrors
    # load_graph: ``source -> target`` ("source depends on target").
    successors: dict[str, set[str]] = {}
    predecessors: dict[str, set[str]] = {}
    for rel in relationships:
        successors.setdefault(rel.source, set()).add(rel.target)
        predecessors.setdefault(rel.target, set()).add(rel.source)

    def _ball(adjacency: dict[str, set[str]]) -> set[str]:
        """BFS the `depth`-hop ball from node_id, matching get_upstream/down."""
        visited: set[str] = set()
        current = {node_id}
        for _ in range(depth):
            nxt: set[str] = set()
            for n in current:
                for neighbor in adjacency.get(n, ()):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        nxt.add(neighbor)
            current = nxt
        return visited

    wanted = {node_id} | _ball(successors) | _ball(predecessors)

    # Parse only the wanted node files.
    for nid in wanted:
        node = load_node(project_slug, nid, root_alias=root_alias)
        if node:
            graph.add_node(node.id, node=node, name=node.name, type=node.type)

    # Preserve every edge whose endpoints both landed in the neighborhood,
    # carrying the same edge attributes load_graph attaches.
    for rel in relationships:
        if graph.has_node(rel.source) and graph.has_node(rel.target):
            graph.add_edge(
                rel.source,
                rel.target,
                relationship=rel,
                type=rel.type,
                description=rel.description,
            )

    return graph


def _build_paths(project_slug: str, root_alias: str) -> ProjectPaths | None:
    """Build ProjectPaths for ``(root_alias, project_slug)`` or return None.

    For the local root, calls :func:`ProjectPaths.for_project` *without* the
    ``environment`` kwarg so test monkey-patches that don't accept it keep
    working. For external roots, looks up the root's environment first;
    returns None if the alias is not configured.
    """
    if root_alias == LOCAL_ROOT_ALIAS:
        try:
            return ProjectPaths.for_project(project_slug)
        except Exception:
            return None

    root = get_root(root_alias)
    if root is None:
        return None
    try:
        return ProjectPaths.for_project(project_slug, environment=root.environment)
    except Exception:
        return None


def load_node(project_slug: str, node_id: str, root_alias: str = LOCAL_ROOT_ALIAS) -> Node | None:
    """Load a single node by ID.

    Args:
        project_slug: The project
        node_id: Node ID in format type:slug
        root_alias: External root alias, or ``""`` for the local root.

    Returns:
        The Node or None if not found
    """
    if ":" not in node_id:
        return None

    paths = _build_paths(project_slug, root_alias)
    if paths is None:
        return None

    node_type, slug = node_id.split(":", 1)
    try:
        node_file = paths.node_file(node_type, slug)
    except ValueError:
        return None

    if not node_file.exists():
        return None

    return _load_node_safe(node_file)


def load_all_nodes(project_slug: str, root_alias: str = LOCAL_ROOT_ALIAS) -> list[Node]:
    """Load all nodes for a project.

    Args:
        project_slug: The project
        root_alias: External root alias, or ``""`` for the local root.

    Returns:
        List of all nodes
    """
    paths = _build_paths(project_slug, root_alias)
    nodes: list[Node] = []
    if paths is None or not paths.nodes_dir.exists():
        return nodes

    for type_dir in paths.nodes_dir.iterdir():
        if not type_dir.is_dir():
            continue
        for node_file in type_dir.glob("*.yaml"):
            node = _load_node_safe(node_file)
            if node:
                nodes.append(node)

    return nodes


def load_relationships(project_slug: str, root_alias: str = LOCAL_ROOT_ALIAS) -> list[Relationship]:
    """Load all relationships for a project.

    Args:
        project_slug: The project
        root_alias: External root alias, or ``""`` for the local root.

    Returns:
        List of all relationships
    """
    paths = _build_paths(project_slug, root_alias)
    if paths is None:
        return []
    rel_file = _load_relationships_safe(paths.relationships_yaml)
    return rel_file.relationships if rel_file else []


def load_merged_graph(include_external_roots: bool = True) -> nx.DiGraph:
    """Load all nodes and relationships from all projects into a single graph.

    Spans the local root and (by default) all configured external roots.

    Node IDs are qualified to avoid collisions across projects and roots:

    - Local-root nodes:    ``project_slug/type:slug``    (backward compatible)
    - External-root nodes: ``@alias:project_slug/type:slug``

    Cross-project and cross-root references are resolved to their qualified
    form. Each graph node carries ``project`` and ``root`` attributes for
    downstream filtering.

    Args:
        include_external_roots: When False, only the local root is loaded
            (legacy single-root behavior).

    Returns:
        A NetworkX DiGraph containing all roots' nodes and edges.
    """
    import networkx as nx

    graph = nx.DiGraph()

    # Build the list of (root_alias, environment) pairs to walk.
    root_envs: list[tuple[str, EnvironmentPaths | None]] = [(LOCAL_ROOT_ALIAS, None)]
    if include_external_roots:
        from infracontext.federation import load_external_roots

        for alias, resolved in load_external_roots().items():
            root_envs.append((alias, resolved.environment))

    # Pass 1: load all nodes from all (root, project) pairs.
    for root_alias, env in root_envs:
        projects = list_projects(env) if env is not None else list_projects()
        for project_slug in projects:
            paths = _build_paths(project_slug, root_alias)
            if paths is None or not paths.nodes_dir.exists():
                continue
            for type_dir in paths.nodes_dir.iterdir():
                if not type_dir.is_dir():
                    continue
                for node_file in type_dir.glob("*.yaml"):
                    node = _load_node_safe(node_file)
                    if node:
                        graph_id = _qualify(root_alias, project_slug, node.id)
                        graph.add_node(
                            graph_id,
                            node=node,
                            name=node.name,
                            type=node.type,
                            project=project_slug,
                            root=root_alias,
                        )

    # Pass 2: load relationships (all nodes must exist before resolving refs).
    for root_alias, env in root_envs:
        projects = list_projects(env) if env is not None else list_projects()
        for project_slug in projects:
            paths = _build_paths(project_slug, root_alias)
            if paths is None:
                continue
            rel_file = _load_relationships_safe(paths.relationships_yaml)
            if not rel_file:
                continue
            for rel in rel_file.relationships:
                # Resolution context is the *origin* root + project; refs without
                # a root prefix stay within the same root.
                src = _resolve_in_root(rel.source, root_alias, project_slug)
                tgt = _resolve_in_root(rel.target, root_alias, project_slug)
                if src is None or tgt is None:
                    continue
                if graph.has_node(src) and graph.has_node(tgt):
                    graph.add_edge(
                        src,
                        tgt,
                        relationship=rel,
                        type=rel.type,
                        description=rel.description,
                        project=project_slug,
                        root=root_alias,
                    )

    return graph


def _resolve_in_root(ref: str, origin_root: str, origin_project: str) -> str | None:
    """Resolve a reference written within ``origin_root``/``origin_project``.

    Returns the graph qualified ID for the target, or None on parse failure.
    Refs from an external root that lack an ``@`` prefix stay within that
    same root (don't reach back into the local root).
    """
    try:
        scope, node_id = parse_node_ref(ref, origin_project)
    except ValueError:
        return None

    if not ref.startswith("@"):
        # Unqualified -> same root, same project context as origin.
        return _qualify(origin_root, origin_project, node_id)

    # Qualified. Disambiguate scope: external root alias wins over project name.
    roots = all_roots()
    if scope in roots and scope != LOCAL_ROOT_ALIAS:
        from infracontext.config import get_active_project

        target_env = roots[scope].environment
        target_project = get_active_project(target_env)
        if not target_project:
            return None
        return _qualify(scope, target_project, node_id)

    # Otherwise treat as a project within the *origin* root.
    return _qualify(origin_root, scope, node_id)


def unqualify_node_id(qualified_id: str) -> tuple[str, str]:
    """Split a qualified node ID into (scope_label, node_id).

    The node_id is always ``type:slug``. ``scope_label`` is suitable for
    grouping/display and round-trips with ``f"{scope_label}/{node_id}"``
    back to the original qualified ID.

    Three forms are recognized:

    - ``project/type:slug``         -> ``(project, type:slug)``  -- local root
    - ``org/team/type:slug``        -> ``(org/team, type:slug)`` -- hierarchical
    - ``@alias:project/type:slug``  -> ``(@alias:project, type:slug)`` -- external
    - ``type:slug``                 -> ``("", type:slug)``       -- unqualified

    Without the external-root branch, ``@alias:project/type:slug`` would be
    split at the *first* colon, losing the external root entirely and
    misreporting impact/SPoF output that groups by project.
    """
    # External-root form: '@alias:project/type:slug'. Project slugs can be
    # hierarchical (e.g. 'org/team'); node IDs (type:slug) never contain '/'.
    # So the *last* '/' is the scope/node boundary.
    if qualified_id.startswith("@"):
        slash_pos = qualified_id.rfind("/")
        if slash_pos != -1:
            return qualified_id[:slash_pos], qualified_id[slash_pos + 1 :]
        # Malformed @-prefixed ID: fall through and treat as unqualified.
        return "", qualified_id

    # Local form: project[/sub]/type:slug or bare type:slug.
    colon_pos = qualified_id.find(":")
    if colon_pos == -1:
        return "", qualified_id

    slash_pos = qualified_id.rfind("/", 0, colon_pos)
    if slash_pos == -1:
        return "", qualified_id

    return qualified_id[:slash_pos], qualified_id[slash_pos + 1 :]
