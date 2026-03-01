"""Application configuration."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from infracontext.paths import (
    EnvironmentNotFoundError,
    EnvironmentPaths,
    InvalidProjectSlugError,
    ProjectPaths,
    validate_project_slug,
)
from infracontext.storage import read_model, read_yaml, write_yaml

if TYPE_CHECKING:
    from infracontext.models.project import ProjectConfig

log = logging.getLogger(__name__)

# Keys that have been renamed across schema versions.
# Maps old key -> new key. Matched keys are migrated silently with a warning.
_CONFIG_KEY_RENAMES: dict[str, str] = {
    "active_tenant": "active_project",
}


class AppConfig(BaseModel):
    """Environment-level configuration stored in .infracontext/config.yaml."""

    active_project: str | None = Field(default=None, description="Currently active project slug")

    model_config = {"extra": "forbid"}


def _migrate_config_keys(data: dict) -> dict:
    """Migrate renamed config keys and strip unknown keys.

    Returns a cleaned copy. Logs deprecation warnings for renamed keys
    and warnings for unrecognised keys that are dropped.
    """
    known_fields = set(AppConfig.model_fields)
    result: dict = {}
    for key, value in data.items():
        if key in _CONFIG_KEY_RENAMES:
            new_key = _CONFIG_KEY_RENAMES[key]
            log.warning(
                "config.yaml: '%s' has been renamed to '%s' -- please update your config file",
                key,
                new_key,
            )
            # Only migrate if the new key is not already set
            if new_key not in data:
                result[new_key] = value
        elif key in known_fields:
            result[key] = value
        else:
            log.warning(
                "config.yaml: ignoring unknown key '%s' -- it may be from a newer or older schema version",
                key,
            )
    return result


def load_config(environment: EnvironmentPaths | None = None) -> AppConfig:
    """Load environment configuration from .infracontext/config.yaml."""
    if environment is None:
        try:
            environment = EnvironmentPaths.current()
        except EnvironmentNotFoundError:
            return AppConfig()

    data = read_yaml(environment.config_yaml)
    if not data:
        return AppConfig()
    data = _migrate_config_keys(data)
    return AppConfig.model_validate(data)


def save_config(config: AppConfig, environment: EnvironmentPaths | None = None) -> None:
    """Save environment configuration to .infracontext/config.yaml."""
    if environment is None:
        environment = EnvironmentPaths.current()

    environment.ensure_dirs()
    write_yaml(environment.config_yaml, config.model_dump(exclude_none=True))


def get_active_project(environment: EnvironmentPaths | None = None) -> str | None:
    """Get the currently active project slug.

    Priority:
    1. IC_PROJECT environment variable
    2. active_project in .infracontext/config.yaml
    """
    # Environment variable takes precedence
    if env_project := os.environ.get("IC_PROJECT"):
        return env_project

    return load_config(environment).active_project


def set_active_project(project_slug: str | None, environment: EnvironmentPaths | None = None) -> None:
    """Set the active project in config.yaml."""
    if project_slug is not None:
        project_slug = validate_project_slug(project_slug)
    config = load_config(environment)
    config.active_project = project_slug
    save_config(config, environment)


def load_project_config(project_slug: str, environment: EnvironmentPaths | None = None) -> ProjectConfig | None:
    """Load project configuration from project.yaml.

    Returns None if project.yaml doesn't exist (backward compatibility).
    """
    # Import here to avoid circular dependency
    from infracontext.models.project import ProjectConfig

    try:
        paths = ProjectPaths.for_project(project_slug, environment)
    except InvalidProjectSlugError:
        return None
    project_yaml = paths.root / "project.yaml"
    if not project_yaml.exists():
        return None
    return read_model(project_yaml, ProjectConfig)
