"""Tests for the CheckMK (Livestatus over SSH) source plugin."""

import time

import pytest

from infracontext.models.node import Node
from infracontext.runs import load_run_records
from infracontext.sources.base import SyncStatus
from infracontext.sources.checkmk import CheckMKSource, LivestatusError
from infracontext.storage import read_model, read_yaml, write_model, write_yaml


def _utc_today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _host_row(name, address, alias=None, labels=None, groups=None):
    return [name, address, alias or name, labels or {}, groups or []]


@pytest.fixture()
def checkmk_env(tmp_project, monkeypatch_environment, monkeypatch):
    """Patched environment plus helpers to configure the source and fake Livestatus."""

    def _configure(name="cmk-test", **overrides):
        config = {
            "version": "2.0",
            "name": name,
            "type": "checkmk",
            "status": "configured",
            "ssh_alias": "monitor",
            "site": "mysite",
        }
        config.update(overrides)
        write_yaml(tmp_project.source_file(name), config)
        return name

    def _fake_rows(rows):
        monkeypatch.setattr(
            CheckMKSource,
            "_run_livestatus",
            lambda self, config, query: rows,
        )

    return monkeypatch_environment, tmp_project, _configure, _fake_rows


class TestValidateConfig:
    def test_requires_ssh_alias_and_site(self):
        errors = CheckMKSource().validate_config({})
        assert any("ssh_alias" in e for e in errors)
        assert any("site" in e for e in errors)

    def test_rejects_shell_metacharacters_in_site(self):
        errors = CheckMKSource().validate_config(
            {"ssh_alias": "monitor", "site": "x; rm -rf /"}
        )
        assert any("invalid characters" in e for e in errors)

    def test_rejects_bad_regex_and_bad_default_type(self):
        errors = CheckMKSource().validate_config(
            {"ssh_alias": "m", "site": "s", "exclude_patterns": ["("], "default_node_type": "nope"}
        )
        assert any("exclude_patterns" in e for e in errors)
        assert any("default_node_type" in e for e in errors)

    def test_valid_config_passes(self):
        assert CheckMKSource().validate_config({"ssh_alias": "m", "site": "prod"}) == []


