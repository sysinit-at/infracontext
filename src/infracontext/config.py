"""Application configuration."""

from __future__ import annotations

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


class AppConfig(BaseModel):
    """Environment-level configuration stored in .infracontext/config.yaml."""

    active_project: str | None = Field(default=None, description="Currently active project slug")

    model_config = {"extra": "forbid"}


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
