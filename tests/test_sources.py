"""Tests for infracontext.sources — SSH config parsing, source merge helper,
sync guard (failed/partial/empty runs never rewrite node files), run records,
and write-once first_seen."""

import os
import time

import pytest

from infracontext.models.node import Node, NodeType
from infracontext.models.relationship import Relationship, RelationshipType
from infracontext.runs import load_run_records
from infracontext.sources.base import SyncStatus, merge_synced_node
from infracontext.sources.proxmox import ProxmoxSource
from infracontext.sources.proxmox import SyncStats as ProxmoxSyncStats
from infracontext.sources.ssh_config import SSHConfigSource, parse_ssh_config
from infracontext.storage import read_model, read_yaml, write_model, write_yaml

# ── SSH Include directive resolution ──────────────────────────────


class TestParseSshConfigIncludes:
    def test_includes_relative_to_dotssh(self, tmp_path, monkeypatch):
        """Relative Include paths resolve against ~/.ssh, not the file's dir."""
        # Per OpenSSH: a relative Include resolves under ~/.ssh
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        main = ssh_dir / "config"
        fragment = ssh_dir / "frag.conf"
        main.write_text("Include frag.conf\n")
        fragment.write_text("Host web-01\n    HostName 10.0.0.1\n")

        # expanduser() reads $HOME, so patch the env var rather than Path.home.
        monkeypatch.setenv("HOME", str(tmp_path))

        hosts = parse_ssh_config(main)
        assert len(hosts) == 1
        assert hosts[0].name == "web-01"
        assert hosts[0].hostname == "10.0.0.1"

    def test_includes_absolute_path(self, tmp_path, monkeypatch):
        included = tmp_path / "extra.conf"
        included.write_text("Host db-01\n    HostName 10.0.0.2\n")
        main = tmp_path / "config"
        main.write_text(f"Include {included}\nHost web-01\n    HostName 10.0.0.1\n")

        monkeypatch.setenv("HOME", str(tmp_path / "nohome"))

        hosts = parse_ssh_config(main)
        names = {h.name for h in hosts}
        assert names == {"web-01", "db-01"}

    def test_includes_glob_pattern(self, tmp_path, monkeypatch):
        """Include accepts glob patterns (e.g. conf.d/*)."""
        # Relative Include resolves under ~/.ssh, so conf.d lives there too.
        ssh_dir = tmp_path / ".ssh"
        confd = ssh_dir / "conf.d"
        confd.mkdir(parents=True)
        (confd / "a.conf").write_text("Host host-a\n    HostName 10.0.0.10\n")
        (confd / "b.conf").write_text("Host host-b\n    HostName 10.0.0.11\n")
        # Non-matching extension shouldn't be picked up
        (confd / "readme.txt").write_text("Host host-x\n    HostName 10.0.0.99\n")

        main = ssh_dir / "config"
        main.write_text("Include conf.d/*.conf\n")

        monkeypatch.setenv("HOME", str(tmp_path))

        hosts = parse_ssh_config(main)
        names = {h.name for h in hosts}
        assert names == {"host-a", "host-b"}

    def test_missing_include_is_skipped_not_fatal(self, tmp_path, monkeypatch):
        """A non-existent Include target shouldn't abort the whole parse."""
        main = tmp_path / "config"
        main.write_text(
            "Include does-not-exist.conf\nHost web-01\n    HostName 10.0.0.1\n"
        )
        monkeypatch.setenv("HOME", str(tmp_path))

        hosts = parse_ssh_config(main)
        assert len(hosts) == 1
        assert hosts[0].name == "web-01"

    def test_wildcard_hosts_skipped(self, tmp_path, monkeypatch):
        """Host patterns with * or ? are not concrete hosts — skip them."""
        main = tmp_path / "config"
        main.write_text(
            "Host *\n    User default\nHost web-01\n    HostName 10.0.0.1\n"
        )
        monkeypatch.setenv("HOME", str(tmp_path))

        hosts = parse_ssh_config(main)
        assert [h.name for h in hosts] == ["web-01"]

    def test_large_fleet_not_truncated_by_file_cap(self, tmp_path, monkeypatch):
        """The include cap counts *files*, never hosts. A fleet with far more
        hosts than the file cap (here 150 hosts across 3 fragments) must
        import completely — this regressed once by capping on host count.
        """
        ssh_dir = tmp_path / ".ssh"
        confd = ssh_dir / "conf.d"
        confd.mkdir(parents=True)
        for frag in range(3):
            body = "".join(
                f"Host host-{frag}-{i}\n    HostName 10.0.{frag}.{i}\n" for i in range(50)
            )
            (confd / f"{frag}.conf").write_text(body)
        main = ssh_dir / "config"
        main.write_text("Include conf.d/*.conf\n")
        monkeypatch.setenv("HOME", str(tmp_path))

        hosts = parse_ssh_config(main)
        assert len(hosts) == 150

    def test_file_cap_stops_include_explosion(self, tmp_path, monkeypatch, caplog):
        """Beyond _MAX_INCLUDE_FILES parsed files, further includes are
        ignored with a single warning (not one per skipped file).
        """
        import logging

        from infracontext.sources import ssh_config as mod

        ssh_dir = tmp_path / ".ssh"
        confd = ssh_dir / "conf.d"
        confd.mkdir(parents=True)
        for i in range(5):
            (confd / f"{i:02d}.conf").write_text(
                f"Host host-{i}\n    HostName 10.0.0.{i}\n"
            )
        main = ssh_dir / "config"
        main.write_text("Include conf.d/*.conf\n")
        monkeypatch.setenv("HOME", str(tmp_path))
        # Cap of 3 files total: the root config + two fragments.
        monkeypatch.setattr(mod, "_MAX_INCLUDE_FILES", 3)

        with caplog.at_level(logging.WARNING):
            hosts = parse_ssh_config(main)

        assert len(hosts) == 2  # fragments 00 and 01 only
        assert caplog.text.count("include expansion reached") == 1


