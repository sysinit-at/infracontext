"""Tests for infracontext.sources — SSH config parsing, source merge helper."""


from infracontext.models.node import Node, NodeType
from infracontext.sources.base import merge_synced_node
from infracontext.sources.ssh_config import parse_ssh_config

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
