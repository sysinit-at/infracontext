"""Tests for the SNMP source plugin (device discovery + LLDP topology)."""

import os
import time

import pytest

from infracontext.models.node import Node, NodeType, Observability
from infracontext.runs import load_run_records
from infracontext.sources.base import SyncStatus
from infracontext.sources.snmp import (
    ENTITY,
    IF_TABLE,
    IF_X_TABLE,
    LLDP_REM,
    SYS,
    SNMPError,
    SNMPSource,
    build_identity_index,
    match_neighbor,
    parse_hardware,
    parse_interfaces,
    parse_neighbors,
    parse_system,
)
from infracontext.storage import read_model, read_yaml, write_model, write_yaml


def _utc_today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


# ── canned-walk builders (produce (oid, value) pairs) ──────────────


def sys_rows(name, descr="a device", location="rack-1", uptime=12345):
    return [
        (f"{SYS}.1.0", descr),
        (f"{SYS}.3.0", uptime),
        (f"{SYS}.5.0", name),
        (f"{SYS}.6.0", location),
    ]


def entity_rows(entries):
    """entries: list of (idx, class, mfg, model, serial)."""
    rows = []
    for idx, cls, mfg, model, serial in entries:
        rows += [
            (f"{ENTITY}.5.{idx}", cls),
            (f"{ENTITY}.12.{idx}", mfg),
            (f"{ENTITY}.13.{idx}", model),
            (f"{ENTITY}.11.{idx}", serial),
        ]
    return rows


def if_rows(entries):
    """entries: list of dicts with idx and optional descr/speed/mac/admin/oper."""
    rows = []
    for e in entries:
        i = e["idx"]
        rows += [
            (f"{IF_TABLE}.2.{i}", e.get("descr", "")),
            (f"{IF_TABLE}.5.{i}", e.get("speed", 0)),
            (f"{IF_TABLE}.6.{i}", e.get("mac", b"")),
            (f"{IF_TABLE}.7.{i}", e.get("admin", 1)),
            (f"{IF_TABLE}.8.{i}", e.get("oper", 1)),
        ]
    return rows


def ifx_rows(entries):
    rows = []
    for e in entries:
        i = e["idx"]
        if "name" in e:
            rows.append((f"{IF_X_TABLE}.1.{i}", e["name"]))
        if "high" in e:
            rows.append((f"{IF_X_TABLE}.15.{i}", e["high"]))
    return rows


def lldp_rows(neighbors):
    """neighbors: list of dicts with index and optional sysname/portid/portdesc."""
    rows = []
    for n in neighbors:
        idx = n["index"]
        rows.append((f"{LLDP_REM}.9.{idx}", n.get("sysname", "")))
        if "portid" in n:
            rows.append((f"{LLDP_REM}.7.{idx}", n["portid"]))
        if "portdesc" in n:
            rows.append((f"{LLDP_REM}.8.{idx}", n["portdesc"]))
    return rows


def target(sys=None, entity=None, iftab=None, ifx=None, lldp=None):
    """Assemble one host's per-base canned walk data."""
    return {
        SYS: sys or [],
        ENTITY: entity or [],
        IF_TABLE: iftab or [],
        IF_X_TABLE: ifx or [],
        LLDP_REM: lldp or [],
    }


@pytest.fixture()
def snmp_env(tmp_project, monkeypatch_environment, monkeypatch):
    """Patched environment plus helpers to configure the source and fake walks."""

    def _configure(name="snmp-test", **overrides):
        config = {
            "version": "2.0",
            "name": name,
            "type": "snmp",
            "status": "configured",
            "snmp_version": "2c",
            "targets": [{"host": "10.0.0.1"}],
        }
        config.update(overrides)
        write_yaml(tmp_project.source_file(name), config)
        return name

    def _fake_walks(data, error_hosts=()):
        """data: {host: {base: rows}}. error_hosts raise SNMPError (partial)."""

        def _walk(self, config, host, base):  # noqa: ARG001
            if host in error_hosts:
                raise SNMPError(f"timeout walking {base} on {host}")
            return data.get(host, {}).get(base, [])

        monkeypatch.setattr(SNMPSource, "_walk", _walk)

    return monkeypatch_environment, tmp_project, _configure, _fake_walks


# ── pure parser units ──────────────────────────────────────────────