class TestIsSystemConfig:
    def test_etc_ssh_is_system(self):
        from pathlib import Path

        from infracontext.sources.ssh_config import _is_system_config

        assert _is_system_config(Path("/etc/ssh/ssh_config"))
        assert _is_system_config(Path("/private/etc/ssh/ssh_config"))  # macOS

    def test_user_config_is_not_system(self):
        from pathlib import Path

        from infracontext.sources.ssh_config import _is_system_config

        assert not _is_system_config(Path.home() / ".ssh" / "config")
        assert not _is_system_config(Path("/etc/sshd_wannabe/config"))


# ── merge_synced_node helper ──────────────────────────────────────


class TestMergeSyncedNode:
    def _existing(self) -> Node:
        return Node(
            id="vm:web",
            slug="web",
            type=NodeType.VM,
            name="Old Name",
            ssh_alias="old-alias",
            domains=["old.example.com"],
            description="hand-written docs",
            notes="manual notes",
            endpoints=[],
            observability=[],
            learnings=[],
        )

    def _new(self) -> Node:
        return Node(
            id="vm:web",
            slug="web",
            type=NodeType.VM,
            name="New Name",
            ssh_alias="new-alias",
            ip_addresses=["10.0.0.5"],
            source_id="proxmox:cluster:qemu:100",
            source="proxmox",
            managed_by="proxmox",
            attributes={"proxmox": {"vmid": 100}},
        )

    def test_source_managed_fields_taken_from_new(self):
        merged = merge_synced_node(self._new(), self._existing(), preserve_ssh_alias=True)
        assert merged.name == "New Name"
        assert merged.ip_addresses == ["10.0.0.5"]
        assert merged.source_id == "proxmox:cluster:qemu:100"
        assert merged.attributes == {"proxmox": {"vmid": 100}}

    def test_manual_fields_preserved_from_existing(self):
        merged = merge_synced_node(self._new(), self._existing(), preserve_ssh_alias=True)
        assert merged.description == "hand-written docs"
        assert merged.notes == "manual notes"
        assert merged.domains == ["old.example.com"]

    def test_preserve_ssh_alias_true_keeps_existing(self):
        """Proxmox path: SSH alias is manually managed."""
        merged = merge_synced_node(self._new(), self._existing(), preserve_ssh_alias=True)
        assert merged.ssh_alias == "old-alias"

    def test_preserve_ssh_alias_false_takes_new(self):
        """SSH-config path: the alias is authoritative from the source."""
        merged = merge_synced_node(self._new(), self._existing(), preserve_ssh_alias=False)
        assert merged.ssh_alias == "new-alias"

    def test_first_seen_is_write_once(self):
        """first_seen always comes from the existing node -- absent stays absent."""
        existing = self._existing()
        new = self._new().model_copy(update={"first_seen": "2026-07-16"})
        merged = merge_synced_node(new, existing, preserve_ssh_alias=True)
        assert merged.first_seen is None  # existing has none: no mass rewrite

        existing_with = existing.model_copy(update={"first_seen": "2020-01-01"})
        merged = merge_synced_node(new, existing_with, preserve_ssh_alias=True)
        assert merged.first_seen == "2020-01-01"

    def test_unknown_fields_survive_merge_and_write(self, tmp_path):
        """The full sync-update flow: a node file carrying newer-schema fields
        is read, merged with fresh source data, and written back -- the
        unknown fields must survive (merge_synced_node builds on model_copy of
        the existing node so the read_model stash rides along)."""
        node_file = tmp_path / "web.yaml"
        write_yaml(
            node_file,
            {
                "version": "2.0",
                "id": "vm:web",
                "slug": "web",
                "type": "vm",
                "name": "Old Name",
                "ssh_alias": "old-alias",
                "description": "hand-written docs",
                "lifecycle": "production",  # top-level field from a newer ic
            },
        )

        existing = read_model(node_file, Node)
        merged = merge_synced_node(self._new(), existing, preserve_ssh_alias=True)
        write_model(node_file, merged)

        rewritten = read_yaml(node_file)
        assert rewritten["lifecycle"] == "production"
        assert rewritten["ip_addresses"] == ["10.0.0.5"]  # sync applied
        assert rewritten["description"] == "hand-written docs"  # manual kept


