"""SSH config source plugin.

Imports nodes from SSH config files. Supports auto-discovery based on project hierarchy.
Convention: Project <customer>/<project> → SSH config at ~/.ssh/conf.d/<customer>/<project>.conf
"""

import contextlib
import re
import time
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path

from infracontext.models.node import Node, NodeType
from infracontext.paths import ProjectPaths
from infracontext.sources.base import SourcePlugin, SyncResult, SyncStatus
from infracontext.sources.registry import register_plugin
from infracontext.storage import read_model, read_yaml, write_model, write_yaml


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
    """Generate a URL-safe slug from a name."""
    slug = re.sub(r"[^a-z0-9-]", "-", name.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")[:100]
    return slug or "node"


def parse_ssh_config(path: Path) -> list[SSHHost]:
    """Parse SSH config file into host entries.

    Skips wildcard patterns (*, ?) and returns only concrete hosts.
    """
    hosts: list[SSHHost] = []
    current_host: SSHHost | None = None

    for line in path.read_text().splitlines():
        line = line.strip()

        # Skip empty lines and comments
        if not line or line.startswith("#"):
            continue

        # Case-insensitive directive matching
        line_lower = line.lower()

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
        """Synchronize from SSH config to local YAML files."""
        paths = ProjectPaths.for_project(project_slug)
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

            for host in hosts:
                try:
                    node_type = self._determine_node_type(host, config)
                    slug = generate_slug(host.name)
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

                    # Save node
                    node_file = paths.node_file(node.type, slug)
                    is_new = not node_file.exists()
                    existing = read_model(node_file, Node) if node_file.exists() else None
                    if existing and existing.source_id != source_id:
                        stats.errors.append(
                            f"Slug collision for '{slug}' ({node.type}): existing node '{existing.id}' "
                            f"is bound to source_id '{existing.source_id or 'manual'}', refusing to overwrite."
                        )
                        continue

                    if existing:
                        # Preserve manually-managed fields from existing node
                        node = Node(
                            # Identity (from new)
                            version=node.version,
                            id=node.id,
                            slug=node.slug,
                            type=node.type,
                            name=node.name,
                            # SSH-config-managed (from new)
                            ssh_alias=node.ssh_alias,
                            ip_addresses=node.ip_addresses,
                            source_id=node.source_id,
                            source=node.source,
                            managed_by=node.managed_by,
                            attributes=node.attributes,
                            # Manually-managed (preserve existing)
                            domains=existing.domains,
                            description=existing.description,
                            notes=existing.notes,
                            source_paths=existing.source_paths,
                            endpoints=existing.endpoints,
                            functions=existing.functions,
                            observability=existing.observability,
                            triage=existing.triage,
                            learnings=existing.learnings,
                        )

                    paths.node_type_dir(node.type).mkdir(parents=True, exist_ok=True)
                    write_model(node_file, node)

                    if is_new:
                        stats.nodes_created += 1
                    else:
                        stats.nodes_updated += 1

                except Exception as e:
                    stats.errors.append(f"Error processing host '{host.name}': {e}")

            # Update source status
            duration_ms = int((time.monotonic() - start_time) * 1000)

            config["last_sync_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            config["last_sync_status"] = "success" if not stats.errors else "partial"
            config["last_sync_stats"] = {
                "nodes_created": stats.nodes_created,
                "nodes_updated": stats.nodes_updated,
                "skipped": stats.skipped,
                "errors": stats.errors,
            }
            write_yaml(source_file, config)

            status = SyncStatus.SUCCESS if not stats.errors else SyncStatus.PARTIAL
            message = f"Synced {stats.nodes_created + stats.nodes_updated} nodes from {len(hosts)} hosts"
            if stats.errors:
                message += f" ({len(stats.errors)} errors)"

            return SyncResult(
                status=status,
                message=message,
                nodes_created=stats.nodes_created,
                nodes_updated=stats.nodes_updated,
                errors=stats.errors,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)

            config["last_sync_status"] = "failed"
            config["last_sync_message"] = str(e)
            write_yaml(source_file, config)

            return SyncResult(
                status=SyncStatus.FAILED,
                message=str(e),
                errors=[str(e)],
                duration_ms=duration_ms,
            )
