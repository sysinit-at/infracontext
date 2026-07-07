"""User-level registry of infracontext environments.

Without this, ``ic`` only works from *inside* an environment repo (it walks up
from the cwd looking for ``.infracontext/``). The registry lets an operator name
their environments once and reach them from anywhere -- either by exporting
``IC_ROOT`` or by registering a default:

    environments:
      home: /Users/me/infra
      work: /Users/me/work/infra
    default: home

The file lives at ``$XDG_CONFIG_HOME/infracontext/environments.yaml`` (falling
back to ``~/.config`` when the XDG variable is unset).

Every read is defensive: a missing or malformed registry yields an empty
registry and a one-line stderr warning rather than a traceback, because
environment discovery runs on *every* ``ic`` invocation and must never be the
thing that crashes a command.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from infracontext.paths import INFRACONTEXT_DIR
from infracontext.storage import StorageError, read_yaml, write_yaml


class EnvironmentRegistry(BaseModel):
    """The parsed ``environments.yaml`` document."""

    environments: dict[str, str] = Field(default_factory=dict)
    default: str | None = None

    # User-level file edited by hand: tolerate unknown keys from newer versions
    # rather than discarding the whole registry over one stray line.
    model_config = {"extra": "ignore"}


def _warn(message: str) -> None:
    """Emit a best-effort warning to stderr (never raises)."""
    print(f"ic: {message}", file=sys.stderr)


def registry_path() -> Path:
    """Return the path to the environment registry file (respects XDG)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    config_root = Path(base).expanduser() if base else Path.home() / ".config"
    return config_root / "infracontext" / "environments.yaml"


def resolve_environment_path(path: Path | str) -> Path:
    """Expand ``~`` and make ``path`` absolute (does not require existence)."""
    return Path(path).expanduser().resolve()


def is_valid_environment(path: Path) -> bool:
    """True when ``path`` contains a ``.infracontext/`` directory."""
    return (path / INFRACONTEXT_DIR).is_dir()


def load_registry() -> EnvironmentRegistry:
    """Load the registry, returning an empty one on any error.

    A missing file is normal (empty registry). A malformed file (bad YAML or a
    schema violation) is reported once on stderr and then treated as empty, so
    a broken registry can never abort a command.
    """
    path = registry_path()
    try:
        data = read_yaml(path)
    except StorageError as e:
        _warn(f"ignoring malformed environment registry {path}: {e}")
        return EnvironmentRegistry()

    if not data:
        return EnvironmentRegistry()

    try:
        return EnvironmentRegistry.model_validate(data)
    except ValidationError as e:
        _warn(f"ignoring malformed environment registry {path}: {e}")
        return EnvironmentRegistry()


def save_registry(registry: EnvironmentRegistry) -> None:
    """Persist the registry to disk (creating parent directories as needed)."""
    write_yaml(registry_path(), registry.model_dump(mode="json", exclude_none=True))


def add_environment(name: str, path: Path | str, *, make_default: bool = False) -> Path:
    """Register ``name`` -> ``path`` and return the resolved absolute path.

    Sets the entry as the default when ``make_default`` is true, or when no
    default exists yet (so the first registered environment is usable for
    default resolution without an extra flag).
    """
    registry = load_registry()
    resolved = resolve_environment_path(path)
    registry.environments[name] = str(resolved)
    if make_default or registry.default is None:
        registry.default = name
    save_registry(registry)
    return resolved


def set_default(name: str) -> None:
    """Mark ``name`` as the default environment.

    Raises:
        KeyError: If ``name`` is not registered.
    """
    registry = load_registry()
    if name not in registry.environments:
        raise KeyError(name)
    registry.default = name
    save_registry(registry)


def remove_environment(name: str) -> bool:
    """Remove ``name`` from the registry. Returns False if it was absent."""
    registry = load_registry()
    if name not in registry.environments:
        return False
    del registry.environments[name]
    if registry.default == name:
        registry.default = None
    save_registry(registry)
    return True


def default_environment_root() -> Path | None:
    """Resolve the default environment's root, or None.

    Returns None (with a stderr warning) when the default points at a missing
    entry or a path that no longer contains ``.infracontext/`` -- discovery
    then falls through to "no environment found" rather than silently using a
    stale path.
    """
    registry = load_registry()
    if not registry.default:
        return None

    path_str = registry.environments.get(registry.default)
    if not path_str:
        _warn(f"registry default '{registry.default}' has no path entry; ignoring.")
        return None

    root = Path(path_str).expanduser()
    if is_valid_environment(root):
        return root

    _warn(
        f"registry default '{registry.default}' -> {root} no longer contains a "
        f"{INFRACONTEXT_DIR}/ directory; ignoring."
    )
    return None
