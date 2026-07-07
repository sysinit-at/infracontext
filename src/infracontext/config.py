"""Application configuration."""

from __future__ import annotations

import logging
import os
import re
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

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


class ConfigError(ValueError):
    """Raised when .infracontext/config.yaml is present but schema-invalid.

    Inherits from :class:`ValueError` to match the codebase convention for
    user-facing configuration errors (e.g.
    :class:`infracontext.paths.InvalidProjectSlugError`) so existing
    ``except ValueError`` handlers surface it cleanly instead of dumping a
    raw pydantic traceback.
    """

# Keys that have been renamed across schema versions.
# Maps old key -> new key. Matched keys are migrated silently with a warning.
_CONFIG_KEY_RENAMES: dict[str, str] = {
    "active_tenant": "active_project",
}


_EXTERNAL_ROOT_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


class ExternalRootMode(StrEnum):
    """Write mode for an external root."""

    READ_ONLY = "read-only"
    READ_WRITE = "read-write"


class ExternalRoot(BaseModel):
    """An external infracontext repository included into the local view.

    The local repository acts as the working environment. External roots are
    additional .infracontext/ directories (usually other git repos) that
    contribute nodes and relationships into the federated view.

    External roots are referenced via qualified node IDs (``@alias:type:slug``)
    in the same syntax as cross-project references. Their resolution is
    handled by the federation module.
    """

    alias: str = Field(
        ...,
        description="Short identifier used to reference this root (e.g., 'fleet')",
    )
    path: str = Field(
        ...,
        description="Path to the root directory containing .infracontext/ (supports ~ expansion)",
    )
    mode: ExternalRootMode = Field(
        default=ExternalRootMode.READ_ONLY,
        description="Write mode. Read-only (default) refuses writes; read-write allows edits",
    )
    description: str | None = Field(
        default=None,
        description="Free-form description of what this root contains",
    )

    model_config = {"extra": "forbid"}

    @field_validator("alias")
    @classmethod
    def _validate_alias(cls, value: str) -> str:
        if not _EXTERNAL_ROOT_ALIAS_RE.fullmatch(value):
            raise ValueError(
                f"Invalid external root alias '{value}'. "
                "Aliases must start with a lowercase letter and contain only "
                "lowercase letters, digits, hyphens, and underscores."
            )
        return value


class AppConfig(BaseModel):
    """Environment-level configuration stored in .infracontext/config.yaml."""

    active_project: str | None = Field(default=None, description="Currently active project slug")
    external_roots: list[ExternalRoot] = Field(
        default_factory=list,
        description="External infracontext repositories federated into this view",
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _validate_unique_aliases(self) -> AppConfig:
        seen: set[str] = set()
        for root in self.external_roots:
            if root.alias in seen:
                raise ValueError(f"Duplicate external root alias '{root.alias}'")
            seen.add(root.alias)
        return self


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


def _format_config_loc(loc: tuple[object, ...]) -> str:
    """Render a pydantic error location as a config key path.

    Ints become bracketed indices so ``('external_roots', 0, 'mode')``
    reads as ``external_roots[0].mode``.
    """
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        elif parts:
            parts.append(f".{item}")
        else:
            parts.append(str(item))
    return "".join(parts) or "<root>"


def _config_error(path: EnvironmentPaths, exc: ValidationError) -> ConfigError:
    """Build an actionable :class:`ConfigError` from a pydantic failure."""
    details = []
    for error in exc.errors():
        loc = _format_config_loc(error["loc"])
        msg = error["msg"]
        if "input" in error:
            details.append(f"{loc}: {msg} (got {error['input']!r})")
        else:
            details.append(f"{loc}: {msg}")
    return ConfigError(f"invalid {path.config_yaml}: " + "; ".join(details))


def load_config(environment: EnvironmentPaths | None = None) -> AppConfig:
    """Load environment configuration from .infracontext/config.yaml.

    Raises:
        ConfigError: If the file exists but violates the schema (e.g. an
            invalid ``external_roots`` entry). Callers that must not abort on
            a broken config (doctor, federation) catch this explicitly.
    """
    if environment is None:
        try:
            environment = EnvironmentPaths.current()
        except EnvironmentNotFoundError:
            return AppConfig()

    data = read_yaml(environment.config_yaml)
    if not data:
        return AppConfig()
    data = _migrate_config_keys(data)
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise _config_error(environment, exc) from exc


def save_config(config: AppConfig, environment: EnvironmentPaths | None = None) -> None:
    """Save environment configuration to .infracontext/config.yaml."""
    if environment is None:
        environment = EnvironmentPaths.current()

    environment.ensure_dirs()
    # mode="json" so StrEnum fields (e.g. ExternalRoot.mode) serialize as strings.
    write_yaml(environment.config_yaml, config.model_dump(mode="json", exclude_none=True))


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
