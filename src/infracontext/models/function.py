"""Function model for node capabilities."""

from enum import StrEnum

from pydantic import BaseModel, Field


class FunctionType(StrEnum):
    """Types of functions that nodes can perform."""

    # Networking functions
    REVERSE_PROXY = "reverse-proxy"
    LOAD_BALANCER = "load-balancer"
    FIREWALL = "firewall"
    GATEWAY = "gateway"
    VPN = "vpn"

    # Web serving
    WEB_SERVER = "web-server"
    APP_SERVER = "app-server"
    API_SERVER = "api-server"

    # Data services
    DATABASE = "database"
    CACHE = "cache"
    SEARCH = "search"
    MESSAGE_QUEUE = "message-queue"

    # Storage
    NFS_SERVER = "nfs-server"
    STORAGE = "storage"
    BACKUP = "backup"

    # Monitoring/Ops
    MONITORING = "monitoring"
    LOGGING = "logging"
    SCHEDULER = "scheduler"

    # Other
    CUSTOM = "custom"


class BackendGroup(BaseModel):
    """Backend group configuration for load balancing/routing."""

    nodes: list[str] = Field(default_factory=list, description="List of node IDs in this backend group")
    health_check: str | None = Field(default=None, description="Health check endpoint or command")
    attributes: dict[str, str | int | bool] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class Function(BaseModel):
    """A capability or service that a node performs."""

    name: FunctionType = Field(..., description="Type of function (e.g., 'reverse-proxy', 'database')")
    endpoints: list[str] = Field(default_factory=list, description="Endpoint names this function binds to")
    backend_groups: dict[str, BackendGroup] = Field(
        default_factory=dict,
        description="Named backend groups for load balancing/routing",
    )
    applications: list[str] = Field(default_factory=list, description="Application tags this function belongs to")
    attributes: dict[str, str | int | bool] = Field(default_factory=dict, description="Function-specific configuration")

    model_config = {"extra": "forbid"}
