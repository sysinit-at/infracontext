"""Path management for infracontext - repo-centric architecture.

infracontext stores all data in a .infracontext/ directory within your environment repo.
Local overrides (ssh_alias, source_paths) go in .infracontext.local.yaml (gitignored).
"""

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel

INFRACONTEXT_DIR = ".infracontext"
LOCAL_OVERRIDES_FILE = ".infracontext.local.yaml"

log = logging.getLogger(__name__)


class EnvironmentNotFoundError(Exception):
    """Raised when no .infracontext/ directory is found."""

    pass


class InvalidProjectSlugError(ValueError):
    """Raised when a project slug is invalid or unsafe."""

    pass


_PROJECT_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$")


def validate_project_slug(project_slug: str) -> str:
    """Validate project slug format and reject unsafe values."""
    slug = project_slug.strip()
    if not slug:
        raise InvalidProjectSlugError("Project name cannot be empty.")

    if not _PROJECT_SLUG_RE.fullmatch(slug):
        raise InvalidProjectSlugError(
            "Invalid project name. Use letters, numbers, dots, underscores, and hyphens "
            "(optional one-level hierarchy: customer/project)."
        )

    return slug


def _validate_path_component(value: str, component_name: str) -> str:
    """Validate single path component used under project directories."""
    part = value.strip()
    if not part:
        raise ValueError(f"{component_name} cannot be empty.")
    if "/" in part or "\\" in part:
        raise ValueError(f"Invalid {component_name}: path separators are not allowed.")
    if part in {".", ".."}:
        raise ValueError(f"Invalid {component_name}: '{part}' is not allowed.")
    return part


def find_git_root(start: Path | None = None) -> Path | None:
    """Find the git repository root, or None if not in a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=start or Path.cwd(),
            check=False,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except FileNotFoundError:
        pass
    return None


def _walk_up_for_environment(start: Path) -> Path | None:
    """Walk up from ``start`` looking for a ``.infracontext/`` directory.

    Stops at the git root if inside a git repo, or the filesystem root
    otherwise. Returns None if nothing is found.
    """
    current = start.resolve()
    git_root = find_git_root(current)
    stop_at = git_root or Path(current.anchor)

    while current >= stop_at:
        if (current / INFRACONTEXT_DIR).is_dir():
            return current
        if current == stop_at:
            break
        current = current.parent

    return None


def _environment_root_from_ic_root() -> Path | None:
    """Resolve the ``IC_ROOT`` override, or None if unset/invalid.

    An invalid ``IC_ROOT`` (set but not pointing at a ``.infracontext/`` repo)
    is a likely typo, so we warn to stderr and fall through to the cwd walk-up
    rather than silently ignoring it.
    """
    raw = os.environ.get("IC_ROOT")
    if not raw:
        return None
    root = Path(raw).expanduser().resolve()
    if (root / INFRACONTEXT_DIR).is_dir():
        return root
    print(
        f"ic: IC_ROOT={raw!r} does not contain a {INFRACONTEXT_DIR}/ directory; ignoring.",
        file=sys.stderr,
    )
    return None


def _environment_root_from_registry() -> Path | None:
    """Resolve the registered default environment, or None. Never raises."""
    try:
        from infracontext.envregistry import default_environment_root

        return default_environment_root()
    except Exception:  # pragma: no cover - registry issues must never crash discovery
        return None


def find_environment_root(start: Path | None = None) -> Path | None:
    """Find the environment root containing a ``.infracontext/`` directory.

    Resolution order (when called with no explicit ``start``):

    1. ``IC_ROOT`` environment variable, if it points at a valid environment.
    2. Walk up from the current working directory (the original behavior).
    3. The default environment registered via ``ic config env`` (see
       :mod:`infracontext.envregistry`).

    When ``start`` is provided (internal callers and tests), only the walk-up
    is performed -- ``IC_ROOT`` and the registry apply strictly to global
    discovery so they can't leak into scoped lookups.

    Returns None if no ``.infracontext/`` directory is found.
    """
    if start is not None:
        return _walk_up_for_environment(start)

    ic_root = _environment_root_from_ic_root()
    if ic_root is not None:
        return ic_root

    found = _walk_up_for_environment(Path.cwd())
    if found is not None:
        return found

    return _environment_root_from_registry()


def require_environment_root() -> Path:
    """Get environment root or raise EnvironmentNotFoundError."""
    root = find_environment_root()
    if root is None:
        raise EnvironmentNotFoundError(f"No {INFRACONTEXT_DIR}/ directory found. Run 'ic init' to create one.")
    return root


def _detect_legacy_tenants_dir(environment_root: Path) -> Path | None:
    """Check for a legacy tenants/ directory inside .infracontext/.

    Returns the path if it exists *and contains anything*, None otherwise.
    An empty leftover directory has nothing to migrate -- warning about it
    on every command would be pure noise.
    """
    tenants_dir = environment_root / INFRACONTEXT_DIR / "tenants"
    if tenants_dir.is_dir() and any(tenants_dir.iterdir()):
        return tenants_dir
    return None


class EnvironmentPaths(BaseModel):
    """Path structure for an environment's infracontext data."""

    root: Path  # Environment root (where .infracontext/ lives)
    infracontext_dir: Path  # .infracontext/
    config_yaml: Path  # .infracontext/config.yaml
    projects_dir: Path  # .infracontext/projects/
    local_overrides: Path  # .infracontext.local.yaml

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def from_root(cls, root: Path) -> EnvironmentPaths:
        """Create environment paths from a root directory."""
        ic_dir = root / INFRACONTEXT_DIR
        return cls(
            root=root,
            infracontext_dir=ic_dir,
            config_yaml=ic_dir / "config.yaml",
            projects_dir=ic_dir / "projects",
            local_overrides=root / LOCAL_OVERRIDES_FILE,
        )

    @classmethod
    def current(cls) -> EnvironmentPaths:
        """Get paths for the current environment (auto-discovered)."""
        return cls.from_root(require_environment_root())

    def ensure_dirs(self) -> None:
        """Create the base directory structure."""
        self.infracontext_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(exist_ok=True)


