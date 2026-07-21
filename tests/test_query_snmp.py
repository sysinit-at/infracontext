"""Tests for the SNMP query plugin (live device status) + its CLI wiring.

The single network seam ``SNMPQueryPlugin._walk`` is replaced with canned-walk
data throughout, so the plugin's logic runs without a real device.
"""

import json

import pytest
from typer.testing import CliRunner

from infracontext.cli.query import app
from infracontext.models.node import Node, NodeType
from infracontext.query.snmp import (
    IF_OPER_STATUS,
    IF_TABLE,
    IF_X_TABLE,
    SYS,
    SNMPQueryPlugin,
    _format_uptime,
)
from infracontext.sources.snmp import SNMPError

runner = CliRunner()


# ── canned-walk builders (produce (oid, value) pairs) ──────────────


def sys_rows(name, descr="a device", location="rack-1", uptime=12345):
    return [
        (f"{SYS}.1.0", descr),
        (f"{SYS}.3.0", uptime),
        (f"{SYS}.5.0", name),
        (f"{SYS}.6.0", location),
    ]


def oper_rows(*statuses):
    """statuses: ifOperStatus codes, one per interface index (1=up, 2=down)."""
    return [(f"{IF_OPER_STATUS}.{i}", code) for i, code in enumerate(statuses, start=1)]