class TestParsers:
    def test_parse_system(self):
        info = parse_system(sys_rows("core-sw", descr="IOS", location="dc-1", uptime=42))
        assert info == {"sys_descr": "IOS", "sys_uptime": 42, "sys_name": "core-sw", "sys_location": "dc-1"}

    def test_hardware_prefers_chassis_over_module(self):
        rows = entity_rows(
            [
                ("1", 9, "ModMfg", "ModModel", "MOD1"),  # module
                ("2", 3, "Cisco", "C9300", "FCW1"),  # chassis wins
            ]
        )
        assert parse_hardware(rows) == {"manufacturer": "Cisco", "model": "C9300", "serial": "FCW1"}

    def test_hardware_empty_when_no_identifiers(self):
        assert parse_hardware(entity_rows([("1", 3, "", "", "")])) == {}

    def test_interfaces_merge_and_status_labels(self):
        ifaces, truncated, total = parse_interfaces(
            if_rows([{"idx": "1", "descr": "Gi0/1", "speed": 1_000_000_000, "mac": bytes([0, 17, 34, 51, 68, 85]),
                      "admin": 1, "oper": 2}]),
            ifx_rows([{"idx": "1", "name": "Gi0/1"}]),
            64,
        )
        assert not truncated and total == 1
        assert ifaces[0] == {"name": "Gi0/1", "admin": "up", "oper": "down", "speed_mbps": 1000,
                             "mac": "00:11:22:33:44:55"}

    def test_interfaces_capped_with_truncation(self):
        entries = [{"idx": str(i), "descr": f"if{i}"} for i in range(1, 71)]
        ifaces, truncated, total = parse_interfaces(if_rows(entries), [], 64)
        assert len(ifaces) == 64 and truncated and total == 70

    def test_neighbors_index_local_port(self):
        neighbors = parse_neighbors(lldp_rows([{"index": "0.7.3", "sysname": "peer", "portdesc": "Gi1/2"}]))
        assert neighbors == [{"remote_sysname": "peer", "remote_port": "Gi1/2", "local_port": "7"}]

    def test_match_neighbor_shared_identity_guard(self):
        a = Node(id="vm:a", slug="a", type=NodeType.VM, name="a", domains=["shared.example.com"])
        b = Node(id="vm:b", slug="b", type=NodeType.VM, name="b", domains=["shared.example.com"])
        index = build_identity_index([a, b])
        # Ambiguous name owned by two nodes -> no match (never a wrong edge).
        assert match_neighbor("shared.example.com", index) is None
        assert match_neighbor("a", index) == "vm:a"


# ── validate_config ────────────────────────────────────────────────


class TestValidateConfig:
    def test_requires_targets(self):
        errors = SNMPSource().validate_config({"name": "x"})
        assert any("targets" in e for e in errors)

    def test_rejects_bad_version_and_node_type(self):
        errors = SNMPSource().validate_config(
            {"name": "x", "snmp_version": "9", "targets": ["1.2.3.4"], "default_node_type": "nope"}
        )
        assert any("snmp_version" in e for e in errors)
        assert any("default_node_type" in e for e in errors)

    def test_rejects_non_integer_port(self):
        errors = SNMPSource().validate_config({"name": "x", "targets": ["1.2.3.4"], "port": "abc"})
        assert any("port" in e for e in errors)

    def test_v3_requires_user(self):
        errors = SNMPSource().validate_config({"name": "x", "snmp_version": "3", "targets": ["1.2.3.4"]})
        assert any("v3_user" in e for e in errors)

    def test_valid_config_passes(self):
        assert SNMPSource().validate_config({"name": "x", "targets": [{"host": "1.2.3.4"}]}) == []


# ── sync ────────────────────────────────────────────────────────────