class ProjectPaths(BaseModel):
    """Path structure for a project's data within an environment."""

    root: Path  # .infracontext/projects/<project>/
    nodes_dir: Path
    relationships_yaml: Path
    sources_dir: Path

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def for_project(cls, project_slug: str, environment: EnvironmentPaths | None = None) -> ProjectPaths:
        """Create path structure for a project."""
        if environment is None:
            environment = EnvironmentPaths.current()

        slug = validate_project_slug(project_slug)
        projects_root = environment.projects_dir.resolve(strict=False)
        # Check literal path before resolving to catch traversal without symlinks
        literal = projects_root / slug
        try:
            literal.relative_to(projects_root)
        except ValueError as e:
            raise InvalidProjectSlugError("Project path escapes the projects directory.") from e
        # Check again after resolving to catch symlink-based escapes
        root = literal.resolve(strict=False)
        try:
            root.relative_to(projects_root)
        except ValueError as e:
            raise InvalidProjectSlugError("Project path escapes the projects directory.") from e

        return cls(
            root=root,
            nodes_dir=root / "nodes",
            relationships_yaml=root / "relationships.yaml",
            sources_dir=root / "sources",
        )

    def node_type_dir(self, node_type: str) -> Path:
        """Get the directory for a specific node type."""
        return self.nodes_dir / _validate_path_component(node_type, "node type")

    def node_file(self, node_type: str, slug: str) -> Path:
        """Get the path to a specific node's YAML file."""
        safe_slug = _validate_path_component(slug, "node slug")
        return self.node_type_dir(node_type) / f"{safe_slug}.yaml"

    def source_file(self, source_name: str) -> Path:
        """Get the path to a source configuration file."""
        safe_name = _validate_path_component(source_name, "source name")
        return self.sources_dir / f"{safe_name}.yaml"

    def ensure_dirs(self) -> None:
        """Create all necessary directories for this project."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.nodes_dir.mkdir(exist_ok=True)
        self.sources_dir.mkdir(exist_ok=True)


def list_projects(environment: EnvironmentPaths | None = None) -> list[str]:
    """List all project slugs in the environment.

    Also checks for a legacy tenants/ directory and logs a warning
    if one is found, directing the user to rename it.
    """
    if environment is None:
        try:
            environment = EnvironmentPaths.current()
        except EnvironmentNotFoundError:
            return []

    # Detect legacy tenants/ directory
    legacy_dir = _detect_legacy_tenants_dir(environment.root)
    if legacy_dir is not None:
        log.warning(
            "Found legacy 'tenants/' directory at %s. "
            "Rename it to 'projects/' or run 'ic migrate legacy' to migrate.",
            legacy_dir,
        )

    if not environment.projects_dir.exists():
        return []

    projects = []
    # Look for directories containing nodes/ or relationships.yaml
    for item in environment.projects_dir.rglob("nodes"):
        if item.is_dir():
            rel_path = item.parent.relative_to(environment.projects_dir)
            projects.append(str(rel_path))

    return sorted(set(projects))


def project_exists(slug: str, environment: EnvironmentPaths | None = None) -> bool:
    """Check if a project exists."""
    try:
        paths = ProjectPaths.for_project(slug, environment)
    except InvalidProjectSlugError:
        return False
    return paths.nodes_dir.exists() or paths.relationships_yaml.exists()
