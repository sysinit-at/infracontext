"""Federation: external roots and qualified node IDs.

An *external root* is another .infracontext/ directory whose nodes are visible
in the local view but live in a separate repository. References across roots
use the same ``@scope:type:slug`` syntax as cross-project references; the
``scope`` is resolved first as an external root alias, then as a local project.

The local working directory is referred to as the *local root*. It has the
empty alias ``""``. External roots have non-empty aliases configured in
``.infracontext/config.yaml`` under ``external_roots``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from infracontext.paths import EnvironmentPaths

if TYPE_CHECKING:
    from infracontext.config import ExternalRoot

log = logging.getLogger(__name__)

# Sentinel alias for the local root in API surfaces that need to name it.
LOCAL_ROOT_ALIAS = ""


class ExternalRootError(Exception):
    """Raised when an external root cannot be resolved."""


class ReadOnlyRootError(Exception):
    """Raised when a write is attempted against a read-only external root."""


@dataclass(frozen=True)
class ResolvedRoot:
    """An external root that has been resolved on disk.

    Attributes:
        alias: Root alias as configured; ``""`` for the local root.
        environment: EnvironmentPaths rooted at the resolved path.
        writable: Whether writes are permitted against this root.
        description: Optional human description from config.
    """

    alias: str
    environment: EnvironmentPaths
    writable: bool
    description: str | None = None

    @property
    def is_local(self) -> bool:
        return self.alias == LOCAL_ROOT_ALIAS


def _expand_path(raw: str, anchor: Path) -> Path:
    """Expand ``~`` and resolve relative paths against ``anchor``."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (anchor / p).resolve(strict=False)
    return p


def resolve_external_root(
    external_root: ExternalRoot,
    *,
    anchor: Path,
) -> ResolvedRoot:
    """Resolve a single :class:`ExternalRoot` configuration into a :class:`ResolvedRoot`.

    Args:
        external_root: An :class:`infracontext.config.ExternalRoot` instance.
        anchor: Directory used as the base for relative ``path`` entries
            (typically the local environment root).

    Raises:
        ExternalRootError: If the path does not contain a ``.infracontext/`` dir.
    """
    from infracontext.config import ExternalRootMode

    path = _expand_path(external_root.path, anchor)
    if not (path / ".infracontext").is_dir():
        raise ExternalRootError(
            f"External root '{external_root.alias}' at {path} does not contain a "
            f".infracontext/ directory."
        )
    env = EnvironmentPaths.from_root(path)
    writable = external_root.mode == ExternalRootMode.READ_WRITE
    return ResolvedRoot(
        alias=external_root.alias,
        environment=env,
        writable=writable,
        description=external_root.description,
    )


def load_external_roots(
    local_environment: EnvironmentPaths | None = None,
) -> dict[str, ResolvedRoot]:
    """Load and resolve all external roots configured for the local environment.

    Returns a mapping from alias to :class:`ResolvedRoot`. Unresolvable roots
    are logged and skipped rather than raised, so a missing fleet repo doesn't
    break local-only commands.
    """
    # Local import to avoid module-load-time circular dependency with config.
    from infracontext.config import AppConfig, load_config

    if local_environment is None:
        from infracontext.paths import EnvironmentNotFoundError

        try:
            local_environment = EnvironmentPaths.current()
        except EnvironmentNotFoundError:
            return {}

    config: AppConfig = load_config(local_environment)
    resolved: dict[str, ResolvedRoot] = {}
    for entry in config.external_roots:
        try:
            resolved[entry.alias] = resolve_external_root(entry, anchor=local_environment.root)
        except ExternalRootError as exc:
            log.warning("Skipping external root '%s': %s", entry.alias, exc)
    return resolved


