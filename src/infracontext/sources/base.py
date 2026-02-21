"""Base class for infrastructure source plugins."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel


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
    relationships_created: int = 0
    relationships_deleted: int = 0
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0


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
