"""Proxmox VE source plugin.

Syncs hosts, VMs, containers, storage, and networks from Proxmox VE clusters.
"""

import fnmatch
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from proxmoxer import ProxmoxAPI

from infracontext.credentials.keychain import get_credential
from infracontext.models.function import Function, FunctionType
from infracontext.models.node import Node, NodeType
from infracontext.models.relationship import Relationship, RelationshipFile, RelationshipType
from infracontext.paths import ProjectPaths
from infracontext.sources.base import SourcePlugin, SyncResult, SyncStatus
from infracontext.sources.registry import register_plugin
from infracontext.storage import read_model, read_yaml, write_model, write_yaml


@dataclass
class SyncStats:
    """Statistics from a sync operation."""

    hosts_created: int = 0
    hosts_updated: int = 0
    vms_created: int = 0
    vms_updated: int = 0
    containers_created: int = 0
    containers_updated: int = 0
    storage_created: int = 0
    storage_updated: int = 0
    networks_created: int = 0
    networks_updated: int = 0
    relationships_created: int = 0
    relationships_updated: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "hosts": {"created": self.hosts_created, "updated": self.hosts_updated},
            "vms": {"created": self.vms_created, "updated": self.vms_updated},
            "containers": {"created": self.containers_created, "updated": self.containers_updated},
            "storage": {"created": self.storage_created, "updated": self.storage_updated},
            "networks": {"created": self.networks_created, "updated": self.networks_updated},
            "relationships": {"created": self.relationships_created, "updated": self.relationships_updated},
            "errors": self.errors,
            "warnings": self.warnings,
        }


# Mapping of Proxmox storage types to function types
STORAGE_FUNCTION_MAP = {
    "nfs": FunctionType.NFS_SERVER,
    "cifs": FunctionType.NFS_SERVER,
    "glusterfs": FunctionType.NFS_SERVER,
    "cephfs": FunctionType.STORAGE,
    "rbd": FunctionType.STORAGE,
    "lvm": FunctionType.STORAGE,
    "lvmthin": FunctionType.STORAGE,
    "zfspool": FunctionType.STORAGE,
    "dir": FunctionType.STORAGE,
    "btrfs": FunctionType.STORAGE,
}