def all_roots(
    local_environment: EnvironmentPaths | None = None,
) -> dict[str, ResolvedRoot]:
    """Return a mapping of all roots (local + external).

    The local root is keyed by :data:`LOCAL_ROOT_ALIAS` (``""``) so callers can
    distinguish it from external roots.
    """
    if local_environment is None:
        from infracontext.paths import EnvironmentNotFoundError

        try:
            local_environment = EnvironmentPaths.current()
        except EnvironmentNotFoundError:
            return {}

    roots: dict[str, ResolvedRoot] = {
        LOCAL_ROOT_ALIAS: ResolvedRoot(
            alias=LOCAL_ROOT_ALIAS,
            environment=local_environment,
            writable=True,
            description=None,
        )
    }
    roots.update(load_external_roots(local_environment))
    return roots


def get_root(
    alias: str,
    local_environment: EnvironmentPaths | None = None,
) -> ResolvedRoot | None:
    """Look up a single root by alias. ``""`` returns the local root."""
    return all_roots(local_environment).get(alias)


def require_writable_root(
    alias: str,
    local_environment: EnvironmentPaths | None = None,
) -> ResolvedRoot:
    """Return the root for ``alias`` or raise if not present / not writable."""
    root = get_root(alias, local_environment)
    if root is None:
        raise ExternalRootError(f"No such root: '{alias}'")
    if not root.writable:
        raise ReadOnlyRootError(
            f"Root '{alias}' is read-only. Set mode: read-write in "
            f"external_roots to allow writes."
        )
    return root


@dataclass(frozen=True)
class ResolvedRef:
    """A node reference that has been resolved to a concrete root + project + id.

    The ``scope`` of a qualified reference (``@scope:type:slug``) may name
    either an external root alias or a local project. This struct captures
    the resolution outcome so callers don't have to repeat the lookup.
    """

    root_alias: str  # "" for the local root
    project: str  # Project slug within that root
    node_id: str  # type:slug
    qualified_source: str  # Original ref string (for error messages)


def resolve_node_ref(
    ref: str,
    *,
    default_project: str,
    roots: dict[str, ResolvedRoot] | None = None,
    local_environment: EnvironmentPaths | None = None,
) -> ResolvedRef:
    """Resolve a node reference to (root_alias, project, node_id).

    Qualified refs (``@scope:type:slug``) resolve their scope as follows:

    1. If ``scope`` matches an external root alias, the ref points into that
       root and uses the root's :func:`active_project`.
    2. Otherwise, ``scope`` is treated as a local project slug (the existing
       cross-project reference behavior).

    Unqualified refs (``type:slug``) resolve to the local root and
    ``default_project``.

    Args:
        ref: Node reference string.
        default_project: Project used when the ref is unqualified.
        roots: Optional pre-resolved root map (avoids re-loading config).
        local_environment: Optional explicit local environment.

    Raises:
        ValueError: For malformed references.
    """
    # Local import to avoid circular dependency with relationship module.
    from infracontext.models.relationship import parse_node_ref

    scope, node_id = parse_node_ref(ref, default_project)

    # Unqualified -> always local root.
    if not ref.startswith("@"):
        return ResolvedRef(
            root_alias=LOCAL_ROOT_ALIAS,
            project=default_project,
            node_id=node_id,
            qualified_source=ref,
        )

    if roots is None:
        roots = all_roots(local_environment)

    # External root alias wins over local project of the same name.
    if scope in roots and scope != LOCAL_ROOT_ALIAS:
        # Local import avoids cycle.
        from infracontext.config import get_active_project

        target_root = roots[scope]
        project = get_active_project(target_root.environment)
        if not project:
            raise ValueError(
                f"External root '{scope}' has no active_project configured; "
                f"cannot resolve reference '{ref}'."
            )
        return ResolvedRef(
            root_alias=scope,
            project=project,
            node_id=node_id,
            qualified_source=ref,
        )

    # Falls through to local cross-project reference.
    return ResolvedRef(
        root_alias=LOCAL_ROOT_ALIAS,
        project=scope,
        node_id=node_id,
        qualified_source=ref,
    )
