"""Health check and validation for infracontext data."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import typer
from pydantic import BaseModel, ValidationError
from rich.console import Console
from rich.table import Table
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from infracontext.models.chain import ChainFile, expand_chain
from infracontext.models.node import COMPUTE_NODE_TYPES, Node, NodeType
from infracontext.models.project import ProjectConfig
from infracontext.models.relationship import (
    Relationship,
    RelationshipFile,
    RelationshipType,
    get_valid_relationship_types,
    is_cross_project_ref,
)
from infracontext.paths import EnvironmentNotFoundError, EnvironmentPaths, ProjectPaths, list_projects
from infracontext.storage import strip_unknown_fields

app = typer.Typer(name="doctor", help="Validate infrastructure data")
console = Console()
_yaml = YAML()


class Severity(StrEnum):
    """Issue severity levels."""

    ERROR = "error"  # Invalid data, will cause failures
    WARNING = "warning"  # Missing recommended info
    INFO = "info"  # Suggestions for improvement


@dataclass
class Issue:
    """A validation issue found during health check."""

    severity: Severity
    category: str
    file: Path | None
    message: str
    suggestion: str | None = None


@dataclass
class DoctorReport:
    """Validation results from doctor check."""

    issues: list[Issue] = field(default_factory=list)
    files_checked: int = 0
    nodes_checked: int = 0
    relationships_checked: int = 0
    projects_checked: int = 0

    def add(
        self,
        severity: Severity,
        category: str,
        message: str,
        file: Path | None = None,
        suggestion: str | None = None,
    ) -> None:
        self.issues.append(Issue(severity, category, file, message, suggestion))

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.WARNING)

    @property
    def info_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.INFO)

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0


def _check_yaml_syntax(path: Path, report: DoctorReport) -> dict | None:
    """Check YAML syntax and return parsed data if valid."""
    try:
        with path.open("r") as f:
            data = _yaml.load(f)
    except YAMLError as e:
        report.add(
            Severity.ERROR,
            "syntax",
            f"YAML syntax error: {e}",
            file=path,
        )
        return None
    except Exception as e:
        report.add(
            Severity.ERROR,
            "syntax",
            f"Failed to read file: {e}",
            file=path,
        )
        return None

    if data is None:
        return {}
    # A valid-YAML-but-not-a-mapping top level (a stray list or scalar from a
    # hand-edit) would otherwise blow up on `dict(data)`; report it clearly
    # instead, consistent with storage.read_yaml's graceful degradation.
    if not isinstance(data, dict):
        # ruamel loads sequences/mappings as CommentedSeq/CommentedMap; report
        # the plain built-in name ("list", "str", ...) to match read_yaml.
        kind = "list" if isinstance(data, list) else type(data).__name__
        report.add(
            Severity.ERROR,
            "syntax",
            f"Expected a mapping at the top level, got {kind}",
            file=path,
        )
        return None
    return dict(data)


def _validate_model[T: BaseModel](model_cls: type[T], data: dict, path: Path, report: DoctorReport) -> T | None:
    """Validate ``data``, downgrading unknown-field errors to warnings.

    Unknown fields (top-level or nested) are schema drift from a newer/older
    infracontext version, not necessarily broken data: the read path strips
    them with a warning and preserves them on rewrite, so doctor mirrors that
    as a WARNING (typos still surface). Every other validation failure stays
    an ERROR. Returns the model, or None when real errors are present.
    """
    try:
        return model_cls.model_validate(data)
    except ValidationError as e:
        real_errors = False
        for error in e.errors():
            loc = ".".join(str(x) for x in error["loc"])
            if error["type"] == "extra_forbidden":
                report.add(
                    Severity.WARNING,
                    "unknown_field",
                    f"Unknown field '{loc}' (typo, or written by a newer infracontext?)",
                    file=path,
                    suggestion="Remove the field if it's a typo, or upgrade infracontext.",
                )
            else:
                real_errors = True
                report.add(
                    Severity.ERROR,
                    "schema",
                    f"Validation error at '{loc}': {error['msg']}",
                    file=path,
                )
        if real_errors:
            return None
        # Only unknown fields: strip them (as the read path does) and retry.
        try:
            return model_cls.model_validate(strip_unknown_fields(data, model_cls, source=path, warn=False))
        except ValidationError:  # pragma: no cover - stripping removed every failing key
            return None


def _check_node(path: Path, data: dict, report: DoctorReport) -> Node | None:
    """Validate a node file against the schema."""
    node = _validate_model(Node, data, path, report)
    if node is None:
        return None
    report.nodes_checked += 1

    # Unknown enum variant from a newer version: loadable, but surface it.
    if not isinstance(node.type, NodeType):
        report.add(
            Severity.WARNING,
            "unknown_variant",
            f"Node '{node.id}' has unknown type '{node.type}'",
            file=path,
            suggestion="Written by a newer infracontext? Upgrade, or fix the type if it's a typo.",
        )

    # Check for missing ssh_alias on compute nodes
    if node.type in COMPUTE_NODE_TYPES and not node.ssh_alias:
        report.add(
            Severity.WARNING,
            "missing_info",
            f"Compute node '{node.id}' has no ssh_alias",
            file=path,
            suggestion="Add ssh_alias for SSH-based operations",
        )

    # Check for empty description
    if not node.description and not node.notes:
        report.add(
            Severity.INFO,
            "missing_info",
            f"Node '{node.id}' has no description or notes",
            file=path,
            suggestion="Consider adding a description for documentation",
        )

    # Check for observability config on compute nodes
    if node.type in COMPUTE_NODE_TYPES and not node.observability:
        report.add(
            Severity.INFO,
            "missing_info",
            f"Compute node '{node.id}' has no observability config",
            file=path,
            suggestion="Add prometheus/loki/monit config for 'ic query' support",
        )

    # Check for blank learnings (empty/whitespace-only context or finding)
    for idx, learning in enumerate(node.learnings):
        blank = [name for name, value in (("context", learning.context), ("finding", learning.finding)) if not value.strip()]
        if blank:
            report.add(
                Severity.INFO,
                "blank_learning",
                f"Node '{node.id}' learning #{idx + 1} ({learning.date}) has a blank {' and '.join(blank)}",
                file=path,
                suggestion="Fill in the learning or delete the entry -- blank learnings add noise to 'ic ctx'.",
            )

    return node


def _check_project_config(path: Path, data: dict, report: DoctorReport) -> ProjectConfig | None:
    """Validate a project config file."""
    return _validate_model(ProjectConfig, data, path, report)


def _check_relationships(
    path: Path,
    data: dict,
    all_node_ids: set[str],
    report: DoctorReport,
    project_slug: str = "",
) -> list[Relationship]:
    """Validate relationships file and check for orphaned references.

    Handles cross-project references (@project:type:slug) by loading
    the referenced node from the target project.
    """
    from infracontext.graph.loader import load_node
    from infracontext.models.relationship import is_cross_project_ref, parse_node_ref

    relationships: list[Relationship] = []

    rel_file = _validate_model(RelationshipFile, data, path, report)
    if rel_file is None:
        return []
    relationships = rel_file.relationships
    report.relationships_checked += len(relationships)

    # Unknown enum variants from a newer version: loadable, but surface them.
    for rel in relationships:
        if not isinstance(rel.type, RelationshipType):
            report.add(
                Severity.WARNING,
                "unknown_variant",
                f"Relationship {rel.source} --{rel.type}--> {rel.target} has an unknown type",
                file=path,
                suggestion="Written by a newer infracontext? Upgrade, or fix the type if it's a typo.",
            )

    # Lazy-import federation to avoid loading external roots when validating
    # standalone YAML (e.g., in unit tests with patched loaders).
    from infracontext.federation import LOCAL_ROOT_ALIAS, all_roots, resolve_node_ref

    roots = all_roots()

    # Check for orphaned relationships (references to non-existent nodes)
    for rel in relationships:
        for label, ref in [("source", rel.source), ("target", rel.target)]:
            if is_cross_project_ref(ref):
                # Qualified: could be cross-project (local) or cross-root.
                try:
                    resolved = resolve_node_ref(ref, default_project=project_slug, roots=roots)
                except ValueError as e:
                    report.add(
                        Severity.ERROR,
                        "cross_project",
                        f"Invalid qualified {label} reference '{ref}': {e}",
                        file=path,
                    )
                    continue

                # Distinguish "scope is a known external root alias" from
                # "scope is a local project name" for clearer errors.
                scope, _ = parse_node_ref(ref, project_slug)
                is_external = resolved.root_alias != LOCAL_ROOT_ALIAS
                category = "cross_root" if is_external else "cross_project"

                node = load_node(
                    resolved.project,
                    resolved.node_id,
                    root_alias=resolved.root_alias,
                )
                if node is None:
                    where = (
                        f"external root '{resolved.root_alias}' "
                        f"(project '{resolved.project}')"
                        if is_external
                        else f"project '{resolved.project}'"
                    )
                    report.add(
                        Severity.ERROR,
                        category,
                        f"Relationship references non-existent {label} node "
                        f"'{resolved.node_id}' in {where}",
                        file=path,
                        suggestion=(
                            f"Check that node '{resolved.node_id}' exists "
                            f"in {where}, or remove the relationship. "
                            f"Reference '@{scope}:...' resolves to "
                            f"{'external root' if is_external else 'local project'} "
                            f"'{scope}'."
                        ),
                    )
            else:
                # Same-project: check against local node IDs
                if ref not in all_node_ids:
                    report.add(
                        Severity.ERROR,
                        "orphan",
                        f"Relationship references non-existent {label} node: '{ref}'",
                        file=path,
                        suggestion=f"Remove relationship or create node '{ref}'",
                    )

    # Check for duplicate relationships
    seen: set[tuple[str, str, str]] = set()
    for rel in relationships:
        key = (rel.source, rel.type, rel.target)
        if key in seen:
            report.add(
                Severity.WARNING,
                "redundant",
                f"Duplicate relationship: {rel.source} --{rel.type}--> {rel.target}",
                file=path,
            )
        seen.add(key)

    return relationships


def _check_chains(
    path: Path,
    data: dict,
    all_node_ids: set[str],
    report: DoctorReport,
    project_slug: str = "",
) -> list[Relationship]:
    """Validate chains.yaml: duplicate names and dangling member refs.

    Returns the chain-expanded pairwise relationships so the caller can re-run
    the constraint matrix over them (counted into ``relationships_checked`` --
    they are the edges every graph consumer sees). Chain-specific findings are
    WARNING, never ERROR: an unresolvable member merely drops that edge at
    load time, it does not break the graph.
    """
    from infracontext.federation import LOCAL_ROOT_ALIAS, all_roots, resolve_node_ref
    from infracontext.graph.loader import load_node

    chain_file = _validate_model(ChainFile, data, path, report)
    if chain_file is None:
        return []

    # Duplicate chain names (must be unique per project).
    seen_names: set[str] = set()
    for chain in chain_file.chains:
        if chain.name in seen_names:
            report.add(
                Severity.WARNING,
                "chain",
                f"Duplicate chain name '{chain.name}'",
                file=path,
                suggestion="Chain names must be unique per project; rename or merge the duplicates.",
            )
        seen_names.add(chain.name)

    roots = all_roots()
    expanded: list[Relationship] = []
    for chain in chain_file.chains:
        # Unknown edge type from a newer version: loadable, but surface it.
        if not isinstance(chain.type, RelationshipType):
            report.add(
                Severity.WARNING,
                "unknown_variant",
                f"Chain '{chain.name}' has an unknown type '{chain.type}'",
                file=path,
                suggestion="Written by a newer infracontext? Upgrade, or fix the type if it's a typo.",
            )

        # `via` describes the edge *into* a member; the first member has no
        # inbound hop, so a via there never reaches any expanded edge and the
        # hand-authored context is silently lost from every graph view.
        if chain.members[0].via:
            report.add(
                Severity.WARNING,
                "chain",
                f"Chain '{chain.name}' sets 'via' on its first member '{chain.members[0].id}'; "
                f"'via' describes the edge into a member, so it never appears in the expanded graph",
                file=path,
                suggestion="Move the text into the chain description or onto the member the traffic reaches.",
            )

        # Dangling member refs (local, cross-project, or cross-root).
        for member in chain.members:
            if is_cross_project_ref(member.id):
                try:
                    resolved = resolve_node_ref(member.id, default_project=project_slug, roots=roots)
                except ValueError as e:
                    report.add(
                        Severity.WARNING,
                        "chain",
                        f"Chain '{chain.name}' has an invalid member reference '{member.id}': {e}",
                        file=path,
                    )
                    continue
                node = load_node(resolved.project, resolved.node_id, root_alias=resolved.root_alias)
                if node is None:
                    where = (
                        f"external root '{resolved.root_alias}' (project '{resolved.project}')"
                        if resolved.root_alias != LOCAL_ROOT_ALIAS
                        else f"project '{resolved.project}'"
                    )
                    report.add(
                        Severity.WARNING,
                        "chain",
                        f"Chain '{chain.name}' references non-existent member "
                        f"'{resolved.node_id}' in {where}",
                        file=path,
                        suggestion=f"Create the node or fix the member reference '{member.id}'.",
                    )
            elif member.id not in all_node_ids:
                report.add(
                    Severity.WARNING,
                    "chain",
                    f"Chain '{chain.name}' references non-existent member: '{member.id}'",
                    file=path,
                    suggestion=f"Create node '{member.id}' or fix the member reference.",
                )

        expanded.extend(expand_chain(chain))

    report.relationships_checked += len(expanded)
    return expanded


def _check_relationship_constraints(
    path: Path,
    relationships: list[Relationship],
    nodes_by_id: dict[str, Node],
    report: DoctorReport,
    project_slug: str,
) -> None:
    """Re-validate the (source_type, target_type, type) constraint matrix.

    RELATIONSHIP_CONSTRAINTS is enforced at create time only; hand-edited YAML
    can carry invalid triples that silently poison graph traversals
    ('ic graph impact', 'ic ctx'). WARNING, never ERROR: the matrix itself may
    simply be missing a legitimate pairing.

    Endpoints that don't resolve (orphans, unresolvable external roots) are
    skipped silently -- the orphan/cross_project checks already report the
    missing node. Endpoints or relationship types that aren't known enum
    members are skipped too (the unknown_variant check covers them).
    """
    # Lazy-import federation/loader, matching _check_relationships (tests
    # validating standalone YAML patch these loaders).
    from infracontext.federation import all_roots, resolve_node_ref
    from infracontext.graph.loader import load_node

    roots = all_roots()

    def _endpoint_type(ref: str) -> str | None:
        if not is_cross_project_ref(ref):
            node = nodes_by_id.get(ref)
        else:
            try:
                resolved = resolve_node_ref(ref, default_project=project_slug, roots=roots)
            except ValueError:
                return None
            node = load_node(resolved.project, resolved.node_id, root_alias=resolved.root_alias)
        if node is None or not isinstance(node.type, NodeType):
            return None
        return str(node.type)

    for rel in relationships:
        if not isinstance(rel.type, RelationshipType):
            continue
        source_type = _endpoint_type(rel.source)
        target_type = _endpoint_type(rel.target)
        if source_type is None or target_type is None:
            continue
        allowed = get_valid_relationship_types(source_type, target_type)
        if rel.type in allowed:
            continue
        if allowed:
            detail = f"valid types for this pair: {', '.join(allowed)}"
        else:
            detail = f"no relationship types are defined for {source_type} -> {target_type}"
        report.add(
            Severity.WARNING,
            "constraint",
            f"Relationship {rel.source} --{rel.type}--> {rel.target} violates the type constraint matrix ({detail})",
            file=path,
            suggestion=(
                "Fix the relationship type, or extend RELATIONSHIP_CONSTRAINTS in "
                "models/relationship.py if this pairing is legitimate."
            ),
        )


def _check_duplicate_identifiers(
    project_slug: str,
    nodes: list[tuple[Path, Node]],
    report: DoctorReport,
) -> None:
    """Flag duplicate ssh_alias / IP addresses within one project."""
    by_alias: dict[str, list[Node]] = {}
    by_ip: dict[str, list[Node]] = {}
    for _, node in nodes:
        if node.ssh_alias:
            by_alias.setdefault(node.ssh_alias, []).append(node)
        for ip in node.ip_addresses:
            by_ip.setdefault(ip, []).append(node)

    for alias, dupes in sorted(by_alias.items()):
        if len(dupes) < 2:
            continue
        ids = ", ".join(sorted(n.id for n in dupes))
        report.add(
            Severity.WARNING,
            "duplicate",
            f"ssh_alias '{alias}' is shared by {len(dupes)} nodes in project '{project_slug}' "
            f"({ids}) -- 'ic ssh {alias}' fuzzy resolution is ambiguous and may connect to the wrong node",
            suggestion="Give each node its own ssh_alias, or remove the stale one.",
        )

    for ip, dupes in sorted(by_ip.items()):
        if len(dupes) < 2:
            continue
        ids = ", ".join(sorted(n.id for n in dupes))
        report.add(
            Severity.WARNING,
            "duplicate",
            f"IP address {ip} appears on {len(dupes)} nodes in project '{project_slug}' ({ids})",
            suggestion="Shared VIPs / floating IPs can be legitimate; otherwise remove the stale address.",
        )


def _check_cross_project_ssh_aliases(
    nodes_by_project: dict[str, list[tuple[Path, Node]]],
    report: DoctorReport,
) -> None:
    """Flag ssh_aliases reused across projects (INFO -- often legitimate)."""
    alias_sites: dict[str, dict[str, set[str]]] = {}
    for slug, nodes in nodes_by_project.items():
        for _, node in nodes:
            if node.ssh_alias:
                alias_sites.setdefault(node.ssh_alias, {}).setdefault(slug, set()).add(node.id)

    for alias, sites in sorted(alias_sites.items()):
        if len(sites) < 2:
            continue
        where = "; ".join(f"{slug}: {', '.join(sorted(ids))}" for slug, ids in sorted(sites.items()))
        report.add(
            Severity.INFO,
            "duplicate",
            f"ssh_alias '{alias}' is used in {len(sites)} projects ({where})",
            suggestion=(
                "Often legitimate (the same host viewed from dev/staging/prod); "
                "verify the alias points where each project expects."
            ),
        )


# Edge types along which an application "accounts for" downstream nodes.
_COVERAGE_EDGE_TYPES = frozenset(
    {RelationshipType.CONTAINS, RelationshipType.DEPENDS_ON, RelationshipType.USES}
)
# Node types expected to be grouped under an application.
_COVERAGE_NODE_TYPES = COMPUTE_NODE_TYPES | {NodeType.SERVICE, NodeType.SERVICE_CLUSTER}


def _check_application_coverage(
    project_slug: str,
    nodes: list[tuple[Path, Node]],
    relationships: list[Relationship],
    report: DoctorReport,
) -> None:
    """Flag compute/service nodes not grouped under any application (INFO).

    Distinct from 'ic graph orphans' (nodes with no edges at all): this asks
    whether a node is reachable from an *application* node along
    contains/depends_on/uses edges, i.e. whether the business layer accounts
    for it. Skipped entirely when the project has no application nodes --
    otherwise it would flag every node in unstructured repos as noise.
    """
    app_ids = {n.id for _, n in nodes if n.type == NodeType.APPLICATION}
    if not app_ids:
        return

    successors: dict[str, set[str]] = {}
    for rel in relationships:
        if rel.type in _COVERAGE_EDGE_TYPES:
            successors.setdefault(rel.source, set()).add(rel.target)

    reached: set[str] = set(app_ids)
    frontier = list(app_ids)
    while frontier:
        for nxt in successors.get(frontier.pop(), ()):
            if nxt not in reached:
                reached.add(nxt)
                frontier.append(nxt)

    for path, node in nodes:
        if node.type in _COVERAGE_NODE_TYPES and node.id not in reached:
            report.add(
                Severity.INFO,
                "ungrouped",
                f"Node '{node.id}' is not reachable from any application node "
                f"via contains/depends_on/uses (project '{project_slug}')",
                file=path,
                suggestion=(
                    "Unlike 'ic graph orphans', the node may well have edges -- it just "
                    "isn't grouped under an application. Link it (directly or transitively) "
                    "to show which business function it serves."
                ),
            )


def _check_source_presence(
    project_slug: str,
    environment: EnvironmentPaths,
    nodes: list[tuple[Path, Node]],
    report: DoctorReport,
) -> None:
    """Warn about source-managed nodes absent from recent successful syncs.

    Presence is derived from the committed run records in .infracontext/runs/
    (see infracontext.runs). Strictly scoped to nodes with ``managed_by`` set:
    manual nodes have no sync that could confirm them and must never warn.
    Sources without any counting (successful, non-empty) run record are
    skipped -- there is no evidence to classify against.
    """
    from infracontext.runs import Presence, classify_presence, load_run_records

    managed: dict[str, list[tuple[Path, Node]]] = {}
    for path, node in nodes:
        if node.managed_by:
            managed.setdefault(node.managed_by, []).append((path, node))

    for source, entries in sorted(managed.items()):
        records = load_run_records(environment, project=project_slug, source=source)
        classified = classify_presence([node.id for _, node in entries], records)
        for path, node in entries:
            info = classified.get(node.id)
            if info is None or info.presence is Presence.PRESENT:
                continue
            syncs = f"{info.consecutive_misses} sync{'s' if info.consecutive_misses != 1 else ''}"
            report.add(
                Severity.WARNING,
                "presence",
                f"Node '{node.id}' not seen by source '{source}' in {syncs} ({info.presence})",
                file=path,
                suggestion=f"Investigate, or remove it with 'ic describe node delete {node.id}'.",
            )


def _check_project(slug: str, environment: EnvironmentPaths, report: DoctorReport) -> list[tuple[Path, Node]]:
    """Check all files in a project. Returns the parsed (file, node) pairs."""
    paths = ProjectPaths.for_project(slug, environment)
    report.projects_checked += 1

    # Check project.yaml if it exists
    project_yaml = paths.root / "project.yaml"
    if project_yaml.exists():
        report.files_checked += 1
        data = _check_yaml_syntax(project_yaml, report)
        if data is not None:
            _check_project_config(project_yaml, data, report)

    # Collect all parsed nodes for relationship / lint validation
    nodes: list[tuple[Path, Node]] = []
    all_node_ids: set[str] = set()

    # Check all node files
    if paths.nodes_dir.exists():
        for node_type_dir in paths.nodes_dir.iterdir():
            if not node_type_dir.is_dir():
                continue
            for node_file in node_type_dir.glob("*.yaml"):
                report.files_checked += 1
                data = _check_yaml_syntax(node_file, report)
                if data is not None:
                    node = _check_node(node_file, data, report)
                    if node:
                        # The declared id must match where the file lives:
                        # nodes/<type>/<slug>.yaml -> id "<type>:<slug>".
                        expected_id = f"{node_type_dir.name}:{node_file.stem}"
                        if node.id != expected_id:
                            report.add(
                                Severity.ERROR,
                                "id_path_mismatch",
                                f"Node id '{node.id}' does not match its file location "
                                f"(expected '{expected_id}' from "
                                f"{node_type_dir.name}/{node_file.name})",
                                file=node_file,
                                suggestion=(
                                    f"Rename the file to '{node.slug}.yaml' under "
                                    f"'{node.type}/', or correct the id/type/slug."
                                ),
                            )
                        nodes.append((node_file, node))
                        all_node_ids.add(node.id)

    # Check relationships
    relationships: list[Relationship] = []
    if paths.relationships_yaml.exists():
        report.files_checked += 1
        data = _check_yaml_syntax(paths.relationships_yaml, report)
        if data is not None:
            relationships = _check_relationships(
                paths.relationships_yaml, data, all_node_ids, report, project_slug=slug
            )

    # Check chains (expanded into pairwise edges, same view the loaders build)
    chain_edges: list[Relationship] = []
    if paths.chains_yaml.exists():
        report.files_checked += 1
        data = _check_yaml_syntax(paths.chains_yaml, report)
        if data is not None:
            chain_edges = _check_chains(paths.chains_yaml, data, all_node_ids, report, project_slug=slug)

    if relationships or chain_edges:
        nodes_by_id = {node.id: node for _, node in nodes}
        if relationships:
            _check_relationship_constraints(paths.relationships_yaml, relationships, nodes_by_id, report, slug)
        if chain_edges:
            _check_relationship_constraints(paths.chains_yaml, chain_edges, nodes_by_id, report, slug)

    _check_duplicate_identifiers(slug, nodes, report)
    _check_application_coverage(slug, nodes, relationships + chain_edges, report)
    _check_source_presence(slug, environment, nodes, report)

    # Check for empty project
    if not all_node_ids:
        report.add(
            Severity.INFO,
            "empty",
            f"Project '{slug}' has no nodes",
            file=paths.root,
            suggestion="Add nodes with 'ic describe node create'",
        )

    return nodes


def _check_local_overrides(environment: EnvironmentPaths, report: DoctorReport) -> None:
    """Validate .infracontext.local.yaml if it exists."""
    from infracontext.overrides import LocalOverrides, NodeOverrides

    if not environment.local_overrides.exists():
        return

    report.files_checked += 1
    data = _check_yaml_syntax(environment.local_overrides, report)
    if data is None:
        return

    try:
        if "nodes" in data and isinstance(data["nodes"], dict):
            for node_id, overrides in data["nodes"].items():
                if isinstance(overrides, dict):
                    try:
                        NodeOverrides.model_validate(overrides)
                    except ValidationError as e:
                        for error in e.errors():
                            loc = ".".join(str(x) for x in error["loc"])
                            report.add(
                                Severity.ERROR,
                                "local_overrides",
                                f"Node '{node_id}' override error at '{loc}': {error['msg']}",
                                file=environment.local_overrides,
                            )
                else:
                    report.add(
                        Severity.ERROR,
                        "local_overrides",
                        f"Node '{node_id}' override must be a mapping, got {type(overrides).__name__}",
                        file=environment.local_overrides,
                    )
        LocalOverrides.model_validate(data)
    except ValidationError as e:
        for error in e.errors():
            loc = ".".join(str(x) for x in error["loc"])
            report.add(
                Severity.ERROR,
                "local_overrides",
                f"Validation error at '{loc}': {error['msg']}",
                file=environment.local_overrides,
            )


def _check_external_roots(environment: EnvironmentPaths, report: DoctorReport) -> None:
    """Validate external_roots configuration.

    Checks:
    - Each configured root resolves to a directory containing .infracontext/.
    - Root aliases don't collide with local project slugs (refs would be
      ambiguous; external root would win, surprising local-project users).
    - Duplicate node IDs between local root and any external root are warned
      (federation favors a single home per node).
    """
    from infracontext.config import ConfigError, load_config
    from infracontext.federation import ExternalRootError, resolve_external_root
    from infracontext.storage import StorageError

    # A schema-invalid config.yaml must be reported, never crash the whole
    # health check -- doctor must always run to completion.
    try:
        config = load_config(environment)
    except (ConfigError, StorageError) as e:
        # ConfigError already reads "invalid <path>: <key>: <expected>".
        report.add(
            Severity.ERROR,
            "config",
            str(e),
            file=environment.config_yaml,
            suggestion="Fix the reported key(s) in .infracontext/config.yaml.",
        )
        return
    if not config.external_roots:
        return

    # Project slugs in the local root.
    local_projects = set(list_projects(environment))

    # Collect local node IDs once for duplicate detection.
    local_node_ids: set[str] = set()
    for slug in local_projects:
        try:
            paths = ProjectPaths.for_project(slug, environment)
        except Exception:
            continue
        if not paths.nodes_dir.exists():
            continue
        for type_dir in paths.nodes_dir.iterdir():
            if not type_dir.is_dir():
                continue
            for node_file in type_dir.glob("*.yaml"):
                node = read_model_or_none(node_file)
                if node is not None:
                    local_node_ids.add(node.id)

    for entry in config.external_roots:
        # Alias must not shadow a local project name.
        if entry.alias in local_projects:
            report.add(
                Severity.ERROR,
                "external_root",
                f"External root alias '{entry.alias}' collides with a local "
                f"project of the same name. References '@{entry.alias}:...' "
                f"would always resolve to the external root, never the local "
                f"project.",
                file=environment.config_yaml,
                suggestion=(
                    "Rename the external root alias or the local project."
                ),
            )

        # Resolve the root path.
        try:
            resolved = resolve_external_root(entry, anchor=environment.root)
        except ExternalRootError as e:
            report.add(
                Severity.ERROR,
                "external_root",
                str(e),
                file=environment.config_yaml,
                suggestion=(
                    f"Check that '{entry.path}' contains a .infracontext/ "
                    f"directory, or remove the entry from external_roots."
                ),
            )
            continue

        # Duplicate-detection across the external root's nodes.
        try:
            external_projects = list_projects(resolved.environment)
        except Exception:
            external_projects = []
        for ext_project in external_projects:
            try:
                ext_paths = ProjectPaths.for_project(ext_project, resolved.environment)
            except Exception:
                continue
            if not ext_paths.nodes_dir.exists():
                continue
            for type_dir in ext_paths.nodes_dir.iterdir():
                if not type_dir.is_dir():
                    continue
                for node_file in type_dir.glob("*.yaml"):
                    node = read_model_or_none(node_file)
                    if node is None:
                        continue
                    if node.id in local_node_ids:
                        report.add(
                            Severity.WARNING,
                            "external_root",
                            f"Node '{node.id}' is defined in both the local "
                            f"root and external root '{entry.alias}'. "
                            f"Federation expects a single home per node.",
                            file=node_file,
                            suggestion=(
                                f"Choose one home for '{node.id}' and "
                                f"replace the duplicate with a relationship "
                                f"reference."
                            ),
                        )


def read_model_or_none(node_file: Path) -> Node | None:
    """Read a Node YAML, swallowing schema errors (doctor reports them
    separately via _check_project)."""
    try:
        from infracontext.storage import read_model

        return read_model(node_file, Node)
    except Exception:
        return None


def run_doctor(environment: EnvironmentPaths | None = None) -> DoctorReport:
    """Run all health checks and return report."""
    if environment is None:
        environment = EnvironmentPaths.current()

    report = DoctorReport()

    # Check config.yaml
    if environment.config_yaml.exists():
        report.files_checked += 1
        _check_yaml_syntax(environment.config_yaml, report)

    # Check external_roots configuration and duplicate node IDs across roots.
    _check_external_roots(environment, report)

    # Check local overrides
    _check_local_overrides(environment, report)

    # Check all projects
    projects = list_projects(environment)
    if not projects:
        report.add(
            Severity.INFO,
            "empty",
            "No projects found",
            file=environment.projects_dir,
            suggestion="Create a project with 'ic describe project create <name>'",
        )

    nodes_by_project: dict[str, list[tuple[Path, Node]]] = {}
    for slug in projects:
        nodes_by_project[slug] = _check_project(slug, environment, report)

    _check_cross_project_ssh_aliases(nodes_by_project, report)

    return report


def _display_report(report: DoctorReport) -> None:
    """Display the doctor report to the console."""
    # Summary
    console.print()
    console.print(
        f"[bold]Checked:[/bold] {report.files_checked} files, {report.nodes_checked} nodes, {report.relationships_checked} relationships, {report.projects_checked} projects"
    )
    console.print()

    if not report.issues:
        console.print("[green]✓ No issues found[/green]")
        return

    # Group issues by severity
    errors = [i for i in report.issues if i.severity == Severity.ERROR]
    warnings = [i for i in report.issues if i.severity == Severity.WARNING]
    infos = [i for i in report.issues if i.severity == Severity.INFO]

    def _print_issues(issues: list[Issue], label: str, style: str) -> None:
        if not issues:
            return
        console.print(f"[{style}]{label} ({len(issues)}):[/{style}]")
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Category", style="dim")
        table.add_column("Message")
        table.add_column("File", style="dim")

        for issue in issues:
            file_str = str(issue.file.name) if issue.file else ""
            table.add_row(issue.category, issue.message, file_str)

        console.print(table)
        console.print()

    _print_issues(errors, "Errors", "red bold")
    _print_issues(warnings, "Warnings", "yellow bold")
    _print_issues(infos, "Info", "blue bold")

    # Summary line
    parts = []
    if report.error_count:
        parts.append(f"[red]{report.error_count} error(s)[/red]")
    if report.warning_count:
        parts.append(f"[yellow]{report.warning_count} warning(s)[/yellow]")
    if report.info_count:
        parts.append(f"[blue]{report.info_count} suggestion(s)[/blue]")

    console.print("Summary: " + ", ".join(parts))


@app.callback(invoke_without_command=True)
def doctor(
    fix: bool = typer.Option(False, "--fix", "-f", help="Attempt to auto-fix issues (not yet implemented)"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show all checks, including passed (not yet implemented)"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Check infrastructure data for validity and completeness.

    Validates:
    - YAML syntax
    - Schema compliance (Pydantic models)
    - Missing recommended info (ssh_alias, descriptions)
    - Orphaned relationships (references to non-existent nodes)
    - Duplicate/redundant entries
    - Relationship type constraints (create-time matrix, re-validated)
    - Chains (chains.yaml): duplicate names, dangling members, pair constraints
    - Duplicate ssh_alias / IP addresses across nodes
    - Compute/service nodes not grouped under any application
    - Blank learnings (whitespace-only context or finding)
    - Source-managed nodes absent from recent successful syncs (run records)
    """
    try:
        report = run_doctor()
    except EnvironmentNotFoundError:
        console.print("[red]No infracontext environment found. Run 'ic init' first.[/red]")
        raise typer.Exit(1) from None

    if json_output:
        import json

        output = {
            "summary": {
                "files_checked": report.files_checked,
                "nodes_checked": report.nodes_checked,
                "relationships_checked": report.relationships_checked,
                "projects_checked": report.projects_checked,
                "errors": report.error_count,
                "warnings": report.warning_count,
                "info": report.info_count,
            },
            "issues": [
                {
                    "severity": i.severity,
                    "category": i.category,
                    "file": str(i.file) if i.file else None,
                    "message": i.message,
                    "suggestion": i.suggestion,
                }
                for i in report.issues
            ],
        }
        console.print(json.dumps(output, indent=2))
    else:
        _display_report(report)

    if report.has_errors:
        raise typer.Exit(1)
