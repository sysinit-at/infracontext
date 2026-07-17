"""SSH config source plugin.

Imports nodes from SSH config files. Supports auto-discovery based on project hierarchy.
Convention: Project <customer>/<project> → SSH config at ~/.ssh/conf.d/<customer>/<project>.conf
"""

import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path

from infracontext.models.node import Node, NodeType, slugify
from infracontext.paths import EnvironmentPaths, ProjectPaths
from infracontext.sources.base import (
    NodeChange,
    PlannedNodeWrite,
    SourcePlugin,
    SyncResult,
    SyncStatus,
    apply_node_writes,
    merge_synced_node,
    record_sync_run,
)
from infracontext.sources.dedup import find_duplicate_candidates, load_existing_nodes, overlap_warning
from infracontext.sources.registry import register_plugin
from infracontext.storage import read_model, read_yaml, write_yaml

log = logging.getLogger(__name__)


@dataclass
class SSHHost:
    """Parsed SSH host entry."""

    name: str
    hostname: str | None = None
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None


@dataclass
class SyncStats:
    """Statistics from SSH config sync."""

    nodes_created: int = 0
    nodes_updated: int = 0
    nodes_unchanged: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def is_ip_address(value: str) -> bool:
    """Check if a string is a valid IP address."""
    try:
        ip_address(value)
        return True
    except ValueError:
        return False


def generate_slug(name: str) -> str:
    """Generate a URL-safe slug from a name.

    Thin alias for :func:`infracontext.models.node.slugify`, kept for
    backward compatibility with any external callers of this module.
    """
    return slugify(name)


def parse_ssh_config(path: Path) -> list[SSHHost]:
    """Parse an SSH config file (and any ``Include`` directives) into host entries.

    Skips wildcard patterns (*, ?) and returns only concrete hosts. ``Include``
    directives are resolved recursively: relative paths are interpreted
    against ``~/.ssh/`` (matching OpenSSH), globs are expanded, and recursion
    is capped to prevent pathological include chains or symlink loops.

    Unresolvable include targets are skipped with a debug log rather than
    aborting the whole parse, so a missing optional file doesn't hide the
    hosts that *are* readable.
    """
    return _parse_ssh_config(path, depth=0, seen=set())


# Caps for Include resolution: defend against deep chains and accidental
# explosion via glob. Generous enough for real-world split configs.
_MAX_INCLUDE_DEPTH = 5
_MAX_INCLUDE_FILES = 100


def _parse_ssh_config(path: Path, *, depth: int, seen: set[Path]) -> list[SSHHost]:
    """Recursive worker for :func:`parse_ssh_config`.

    ``seen`` tracks real-path identity to avoid parsing the same file twice
    (symlink loops, or the same file included from multiple places). Its size
    doubles as the file counter for the ``_MAX_INCLUDE_FILES`` cap — the cap
    is on *files parsed*, never on the number of hosts found, so large fleets
    (hundreds of hosts across a handful of fragments) are never truncated.
    """
    try:
        real = path.resolve()
    except OSError:
        real = path
    if real in seen or depth > _MAX_INCLUDE_DEPTH:
        log.debug("Skipping already-seen or too-deep SSH include: %s", path)
        return []
    seen.add(real)
    if len(seen) > _MAX_INCLUDE_FILES:
        # Warn once, exactly when the cap is first exceeded; later files are
        # debug-only so a huge include tree doesn't flood the terminal.
        level = log.warning if len(seen) == _MAX_INCLUDE_FILES + 1 else log.debug
        level(
            "SSH config include expansion reached %d files; ignoring %s and "
            "any further includes",
            _MAX_INCLUDE_FILES,
            path,
        )
        return []

    try:
        text = path.read_text()
    except OSError as e:
        log.debug("Cannot read SSH config %s: %s", path, e)
        return []

    hosts: list[SSHHost] = []
    current_host: SSHHost | None = None

    for line in text.splitlines():
        line = line.strip()

        # Skip empty lines and comments
        if not line or line.startswith("#"):
            continue

        # Case-insensitive directive matching
        line_lower = line.lower()

        if line_lower.startswith("include "):
            included = _resolve_include(line[len("include ") :].strip(), path)
            for inc_path in included:
                hosts.extend(_parse_ssh_config(inc_path, depth=depth + 1, seen=seen))
            continue

        if line_lower.startswith("host "):
            host_pattern = line[5:].strip()

            # Skip wildcards
            if "*" in host_pattern or "?" in host_pattern:
                current_host = None
                continue

            # Handle multiple hosts on same line (take first)
            host_names = host_pattern.split()
            if host_names:
                current_host = SSHHost(name=host_names[0])
                hosts.append(current_host)
            else:
                current_host = None

        elif current_host:
            if line_lower.startswith("hostname "):
                current_host.hostname = line[9:].strip()
            elif line_lower.startswith("user "):
                current_host.user = line[5:].strip()
            elif line_lower.startswith("port "):
                with contextlib.suppress(ValueError):
                    current_host.port = int(line[5:].strip())
            elif line_lower.startswith("identityfile "):
                current_host.identity_file = line[13:].strip()

    return hosts


