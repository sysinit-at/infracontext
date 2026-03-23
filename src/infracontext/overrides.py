"""Local overrides for node properties.

Local overrides allow team members to customize machine-specific settings
without modifying the shared node definitions. Stored in .infracontext.local.yaml.

Only specific fields can be overridden:
- ssh_alias: Different team members may have different SSH configs
- source_paths: Local paths to source code checkouts
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

from infracontext.paths import EnvironmentPaths
from infracontext.storage import read_yaml

log = logging.getLogger(__name__)


class NodeOverrides(BaseModel):
    """Overridable fields for a single node."""

    ssh_alias: str | None = Field(default=None, description="SSH alias override")
    source_paths: list[str] | None = Field(default=None, description="Local source paths override (must be absolute)")

    model_config = {"extra": "forbid"}

    @field_validator("source_paths")
    @classmethod
    def validate_absolute_paths(cls, v: list[str] | None) -> list[str] | None:
        """Ensure all source paths are absolute."""
        if v is None:
            return v
        for p in v:
            if not Path(p).is_absolute():
                raise ValueError(f"source_paths must be absolute, got relative path: {p}")
        return v


class LocalOverrides(BaseModel):
    """Local overrides loaded from .infracontext.local.yaml."""

    nodes: dict[str, NodeOverrides] = Field(default_factory=dict, description="Per-node overrides keyed by node ID")

    model_config = {"extra": "forbid"}


def load_local_overrides(environment: EnvironmentPaths | None = None) -> LocalOverrides:
    """Load local overrides from .infracontext.local.yaml.

    Returns empty overrides if file doesn't exist or can't be parsed.
    """
    if environment is None:
        environment = EnvironmentPaths.current()

    if not environment.local_overrides.exists():
        return LocalOverrides()

    try:
        data = read_yaml(environment.local_overrides)
    except Exception as e:
        log.warning("Failed to parse %s: %s — ignoring local overrides", environment.local_overrides, e)
        return LocalOverrides()

    if not data:
        return LocalOverrides()

    try:
        # Convert raw node dicts to NodeOverrides models
        if "nodes" in data and isinstance(data["nodes"], dict):
            for node_id, overrides in data["nodes"].items():
                if isinstance(overrides, dict):
                    data["nodes"][node_id] = NodeOverrides.model_validate(overrides)

        return LocalOverrides.model_validate(data)
    except (ValidationError, Exception) as e:
        log.warning("Invalid %s: %s — ignoring local overrides", environment.local_overrides, e)
        return LocalOverrides()


def get_node_overrides(node_id: str, environment: EnvironmentPaths | None = None, project: str | None = None) -> NodeOverrides:
    """Get overrides for a specific node.

    Lookup order (first match wins):
      1. ``<project>/<node_id>``  (project-scoped key)
      2. ``<node_id>``            (global key, backwards-compatible)
    """
    overrides = load_local_overrides(environment)
    if project:
        scoped = overrides.nodes.get(f"{project}/{node_id}")
        if scoped is not None:
            return scoped
    return overrides.nodes.get(node_id, NodeOverrides())


def apply_overrides_to_node(
    node_data: dict, node_id: str, environment: EnvironmentPaths | None = None, project: str | None = None
) -> dict:
    """Apply local overrides to a node's data dict.

    Modifies the dict in place and returns it.
    Only overrides non-None values from the local overrides.
    """
    overrides = get_node_overrides(node_id, environment, project)

    if overrides.ssh_alias is not None:
        node_data["ssh_alias"] = overrides.ssh_alias

    if overrides.source_paths is not None:
        node_data["source_paths"] = overrides.source_paths

    return node_data