class TestSync:
    def test_creates_nodes_with_observability_and_run_record(self, checkmk_env):
        env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows(
            [
                _host_row("web-01.example.com", "10.0.0.1", labels={"cmk/os_family": "linux"}),
                _host_row("db-01", "10.0.0.2", groups=["databases"]),
            ]
        )

        result = CheckMKSource().sync("testproject", "cmk-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.nodes_created == 2
        node = read_model(project.node_file("vm", "web-01-example-com"), Node)
        assert node.first_seen == _utc_today()
        assert node.managed_by == "cmk-test"
        assert node.ip_addresses == ["10.0.0.1"]
        assert "web-01.example.com" in node.domains
        assert node.attributes["checkmk"]["labels"] == {"os_family": "linux"}
        obs = [o for o in node.observability if o.type == "checkmk"]
        assert obs and obs[0].host_name == "web-01.example.com"

        records = load_run_records(env, project="testproject", source="cmk-test")
        assert records and set(records[-1].created) == {
            "vm:web-01-example-com",
            "vm:db-01",
        }

    def test_strip_domain_suffixes_shortens_slug_not_name(self, checkmk_env):
        _env, project, configure, fake_rows = checkmk_env
        configure(strip_domain_suffixes=[".example.com"])
        fake_rows([_host_row("web-01.example.com", "10.0.0.1")])

        CheckMKSource().sync("testproject", "cmk-test")

        node = read_model(project.node_file("vm", "web-01"), Node)
        assert node.name == "web-01.example.com"
        assert node.slug == "web-01"

    def test_device_type_label_maps_node_type(self, checkmk_env):
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows(
            [
                _host_row("core-switch", "10.0.0.9", labels={"cmk/device_type": "switch"}),
                _host_row("app-container", "10.0.0.8", labels={"cmk/device_type": "container"}),
            ]
        )

        CheckMKSource().sync("testproject", "cmk-test")

        assert read_model(project.node_file("network_device", "core-switch"), Node).type == "network_device"
        assert read_model(project.node_file("oci_container", "app-container"), Node).type == "oci_container"

    def test_type_patterns_override_labels(self, checkmk_env):
        _env, project, configure, fake_rows = checkmk_env
        configure(type_patterns={"physical_host": ["^storage"]})
        fake_rows([_host_row("storage1", "10.0.0.4", labels={"cmk/device_type": "vm"})])

        CheckMKSource().sync("testproject", "cmk-test")

        assert project.node_file("physical_host", "storage1").exists()

    def test_hex_container_ids_excluded_by_default(self, checkmk_env):
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows(
            [
                _host_row("aabbccddeeff", "10.0.0.7"),
                _host_row("web-01", "10.0.0.1"),
            ]
        )

        result = CheckMKSource().sync("testproject", "cmk-test")

        assert result.nodes_created == 1
        assert not project.node_file("vm", "aabbccddeeff").exists()

    def test_non_ip_address_lands_in_domains(self, checkmk_env):
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows([_host_row("special", "special.example.net")])

        CheckMKSource().sync("testproject", "cmk-test")

        node = read_model(project.node_file("vm", "special"), Node)
        assert node.ip_addresses == []
        assert node.domains == ["special.example.net"]

    def test_resync_is_unchanged_and_preserves_manual_fields(self, checkmk_env):
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows([_host_row("web-01", "10.0.0.1")])
        plugin = CheckMKSource()
        plugin.sync("testproject", "cmk-test")

        # Operator adds manual context between syncs.
        node_file = project.node_file("vm", "web-01")
        node = read_model(node_file, Node)
        node = node.model_copy(update={"description": "hand-written", "ssh_alias": "my-alias"})
        write_model(node_file, node)

        result = plugin.sync("testproject", "cmk-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.nodes_unchanged == 1
        node = read_model(node_file, Node)
        assert node.description == "hand-written"
        assert node.ssh_alias == "my-alias"

    def test_device_type_relabel_relocates_node_without_stale_duplicate(self, checkmk_env):
        """A host gaining a cmk/device_type label changes node type — the old
        file must be removed, not left behind as a duplicate node."""
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows([_host_row("core-sw", "10.0.0.9")])
        plugin = CheckMKSource()
        plugin.sync("testproject", "cmk-test")
        first_seen = read_model(project.node_file("vm", "core-sw"), Node).first_seen

        fake_rows([_host_row("core-sw", "10.0.0.9", labels={"cmk/device_type": "switch"})])
        result = plugin.sync("testproject", "cmk-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.nodes_updated == 1
        assert result.nodes_created == 0
        assert any("Relocated" in w for w in result.warnings)
        assert not project.node_file("vm", "core-sw").exists()
        node = read_model(project.node_file("network_device", "core-sw"), Node)
        assert node.first_seen == first_seen  # write-once survives relocation

    def test_slug_config_change_relocates_and_preserves_manual_fields(self, checkmk_env):
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows([_host_row("web-01.example.com", "10.0.0.1")])
        plugin = CheckMKSource()
        plugin.sync("testproject", "cmk-test")
        old_file = project.node_file("vm", "web-01-example-com")
        node = read_model(old_file, Node)
        write_model(old_file, node.model_copy(update={"description": "manual notes"}))

        configure(strip_domain_suffixes=[".example.com"])
        result = plugin.sync("testproject", "cmk-test")

        assert result.nodes_updated == 1
        assert not old_file.exists()
        node = read_model(project.node_file("vm", "web-01"), Node)
        assert node.description == "manual notes"

    def test_relocation_destination_collision_is_guarded(self, checkmk_env):
        """If the relocation target slot is occupied by a foreign node,
        nothing may be written or deleted."""
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows([_host_row("core-sw", "10.0.0.9")])
        plugin = CheckMKSource()
        plugin.sync("testproject", "cmk-test")

        project.node_type_dir("network_device").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("network_device", "core-sw"),
            Node(
                id="network_device:core-sw",
                slug="core-sw",
                type="network_device",
                name="core-sw",
                source_id="manual:other",
            ),
        )

        fake_rows([_host_row("core-sw", "10.0.0.9", labels={"cmk/device_type": "switch"})])
        result = plugin.sync("testproject", "cmk-test")

        assert result.status is SyncStatus.PARTIAL
        assert project.node_file("vm", "core-sw").exists()  # old node untouched
        foreign = read_model(project.node_file("network_device", "core-sw"), Node)
        assert foreign.source_id == "manual:other"

    def test_relocation_rewrites_relationship_and_chain_references(self, checkmk_env):
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows([_host_row("core-sw", "10.0.0.9"), _host_row("web-01", "10.0.0.1")])
        plugin = CheckMKSource()
        plugin.sync("testproject", "cmk-test")
        write_yaml(
            project.relationships_yaml,
            {
                "version": "2.0",
                "relationships": [
                    {"source": "vm:web-01", "target": "vm:core-sw", "type": "connects_to"}
                ],
            },
        )
        write_yaml(
            project.chains_yaml,
            {
                "version": "2.0",
                "chains": [
                    {
                        "name": "web-path",
                        "members": ["vm:web-01", {"id": "vm:core-sw", "via": "uplink"}],
                    }
                ],
            },
        )

        fake_rows(
            [
                _host_row("core-sw", "10.0.0.9", labels={"cmk/device_type": "switch"}),
                _host_row("web-01", "10.0.0.1"),
            ]
        )
        result = plugin.sync("testproject", "cmk-test")

        assert any("Rewrote 2 relationship/chain reference" in w for w in result.warnings)
        rels = read_yaml(project.relationships_yaml)["relationships"]
        assert rels[0]["target"] == "network_device:core-sw"
        chain = read_yaml(project.chains_yaml)["chains"][0]
        assert chain["members"][1]["id"] == "network_device:core-sw"

    def test_reference_rewrite_preserves_comments_and_unrelated_files(self, checkmk_env):
        """The rewrite must go through the comment-preserving writer, and
        files that don't mention a relocated id must not be touched."""
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows([_host_row("core-sw", "10.0.0.9"), _host_row("web-01", "10.0.0.1")])
        plugin = CheckMKSource()
        plugin.sync("testproject", "cmk-test")
        project.relationships_yaml.write_text(
            "# hand-written topology notes\n"
            "version: '2.0'\n"
            "relationships:\n"
            "  # uplink edge\n"
            "  - source: vm:web-01\n"
            "    target: vm:core-sw\n"
            "    type: connects_to\n"
        )
        project.chains_yaml.write_text(
            "version: '2.0'\nchains:\n  - name: unrelated\n    members: [vm:web-01, vm:other]\n"
        )
        chains_before = project.chains_yaml.read_text()

        fake_rows(
            [
                _host_row("core-sw", "10.0.0.9", labels={"cmk/device_type": "switch"}),
                _host_row("web-01", "10.0.0.1"),
            ]
        )
        plugin.sync("testproject", "cmk-test")

        rel_text = project.relationships_yaml.read_text()
        assert "network_device:core-sw" in rel_text
        assert "# hand-written topology notes" in rel_text
        assert "# uplink edge" in rel_text
        assert project.chains_yaml.read_text() == chains_before  # untouched

    def test_superstring_ids_do_not_cause_unrelated_rewrite(self, checkmk_env):
        """Renaming vm:core-sw must not touch a file that only references
        vm:core-switch — the substring gate alone would let it through, so
        the updater has to veto the write when nothing matched exactly."""
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows([_host_row("core-sw", "10.0.0.9"), _host_row("core-switch", "10.0.0.10")])
        plugin = CheckMKSource()
        plugin.sync("testproject", "cmk-test")
        # Deliberately hand-formatted: any rewrite would normalize it.
        project.relationships_yaml.write_text(
            "version: '2.0'\n"
            "relationships:\n"
            "    -   source:   vm:core-switch\n"
            "        target:   vm:core-switch\n"
            "        type: connects_to\n"
        )
        before = project.relationships_yaml.read_text()

        fake_rows(
            [
                _host_row("core-sw", "10.0.0.9", labels={"cmk/device_type": "switch"}),
                _host_row("core-switch", "10.0.0.10"),
            ]
        )
        result = plugin.sync("testproject", "cmk-test")

        assert any("Relocated" in w for w in result.warnings)
        assert project.relationships_yaml.read_text() == before

    def test_reference_rewrite_failure_degrades_to_warning(self, checkmk_env, monkeypatch):
        """Node writes are already applied when the rewrite runs — a broken
        reference file must not abort the run half-recorded."""
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows([_host_row("core-sw", "10.0.0.9"), _host_row("web-01", "10.0.0.1")])
        plugin = CheckMKSource()
        plugin.sync("testproject", "cmk-test")
        write_yaml(
            project.relationships_yaml,
            {
                "version": "2.0",
                "relationships": [
                    {"source": "vm:web-01", "target": "vm:core-sw", "type": "connects_to"}
                ],
            },
        )

        def _boom(path, updater, **kwargs):
            raise OSError("disk on fire")

        # The rewrite helper lives in sources.base (shared by all plugins).
        monkeypatch.setattr("infracontext.sources.base.update_yaml", _boom)
        fake_rows(
            [
                _host_row("core-sw", "10.0.0.9", labels={"cmk/device_type": "switch"}),
                _host_row("web-01", "10.0.0.1"),
            ]
        )
        result = plugin.sync("testproject", "cmk-test")

        assert result.status is SyncStatus.SUCCESS
        assert any("Could not rewrite references" in w for w in result.warnings)
        # Node relocation itself still happened and was recorded.
        assert project.node_file("network_device", "core-sw").exists()
        records = load_run_records(_env, project="testproject", source="cmk-test")
        assert records[-1].status == "success"

    def test_relocation_onto_manual_node_is_blocked(self, checkmk_env):
        """A user-owned node (no source_id) at the relocation target must
        never be overwritten — that would destroy hand-written data."""
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows([_host_row("core-sw", "10.0.0.9")])
        plugin = CheckMKSource()
        plugin.sync("testproject", "cmk-test")

        project.node_type_dir("network_device").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("network_device", "core-sw"),
            Node(
                id="network_device:core-sw",
                slug="core-sw",
                type="network_device",
                name="core-sw",
                notes="hand-written runbook",
            ),
        )

        fake_rows([_host_row("core-sw", "10.0.0.9", labels={"cmk/device_type": "switch"})])
        result = plugin.sync("testproject", "cmk-test")

        assert result.status is SyncStatus.PARTIAL
        assert any("consolidate" in e for e in result.errors)
        assert project.node_file("vm", "core-sw").exists()
        manual = read_model(project.node_file("network_device", "core-sw"), Node)
        assert manual.notes == "hand-written runbook"
        assert manual.source_id is None

    def test_stale_duplicate_at_old_location_is_cleaned_up(self, checkmk_env):
        """If the destination already carries this source_id and a second
        file for the same host exists elsewhere, the sync merges into the
        destination and removes the stale file."""
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows([_host_row("core-sw", "10.0.0.9", labels={"cmk/device_type": "switch"})])
        plugin = CheckMKSource()
        plugin.sync("testproject", "cmk-test")

        # Simulate a duplicate left behind by an older sync at the vm path.
        dest = read_model(project.node_file("network_device", "core-sw"), Node)
        project.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("vm", "core-sw"),
            dest.model_copy(update={"id": "vm:core-sw", "type": "vm"}),
        )

        result = plugin.sync("testproject", "cmk-test")

        assert result.status is SyncStatus.SUCCESS
        assert any("stale duplicate" in w for w in result.warnings)
        assert not project.node_file("vm", "core-sw").exists()
        assert project.node_file("network_device", "core-sw").exists()

    def test_manual_node_at_same_slug_is_adopted(self, checkmk_env):
        """Pre-existing semantic shared with the other sources: a manual node
        at its own slug is enriched, keeping hand-written fields."""
        _env, project, configure, fake_rows = checkmk_env
        configure()
        project.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("vm", "web-01"),
            Node(id="vm:web-01", slug="web-01", type="vm", name="web-01", description="manual"),
        )
        fake_rows([_host_row("web-01", "10.0.0.1")])

        result = CheckMKSource().sync("testproject", "cmk-test")

        assert result.nodes_updated == 1
        node = read_model(project.node_file("vm", "web-01"), Node)
        assert node.description == "manual"
        assert node.managed_by == "cmk-test"
        assert node.ip_addresses == ["10.0.0.1"]

    def test_two_hosts_mapping_to_same_slug_in_one_run_is_an_error(self, checkmk_env):
        """Case-variant host names slugify identically — last-write-wins
        would silently drop one host, so it must be a guarded error."""
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows([_host_row("web-01", "10.0.0.1"), _host_row("WEB-01", "10.0.0.2")])

        result = CheckMKSource().sync("testproject", "cmk-test")

        assert result.status is SyncStatus.PARTIAL
        assert any("Slug collision within sync" in e for e in result.errors)
        assert not project.node_file("vm", "web-01").exists()  # sync guard held

    def test_yaml11_bool_label_values_do_not_cause_phantom_updates(self, checkmk_env):
        """CheckMK piggyback labels carry the value "yes". A YAML 1.1 reader
        turns that into True on reload, so every re-sync classified those
        nodes as updated forever (found against a live site)."""
        _env, _project, configure, fake_rows = checkmk_env
        configure()
        fake_rows([_host_row("www30", "10.0.0.30", labels={"cmk/piggyback_source_PVE-A": "yes"})])
        plugin = CheckMKSource()
        plugin.sync("testproject", "cmk-test")

        result = plugin.sync("testproject", "cmk-test")

        assert result.nodes_updated == 0
        assert result.nodes_unchanged == 1

    def test_livestatus_failure_is_failed_and_writes_nothing(self, checkmk_env, monkeypatch):
        env, project, configure, _fake_rows = checkmk_env
        configure()

        def _boom(self, config, query):
            raise LivestatusError("Livestatus query via 'monitor' failed (exit 255): denied")

        monkeypatch.setattr(CheckMKSource, "_run_livestatus", _boom)

        result = CheckMKSource().sync("testproject", "cmk-test")

        assert result.status is SyncStatus.FAILED
        assert "denied" in result.message
        assert not project.node_file("vm", "web-01").exists()
        records = load_run_records(env, project="testproject", source="cmk-test")
        assert records and records[-1].status == "failed"

    def test_slug_collision_with_foreign_source_is_guarded(self, checkmk_env):
        _env, project, configure, fake_rows = checkmk_env
        configure()
        fake_rows([_host_row("web-01", "10.0.0.1")])
        project.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("vm", "web-01"),
            Node(
                id="vm:web-01",
                slug="web-01",
                type="vm",
                name="web-01",
                source_id="proxmox:other:qemu:1",
            ),
        )

        result = CheckMKSource().sync("testproject", "cmk-test")

        # Collision is an error -> sync guard: nothing rewritten.
        assert result.status is SyncStatus.PARTIAL
        node = read_model(project.node_file("vm", "web-01"), Node)
        assert node.source_id == "proxmox:other:qemu:1"