def _resolve_include(pattern: str, config_path: Path) -> list[Path]:
    """Resolve an SSH ``Include`` argument to a list of existing files.

    Per ssh_config(5): a relative path is resolved against ``~/.ssh`` when
    included from a user configuration file, or against ``/etc/ssh`` when the
    including file itself lives under ``/etc/ssh``. Glob patterns (``*``,
    ``?``) are expanded; non-matching patterns yield nothing. Returns paths
    sorted for deterministic order.
    """
    import glob

    relative_base = (
        Path("/etc/ssh") if _is_system_config(config_path) else Path.home() / ".ssh"
    )

    # An Include line may name several files/patterns.
    results: list[Path] = []
    for token in pattern.split():
        expanded = Path(token).expanduser()
        if not expanded.is_absolute():
            expanded = relative_base / expanded

        matches = sorted(Path(m) for m in glob.glob(str(expanded)))
        # glob already filtered to existing paths; keep only files (skip dirs).
        results.extend(m for m in matches if m.is_file())

        if len(results) > _MAX_INCLUDE_FILES:
            break
    return results


def _is_system_config(config_path: Path) -> bool:
    """True when ``config_path`` is part of the system-wide SSH config tree.

    Pure path-component check (no filesystem access): ``/etc/ssh/...`` and
    the macOS physical location ``/private/etc/ssh/...`` both count.
    """
    parts = config_path.parts
    return parts[:3] == ("/", "etc", "ssh") or parts[:4] == ("/", "private", "etc", "ssh")


def derive_config_path_from_project(project_slug: str) -> Path | None:
    """Derive SSH config path from project hierarchy.

    Convention: Project <customer>/<project> → ~/.ssh/conf.d/<customer>/<project>.conf
    """
    if "/" not in project_slug:
        return None

    ssh_dir = Path.home() / ".ssh" / "conf.d"
    config_path = ssh_dir / f"{project_slug}.conf"
    return config_path


