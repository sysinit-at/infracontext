"""Relationship model for connections between nodes."""

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

# Cross-project reference separator.
# Format: @project:node_type:slug  (e.g. @vagt/dev:vm:qoncept-proxy-01)
# Unqualified references (no @ prefix) use the current project.
CROSS_PROJECT_PREFIX = "@"


def parse_node_ref(ref: str, default_project: str) -> tuple[str, str]:
    """Parse a node reference into (project, node_id).

    Qualified format:   @project:node_type:slug -> (project, node_type:slug)
    Unqualified format: node_type:slug          -> (default_project, node_type:slug)

    The project portion may contain '/' for hierarchical projects (e.g. vagt/dev).

    Args:
        ref: The node reference string.
        default_project: Project to use when ref is unqualified.

    Returns:
        Tuple of (project_slug, node_id).

    Raises:
        ValueError: If the reference format is invalid.
    """
    if ref.startswith(CROSS_PROJECT_PREFIX):
        # Qualified: @project:type:slug
        rest = ref[len(CROSS_PROJECT_PREFIX) :]
        parts = rest.split(":", 2)
        if len(parts) < 3:
            raise ValueError(
                f"Invalid cross-project reference '{ref}'. "
                f"Expected format: @project:node_type:slug"
            )
        project = parts[0]
        node_id = f"{parts[1]}:{parts[2]}"
        if not project:
            raise ValueError(f"Empty project in cross-project reference '{ref}'")
        return project, node_id

    # Unqualified: type:slug
    if ":" not in ref:
        raise ValueError(f"Invalid node reference '{ref}'. Expected format: node_type:slug")
    return default_project, ref


def is_cross_project_ref(ref: str) -> bool:
    """Check whether a node reference is a cross-project reference."""
    return ref.startswith(CROSS_PROJECT_PREFIX)


def format_node_ref(project: str, node_id: str, current_project: str) -> str:
    """Format a node reference, qualifying it only if it's cross-project.

    Args:
        project: The project the node belongs to.
        node_id: The node ID (type:slug).
        current_project: The current/default project.

    Returns:
        Unqualified node_id if same project, otherwise @project:node_id.
    """
    if project == current_project:
        return node_id
    return f"{CROSS_PROJECT_PREFIX}{project}:{node_id}"


class RelationshipType(StrEnum):
    """Types of relationships between nodes."""

    DEPENDS_ON = "depends_on"
    USES = "uses"
    RUNS_ON = "runs_on"
    HOSTED_BY = "hosted_by"
    MEMBER_OF = "member_of"
    CONTAINS = "contains"
    CONNECTS_TO = "connects_to"
    FRONTED_BY = "fronted_by"
    RESOLVES_TO = "resolves_to"
    ROUTES_TO = "routes_to"
    USES_STORAGE = "uses_storage"
    MOUNTS = "mounts"
    READS_FROM = "reads_from"
    WRITES_TO = "writes_to"
    REPLICATES_TO = "replicates_to"
    # Physical / datacenter layer (added in ic 0.4.0)
    LOCATED_IN = "located_in"  # Physical containment, child -> container
    POWERED_BY = "powered_by"  # Power draw, consumer -> supplier
    MANAGES = "manages"  # Out-of-band control, controller -> host