def if_rows(entries):
    rows = []
    for e in entries:
        i = e["idx"]
        rows += [
            (f"{IF_TABLE}.2.{i}", e.get("descr", "")),
            (f"{IF_TABLE}.5.{i}", e.get("speed", 0)),
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
        if "alias" in e:
            rows.append((f"{IF_X_TABLE}.18.{i}", e["alias"]))
    return rows


def fake_walk(walks, error=None):
    """Build a ``_walk`` replacement returning canned rows keyed by base OID.

    ``error`` (an exception) makes every walk raise it, exercising error shaping.
    """

    def _walk(self, source_config, host, base_oid):  # noqa: ARG001
        if error is not None:
            raise error
        return walks.get(base_oid, [])

    return _walk


SOURCE = {"name": "snmp-test", "type": "snmp", "snmp_version": "2c"}


# ── plugin: status ─────────────────────────────────────────────────


class TestStatus:
    def test_reports_name_uptime_and_interface_counts(self, monkeypatch):
        monkeypatch.setattr(
            SNMPQueryPlugin,
            "_walk",
            fake_walk({
                SYS: sys_rows("core-sw-01", location="dc-a", uptime=8_640_000),  # 1 day
                IF_OPER_STATUS: oper_rows(1, 1, 2, 5),  # 2 up, 1 down, 1 other
            }),
        )

        result = SNMPQueryPlugin().query(SOURCE, "10.0.0.1", "status")

        assert result.success
        assert result.source_type == "snmp"
        assert result.data["sys_name"] == "core-sw-01"
        assert result.data["sys_uptime_ticks"] == 8_640_000
        assert result.data["sys_uptime"] == "1d"
        assert result.data["sys_location"] == "dc-a"
        assert result.data["interface_counts"] == {"total": 4, "up": 2, "down": 1, "other": 1}

    def test_status_is_default_query_type(self, monkeypatch):
        monkeypatch.setattr(
            SNMPQueryPlugin, "_walk", fake_walk({SYS: sys_rows("sw"), IF_OPER_STATUS: oper_rows(1)})
        )
        result = SNMPQueryPlugin().query(SOURCE, "10.0.0.1")
        assert result.success
        assert result.data["interface_counts"] == {"total": 1, "up": 1, "down": 0}

    def test_no_uptime_omits_human_field(self, monkeypatch):
        monkeypatch.setattr(
            SNMPQueryPlugin,
            "_walk",
            fake_walk({SYS: [(f"{SYS}.5.0", "sw")], IF_OPER_STATUS: []}),
        )
        result = SNMPQueryPlugin().query(SOURCE, "10.0.0.1", "status")
        assert result.success
        assert result.data["sys_uptime_ticks"] is None
        assert "sys_uptime" not in result.data
        assert result.data["interface_counts"] == {"total": 0, "up": 0, "down": 0}


# ── plugin: interfaces ─────────────────────────────────────────────


class TestInterfaces:
    def test_table_includes_name_status_speed_and_alias(self, monkeypatch):
        monkeypatch.setattr(
            SNMPQueryPlugin,
            "_walk",
            fake_walk({
                IF_TABLE: if_rows([
                    {"idx": "1", "descr": "GigabitEthernet0/1", "speed": 1_000_000_000, "admin": 1, "oper": 2},
                    {"idx": "2", "descr": "GigabitEthernet0/2", "admin": 1, "oper": 1},
                ]),
                IF_X_TABLE: ifx_rows([
                    {"idx": "1", "name": "Gi0/1", "alias": "uplink to core"},
                    {"idx": "2", "name": "Gi0/2", "high": 10000},
                ]),
            }),
        )

        result = SNMPQueryPlugin().query(SOURCE, "10.0.0.1", "interfaces")

        assert result.success
        assert result.data["total"] == 2
        first, second = result.data["interfaces"]
        assert first == {
            "name": "Gi0/1",  # ifName preferred over ifDescr
            "admin": "up",
            "oper": "down",
            "speed_mbps": 1000,  # from ifSpeed (bits/s)
            "alias": "uplink to core",
        }
        assert second["name"] == "Gi0/2"
        assert second["oper"] == "up"
        assert second["speed_mbps"] == 10000  # ifHighSpeed (Mbps) preferred
        assert "alias" not in second  # empty alias dropped

    def test_empty_table_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(SNMPQueryPlugin, "_walk", fake_walk({IF_TABLE: [], IF_X_TABLE: []}))
        result = SNMPQueryPlugin().query(SOURCE, "10.0.0.1", "interfaces")
        assert result.success
        assert result.data == {"interfaces": [], "total": 0}


# ── plugin: error shaping ───────────────────────────────────────────


class TestErrors:
    def test_missing_host_selector_errors_without_walking(self, monkeypatch):
        walked = {"called": False}

        def _walk(self, *_a, **_k):
            walked["called"] = True
            return []

        monkeypatch.setattr(SNMPQueryPlugin, "_walk", _walk)

        result = SNMPQueryPlugin().query(SOURCE, "", "status")

        assert not result.success
        assert walked["called"] is False
        assert "no 'snmp' observability" in result.error

    def test_unknown_query_type_errors(self):
        result = SNMPQueryPlugin().query(SOURCE, "10.0.0.1", "bogus")
        assert not result.success
        assert "Unknown query_type" in result.error
        assert "status" in result.error and "interfaces" in result.error

    def test_walk_snmp_error_degrades_to_one_line(self, monkeypatch):
        monkeypatch.setattr(
            SNMPQueryPlugin, "_walk", fake_walk({}, error=SNMPError("no route to host"))
        )
        result = SNMPQueryPlugin().query(SOURCE, "10.0.0.1", "status")
        assert not result.success
        assert result.error == "no route to host"
        assert "\n" not in result.error

    def test_puresnmp_timeout_is_shaped_into_clear_error(self, monkeypatch):
        """A real puresnmp Timeout out of ``_awalk`` becomes a clear one-line
        error naming the host — the full shaping path (``_walk`` wrap +
        ``query`` catch), exercised without a device."""
        from puresnmp.exc import Timeout

        async def _boom(self, source_config, host, base_oid):  # noqa: ARG001
            raise Timeout("no response received")

        monkeypatch.setattr(SNMPQueryPlugin, "_awalk", _boom)

        result = SNMPQueryPlugin().query(SOURCE, "10.0.0.42", "status")

        assert not result.success
        assert "timeout" in result.error.lower()
        assert "10.0.0.42" in result.error
        assert "\n" not in result.error


class TestFormatUptime:
    @pytest.mark.parametrize(
        ("ticks", "expected"),
        [
            (None, None),
            (-1, None),
            (0, "0m"),
            (12345, "2m"),  # 123s
            (8_640_000, "1d"),
            (8_640_000 + 100 * (2 * 3600 + 3 * 60), "1d 2h 3m"),
        ],
    )
    def test_format(self, ticks, expected):
        assert _format_uptime(ticks) == expected


# ── CLI: `ic query snmp` standalone command ─────────────────────────


def _patch_snmp_cli(monkeypatch, *, obs, source_config, walks=None, walk_error=None):
    """Wire the CLI helpers so a single snmp source/obs is resolved."""
    from infracontext.overrides import NodeOverrides

    node = Node(
        id="network_device:core-sw", slug="core-sw", type=NodeType.NETWORK_DEVICE, name="core-sw"
    )
    monkeypatch.setattr("infracontext.cli.query.require_project", lambda: "demo")
    monkeypatch.setattr("infracontext.cli.query.require_node", lambda _p, _n: node)
    monkeypatch.setattr(
        "infracontext.cli.query.get_node_observability",
        lambda _p, _n, obs_type, node=None: (obs if obs_type == "snmp" else None),
    )
    monkeypatch.setattr("infracontext.cli.query.get_node_ssh_target", lambda _p, _n, node=None: None)
    monkeypatch.setattr("infracontext.cli.query.get_node_overrides", lambda *_a, **_k: NodeOverrides())
    monkeypatch.setattr(
        "infracontext.cli.query.get_source_config",
        lambda _p, source_type, _name=None, sources=None: (source_config if source_type == "snmp" else None),
    )
    if walks is not None or walk_error is not None:
        monkeypatch.setattr(SNMPQueryPlugin, "_walk", fake_walk(walks or {}, error=walk_error))


class TestQuerySnmpCommand:
    def test_status_pretty(self, monkeypatch):
        _patch_snmp_cli(
            monkeypatch,
            obs={"type": "snmp", "instance": "10.0.0.1", "source": "snmp-test"},
            source_config=SOURCE,
            walks={SYS: sys_rows("core-sw-01", uptime=8_640_000), IF_OPER_STATUS: oper_rows(1, 2)},
        )

        result = runner.invoke(app, ["snmp", "network_device:core-sw"])

        assert result.exit_code == 0, result.output
        assert "core-sw-01" in result.output
        assert "1 up" in result.output
        assert "1 down" in result.output

    def test_interfaces_json(self, monkeypatch):
        _patch_snmp_cli(
            monkeypatch,
            obs={"type": "snmp", "instance": "10.0.0.1", "source": "snmp-test"},
            source_config=SOURCE,
            walks={
                IF_TABLE: if_rows([{"idx": "1", "descr": "Gi0/1", "oper": 1}]),
                IF_X_TABLE: ifx_rows([{"idx": "1", "name": "Gi0/1", "alias": "uplink"}]),
            },
        )

        result = runner.invoke(app, ["snmp", "network_device:core-sw", "-t", "interfaces", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["total"] == 1
        assert payload["interfaces"][0]["alias"] == "uplink"
        assert payload["interfaces"][0]["oper"] == "up"

    def test_no_source_configured_exits_with_hint(self, monkeypatch):
        _patch_snmp_cli(monkeypatch, obs=None, source_config=None)

        result = runner.invoke(app, ["snmp", "network_device:core-sw"])

        assert result.exit_code == 1
        assert "No SNMP source configured" in result.output

    def test_falls_back_to_node_host_when_no_obs_instance(self, monkeypatch):
        """With a source configured but no snmp observability instance, the
        standalone command derives the walk host from the node (slug here) and
        says so — unlike `ic query status`, which skips the device entirely."""
        _patch_snmp_cli(
            monkeypatch,
            obs=None,  # node declares no snmp observability instance
            source_config=SOURCE,
            walks={SYS: sys_rows("core-sw-01"), IF_OPER_STATUS: oper_rows(1)},
        )

        result = runner.invoke(app, ["snmp", "network_device:core-sw"])

        assert result.exit_code == 0, result.output
        assert "No SNMP instance in node, using: core-sw" in result.output
        assert "core-sw-01" in result.output

    def test_walk_failure_reports_error(self, monkeypatch):
        _patch_snmp_cli(
            monkeypatch,
            obs={"type": "snmp", "instance": "10.0.0.1", "source": "snmp-test"},
            source_config=SOURCE,
            walk_error=SNMPError("SNMP timeout after 2s querying 10.0.0.1"),
        )

        result = runner.invoke(app, ["snmp", "network_device:core-sw"])

        assert result.exit_code == 1
        assert "timeout" in result.output.lower()


def test_snmp_fallback_host_prefers_ip_then_domain_then_slug():
    from infracontext.cli.query import _snmp_fallback_host

    with_ip = Node(
        id="network_device:a", slug="a", type=NodeType.NETWORK_DEVICE, name="a",
        ip_addresses=["10.1.1.1"], domains=["d.example.com"],
    )
    with_domain = Node(
        id="network_device:b", slug="b", type=NodeType.NETWORK_DEVICE, name="b",
        domains=["d.example.com"],
    )
    bare = Node(id="network_device:c", slug="c", type=NodeType.NETWORK_DEVICE, name="c")

    assert _snmp_fallback_host(with_ip) == "10.1.1.1"
    assert _snmp_fallback_host(with_domain) == "d.example.com"
    assert _snmp_fallback_host(bare) == "c"


# ── CLI: `ic query status` integration (snmp section) ───────────────


def _patch_status_cli(monkeypatch, *, snmp_obs, snmp_config, walks):
    from infracontext.overrides import NodeOverrides

    node = Node(id="vm:web", slug="web", type=NodeType.VM, name="Web")
    monkeypatch.setattr("infracontext.cli.query.require_project", lambda: "demo")
    monkeypatch.setattr("infracontext.cli.query.require_node", lambda _p, _n: node)
    monkeypatch.setattr(
        "infracontext.cli.query.get_node_observability",
        lambda _p, _n, obs_type, node=None: (snmp_obs if obs_type == "snmp" else None),
    )
    monkeypatch.setattr("infracontext.cli.query.get_node_ssh_target", lambda _p, _n, node=None: None)
    monkeypatch.setattr("infracontext.cli.query.get_node_overrides", lambda *_a, **_k: NodeOverrides())
    monkeypatch.setattr(
        "infracontext.cli.query.get_source_config",
        lambda _p, source_type, _name=None, sources=None: (snmp_config if source_type == "snmp" else None),
    )
    monkeypatch.setattr(SNMPQueryPlugin, "_walk", fake_walk(walks))


class TestQueryStatusSnmpSection:
    def test_snmp_section_appears_when_node_has_snmp_observability(self, monkeypatch):
        _patch_status_cli(
            monkeypatch,
            snmp_obs={"type": "snmp", "instance": "10.0.0.1", "source": "snmp-test"},
            snmp_config=SOURCE,
            walks={SYS: sys_rows("core-sw-01", uptime=8_640_000), IF_OPER_STATUS: oper_rows(1, 1, 2)},
        )

        result = runner.invoke(app, ["status", "vm:web"])

        assert result.exit_code == 0, result.output
        assert "SNMP" in result.output
        assert "core-sw-01" in result.output
        assert "2 up" in result.output
        assert "1 down" in result.output

    def test_snmp_section_absent_without_observability(self, monkeypatch):
        _patch_status_cli(monkeypatch, snmp_obs=None, snmp_config=SOURCE, walks={})

        result = runner.invoke(app, ["status", "vm:web"])

        assert result.exit_code == 0, result.output
        # No snmp obs entry -> no SNMP section (guidance shown instead).
        assert "No monitoring sources configured" in result.output

    def test_snmp_walk_failure_degrades_inline_without_hiding_run(self, monkeypatch):
        _patch_status_cli(
            monkeypatch,
            snmp_obs={"type": "snmp", "instance": "10.0.0.1", "source": "snmp-test"},
            snmp_config=SOURCE,
            walks={},
        )
        monkeypatch.setattr(
            SNMPQueryPlugin, "_walk", fake_walk({}, error=SNMPError("SNMP timeout after 2s querying 10.0.0.1"))
        )

        result = runner.invoke(app, ["status", "vm:web"])

        assert result.exit_code == 0, result.output
        assert "SNMP" in result.output
        assert "timeout" in result.output.lower()
