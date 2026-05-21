"""Load nodes and relationships into a NetworkX graph."""

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
from infracontext.storage import read_model


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
) -> str | None:
    """Resolve a potentially cross-project or cross-root node reference.

    Returns the graph node ID used for the resolved node, or None if it
    cannot be found. Semantics inside :func:`load_graph` (single-project view):

    - Same-project refs: return the bare ``type:slug``.
    - Local cross-project refs: qualified as ``project/type:slug`` so a
      ``@other:vm:foo`` cannot collide with a local ``vm:foo`` of the
      current project.
    - Cross-root refs: qualified as ``@alias:project/type:slug`` so local and
      external nodes with overlapping IDs can coexist.
    """
    resolved = resolve_node_ref(ref, default_project=project_slug)

    # Same root, same project -> graph uses bare type:slug to match the IDs
    # under which we registered the current-project nodes.
    if resolved.root_alias == LOCAL_ROOT_ALIAS and resolved.project == project_slug:
        return resolved.node_id

    graph_id = _qualify(resolved.root_alias, resolved.project, resolved.node_id)
    if not graph.has_node(graph_id):
        node = load_node(
            resolved.project, resolved.node_id, root_alias=resolved.root_alias
        )
        if node is None:
            return None
        attrs: dict[str, object] = {
            "node": node,
            "name": node.name,
            "type": node.type,
            "project": resolved.project,
        }
        if resolved.root_alias != LOCAL_ROOT_ALIAS:
            attrs["root"] = resolved.root_alias
        graph.add_node(graph_id, **attrs)

    return graph_id


def load_graph(project_slug: str) -> nx.DiGraph:
    """Load all nodes and relationships for a project into a directed graph.

    Nodes are stored as graph nodes with their full data as attributes.
    Relationships are stored as edges with relationship type and metadata.

    Cross-project references (using @project:type:slug format) are resolved
    by loading only the specific referenced nodes from other projects.

    Args:
        project_slug: The project to load

    Returns:
        A NetworkX DiGraph with nodes and edges
    """
    paths = ProjectPaths.for_project(project_slug)
    graph = nx.DiGraph()

    # Load all nodes from this project
    if paths.nodes_dir.exists():
        for type_dir in paths.nodes_dir.iterdir():
            if not type_dir.is_dir():
                continue
            for node_file in type_dir.glob("*.yaml"):
                node = read_model(node_file, Node)
                if node:
                    graph.add_node(
                        node.id,
                        node=node,
                        name=node.name,
                        type=node.type,
                    )

    # Load relationships, resolving cross-project refs
    rel_file = read_model(paths.relationships_yaml, RelationshipFile)
    if rel_file:
        for rel in rel_file.relationships:
            # Resolve source and target, handling cross-project refs
            if is_cross_project_ref(rel.source) or is_cross_project_ref(rel.target):
                source_id = _resolve_cross_project_node(rel.source, project_slug, graph)
                target_id = _resolve_cross_project_node(rel.target, project_slug, graph)
                if source_id is None or target_id is None:
                    continue
            else:
                source_id = rel.source
                target_id = rel.target

            # Only add edge if both nodes exist
            if graph.has_node(source_id) and graph.has_node(target_id):
                graph.add_edge(
                    source_id,
                    target_id,
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

    return read_model(node_file, Node)


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
            node = read_model(node_file, Node)
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
    rel_file = read_model(paths.relationships_yaml, RelationshipFile)
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
                    node = read_model(node_file, Node)
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
            rel_file = read_model(paths.relationships_yaml, RelationshipFile)
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
