"""Relationship model for connections between nodes."""

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


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


# Relationship constraints: (source_type, target_type) -> [allowed_relationship_types]
RELATIONSHIP_CONSTRAINTS: dict[tuple[str, str], list[str]] = {
    # Application relationships
    ("application", "service"): ["contains", "depends_on", "uses"],
    ("application", "service_cluster"): ["contains", "depends_on"],
    ("application", "domain"): ["uses"],
    ("application", "external_service"): ["depends_on", "uses"],
    ("application", "cdn_endpoint"): ["uses"],
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
    ("service_cluster", "vm"): ["runs_on"],
    # VM relationships
    ("vm", "physical_host"): ["runs_on", "hosted_by"],
    ("vm", "hypervisor_cluster"): ["member_of", "runs_on"],
    ("vm", "vm"): ["depends_on", "connects_to", "routes_to", "fronted_by"],
    ("vm", "block_storage"): ["uses_storage", "mounts"],
    ("vm", "nfs_share"): ["mounts"],
    ("vm", "network"): ["connects_to", "member_of"],
    ("vm", "subnet"): ["connects_to", "member_of"],
    ("vm", "storage"): ["uses_storage", "mounts"],
    # LXC Container relationships
    ("lxc_container", "vm"): ["runs_on", "hosted_by"],
    ("lxc_container", "physical_host"): ["runs_on", "hosted_by"],
    ("lxc_container", "lxc_container"): ["depends_on", "connects_to"],
    ("lxc_container", "network"): ["connects_to", "member_of"],
    ("lxc_container", "storage"): ["uses_storage", "mounts"],
    # OCI Container relationships
    ("oci_container", "vm"): ["runs_on", "hosted_by"],
    ("oci_container", "lxc_container"): ["runs_on", "hosted_by"],
    ("oci_container", "physical_host"): ["runs_on", "hosted_by"],
    ("oci_container", "docker_compose"): ["member_of"],
    ("oci_container", "podman_compose"): ["member_of"],
    ("oci_container", "podman_quadlet"): ["member_of"],
    ("oci_container", "oci_container"): ["depends_on", "connects_to"],
    ("oci_container", "network"): ["connects_to", "member_of"],
    ("oci_container", "storage"): ["uses_storage", "mounts"],
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
    ("physical_host", "storage"): ["uses_storage"],
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
    type: RelationshipType
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