# Relationship constraints: (source_type, target_type) -> [allowed_relationship_types]
RELATIONSHIP_CONSTRAINTS: dict[tuple[str, str], list[str]] = {
    # Application relationships
    ("application", "service"): ["contains", "depends_on", "uses"],
    ("application", "service_cluster"): ["contains", "depends_on", "runs_on", "uses"],
    ("application", "domain"): ["uses"],
    ("application", "external_service"): ["depends_on", "uses"],
    ("application", "cdn_endpoint"): ["uses"],
    ("application", "vm"): ["runs_on", "depends_on", "uses"],
    ("application", "lxc_container"): ["runs_on", "depends_on", "uses"],
    ("application", "application"): ["depends_on", "uses"],
    ("application", "nfs_share"): ["mounts", "uses_storage"],
    ("application", "physical_host"): ["depends_on", "uses"],
    # Service relationships
    ("service", "service"): ["depends_on", "connects_to", "uses"],
    ("service", "vm"): ["runs_on", "hosted_by"],
    ("service", "lxc_container"): ["runs_on", "hosted_by"],
    ("service", "oci_container"): ["runs_on", "hosted_by"],
    ("service", "docker_compose"): ["runs_on", "member_of"],
    ("service", "podman_compose"): ["runs_on", "member_of"],
    ("service", "podman_quadlet"): ["runs_on", "member_of"],
    ("service", "physical_host"): ["runs_on", "hosted_by"],
    ("service", "nfs_share"): ["mounts", "uses_storage", "reads_from", "writes_to"],
    ("service", "filesystem"): ["mounts", "reads_from", "writes_to"],
    ("service", "block_storage"): ["uses_storage", "mounts"],
    ("service", "object_storage"): ["uses_storage", "reads_from", "writes_to"],
    ("service", "external_service"): ["depends_on", "connects_to", "uses"],
    ("service", "network"): ["connects_to", "member_of"],
    ("service", "subnet"): ["connects_to", "member_of"],
    # Service cluster relationships
    ("service_cluster", "service"): ["contains", "member_of"],
    ("service_cluster", "vm"): ["runs_on", "connects_to", "depends_on"],
    ("service_cluster", "service_cluster"): ["connects_to", "depends_on"],
    ("service_cluster", "physical_host"): ["connects_to", "depends_on"],
    ("service_cluster", "nfs_share"): ["mounts", "uses_storage"],
    ("service_cluster", "storage"): ["uses_storage"],
    # VM relationships
    ("vm", "physical_host"): ["runs_on", "hosted_by"],
    ("vm", "hypervisor_cluster"): ["member_of", "runs_on"],
    ("vm", "vm"): ["depends_on", "connects_to", "routes_to", "fronted_by"],
    ("vm", "block_storage"): ["uses_storage", "mounts"],
    ("vm", "nfs_share"): ["mounts"],
    ("vm", "network"): ["connects_to", "member_of"],
    ("vm", "subnet"): ["connects_to", "member_of"],
    ("vm", "storage"): ["uses_storage", "mounts"],
    ("vm", "lxc_container"): ["connects_to", "routes_to", "depends_on"],
    ("vm", "service_cluster"): ["member_of", "connects_to", "routes_to", "fronted_by"],
    # LXC Container relationships
    ("lxc_container", "vm"): ["runs_on", "hosted_by", "connects_to", "depends_on"],
    ("lxc_container", "physical_host"): ["runs_on", "hosted_by"],
    ("lxc_container", "hypervisor_cluster"): ["member_of", "runs_on"],
    ("lxc_container", "lxc_container"): ["depends_on", "connects_to"],
    ("lxc_container", "network"): ["connects_to", "member_of"],
    ("lxc_container", "storage"): ["uses_storage", "mounts"],
    ("lxc_container", "nfs_share"): ["mounts"],
    ("lxc_container", "service_cluster"): ["member_of"],
    # OCI Container relationships
    ("oci_container", "vm"): ["runs_on", "hosted_by", "connects_to", "depends_on"],
    ("oci_container", "lxc_container"): ["runs_on", "hosted_by"],
    ("oci_container", "physical_host"): ["runs_on", "hosted_by"],
    ("oci_container", "docker_compose"): ["member_of"],
    ("oci_container", "podman_compose"): ["member_of"],
    ("oci_container", "podman_quadlet"): ["member_of"],
    ("oci_container", "oci_container"): ["depends_on", "connects_to"],
    ("oci_container", "network"): ["connects_to", "member_of"],
    ("oci_container", "storage"): ["uses_storage", "mounts"],
    ("oci_container", "nfs_share"): ["mounts"],
    # Docker Compose relationships
    ("docker_compose", "vm"): ["runs_on", "hosted_by"],
    ("docker_compose", "lxc_container"): ["runs_on", "hosted_by"],
    ("docker_compose", "physical_host"): ["runs_on", "hosted_by"],
    ("docker_compose", "oci_container"): ["contains"],
    # Podman Compose relationships
    ("podman_compose", "vm"): ["runs_on", "hosted_by"],
    ("podman_compose", "lxc_container"): ["runs_on", "hosted_by"],
    ("podman_compose", "physical_host"): ["runs_on", "hosted_by"],
    ("podman_compose", "oci_container"): ["contains"],
    # Podman Quadlet relationships
    ("podman_quadlet", "vm"): ["runs_on", "hosted_by"],
    ("podman_quadlet", "lxc_container"): ["runs_on", "hosted_by"],
    ("podman_quadlet", "physical_host"): ["runs_on", "hosted_by"],
    ("podman_quadlet", "oci_container"): ["contains"],
    # Physical host relationships
    ("physical_host", "hypervisor_cluster"): ["member_of"],
    ("physical_host", "ceph_cluster"): ["member_of"],
    ("physical_host", "network"): ["connects_to", "member_of"],
    ("physical_host", "subnet"): ["connects_to", "member_of"],
    ("physical_host", "storage"): ["uses_storage", "mounts"],
    ("physical_host", "nfs_share"): ["mounts"],
    ("physical_host", "network_device"): ["connects_to"],
    # Network device relationships (switches, routers, firewalls, BMCs)
    ("network_device", "network"): ["connects_to", "member_of"],
    ("network_device", "subnet"): ["connects_to", "member_of"],
    ("network_device", "network_device"): ["connects_to", "routes_to"],
    # A BMC/iLO is modeled as a network_device that also `manages` its host.
    ("network_device", "physical_host"): ["connects_to", "manages"],
    # DNS relationships
    ("domain", "service"): ["resolves_to"],
    ("domain", "vm"): ["resolves_to"],
    ("domain", "external_service"): ["resolves_to"],
    ("domain", "cdn_endpoint"): ["resolves_to"],
    ("domain", "domain"): ["depends_on"],
    ("dns_zone", "domain"): ["contains"],
    # Storage relationships
    ("nfs_share", "physical_host"): ["hosted_by", "runs_on"],
    ("nfs_share", "vm"): ["hosted_by", "runs_on"],
    ("ceph_cluster", "physical_host"): ["contains", "runs_on"],
    ("filesystem", "vm"): ["hosted_by"],
    ("filesystem", "physical_host"): ["hosted_by"],
    ("filesystem", "lxc_container"): ["hosted_by"],
    ("block_storage", "ceph_cluster"): ["hosted_by", "member_of"],
    ("storage", "physical_host"): ["hosted_by", "runs_on"],
    ("storage", "vm"): ["hosted_by", "runs_on"],
    # Kubernetes relationships
    ("k8s_cluster", "physical_host"): ["runs_on"],
    ("k8s_cluster", "vm"): ["runs_on"],
    ("k8s_node", "k8s_cluster"): ["member_of"],
    ("k8s_node", "vm"): ["runs_on", "hosted_by"],
    ("k8s_node", "physical_host"): ["runs_on", "hosted_by"],
    ("k8s_namespace", "k8s_cluster"): ["member_of"],
    ("k8s_pod", "k8s_namespace"): ["member_of", "runs_on"],
    ("k8s_pod", "k8s_node"): ["runs_on"],
    ("k8s_deployment", "k8s_namespace"): ["member_of"],
    ("k8s_deployment", "k8s_pod"): ["contains"],
    ("k8s_service", "k8s_namespace"): ["member_of"],
    ("k8s_service", "k8s_pod"): ["routes_to"],
    ("k8s_service", "k8s_deployment"): ["routes_to"],
    # Network infrastructure
    ("subnet", "network"): ["member_of"],
    # Physical / datacenter layer (added in ic 0.4.0). Kept minimal on purpose:
    # doctor's constraint warning already tells users to extend the matrix.
    # Physical containment (child located_in container).
    ("physical_host", "rack"): ["located_in"],
    ("network_device", "rack"): ["located_in"],
    ("pdu", "rack"): ["located_in"],
    ("ups", "rack"): ["located_in"],
    ("physical_host", "site"): ["located_in"],
    ("network_device", "site"): ["located_in"],
    ("pdu", "site"): ["located_in"],
    ("rack", "site"): ["located_in"],
    # Power draw (consumer powered_by supplier). pdu->pdu is a daisy chain;
    # ups->site is the building feed. A UPS is both located_in and fed by its site.
    ("physical_host", "pdu"): ["powered_by"],
    ("network_device", "pdu"): ["powered_by"],
    ("pdu", "ups"): ["powered_by"],
    ("pdu", "pdu"): ["powered_by"],
    ("ups", "site"): ["located_in", "powered_by"],
    # Containment read the other way (container contains child). Both
    # directions are legal; doctor treats each independently.
    ("rack", "physical_host"): ["contains"],
    ("rack", "network_device"): ["contains"],
    ("rack", "pdu"): ["contains"],
    ("rack", "ups"): ["contains"],
    ("site", "rack"): ["contains"],
}


