"""Node model - the primary infrastructure entity."""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field

from infracontext.models.endpoint import Endpoint
from infracontext.models.function import Function


class NodeType(StrEnum):
    """Types of infrastructure nodes."""

    # Business Layer
    APPLICATION = "application"
    # Service Layer
    SERVICE = "service"
    SERVICE_CLUSTER = "service_cluster"
    # Compute - Physical
    PHYSICAL_HOST = "physical_host"
    # Compute - Virtualization
    HYPERVISOR_CLUSTER = "hypervisor_cluster"
    VM = "vm"
    # Compute - Containers (LXC)
    LXC_CONTAINER = "lxc_container"
    # Compute - Containers (OCI/Podman)
    OCI_CONTAINER = "oci_container"
    PODMAN_COMPOSE_PROJECT = "podman_compose"
    PODMAN_QUADLET_PROJECT = "podman_quadlet"
    # Compute - Containers (Docker)
    DOCKER_COMPOSE_PROJECT = "docker_compose"
    # Compute - Kubernetes
    KUBERNETES_CLUSTER = "k8s_cluster"
    KUBERNETES_NODE = "k8s_node"
    KUBERNETES_NAMESPACE = "k8s_namespace"
    KUBERNETES_POD = "k8s_pod"
    KUBERNETES_SERVICE = "k8s_service"
    KUBERNETES_DEPLOYMENT = "k8s_deployment"
    # Storage
    STORAGE = "storage"
    FILESYSTEM = "filesystem"
    NFS_SHARE = "nfs_share"
    CEPH_CLUSTER = "ceph_cluster"
    BLOCK_STORAGE = "block_storage"
    OBJECT_STORAGE = "object_storage"
    # Network
    NETWORK = "network"
    SUBNET = "subnet"
    # DNS
    DOMAIN = "domain"
    DNS_ZONE = "dns_zone"
    # External
    EXTERNAL_SERVICE = "external_service"
    CDN_ENDPOINT = "cdn_endpoint"


# Node types that support SSH-based USE collection
COMPUTE_NODE_TYPES = frozenset(
    {
        NodeType.PHYSICAL_HOST,
        NodeType.VM,
        NodeType.LXC_CONTAINER,
        NodeType.OCI_CONTAINER,
        NodeType.DOCKER_COMPOSE_PROJECT,
        NodeType.PODMAN_COMPOSE_PROJECT,
    }
)


class ObservabilityType(StrEnum):
    """Types of observability endpoints."""

    # Generic types
    METRICS = "metrics"
    LOGS = "logs"
    EVENTS = "events"
    TRACES = "traces"
    DASHBOARD = "dashboard"
    HEALTH = "health"
    # Specific monitoring systems (for query integration)
    PROMETHEUS = "prometheus"
    LOKI = "loki"
    CHECKMK = "checkmk"


def _normalize_observability_type(v: str) -> str:
    """Normalize observability type to lowercase."""
    return v.strip().lower() if isinstance(v, str) else v


class Observability(BaseModel):
    """An observability endpoint for a node.

    For monitoring query integration, use these type-specific fields:
    - prometheus: set 'instance' (e.g., "web-server:9100")
    - loki: set 'selector' (e.g., '{service_name="web"}')
    - checkmk: set 'host_name' (e.g., "web-server.example.com")
    - monit: no extra config needed (queries via SSH to node)
    """

    type: Annotated[str, BeforeValidator(_normalize_observability_type)]
    name: str = ""
    url: str = ""
    credential_hint: str | None = None
    notes: str | None = None
    # Source reference (for multiple sources of same type)
    source: str | None = Field(default=None, description="Source config name (e.g., 'prometheus-prod')")
    # Prometheus-specific: instance label
    instance: str | None = Field(default=None, description="Prometheus instance label (e.g., 'host:9100')")
    # Loki-specific: LogQL selector
    selector: str | None = Field(default=None, description="Loki LogQL selector (e.g., '{service_name=\"web\"}')")
    # CheckMK-specific: host name
    host_name: str | None = Field(default=None, description="CheckMK host name")
    # Monit-specific
    monit_port: int | None = Field(default=None, description="Monit HTTP port for SSH mode (default: 2812)")
    monit_url: str | None = Field(
        default=None, description="Direct Monit HTTP URL (e.g., http://monit.example.com:2812)"
    )

    model_config = {"extra": "forbid"}


class TriageConfig(BaseModel):
    """Triage hints for the Claude agent.

    Keep this minimal - the agent discovers logs, commands, and check methods itself.
    Just tell it what services matter and any relevant context.
    """

    services: list[str] = Field(default_factory=list, description="Services to check (e.g., nginx, postgres)")
    context: str | None = Field(default=None, description="Free-form hints for troubleshooting this node")
    # Access tier override (None = use tenant default)
    tier: int | None = Field(default=None, description="Access tier override (0-4, see AccessTier enum)")
    collector_script: str | None = Field(default=None, description="Override tenant collector script path")

    model_config = {"extra": "forbid"}


class Learning(BaseModel):
    """A learning discovered during triage or operation."""

    date: str = Field(..., description="ISO date when learning was recorded")
    context: str = Field(..., description="What was being investigated")
    finding: str = Field(..., description="What was discovered")
    source: str = Field(default="agent", description="Who added this: 'agent' or 'human'")

    model_config = {"extra": "forbid"}


class Node(BaseModel):
    """An infrastructure node (VM, container, service, etc.)."""

    version: str = Field(default="2.0", description="Schema version")
    id: str = Field(..., description="Stable ID in format type:slug")
    slug: str = Field(..., description="URL-safe identifier")
    type: NodeType
    name: str = Field(..., description="Human-readable name")

    # SSH connection - CRITICAL for triage
    # This is the SSH alias from ~/.ssh/config that Claude should use for all SSH commands
    ssh_alias: str | None = Field(default=None, description="SSH alias for connecting (from ~/.ssh/config)")

    # Source tracking (for nodes synced from external sources)
    source_id: str | None = Field(
        default=None, description="External source reference (e.g., proxmox:cluster1:qemu:100)"
    )
    source: str | None = Field(default=None, description="Source name (e.g., 'proxmox-prod')")
    managed_by: str | None = Field(default=None, description="Source that manages this node (null = user-defined)")

    # Network identity
    ip_addresses: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)

    # Description
    description: str | None = None
    notes: str | None = Field(default=None, description="Free-form notes (Markdown supported)")
    source_paths: list[str] = Field(default_factory=list, description="Local paths to related source code")

    # V2 fields
    endpoints: list[Endpoint] = Field(default_factory=list)
    functions: list[Function] = Field(default_factory=list)
    observability: list[Observability] = Field(default_factory=list)

    # Additional attributes (for source-specific data)
    attributes: dict[str, str | int | bool | list | dict] = Field(default_factory=dict)

    # Triage configuration (hints for the Claude agent)
    triage: TriageConfig | None = None

    # Learnings discovered during triage/operation
    learnings: list[Learning] = Field(default_factory=list, description="Discovered knowledge about this node")

    model_config = {"extra": "forbid"}

    @classmethod
    def make_id(cls, node_type: NodeType, slug: str) -> str:
        """Create a stable node ID from type and slug."""
        return f"{node_type}:{slug}"