# ── SSH config sync: run records, sync guard, first_seen ──────────


def _utc_today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


@pytest.fixture()
def ssh_sync_env(tmp_project, monkeypatch_environment, tmp_path):
    """A patched environment plus a helper to (re)write the SSH source config."""
    ssh_cfg = tmp_path / "ssh-config-file"

    def _configure(text: str, name: str = "ssh-test"):
        ssh_cfg.write_text(text)
        write_yaml(
            tmp_project.source_file(name),
            {
                "version": "2.0",
                "name": name,
                "type": "ssh_config",
                "status": "configured",
                "config_path": str(ssh_cfg),
            },
        )
        return ssh_cfg

    return monkeypatch_environment, tmp_project, _configure


class TestSSHConfigSync:
    def test_create_sets_first_seen_and_writes_run_record(self, ssh_sync_env):
        env, project, configure = ssh_sync_env
        configure("Host web-01\n    HostName 10.0.0.1\n")

        result = SSHConfigSource().sync("testproject", "ssh-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.nodes_created == 1
        node = read_model(project.node_file("vm", "web-01"), Node)
        assert node.first_seen == _utc_today()

        records = load_run_records(env, project="testproject", source="ssh-test")
        assert len(records) == 1
        assert records[0].status == "success"
        assert records[0].created == ["vm:web-01"]
        assert records[0].updated == []
        assert records[0].confirmed_unchanged == []

    def test_unchanged_resync_confirms_without_rewriting(self, ssh_sync_env):
        env, project, configure = ssh_sync_env
        configure("Host web-01\n    HostName 10.0.0.1\n")
        plugin = SSHConfigSource()
        plugin.sync("testproject", "ssh-test")

        node_file = project.node_file("vm", "web-01")
        os.utime(node_file, (100, 100))  # sentinel: any rewrite bumps mtime

        result = plugin.sync("testproject", "ssh-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.nodes_created == 0
        assert result.nodes_updated == 0
        assert result.nodes_unchanged == 1
        assert node_file.stat().st_mtime == 100  # file untouched

        records = load_run_records(env, project="testproject", source="ssh-test")
        assert records[0].confirmed_unchanged == ["vm:web-01"]

    def test_update_preserves_write_once_first_seen(self, ssh_sync_env):
        env, project, configure = ssh_sync_env
        configure("Host web-01\n    HostName 10.0.0.1\n")
        plugin = SSHConfigSource()
        plugin.sync("testproject", "ssh-test")

        # Backdate first_seen, then change the host so the resync updates it.
        node_file = project.node_file("vm", "web-01")
        data = read_yaml(node_file)
        data["first_seen"] = "2020-01-01"
        write_yaml(node_file, data)
        configure("Host web-01\n    HostName 10.0.0.99\n")

        result = plugin.sync("testproject", "ssh-test")

        assert result.nodes_updated == 1
        node = read_model(node_file, Node)
        assert node.ip_addresses == ["10.0.0.99"]
        assert node.first_seen == "2020-01-01"
        records = load_run_records(env, project="testproject", source="ssh-test")
        assert records[0].updated == ["vm:web-01"]

    def test_existing_node_without_first_seen_stays_without(self, ssh_sync_env):
        _, project, configure = ssh_sync_env
        configure("Host web-01\n    HostName 10.0.0.1\n")
        plugin = SSHConfigSource()
        plugin.sync("testproject", "ssh-test")

        node_file = project.node_file("vm", "web-01")
        data = read_yaml(node_file)
        del data["first_seen"]
        write_yaml(node_file, data)
        configure("Host web-01\n    HostName 10.0.0.99\n")

        result = plugin.sync("testproject", "ssh-test")

        assert result.nodes_updated == 1
        assert "first_seen" not in read_yaml(node_file)

    def test_empty_sync_never_rewrites_node_files(self, ssh_sync_env):
        env, project, configure = ssh_sync_env
        configure("Host web-01\n    HostName 10.0.0.1\n")
        plugin = SSHConfigSource()
        plugin.sync("testproject", "ssh-test")

        node_file = project.node_file("vm", "web-01")
        os.utime(node_file, (100, 100))
        configure("")  # source now reports zero hosts

        result = plugin.sync("testproject", "ssh-test")

        assert result.status is SyncStatus.SUCCESS
        assert "empty-sync guard" in result.message
        assert node_file.stat().st_mtime == 100
        # The empty run is recorded but carries no observations.
        records = load_run_records(env, project="testproject", source="ssh-test")
        assert records[0].seen_node_ids == frozenset()
        assert not records[0].counts_for_presence

    def test_partial_sync_writes_nothing(self, ssh_sync_env):
        env, project, configure = ssh_sync_env
        # bad-01 collides with a node bound to a different source.
        bound = Node(
            id="vm:bad-01", slug="bad-01", type=NodeType.VM, name="bad-01",
            source_id="ssh_config:other:bad-01",
        )
        project.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(project.node_file("vm", "bad-01"), bound)
        configure("Host bad-01\n    HostName 10.0.0.1\nHost good-01\n    HostName 10.0.0.2\n")

        result = SSHConfigSource().sync("testproject", "ssh-test")

        assert result.status is SyncStatus.PARTIAL
        assert result.errors
        assert result.nodes_created == 0
        # Sync guard: the collision blocks ALL writes, good-01 included.
        assert not project.node_file("vm", "good-01").exists()
        records = load_run_records(env, project="testproject", source="ssh-test")
        assert records[0].status == "partial"

    def test_failed_sync_is_recorded(self, ssh_sync_env, monkeypatch):
        env, _, configure = ssh_sync_env
        configure("Host web-01\n    HostName 10.0.0.1\n")

        def _boom(_path):
            raise RuntimeError("boom")

        monkeypatch.setattr("infracontext.sources.ssh_config.parse_ssh_config", _boom)
        result = SSHConfigSource().sync("testproject", "ssh-test")

        assert result.status is SyncStatus.FAILED
        records = load_run_records(env, project="testproject", source="ssh-test")
        assert records[0].status == "failed"
        assert records[0].seen_node_ids == frozenset()


# ── Proxmox sync: guard + run records via a fake PVE client ───────


class _Getter:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class _FakeVMApi:
    def __init__(self, config: dict):
        self.config = _Getter(config)

    def agent(self, _command):
        raise RuntimeError("no guest agent")  # sync swallows this


class _FakeCollection:
    """Mimics proxmoxer's dual list/lookup surface: .get() and (vmid)."""

    def __init__(self, items=(), configs=None):
        self._items = list(items)
        self._configs = configs or {}

    def get(self):
        return list(self._items)

    def __call__(self, vmid):
        return _FakeVMApi(self._configs.get(vmid, {}))


class _FakeNodeApi:
    def __init__(self, status=None, qemu=None, lxc=None):
        self.status = _Getter(status or {})
        self.qemu = qemu or _FakeCollection()
        self.lxc = lxc or _FakeCollection()


class _FakeNodesApi:
    def __init__(self, items, apis):
        self._items = list(items)
        self._apis = apis

    def get(self):
        return list(self._items)

    def __call__(self, name):
        return self._apis[name]


class _FakePVE:
    def __init__(self, *, nodes=(), node_apis=None, cluster_status=(), storage=()):
        class _Cluster:
            pass

        self.cluster = _Cluster()
        self.cluster.status = _Getter(list(cluster_status))
        self.nodes = _FakeNodesApi(nodes, node_apis or {})
        self.storage = _Getter(list(storage))


@pytest.fixture()
def proxmox_sync_env(tmp_project, monkeypatch_environment, monkeypatch):
    """Patched environment plus a helper wiring a fake PVE client."""
    write_yaml(
        tmp_project.source_file("pve-test"),
        {
            "version": "2.0",
            "name": "pve-test",
            "type": "proxmox",
            "status": "configured",
            "api_url": "https://pve.example.com:8006",
            "api_token_id": "user@pam!token",
        },
    )

    def _with_client(fake):
        monkeypatch.setattr(ProxmoxSource, "_get_client", lambda self, config: fake)  # noqa: ARG005
        return ProxmoxSource()

    return monkeypatch_environment, tmp_project, _with_client


class TestProxmoxSync:
    def test_success_creates_host_with_first_seen_and_run_record(self, proxmox_sync_env):
        env, project, with_client = proxmox_sync_env
        fake = _FakePVE(
            nodes=[{"node": "pve-01", "status": "online"}],
            node_apis={"pve-01": _FakeNodeApi()},
        )

        result = with_client(fake).sync("testproject", "pve-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.nodes_created == 1
        node = read_model(project.node_file("physical_host", "pve-01"), Node)
        assert node.first_seen == _utc_today()
        records = load_run_records(env, project="testproject", source="pve-test")
        assert records[0].created == ["physical_host:pve-01"]

    def test_unchanged_resync_confirms_without_rewriting(self, proxmox_sync_env):
        env, project, with_client = proxmox_sync_env
        fake = _FakePVE(
            nodes=[{"node": "pve-01", "status": "online"}],
            node_apis={"pve-01": _FakeNodeApi()},
        )
        plugin = with_client(fake)
        plugin.sync("testproject", "pve-test")

        node_file = project.node_file("physical_host", "pve-01")
        os.utime(node_file, (100, 100))

        result = plugin.sync("testproject", "pve-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.nodes_unchanged == 1
        assert node_file.stat().st_mtime == 100
        records = load_run_records(env, project="testproject", source="pve-test")
        assert records[0].confirmed_unchanged == ["physical_host:pve-01"]

    def test_save_relationships_preserves_file_level_unknown_keys(self, tmp_project):
        """_save_relationships writes back through the instance read_model
        returned, so top-level keys a newer ic added to relationships.yaml
        (stashed as unknown fields) survive every sync rewrite."""
        write_yaml(
            tmp_project.relationships_yaml,
            {
                "future_key": {"enabled": True},
                "relationships": [{"source": "vm:a", "target": "vm:b", "type": "depends_on"}],
            },
        )
        stats = ProxmoxSyncStats()

        ProxmoxSource()._save_relationships(
            tmp_project,
            "pve-test",
            [Relationship(source="vm:c", target="vm:d", type=RelationshipType.RUNS_ON, managed_by="pve-test")],
            stats,
        )

        data = read_yaml(tmp_project.relationships_yaml)
        assert data["future_key"] == {"enabled": True}
        assert len(data["relationships"]) == 2
        assert stats.relationships_created == 1

    def test_empty_result_never_rewrites_existing_nodes(self, proxmox_sync_env):
        env, project, with_client = proxmox_sync_env
        # A node this source manages already exists on disk.
        existing = Node(
            id="vm:old-vm", slug="old-vm", type=NodeType.VM, name="old-vm",
            source_id="proxmox:standalone:qemu:100", source="pve-test", managed_by="pve-test",
        )
        project.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        node_file = project.node_file("vm", "old-vm")
        write_model(node_file, existing)
        os.utime(node_file, (100, 100))

        result = with_client(_FakePVE()).sync("testproject", "pve-test")

        assert result.status is SyncStatus.SUCCESS
        assert "empty-sync guard" in result.message
        assert node_file.stat().st_mtime == 100
        assert not project.relationships_yaml.exists()
        records = load_run_records(env, project="testproject", source="pve-test")
        assert not records[0].counts_for_presence

    def test_slug_collision_blocks_all_writes(self, proxmox_sync_env):
        _, project, with_client = proxmox_sync_env
        # pve-01 collides with a node bound to a different cluster's source_id.
        bound = Node(
            id="physical_host:pve-01", slug="pve-01", type=NodeType.PHYSICAL_HOST,
            name="pve-01", source_id="proxmox:othercluster:node:pve-01",
        )
        project.node_type_dir("physical_host").mkdir(parents=True, exist_ok=True)
        write_model(project.node_file("physical_host", "pve-01"), bound)
        fake = _FakePVE(
            nodes=[{"node": "pve-01", "status": "online"}, {"node": "pve-02", "status": "online"}],
            node_apis={"pve-01": _FakeNodeApi(), "pve-02": _FakeNodeApi()},
        )

        result = with_client(fake).sync("testproject", "pve-test")

        assert result.status is SyncStatus.PARTIAL
        assert result.errors
        # Sync guard: the collision blocks pve-02's write as well.
        assert not project.node_file("physical_host", "pve-02").exists()