def get_valid_relationship_types(source_type: str, target_type: str) -> list[str]:
    """Get valid relationship types for a source->target node type combination."""
    return RELATIONSHIP_CONSTRAINTS.get((source_type, target_type), [])


def get_valid_targets_for_source(source_type: str) -> dict[str, list[str]]:
    """Get all valid target types and their allowed relationships for a source type."""
    result: dict[str, list[str]] = {}
    for (src, tgt), rel_types in RELATIONSHIP_CONSTRAINTS.items():
        if src == source_type:
            result[tgt] = rel_types
    return result


def get_all_valid_pairs_for_type(rel_type: str) -> list[tuple[str, str]]:
    """Get all valid (source_type, target_type) pairs for a relationship type."""
    pairs = []
    for (src, tgt), rel_types in RELATIONSHIP_CONSTRAINTS.items():
        if rel_type in rel_types:
            pairs.append((src, tgt))
    return pairs


class Relationship(BaseModel):
    """A directed relationship between two nodes."""

    source: str = Field(..., description="Source node ID")
    target: str = Field(..., description="Target node ID")
    # Forward-compat: tolerate relationship types from newer versions -- known
    # values become RelationshipType members, unknown strings are preserved
    # verbatim (see Node.type for rationale). `ic doctor` warns about them.
    type: RelationshipType | str = Field(union_mode="left_to_right")
    description: str | None = None
    managed_by: str | None = Field(
        default=None, description="Source that manages this relationship (null = user-defined)"
    )
    attributes: dict[str, str | int | bool] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_not_self_referential(self) -> Relationship:
        if self.source == self.target:
            raise ValueError("A node cannot have a relationship with itself")
        return self


class RelationshipFile(BaseModel):
    """Container for relationships stored in YAML."""

    version: str = Field(default="2.0")
    relationships: list[Relationship] = Field(default_factory=list)

    model_config = {"extra": "forbid"}