@register_plugin
class SSHConfigSource(SourcePlugin):
    """SSH config file infrastructure source plugin."""

    source_type = "ssh_config"

    def validate_config(self, config: dict) -> list[str]:
        """Validate SSH config source configuration."""
        errors = []
        # config_path is optional - will be derived from project if not set
        config_path = config.get("config_path")
        if config_path and not Path(config_path).expanduser().exists():
            errors.append(f"config_path '{config_path}' does not exist")
        return errors

    async def test_connection(self, config: dict) -> tuple[bool, str]:
        """Test that SSH config file is readable and parseable."""
        try:
            config_path = self._resolve_config_path(config, None)
            if not config_path:
                return False, "Cannot determine config_path - set explicitly or use hierarchical project"

            if not config_path.exists():
                return False, f"SSH config file not found: {config_path}"

            hosts = parse_ssh_config(config_path)
            return True, f"Found {len(hosts)} hosts in {config_path}"
        except Exception as e:
            return False, str(e)

    def _resolve_config_path(self, config: dict, project_slug: str | None) -> Path | None:
        """Resolve the SSH config path from config or project."""
        if config.get("config_path"):
            return Path(config["config_path"]).expanduser()

        if project_slug:
            return derive_config_path_from_project(project_slug)

        return None

    def _determine_node_type(self, host: SSHHost, config: dict) -> NodeType:
        """Determine node type based on config patterns or default."""
        default_type = config.get("default_node_type", "vm")
        type_patterns = config.get("type_patterns", {})

        for node_type_str, patterns in type_patterns.items():
            for pattern in patterns:
                if re.match(pattern, host.name):
                    try:
                        return NodeType(node_type_str)
                    except ValueError:
                        pass

        try:
            return NodeType(default_type)
        except ValueError:
            return NodeType.VM

    def sync(self, project_slug: str, source_name: str) -> SyncResult:
        """Synchronize from SSH config to local YAML files.

        Writes are planned first and applied only when the run is a
        non-empty success (sync guard): a failed, partial, or empty run
        never rewrites node files. Every run appends a record under
        ``.infracontext/runs/`` (see :mod:`infracontext.runs`).
        """
        environment = EnvironmentPaths.current()
        paths = ProjectPaths.for_project(project_slug, environment)
        try:
            source_file = paths.source_file(source_name)
        except ValueError as e:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=f"Invalid source name '{source_name}': {e}",
            )

        if not source_file.exists():
            return SyncResult(
                status=SyncStatus.FAILED,
                message=f"Source '{source_name}' not found",
            )

        config = read_yaml(source_file)
        start_time = time.monotonic()
        stats = SyncStats()

        # Resolve config path
        config_path = self._resolve_config_path(config, project_slug)
        if not config_path:
            return SyncResult(
                status=SyncStatus.FAILED,
                message="Cannot determine config_path - set explicitly or use hierarchical project",
            )

        if not config_path.exists():
            return SyncResult(
                status=SyncStatus.FAILED,
                message=f"SSH config file not found: {config_path}",
            )

        try:
            hosts = parse_ssh_config(config_path)
            today = time.strftime("%Y-%m-%d", time.gmtime())

            # Plan phase: build every node and classify it, writing nothing.
            # Existing nodes are snapshotted once for duplicate detection on
            # creations (detection only -- never auto-attach across sources).
            existing_nodes = load_existing_nodes(paths)
            plans: list[PlannedNodeWrite] = []
            for host in hosts:
                try:
                    node_type = self._determine_node_type(host, config)
                    slug = slugify(host.name)
                    node_id = Node.make_id(node_type, slug)
                    source_id = f"ssh_config:{source_name}:{host.name}"

                    # Determine if hostname is IP or domain
                    ip_addresses: list[str] = []
                    domains: list[str] = []
                    if host.hostname:
                        if is_ip_address(host.hostname):
                            ip_addresses.append(host.hostname)
                        else:
                            domains.append(host.hostname)

                    node = Node(
                        id=node_id,
                        slug=slug,
                        type=node_type,
                        name=host.name,
                        ssh_alias=host.name,
                        source_id=source_id,
                        source=source_name,
                        managed_by=source_name,
                        ip_addresses=ip_addresses,
                        domains=domains,
                        attributes={
                            "ssh_config": {
                                "hostname": host.hostname,
                                "user": host.user,
                                "port": host.port,
                                "identity_file": host.identity_file,
                            }
                        },
                    )

                    node_file = paths.node_file(node.type, slug)
                    existing = read_model(node_file, Node) if node_file.exists() else None
                    if existing and existing.source_id is not None and existing.source_id != source_id:
                        stats.errors.append(
                            f"Slug collision for '{slug}' ({node.type}): existing node '{existing.id}' "
                            f"is bound to source_id '{existing.source_id}', refusing to overwrite."
                        )
                        continue

                    if existing:
                        # Preserve manually-managed fields from the existing
                        # node. ssh_alias comes from the new sync (the SSH
                        # config *is* the source of truth for aliases here).
                        node = merge_synced_node(node, existing, preserve_ssh_alias=False)
                        change = NodeChange.CONFIRMED_UNCHANGED if node == existing else NodeChange.UPDATED
                    else:
                        node = node.model_copy(update={"first_seen": today})
                        change = NodeChange.CREATED
                        for overlap in find_duplicate_candidates(
                            existing_nodes,
                            ips=node.ip_addresses,
                            domains=node.domains,
                            ssh_alias=node.ssh_alias,
                        ):
                            stats.warnings.append(overlap_warning(node.id, overlap))

                    plans.append(PlannedNodeWrite(node=node, node_file=node_file, change=change))

                except Exception as e:
                    stats.errors.append(f"Error processing host '{host.name}': {e}")

            # Sync guard: only a non-empty, error-free run may touch node files.
            status = SyncStatus.SUCCESS if not stats.errors else SyncStatus.PARTIAL
            guarded = status is not SyncStatus.SUCCESS or not plans
            if not guarded:
                apply_node_writes(paths, plans)
                for plan in plans:
                    if plan.change is NodeChange.CREATED:
                        stats.nodes_created += 1
                    elif plan.change is NodeChange.UPDATED:
                        stats.nodes_updated += 1
                    else:
                        stats.nodes_unchanged += 1

            # Update source status
            duration_ms = int((time.monotonic() - start_time) * 1000)

            config["last_sync_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            config["last_sync_status"] = str(status)
            config["last_sync_stats"] = {
                "nodes_created": stats.nodes_created,
                "nodes_updated": stats.nodes_updated,
                "nodes_unchanged": stats.nodes_unchanged,
                "skipped": stats.skipped,
                "errors": stats.errors,
                "warnings": stats.warnings,
            }
            write_yaml(source_file, config)

            record_sync_run(environment, project_slug, source_name, status, plans)

            if stats.errors:
                message = (
                    f"Sync found {len(stats.errors)} error(s); no node files were written (sync guard)"
                )
            elif not plans:
                message = f"SSH config reported 0 hosts from {config_path}; no node files were written (empty-sync guard)"
            else:
                message = f"Synced {stats.nodes_created + stats.nodes_updated} nodes from {len(hosts)} hosts"
                if stats.nodes_unchanged:
                    message += f" ({stats.nodes_unchanged} unchanged)"

            return SyncResult(
                status=status,
                message=message,
                nodes_created=stats.nodes_created,
                nodes_updated=stats.nodes_updated,
                nodes_unchanged=stats.nodes_unchanged,
                errors=stats.errors,
                warnings=stats.warnings,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)

            config["last_sync_status"] = "failed"
            config["last_sync_message"] = str(e)
            write_yaml(source_file, config)

            record_sync_run(environment, project_slug, source_name, SyncStatus.FAILED, [])

            return SyncResult(
                status=SyncStatus.FAILED,
                message=str(e),
                errors=[str(e)],
                duration_ms=duration_ms,
            )