class TestSync:
    def test_creates_device_with_hardware_observability_and_run_record(self, snmp_env):
        env, project, configure, fake_walks = snmp_env
        configure()
        fake_walks(
            {
                "10.0.0.1": target(
                    sys=sys_rows("core-sw-01", location="dc-a"),
                    entity=entity_rows([("1", 3, "Cisco", "C9300", "FCW1")]),
                    iftab=if_rows([{"idx": "1", "descr": "Gi0/1", "speed": 1_000_000_000}]),
                )
            }
        )

        result = SNMPSource().sync("testproject", "snmp-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.nodes_created == 1
        node = read_model(project.node_file("network_device", "core-sw-01"), Node)
        assert node.type == "network_device"
        assert node.managed_by == "snmp-test"
        assert node.source_id == "snmp:snmp-test:10.0.0.1"
        assert node.ip_addresses == ["10.0.0.1"]
        assert node.first_seen == _utc_today()
        assert node.attributes["hardware"] == {"manufacturer": "Cisco", "model": "C9300", "serial": "FCW1"}
        assert node.attributes["snmp"]["sys_location"] == "dc-a"
        obs = [o for o in node.observability if o.type == "snmp"]
        assert obs and obs[0].instance == "10.0.0.1" and obs[0].source == "snmp-test"

        records = load_run_records(env, project="testproject", source="snmp-test")
        assert records and records[-1].created == ["network_device:core-sw-01"]

    def test_explicit_target_name_used_slug_from_sysname(self, snmp_env):
        _env, project, configure, fake_walks = snmp_env
        configure(targets=[{"host": "10.0.0.1", "name": "Edge Switch"}])
        fake_walks({"10.0.0.1": target(sys=sys_rows("edge-sw-01"))})

        SNMPSource().sync("testproject", "snmp-test")

        node = read_model(project.node_file("network_device", "edge-sw-01"), Node)
        assert node.slug == "edge-sw-01"  # slug from sysName
        assert node.name == "Edge Switch"  # display name from explicit target

    def test_slug_falls_back_to_host_without_sysname(self, snmp_env):
        _env, project, configure, fake_walks = snmp_env
        configure(targets=[{"host": "10.0.0.9"}])
        fake_walks({"10.0.0.9": target()})  # no sysName

        SNMPSource().sync("testproject", "snmp-test")

        assert project.node_file("network_device", "10-0-0-9").exists()

    def test_interface_summary_capped(self, snmp_env):
        _env, project, configure, fake_walks = snmp_env
        configure(max_interfaces=64)
        entries = [{"idx": str(i), "descr": f"if{i}"} for i in range(1, 71)]
        fake_walks({"10.0.0.1": target(sys=sys_rows("big-sw"), iftab=if_rows(entries))})

        SNMPSource().sync("testproject", "snmp-test")

        node = read_model(project.node_file("network_device", "big-sw"), Node)
        assert len(node.attributes["snmp"]["interfaces"]) == 64
        assert node.attributes["snmp"]["interfaces_truncated"] is True
        assert node.attributes["snmp"]["interfaces_total"] == 70

    def test_lldp_edge_to_existing_node_with_port_attributes(self, snmp_env):
        _env, project, configure, fake_walks = snmp_env
        configure()
        # A server we already know; the switch's LLDP names it.
        project.node_type_dir("physical_host").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("physical_host", "server-01"),
            Node(id="physical_host:server-01", slug="server-01", type=NodeType.PHYSICAL_HOST, name="server-01"),
        )
        fake_walks(
            {
                "10.0.0.1": target(
                    sys=sys_rows("core-sw"),
                    lldp=lldp_rows([{"index": "0.5.1", "sysname": "server-01", "portdesc": "eth0"}]),
                )
            }
        )

        result = SNMPSource().sync("testproject", "snmp-test")

        assert result.relationships_created == 1
        rels = read_yaml(project.relationships_yaml)["relationships"]
        edge = rels[0]
        assert edge["source"] == "network_device:core-sw"
        assert edge["target"] == "physical_host:server-01"
        assert edge["type"] == "connects_to"
        assert edge["managed_by"] == "snmp-test"
        assert edge["attributes"] == {"local_port": "5", "remote_port": "eth0"}

    def test_lldp_edge_between_two_devices_in_same_run(self, snmp_env):
        _env, project, configure, fake_walks = snmp_env
        configure(targets=[{"host": "10.0.0.1"}, {"host": "10.0.0.2"}])
        fake_walks(
            {
                "10.0.0.1": target(sys=sys_rows("sw-1"), lldp=lldp_rows([{"index": "0.1.1", "sysname": "sw-2"}])),
                "10.0.0.2": target(sys=sys_rows("sw-2")),
            }
        )

        result = SNMPSource().sync("testproject", "snmp-test")

        assert result.nodes_created == 2
        rels = read_yaml(project.relationships_yaml)["relationships"]
        assert any(r["source"] == "network_device:sw-1" and r["target"] == "network_device:sw-2" for r in rels)

    def test_unmatched_neighbor_recorded_and_warned(self, snmp_env):
        _env, project, configure, fake_walks = snmp_env
        configure()
        fake_walks(
            {
                "10.0.0.1": target(
                    sys=sys_rows("core-sw"),
                    lldp=lldp_rows([{"index": "0.9.1", "sysname": "mystery-box", "portdesc": "xe-0/0/1"}]),
                )
            }
        )

        result = SNMPSource().sync("testproject", "snmp-test")

        assert result.relationships_created == 0
        node = read_model(project.node_file("network_device", "core-sw"), Node)
        residue = node.attributes["snmp"]["unmatched_neighbors"]
        assert residue == [{"remote_sysname": "mystery-box", "remote_port": "xe-0/0/1", "local_port": "9"}]
        assert any("mystery-box" in w and "unmatched" in w for w in result.warnings)

    def test_resync_unchanged_preserves_manual_fields(self, snmp_env):
        _env, project, configure, fake_walks = snmp_env
        configure()
        fake_walks({"10.0.0.1": target(sys=sys_rows("core-sw"))})
        plugin = SNMPSource()
        plugin.sync("testproject", "snmp-test")

        node_file = project.node_file("network_device", "core-sw")
        node = read_model(node_file, Node)
        write_model(node_file, node.model_copy(update={"description": "manual", "ssh_alias": "core-sw-mgmt"}))

        result = plugin.sync("testproject", "snmp-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.nodes_unchanged == 1
        node = read_model(node_file, Node)
        assert node.description == "manual"
        assert node.ssh_alias == "core-sw-mgmt"

    def test_uptime_drift_alone_does_not_rewrite_node(self, snmp_env):
        """sysUpTime ticks on every poll; it is a live metric, not inventory,
        so it must not be persisted and must not churn an unchanged node file."""
        _env, project, configure, fake_walks = snmp_env
        configure()
        fake_walks({"10.0.0.1": target(sys=sys_rows("core-sw", uptime=1000))})
        plugin = SNMPSource()
        plugin.sync("testproject", "snmp-test")

        node_file = project.node_file("network_device", "core-sw")
        node = read_model(node_file, Node)
        assert "sys_uptime_ticks" not in node.attributes["snmp"]  # live metric not persisted
        os.utime(node_file, (100, 100))  # sentinel: any rewrite bumps mtime

        fake_walks({"10.0.0.1": target(sys=sys_rows("core-sw", uptime=9_999_999))})  # uptime bumped
        result = plugin.sync("testproject", "snmp-test")

        assert result.nodes_unchanged == 1
        assert result.nodes_updated == 0
        assert node_file.stat().st_mtime == 100  # uptime drift must not churn

    def test_unchanged_resync_does_not_rewrite_relationships(self, snmp_env):
        _env, project, configure, fake_walks = snmp_env
        configure()
        project.node_type_dir("physical_host").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("physical_host", "server-01"),
            Node(id="physical_host:server-01", slug="server-01", type=NodeType.PHYSICAL_HOST, name="server-01"),
        )
        data = {
            "10.0.0.1": target(
                sys=sys_rows("core-sw"),
                lldp=lldp_rows([{"index": "0.5.1", "sysname": "server-01", "portdesc": "eth0"}]),
            )
        }
        fake_walks(data)
        plugin = SNMPSource()
        plugin.sync("testproject", "snmp-test")

        os.utime(project.relationships_yaml, (100, 100))  # sentinel
        fake_walks(data)
        plugin.sync("testproject", "snmp-test")

        assert project.relationships_yaml.stat().st_mtime == 100  # no churn

    def test_rename_via_source_id_relocates(self, snmp_env):
        _env, project, configure, fake_walks = snmp_env
        configure()
        fake_walks({"10.0.0.1": target(sys=sys_rows("sw-a"))})
        plugin = SNMPSource()
        plugin.sync("testproject", "snmp-test")
        first_seen = read_model(project.node_file("network_device", "sw-a"), Node).first_seen

        fake_walks({"10.0.0.1": target(sys=sys_rows("sw-b"))})  # sysName changed
        result = plugin.sync("testproject", "snmp-test")

        assert result.nodes_updated == 1
        assert not project.node_file("network_device", "sw-a").exists()
        node = read_model(project.node_file("network_device", "sw-b"), Node)
        assert node.first_seen == first_seen  # write-once survives relocation
        assert any("Relocated" in w for w in result.warnings)

    def test_relocation_rewrites_relationship_references(self, snmp_env):
        _env, project, configure, fake_walks = snmp_env
        configure()
        fake_walks({"10.0.0.1": target(sys=sys_rows("sw-a"))})
        plugin = SNMPSource()
        plugin.sync("testproject", "snmp-test")
        write_yaml(
            project.relationships_yaml,
            {
                "version": "2.0",
                "relationships": [{"source": "vm:app", "target": "network_device:sw-a", "type": "connects_to"}],
            },
        )

        fake_walks({"10.0.0.1": target(sys=sys_rows("sw-b"))})
        result = plugin.sync("testproject", "snmp-test")

        assert any("Rewrote 1 relationship" in w for w in result.warnings)
        rels = read_yaml(project.relationships_yaml)["relationships"]
        assert rels[0]["target"] == "network_device:sw-b"

    def test_relocated_neighbor_edge_is_repointed_not_dangling(self, snmp_env):
        """A neighbor that resolves to a node relocated in the SAME run must not
        leave an edge pointing at the old (now-deleted) id.

        sw-a's LLDP cache still advertises the old sysName 'core-old' while the
        device itself (stable host 10.0.0.2) is renamed core-old -> core-new.
        The freshly-resolved edge sw-a -> core-old must follow the relocation.
        """
        _env, project, configure, fake_walks = snmp_env
        configure(targets=[{"host": "10.0.0.1"}, {"host": "10.0.0.2"}])
        fake_walks(
            {
                "10.0.0.1": target(
                    sys=sys_rows("sw-a"), lldp=lldp_rows([{"index": "0.1.1", "sysname": "core-old"}])
                ),
                "10.0.0.2": target(sys=sys_rows("core-old")),
            }
        )
        plugin = SNMPSource()
        plugin.sync("testproject", "snmp-test")

        # core-old is renamed to core-new, but sw-a still names 'core-old' (cache lag).
        fake_walks(
            {
                "10.0.0.1": target(
                    sys=sys_rows("sw-a"), lldp=lldp_rows([{"index": "0.1.1", "sysname": "core-old"}])
                ),
                "10.0.0.2": target(sys=sys_rows("core-new")),
            }
        )
        result = plugin.sync("testproject", "snmp-test")

        assert any("Relocated" in w for w in result.warnings)
        assert not project.node_file("network_device", "core-old").exists()
        assert project.node_file("network_device", "core-new").exists()
        rels = read_yaml(project.relationships_yaml)["relationships"]
        edge = next(r for r in rels if r["source"] == "network_device:sw-a")
        assert edge["target"] == "network_device:core-new"  # repointed, no dangling id

    def test_partial_target_left_untouched_others_still_sync(self, snmp_env):
        env, project, configure, fake_walks = snmp_env
        configure(targets=[{"host": "10.0.0.1"}, {"host": "10.0.0.2"}])
        # First run: both healthy -> both nodes created.
        fake_walks({"10.0.0.1": target(sys=sys_rows("sw-1")), "10.0.0.2": target(sys=sys_rows("sw-2"))})
        plugin = SNMPSource()
        plugin.sync("testproject", "snmp-test")

        sw2_file = project.node_file("network_device", "sw-2")
        os.utime(sw2_file, (100, 100))  # sentinel: any rewrite bumps mtime

        # Second run: sw-2 errors mid-walk. sw-1 keeps syncing; sw-2 untouched.
        fake_walks(
            {"10.0.0.1": target(sys=sys_rows("sw-1", location="moved")), "10.0.0.2": target(sys=sys_rows("sw-2"))},
            error_hosts=["10.0.0.2"],
        )
        result = plugin.sync("testproject", "snmp-test")

        assert result.status is SyncStatus.PARTIAL
        assert any("10.0.0.2" in e and "untouched" in e for e in result.errors)
        assert sw2_file.stat().st_mtime == 100  # partial target's node untouched
        assert read_model(project.node_file("network_device", "sw-1"), Node).attributes["snmp"]["sys_location"] == "moved"
        records = load_run_records(env, project="testproject", source="snmp-test")
        assert records[0].status == "partial"  # newest-first

    def test_all_targets_partial_is_failed_and_writes_nothing(self, snmp_env):
        env, project, configure, fake_walks = snmp_env
        configure()
        fake_walks({}, error_hosts=["10.0.0.1"])

        result = SNMPSource().sync("testproject", "snmp-test")

        assert result.status is SyncStatus.FAILED
        assert "All SNMP targets failed" in result.message
        assert not project.node_file("network_device", "core-sw").exists()
        records = load_run_records(env, project="testproject", source="snmp-test")
        assert records[-1].status == "failed"

    def test_two_devices_same_sysname_is_slug_collision(self, snmp_env):
        _env, project, configure, fake_walks = snmp_env
        configure(targets=[{"host": "10.0.0.1"}, {"host": "10.0.0.2"}])
        fake_walks({"10.0.0.1": target(sys=sys_rows("dup")), "10.0.0.2": target(sys=sys_rows("dup"))})

        result = SNMPSource().sync("testproject", "snmp-test")

        assert result.status is SyncStatus.PARTIAL
        assert any("Slug collision within sync" in e for e in result.errors)

    def test_foreign_source_id_collision_is_guarded(self, snmp_env):
        _env, project, configure, fake_walks = snmp_env
        configure()
        project.node_type_dir("network_device").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("network_device", "core-sw"),
            Node(
                id="network_device:core-sw",
                slug="core-sw",
                type="network_device",
                name="core-sw",
                source_id="checkmk:other:core-sw",
            ),
        )
        fake_walks({"10.0.0.1": target(sys=sys_rows("core-sw"))})

        result = SNMPSource().sync("testproject", "snmp-test")

        assert result.status is SyncStatus.FAILED
        node = read_model(project.node_file("network_device", "core-sw"), Node)
        assert node.source_id == "checkmk:other:core-sw"  # not overwritten

    def test_manual_node_at_same_slug_is_adopted(self, snmp_env):
        _env, project, configure, fake_walks = snmp_env
        configure()
        project.node_type_dir("network_device").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("network_device", "core-sw"),
            Node(id="network_device:core-sw", slug="core-sw", type="network_device", name="core-sw",
                 description="hand-written"),
        )
        fake_walks({"10.0.0.1": target(sys=sys_rows("core-sw"))})

        result = SNMPSource().sync("testproject", "snmp-test")

        assert result.nodes_updated == 1
        node = read_model(project.node_file("network_device", "core-sw"), Node)
        assert node.description == "hand-written"
        assert node.managed_by == "snmp-test"

    def test_adoption_observability_uses_target_host_not_slug(self, snmp_env):
        # The query plugin resolves instance -> SNMP target; a sysName-derived
        # slug is often not resolvable. The adopted node's entry must carry the
        # configured host and this source's name.
        _env, project, configure, fake_walks = snmp_env
        configure()
        project.node_type_dir("network_device").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("network_device", "core-sw"),
            Node(id="network_device:core-sw", slug="core-sw", type="network_device", name="core-sw"),
        )
        fake_walks({"10.0.0.1": target(sys=sys_rows("core-sw"))})

        SNMPSource().sync("testproject", "snmp-test")

        node = read_model(project.node_file("network_device", "core-sw"), Node)
        obs = [o for o in node.observability if o.type == "snmp"]
        assert [o.instance for o in obs] == ["10.0.0.1"]
        assert obs[0].source == "snmp-test"

    def test_stale_source_owned_observability_healed(self, snmp_env):
        # An entry owned by this source (source field matches) tracking a wrong
        # instance -- e.g. written by an older ic -- is corrected on re-sync.
        _env, project, configure, fake_walks = snmp_env
        configure()
        project.node_type_dir("network_device").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("network_device", "core-sw"),
            Node(
                id="network_device:core-sw",
                slug="core-sw",
                type="network_device",
                name="core-sw",
                observability=[{"type": "snmp", "instance": "core-sw", "source": "snmp-test"}],
            ),
        )
        fake_walks({"10.0.0.1": target(sys=sys_rows("core-sw"))})

        SNMPSource().sync("testproject", "snmp-test")

        node = read_model(project.node_file("network_device", "core-sw"), Node)
        obs = [o for o in node.observability if o.type == "snmp"]
        assert [o.instance for o in obs] == ["10.0.0.1"]  # healed, not duplicated

    def test_legacy_slug_artifact_warned_never_deleted(self, snmp_env):
        # The pre-0.4.0 adoption path wrote {type: snmp, instance: <slug>}
        # with no source field. There is NO reliable provenance separating
        # that artifact from an identical manual entry (adoption itself sets
        # managed_by), so the sync must only WARN with the remediation --
        # source-less entries are never touched, unconditionally.
        _env, project, configure, fake_walks = snmp_env
        configure()
        project.node_type_dir("network_device").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("network_device", "core-sw"),
            Node(
                id="network_device:core-sw",
                slug="core-sw",
                type="network_device",
                name="core-sw",
                source_id="snmp:snmp-test:10.0.0.1",
                source="snmp",
                managed_by="snmp-test",
                observability=[{"type": "snmp", "instance": "core-sw"}],  # the artifact shape
            ),
        )
        fake_walks({"10.0.0.1": target(sys=sys_rows("core-sw"))})

        result = SNMPSource().sync("testproject", "snmp-test")

        assert result.status is SyncStatus.SUCCESS
        node = read_model(project.node_file("network_device", "core-sw"), Node)
        obs = [o for o in node.observability if o.type == "snmp"]
        assert [(o.instance, o.source) for o in obs] == [("core-sw", None)]  # untouched
        warning = next(w for w in result.warnings if "pre-0.4.0 artifact" in w)
        assert "network_device:core-sw" in warning
        # The remediation must name BOTH fields: instance alone leaves the
        # entry source-less (wrong credentials in multi-source projects, and
        # a no-op repeat when the target host equals the slug).
        assert "instance to '10.0.0.1'" in warning
        assert "source to 'snmp-test'" in warning

    def test_following_the_full_remediation_silences_the_warning(self, snmp_env):
        # An entry with instance AND source set per the warning's advice is
        # source-owned: no artifact warning fires -- even in the degenerate
        # case where the configured target host equals the slug (an
        # instance-only edit would have warned forever there).
        _env, project, configure, fake_walks = snmp_env
        configure(targets=[{"host": "core-sw"}])  # target host == slug
        project.node_type_dir("network_device").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("network_device", "core-sw"),
            Node(
                id="network_device:core-sw",
                slug="core-sw",
                type="network_device",
                name="core-sw",
                source_id="snmp:snmp-test:core-sw",
                source="snmp",
                managed_by="snmp-test",
                observability=[{"type": "snmp", "instance": "core-sw", "source": "snmp-test"}],
            ),
        )
        fake_walks({"core-sw": target(sys=sys_rows("core-sw"))})

        result = SNMPSource().sync("testproject", "snmp-test")

        assert result.status is SyncStatus.SUCCESS
        assert not any("pre-0.4.0 artifact" in w for w in result.warnings)
        node = read_model(project.node_file("network_device", "core-sw"), Node)
        obs = [o for o in node.observability if o.type == "snmp"]
        assert [(o.instance, o.source) for o in obs] == [("core-sw", "snmp-test")]

    def test_artifact_with_operator_fields_preserved(self, snmp_env):
        # An entry that STARTED as the artifact but was touched by an operator
        # (notes added) is manual configuration -- exact-shape matching must
        # not delete it, and nothing is added beside it.
        _env, project, configure, fake_walks = snmp_env
        configure()
        project.node_type_dir("network_device").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("network_device", "core-sw"),
            Node(
                id="network_device:core-sw",
                slug="core-sw",
                type="network_device",
                name="core-sw",
                source_id="snmp:snmp-test:10.0.0.1",
                source="snmp",
                managed_by="snmp-test",
                observability=[
                    {"type": "snmp", "instance": "core-sw", "notes": "keep: reachable via mgmt vrf only"}
                ],
            ),
        )
        fake_walks({"10.0.0.1": target(sys=sys_rows("core-sw"))})

        SNMPSource().sync("testproject", "snmp-test")

        node = read_model(project.node_file("network_device", "core-sw"), Node)
        obs = [o for o in node.observability if o.type == "snmp"]
        assert [o.instance for o in obs] == ["core-sw"]  # untouched
        assert obs[0].notes == "keep: reachable via mgmt vrf only"

    def test_legacy_artifact_warned_across_relocation(self, snmp_env):
        # The artifact recorded the slug at write time. When the same sync
        # relocates the node (sysName changed), detection must key on the OLD
        # (persisted) slug -- and still only warn, never delete.
        _env, project, configure, fake_walks = snmp_env
        configure()
        fake_walks({"10.0.0.1": target(sys=sys_rows("sw-a"))})
        plugin = SNMPSource()
        plugin.sync("testproject", "snmp-test")

        # Rewrite the node into the exact legacy on-disk state: the artifact
        # entry instead of the modern host+source one.
        node = read_model(project.node_file("network_device", "sw-a"), Node)
        write_model(
            project.node_file("network_device", "sw-a"),
            node.model_copy(update={"observability": [Observability(type="snmp", instance="sw-a")]}),
        )

        fake_walks({"10.0.0.1": target(sys=sys_rows("sw-b"))})  # sysName changed
        result = plugin.sync("testproject", "snmp-test")

        assert result.status is SyncStatus.SUCCESS
        node = read_model(project.node_file("network_device", "sw-b"), Node)
        obs = [o for o in node.observability if o.type == "snmp"]
        assert [(o.instance, o.source) for o in obs] == [("sw-a", None)]  # untouched
        assert any("pre-0.4.0 artifact" in w for w in result.warnings)

    def test_manual_exact_shape_entry_survives_two_syncs_after_adoption(self, snmp_env):
        # Codex stop-gate lifecycle: adoption of a manual node sets
        # managed_by, so on the SECOND sync an ownership-gated deletion would
        # misclassify the operator's own bare entry as the artifact. It must
        # survive both syncs byte-identically.
        _env, project, configure, fake_walks = snmp_env
        configure()
        project.node_type_dir("network_device").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("network_device", "core-sw"),
            Node(
                id="network_device:core-sw",
                slug="core-sw",
                type="network_device",
                name="core-sw",
                observability=[{"type": "snmp", "instance": "core-sw"}],  # manual, exact shape
            ),
        )
        fake_walks({"10.0.0.1": target(sys=sys_rows("core-sw"))})
        plugin = SNMPSource()

        plugin.sync("testproject", "snmp-test")  # sync 1: adoption sets managed_by
        node = read_model(project.node_file("network_device", "core-sw"), Node)
        assert node.managed_by == "snmp-test"
        assert [(o.instance, o.source) for o in node.observability] == [("core-sw", None)]

        result = plugin.sync("testproject", "snmp-test")  # sync 2: entry now "looks owned"

        node = read_model(project.node_file("network_device", "core-sw"), Node)
        assert [(o.instance, o.source) for o in node.observability] == [("core-sw", None)]
        # The migration hint fires (shape is indistinguishable), but only as a
        # warning -- the operator decides.
        assert any("pre-0.4.0 artifact" in w for w in result.warnings)

    def test_slug_pointing_entry_on_manual_node_preserved(self, snmp_env):
        # Same entry shape, but the node was NOT owned by this source before
        # the sync -- a genuinely manual entry that happens to use the slug
        # must survive adoption untouched (and nothing is added beside it).
        _env, project, configure, fake_walks = snmp_env
        configure()
        project.node_type_dir("network_device").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("network_device", "core-sw"),
            Node(
                id="network_device:core-sw",
                slug="core-sw",
                type="network_device",
                name="core-sw",
                observability=[{"type": "snmp", "instance": "core-sw"}],
            ),
        )
        fake_walks({"10.0.0.1": target(sys=sys_rows("core-sw"))})

        SNMPSource().sync("testproject", "snmp-test")

        node = read_model(project.node_file("network_device", "core-sw"), Node)
        obs = [o for o in node.observability if o.type == "snmp"]
        assert [o.instance for o in obs] == ["core-sw"]
        assert obs[0].source is None

    def test_slug_pointing_entry_on_foreign_owned_node_never_warned(self):
        # Pure-function check: a node owned by a DIFFERENT source never
        # triggers the migration warning (the artifact hypothesis requires
        # this source's own prior ownership).
        from infracontext.sources.snmp import _warn_legacy_slug_observability

        existing = Node(
            id="network_device:core-sw",
            slug="core-sw",
            type="network_device",
            name="core-sw",
            managed_by="snmp-other",
            observability=[{"type": "snmp", "instance": "core-sw"}],
        )
        fresh = Observability(type="snmp", instance="10.0.0.1", source="snmp-test")
        warnings: list[str] = []

        _warn_legacy_slug_observability(existing, existing, fresh, warnings)

        assert warnings == []

    def test_operator_touched_artifact_never_warned(self):
        # An entry with any operator-set field is manual configuration -- no
        # migration nag for it.
        from infracontext.sources.snmp import _warn_legacy_slug_observability

        existing = Node(
            id="network_device:core-sw",
            slug="core-sw",
            type="network_device",
            name="core-sw",
            managed_by="snmp-test",
            observability=[{"type": "snmp", "instance": "core-sw", "notes": "mgmt vrf"}],
        )
        fresh = Observability(type="snmp", instance="10.0.0.1", source="snmp-test")
        warnings: list[str] = []

        _warn_legacy_slug_observability(existing, existing, fresh, warnings)

        assert warnings == []

    def test_manual_snmp_observability_left_alone(self, snmp_env):
        # No source field -> manual entry: never touched, nothing added.
        _env, project, configure, fake_walks = snmp_env
        configure()
        project.node_type_dir("network_device").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("network_device", "core-sw"),
            Node(
                id="network_device:core-sw",
                slug="core-sw",
                type="network_device",
                name="core-sw",
                observability=[{"type": "snmp", "instance": "mgmt.core-sw.example.com"}],
            ),
        )
        fake_walks({"10.0.0.1": target(sys=sys_rows("core-sw"))})

        SNMPSource().sync("testproject", "snmp-test")

        node = read_model(project.node_file("network_device", "core-sw"), Node)
        obs = [o for o in node.observability if o.type == "snmp"]
        assert [o.instance for o in obs] == ["mgmt.core-sw.example.com"]
        assert obs[0].source is None

    def test_overlap_warning_on_duplicate_ip(self, snmp_env):
        _env, project, configure, fake_walks = snmp_env
        configure()
        project.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("vm", "existing"),
            Node(id="vm:existing", slug="existing", type=NodeType.VM, name="existing", ip_addresses=["10.0.0.1"]),
        )
        fake_walks({"10.0.0.1": target(sys=sys_rows("core-sw"))})

        result = SNMPSource().sync("testproject", "snmp-test")

        assert any("overlaps vm:existing" in w for w in result.warnings)


class TestTestConnection:
    async def test_probe_returns_sysname(self, snmp_env):
        _env, _project, configure, fake_walks = snmp_env
        configure()
        fake_walks({"10.0.0.1": target(sys=sys_rows("core-sw-01"))})

        ok, message = await SNMPSource().test_connection(read_yaml(_project.source_file("snmp-test")))

        assert ok
        assert "core-sw-01" in message

    async def test_probe_failure_reports_error(self, snmp_env):
        _env, _project, configure, fake_walks = snmp_env
        configure()
        fake_walks({}, error_hosts=["10.0.0.1"])

        ok, message = await SNMPSource().test_connection(read_yaml(_project.source_file("snmp-test")))

        assert not ok
        assert "timeout" in message

    async def test_invalid_config_reported(self):
        ok, message = await SNMPSource().test_connection({"name": "x", "targets": []})
        assert not ok
        assert "targets" in message