@register_plugin
class ProxmoxSource(SourcePlugin):
    """Proxmox VE infrastructure source plugin."""

    source_type = "proxmox"

    def validate_config(self, config: dict) -> list[str]:
        """Validate Proxmox source configuration."""
        errors = []
        if not config.get("api_url"):
            errors.append("api_url is required")
        if not config.get("api_token_id"):
            errors.append("api_token_id is required")
        return errors

    async def test_connection(self, config: dict) -> tuple[bool, str]:
        """Test connection to Proxmox VE."""
        try:
            pve = self._get_client(config)
            version = pve.version.get()
            nodes = pve.nodes.get()
            return True, f"Connected to PVE {version.get('version')} with {len(nodes)} node(s)"
        except Exception as e:
            return False, str(e)

    def _get_client(self, config: dict) -> ProxmoxAPI:
        """Create authenticated proxmoxer client."""
        parsed = urlparse(config["api_url"])
        host = parsed.hostname or config["api_url"]
        port = parsed.port or 8006

        # Parse token ID: user@realm!tokenid
        token_parts = config["api_token_id"].split("!")
        if len(token_parts) != 2:
            raise ValueError("Invalid token ID format. Expected: user@realm!tokenid")

        user = token_parts[0]
        token_name = token_parts[1]

        # Get token secret from keychain
        credential_key = f"proxmox:{config['name']}"
        token_secret = get_credential(credential_key)
        if not token_secret:
            raise ValueError(f"No credential found for '{credential_key}'. Use 'ic config credential set'")

        return ProxmoxAPI(
            host,
            port=port,
            user=user,
            token_name=token_name,
            token_value=token_secret,
            verify_ssl=config.get("verify_ssl", True),
        )

    def _make_source_id(self, cluster_id: str, resource_type: str, resource_id: str) -> str:
        """Generate cluster-centric source ID.

        Format: proxmox:{cluster_id}:{resource_type}:{resource_id}

        The cluster_id is the stable identifier - this allows multiple source configs
        pointing to different nodes in the same cluster without creating duplicates.
        """
        return f"proxmox:{cluster_id}:{resource_type}:{resource_id}"

    def _make_legacy_source_id(self, source_name: str, cluster_id: str, resource_type: str, resource_id: str) -> str:
        """Generate legacy source ID format for migration matching.

        Old format: proxmox:{source_name}:{cluster_id}:{resource_type}:{resource_id}
        """
        return f"proxmox:{source_name}:{cluster_id}:{resource_type}:{resource_id}"

    def _parse_source_id(self, source_id: str) -> dict | None:
        """Parse a Proxmox source_id into components.

        Returns dict with keys: cluster_id, resource_type, resource_id
        Handles both new (3-part) and legacy (4-part) formats.
        """
        if not source_id or not source_id.startswith("proxmox:"):
            return None

        parts = source_id.split(":")
        if len(parts) == 4:
            # New format: proxmox:{cluster}:{type}:{id}
            return {
                "cluster_id": parts[1],
                "resource_type": parts[2],
                "resource_id": parts[3],
            }
        elif len(parts) == 5:
            # Legacy format: proxmox:{source}:{cluster}:{type}:{id}
            return {
                "source_name": parts[1],
                "cluster_id": parts[2],
                "resource_type": parts[3],
                "resource_id": parts[4],
            }
        return None

    def _should_exclude(
        self,
        vmid: int | None,
        name: str,
        pool: str | None,
        tags: list[str],
        rules: dict,
    ) -> bool:
        """Check if a resource should be excluded based on rules."""
        if vmid and vmid in rules.get("vmids", []):
            return True

        for pattern in rules.get("name_patterns", []):
            if fnmatch.fnmatch(name.lower(), pattern.lower()):
                return True

        if pool and pool in rules.get("pools", []):
            return True

        excluded_tags = set(rules.get("tags", []))
        return bool(excluded_tags & set(tags))

    def _generate_slug(self, name: str) -> str:
        """Generate a URL-safe slug from a name."""
        slug = re.sub(r"[^a-z0-9-]", "-", name.lower())
        slug = re.sub(r"-+", "-", slug).strip("-")[:100]
        return slug or "node"

    def sync(self, project_slug: str, source_name: str) -> SyncResult:
        """Synchronize from Proxmox VE to local YAML files."""
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
        errors = self.validate_config(config)
        if errors:
            return SyncResult(
                status=SyncStatus.FAILED,
                message=f"Invalid config: {', '.join(errors)}",
            )

        start_time = time.monotonic()
        stats = SyncStats()
        exclusion_rules = config.get("exclusion_rules", {})

        # Build index of existing nodes by source_id for rename detection
        self._source_id_index = self._build_source_id_index(paths)

        try:
            pve = self._get_client(config)

            # Get cluster info
            cluster_name = None
            try:
                cluster_status = pve.cluster.status.get()
                for item in cluster_status:
                    if item.get("type") == "cluster":
                        cluster_name = item.get("name")
                        break
            except Exception:
                cluster_name = None

            cluster_id = cluster_name or "standalone"

            # Check for cluster conflicts: warn if another source already manages this cluster
            other_sources_for_cluster = self._find_other_sources_for_cluster(cluster_id, source_name)
            if other_sources_for_cluster:
                sources_list = ", ".join(sorted(other_sources_for_cluster))
                stats.warnings.append(
                    f"Cluster '{cluster_id}' is also managed by: {sources_list}. "
                    f"Nodes will be deduplicated by cluster ID."
                )

            # Track synced nodes
            host_nodes: dict[str, Node] = {}
            storage_nodes: dict[str, Node] = {}
            all_nodes: list[Node] = []
            all_relationships: list[Relationship] = []

            # -------------------------------------------------------------------
            # Sync Hosts (PVE Nodes)
            # -------------------------------------------------------------------
            nodes_data = pve.nodes.get()
            for pve_node in nodes_data:
                node_name = pve_node["node"]
                source_id = self._make_source_id(cluster_id, "node", node_name)

                try:
                    node_status = pve.nodes(node_name).status.get()
                except Exception as e:
                    stats.warnings.append(f"Could not get status for node {node_name}: {e}")
                    node_status = {}

                cpuinfo = node_status.get("cpuinfo", {})
                memory = node_status.get("memory", {})

                slug = self._generate_slug(node_name)
                node_id = Node.make_id(NodeType.PHYSICAL_HOST, slug)

                node = Node(
                    id=node_id,
                    slug=slug,
                    type=NodeType.PHYSICAL_HOST,
                    name=node_name,
                    source_id=source_id,
                    source=source_name,
                    managed_by=source_name,
                    attributes={
                        "proxmox": {
                            "node_name": node_name,
                            "status": pve_node.get("status", "unknown"),
                            "cpu_cores": cpuinfo.get("cpus"),
                            "cpu_model": cpuinfo.get("model"),
                            "memory_total_mb": (memory.get("total", 0) or 0) // (1024 * 1024),
                            "pveversion": node_status.get("pveversion"),
                        },
                        "is_hypervisor": True,
                    },
                )

                saved_node = self._save_node(paths, node, stats, "hosts")
                if saved_node is None:
                    continue
                host_nodes[node_name] = saved_node
                all_nodes.append(saved_node)

            # -------------------------------------------------------------------
            # Sync Storage
            # -------------------------------------------------------------------
            try:
                storage_data = pve.storage.get()
            except Exception as e:
                stats.warnings.append(f"Could not get storage list: {e}")
                storage_data = []

            for storage in storage_data:
                storage_id = storage["storage"]
                storage_type = storage.get("type", "unknown")

                if storage_type in ("cephfs", "rbd"):
                    node_type = NodeType.CEPH_CLUSTER
                elif storage_type in ("nfs", "cifs", "glusterfs"):
                    node_type = NodeType.NFS_SHARE
                else:
                    node_type = NodeType.BLOCK_STORAGE

                source_id = self._make_source_id(cluster_id, "storage", storage_id)

                content_types = storage.get("content", "")
                if isinstance(content_types, str):
                    content_types = [c.strip() for c in content_types.split(",") if c.strip()]

                slug = self._generate_slug(storage_id)
                node_id = Node.make_id(node_type, slug)

                # Add function based on storage type
                functions = []
                function_type = STORAGE_FUNCTION_MAP.get(storage_type)
                if function_type:
                    functions.append(
                        Function(
                            name=function_type,
                            attributes={"storage_type": storage_type},
                        )
                    )

                node = Node(
                    id=node_id,
                    slug=slug,
                    type=node_type,
                    name=f"{storage_id} ({storage_type})",
                    source_id=source_id,
                    source=source_name,
                    managed_by=source_name,
                    functions=functions,
                    attributes={
                        "proxmox": {
                            "storage_id": storage_id,
                            "storage_type": storage_type,
                            "content_types": content_types,
                            "shared": storage.get("shared", 0) == 1,
                            "path": storage.get("path", ""),
                        },
                    },
                )

                saved_node = self._save_node(paths, node, stats, "storage")
                if saved_node is None:
                    continue
                storage_nodes[storage_id] = saved_node
                all_nodes.append(saved_node)

            # -------------------------------------------------------------------
            # Sync VMs and Containers per host
            # -------------------------------------------------------------------
            for pve_node_name, host_node in host_nodes.items():
                # --- VMs (QEMU) ---
                try:
                    vms = pve.nodes(pve_node_name).qemu.get()
                except Exception as e:
                    stats.warnings.append(f"Could not get VMs from {pve_node_name}: {e}")
                    vms = []

                for vm in vms:
                    vmid = vm["vmid"]
                    vm_name = vm.get("name", f"vm-{vmid}")

                    try:
                        vm_config = pve.nodes(pve_node_name).qemu(vmid).config.get()
                    except Exception:
                        vm_config = {}

                    tags_str = vm_config.get("tags", "")
                    tags = [t.strip() for t in tags_str.split(";") if t.strip()] if tags_str else []
                    pool = vm_config.get("pool")

                    if self._should_exclude(vmid, vm_name, pool, tags, exclusion_rules):
                        continue

                    source_id = self._make_source_id(cluster_id, "qemu", str(vmid))

                    # Extract IP addresses from QEMU guest agent
                    ip_addresses = []
                    try:
                        agent_info = pve.nodes(pve_node_name).qemu(vmid).agent("network-get-interfaces").get()
                        for iface in agent_info.get("result", []):
                            for addr in iface.get("ip-addresses", []):
                                if addr.get("ip-address-type") == "ipv4":
                                    ip_addr = addr.get("ip-address")
                                    if ip_addr and not ip_addr.startswith("127."):
                                        ip_addresses.append(ip_addr)
                    except Exception:
                        pass

                    cores = vm_config.get("cores", 1)
                    sockets = vm_config.get("sockets", 1)
                    total_cores = cores * sockets

                    slug = self._generate_slug(vm_name)
                    node_id = Node.make_id(NodeType.VM, slug)

                    node = Node(
                        id=node_id,
                        slug=slug,
                        type=NodeType.VM,
                        name=vm_name,
                        source_id=source_id,
                        source=source_name,
                        managed_by=source_name,
                        ip_addresses=ip_addresses,
                        attributes={
                            "proxmox": {
                                "vmid": vmid,
                                "host_node": pve_node_name,
                                "status": vm.get("status", "unknown"),
                                "cpu_cores": total_cores,
                                "memory_mb": vm_config.get("memory"),
                                "pool": pool,
                                "template": vm_config.get("template", 0) == 1,
                            },
                        },
                    )

                    saved_node = self._save_node(paths, node, stats, "vms")
                    if saved_node is None:
                        continue
                    all_nodes.append(saved_node)

                    # Create runs_on relationship
                    all_relationships.append(
                        Relationship(
                            source=saved_node.id,
                            target=host_node.id,
                            type=RelationshipType.RUNS_ON,
                            managed_by=source_name,
                        )
                    )

                    # Create uses_storage relationships
                    for key, value in vm_config.items():
                        if key.startswith(("scsi", "virtio", "ide", "sata")) and ":" in str(value):
                            storage_id = str(value).split(":")[0]
                            if storage_id in storage_nodes:
                                all_relationships.append(
                                    Relationship(
                                        source=saved_node.id,
                                        target=storage_nodes[storage_id].id,
                                        type=RelationshipType.USES_STORAGE,
                                        managed_by=source_name,
                                    )
                                )

                # --- LXC Containers ---
                try:
                    containers = pve.nodes(pve_node_name).lxc.get()
                except Exception as e:
                    stats.warnings.append(f"Could not get containers from {pve_node_name}: {e}")
                    containers = []

                for ct in containers:
                    vmid = ct["vmid"]
                    ct_name = ct.get("name", f"ct-{vmid}")

                    try:
                        ct_config = pve.nodes(pve_node_name).lxc(vmid).config.get()
                    except Exception:
                        ct_config = {}

                    tags_str = ct_config.get("tags", "")
                    tags = [t.strip() for t in tags_str.split(";") if t.strip()] if tags_str else []
                    pool = ct_config.get("pool")

                    if self._should_exclude(vmid, ct_name, pool, tags, exclusion_rules):
                        continue

                    source_id = self._make_source_id(cluster_id, "lxc", str(vmid))

                    # Extract IP from network config
                    ip_addresses = []
                    for key, value in ct_config.items():
                        if key.startswith("net") and "ip=" in str(value):
                            match = re.search(r"ip=([^/,]+)", str(value))
                            if match and match.group(1) not in ("dhcp", "manual"):
                                ip_addresses.append(match.group(1))

                    slug = self._generate_slug(ct_name)
                    node_id = Node.make_id(NodeType.LXC_CONTAINER, slug)

                    node = Node(
                        id=node_id,
                        slug=slug,
                        type=NodeType.LXC_CONTAINER,
                        name=ct_name,
                        source_id=source_id,
                        source=source_name,
                        managed_by=source_name,
                        ip_addresses=ip_addresses,
                        attributes={
                            "proxmox": {
                                "vmid": vmid,
                                "host_node": pve_node_name,
                                "status": ct.get("status", "unknown"),
                                "cpu_cores": ct_config.get("cores"),
                                "memory_mb": ct_config.get("memory"),
                                "pool": pool,
                                "template": ct_config.get("template", 0) == 1,
                                "unprivileged": ct_config.get("unprivileged", 0) == 1,
                            },
                        },
                    )

                    saved_node = self._save_node(paths, node, stats, "containers")
                    if saved_node is None:
                        continue
                    all_nodes.append(saved_node)

                    # Create runs_on relationship
                    all_relationships.append(
                        Relationship(
                            source=saved_node.id,
                            target=host_node.id,
                            type=RelationshipType.RUNS_ON,
                            managed_by=source_name,
                        )
                    )

                    # Create uses_storage relationships
                    for key, value in ct_config.items():
                        if (key == "rootfs" or key.startswith("mp")) and ":" in str(value):
                            storage_id = str(value).split(":")[0]
                            if storage_id in storage_nodes:
                                all_relationships.append(
                                    Relationship(
                                        source=saved_node.id,
                                        target=storage_nodes[storage_id].id,
                                        type=RelationshipType.USES_STORAGE,
                                        managed_by=source_name,
                                    )
                                )

            # -------------------------------------------------------------------
            # Save relationships
            # -------------------------------------------------------------------
            self._save_relationships(paths, source_name, all_relationships, stats)

            # Update source status
            duration_ms = int((time.monotonic() - start_time) * 1000)

            config["last_sync_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            config["last_sync_status"] = "success" if not stats.errors else "partial"
            config["last_sync_stats"] = stats.to_dict()
            write_yaml(source_file, config)

            status = SyncStatus.SUCCESS if not stats.errors else SyncStatus.PARTIAL
            message = f"Synced {len(all_nodes)} nodes, {len(all_relationships)} relationships"
            if stats.warnings:
                message += f" ({len(stats.warnings)} warnings)"

            return SyncResult(
                status=status,
                message=message,
                nodes_created=stats.hosts_created
                + stats.vms_created
                + stats.containers_created
                + stats.storage_created,
                nodes_updated=stats.hosts_updated
                + stats.vms_updated
                + stats.containers_updated
                + stats.storage_updated,
                relationships_created=stats.relationships_created,
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

    def _normalize_source_id(self, source_id: str) -> str | None:
        """Normalize a source_id to the new cluster-centric format.

        Converts legacy format (proxmox:{source}:{cluster}:{type}:{id})
        to new format (proxmox:{cluster}:{type}:{id}).
        """
        parsed = self._parse_source_id(source_id)
        if not parsed:
            return None
        return self._make_source_id(
            parsed["cluster_id"],
            parsed["resource_type"],
            parsed["resource_id"],
        )

    def _build_source_id_index(self, paths: ProjectPaths) -> dict[str, tuple[Node, Path]]:
        """Build an index mapping normalized source_id to (Node, file_path).

        Indexes by normalized (cluster-centric) source_id, allowing matching
        of both legacy and new format source_ids to the same nodes.
        """
        index: dict[str, tuple[Node, Path]] = {}
        nodes_dir = paths.nodes_dir

        if not nodes_dir.exists():
            return index

        for type_dir in nodes_dir.iterdir():
            if not type_dir.is_dir():
                continue
            for node_file in type_dir.glob("*.yaml"):
                try:
                    node = read_model(node_file, Node)
                    if node and node.source_id:
                        # Index by normalized source_id for deduplication
                        normalized = self._normalize_source_id(node.source_id)
                        if normalized:
                            index[normalized] = (node, node_file)
                except Exception:
                    pass

        return index

    def _find_other_sources_for_cluster(self, cluster_id: str, current_source: str) -> set[str]:
        """Find other source names that have nodes from the same cluster.

        Returns set of source names (excluding current_source) that have nodes
        with source_ids matching this cluster.
        """
        other_sources: set[str] = set()

        for _source_id, (node, _) in self._source_id_index.items():
            parsed = self._parse_source_id(node.source_id or "")
            if not parsed:
                continue

            if parsed["cluster_id"] == cluster_id:
                # Check if this node was managed by a different source
                if node.managed_by and node.managed_by != current_source:
                    other_sources.add(node.managed_by)
                # Also check legacy format which included source_name
                if "source_name" in parsed and parsed["source_name"] != current_source:
                    other_sources.add(parsed["source_name"])

        return other_sources

    def _save_node(self, paths: ProjectPaths, node: Node, stats: SyncStats, stat_key: str) -> Node | None:
        """Save a node to YAML, merging with existing manual additions.

        Handles renames: if source_id matches an existing node with different slug,
        the old file is renamed to the new slug.

        Proxmox-managed fields (overwritten):
        - ip_addresses, attributes, source_id, source, managed_by

        Manually-managed fields (preserved from existing):
        - ssh_alias, domains, description, notes, source_paths, endpoints,
          functions, observability, triage, learnings, local
        """
        node_file = paths.node_file(node.type, node.slug)
        is_new = not node_file.exists()
        existing: Node | None = None
        old_file_to_delete: Path | None = None

        # Check for rename: existing node with same source_id but different slug
        # Use normalized source_id for lookup (handles legacy format migration)
        normalized_source_id = self._normalize_source_id(node.source_id) if node.source_id else None
        if normalized_source_id and normalized_source_id in self._source_id_index:
            existing_node, existing_file = self._source_id_index[normalized_source_id]
            if existing_node.slug != node.slug:
                # Rename detected: same source_id, different slug
                existing = existing_node
                old_file_to_delete = existing_file
                is_new = False
                stats.warnings.append(f"Renamed: {existing_node.slug} → {node.slug} (source_id: {node.source_id})")
            elif existing_file == node_file:
                # Same file, just an update
                existing = existing_node
                is_new = False
                # Check if migrating from legacy source_id format
                if existing_node.source_id and existing_node.source_id != node.source_id:
                    parsed = self._parse_source_id(existing_node.source_id)
                    if parsed and "source_name" in parsed:
                        stats.warnings.append(
                            f"Migrated source_id format: {existing_node.slug} "
                            f"({existing_node.source_id} → {node.source_id})"
                        )

        # If no rename detected, check if file exists at new location
        if existing is None and not is_new:
            existing = read_model(node_file, Node)

        if existing:
            existing_normalized = self._normalize_source_id(existing.source_id) if existing.source_id else None
            if existing_normalized != normalized_source_id:
                stats.errors.append(
                    f"Slug collision for '{node.slug}' ({node.type}): existing node '{existing.id}' "
                    f"is bound to source_id '{existing.source_id or 'manual'}', refusing to overwrite."
                )
                return None

        if existing:
            # Preserve manually-managed fields from existing node
            node = Node(
                # Identity (from new)
                version=node.version,
                id=node.id,
                slug=node.slug,
                type=node.type,
                name=node.name,
                # Proxmox-managed (from new)
                ip_addresses=node.ip_addresses,
                attributes=node.attributes,
                source_id=node.source_id,
                source=node.source,
                managed_by=node.managed_by,
                # Manually-managed (preserve existing)
                ssh_alias=existing.ssh_alias,
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

        # Delete old file after successful write (rename case)
        if old_file_to_delete and old_file_to_delete.exists():
            old_file_to_delete.unlink()

        if is_new:
            setattr(stats, f"{stat_key}_created", getattr(stats, f"{stat_key}_created") + 1)
        else:
            setattr(stats, f"{stat_key}_updated", getattr(stats, f"{stat_key}_updated") + 1)

        return node

    def _save_relationships(
        self,
        paths: ProjectPaths,
        source_name: str,
        new_relationships: list[Relationship],
        stats: SyncStats,
    ) -> None:
        """Merge source-managed relationships with existing relationships."""
        existing = read_model(paths.relationships_yaml, RelationshipFile) or RelationshipFile()

        # Keep non-managed relationships and relationships from other sources
        kept = [r for r in existing.relationships if r.managed_by is None or r.managed_by != source_name]

        # Add new relationships
        for rel in new_relationships:
            # Check for duplicates
            is_dup = any(r.source == rel.source and r.target == rel.target and r.type == rel.type for r in kept)
            if not is_dup:
                kept.append(rel)
                stats.relationships_created += 1

        result = RelationshipFile(relationships=kept)
        write_model(paths.relationships_yaml, result)
