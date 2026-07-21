"""Tests for the NetBox DCIM (REST/JSON over HTTPS) source plugin.

The HTTP layer is faked with a session that maps full request URLs to canned
JSON responses (``_FakeSession``), mirroring the Redfish source tests.
``_page`` wraps a list of results in NetBox's paginated envelope; the module
fixtures describe one site, one rack, and a couple of devices in it.
"""

import asyncio
import os
import time

import pytest

from infracontext.models.node import Node
from infracontext.models.relationship import RelationshipFile
from infracontext.runs import load_run_records
from infracontext.sources.base import SyncStatus
from infracontext.sources.netbox import NetBoxSource
from infracontext.storage import read_model, read_yaml, write_model, write_yaml


def _utc_today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


# ── fake HTTP layer ────────────────────────────────────────────────


class _FakeResp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeSession:
    """Maps a full request URL to a canned response; unknown URL -> 404."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []
        self.headers_seen = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        self.headers_seen.append(kwargs.get("headers", {}))
        entry = self.routes.get(url)
        if entry is None:
            return _FakeResp(404, text="not found")
        if isinstance(entry, _FakeResp):
            return entry
        return _FakeResp(200, json_data=entry)


BASE = "https://netbox.example.com"


def _page(results, *, next_url=None, count=None):
    return {
        "count": count if count is not None else len(results),
        "next": next_url,
        "previous": None,
        "results": list(results),
    }


SITE_DC1 = {
    "id": 1,
    "name": "DC1",
    "slug": "dc1",
    "status": {"value": "active", "label": "Active"},
    "facility": "Equinix FR5",
}
SITE_REF = {"id": 1, "name": "DC1", "slug": "dc1"}

RACK_A1 = {
    "id": 10,
    "name": "Rack A1",
    "site": SITE_REF,
    "status": {"value": "active", "label": "Active"},
    "u_height": 42,
    "facility_id": "A1",
}
RACK_REF = {"id": 10, "name": "Rack A1", "slug": "rack-a1"}


def _device(
    pk,
    name,
    role_slug,
    *,
    rack=RACK_REF,
    site=SITE_REF,
    serial="SN-DEFAULT",
    asset_tag="",
    manufacturer="Dell",
    model="PowerEdge R660",
    u_height=1,
    position=1,
    face="front",
    primary_ip=None,
    status="active",
):
    return {
        "id": pk,
        "name": name,
        "device_type": {
            "id": 5,
            "manufacturer": {"id": 2, "name": manufacturer, "slug": manufacturer.lower()},
            "model": model,
            "slug": "device-type",
            "u_height": u_height,
        },
        "role": {"id": 3, "name": role_slug.replace("-", " ").title(), "slug": role_slug},
        "site": site,
        "rack": rack,
        "position": position,
        "face": {"value": face, "label": face.title()} if face else "",
        "serial": serial,
        "asset_tag": asset_tag,
        "primary_ip": {"id": 9, "family": {"value": 4}, "address": primary_ip} if primary_ip else None,
        "status": {"value": status, "label": status.title()},
    }


def _routes(sites=(SITE_DC1,), racks=(RACK_A1,), devices=()):
    return {
        f"{BASE}/api/dcim/sites/": _page(sites),
        f"{BASE}/api/dcim/racks/": _page(racks),
        f"{BASE}/api/dcim/devices/": _page(devices),
    }


@pytest.fixture()
def netbox_env(tmp_project, monkeypatch_environment):
    """Patched environment plus helpers to configure the source + fake HTTP."""

    def _configure(name="nb-test", **overrides):
        config = {
            "version": "2.0",
            "name": name,
            "type": "netbox",
            "status": "configured",
            "url": BASE,
            # Inline token keeps the keychain out of the test path.
            "token": "tok-abc123",
            "verify_ssl": False,
        }
        config.update(overrides)
        write_yaml(tmp_project.source_file(name), config)
        return name

    def _plugin(routes):
        plugin = NetBoxSource()
        plugin._session = _FakeSession(routes)
        return plugin

    return monkeypatch_environment, tmp_project, _configure, _plugin


def _write_manual_node(project, node_type, slug, **extra):
    project.node_type_dir(node_type).mkdir(parents=True, exist_ok=True)
    node = Node(id=f"{node_type}:{slug}", slug=slug, type=node_type, name=slug, **extra)
    write_model(project.node_file(node_type, slug), node)
    return node


# ── validate_config ────────────────────────────────────────────────


class TestValidateConfig:
    def test_requires_url(self):
        errors = NetBoxSource().validate_config({})
        assert any("url" in e for e in errors)

    def test_valid_config_passes(self):
        assert NetBoxSource().validate_config({"url": BASE}) == []

    def test_role_map_must_be_mapping(self):
        errors = NetBoxSource().validate_config({"url": BASE, "role_map": ["not", "a", "map"]})
        assert any("role_map" in e for e in errors)

    def test_role_map_rejects_unknown_node_type(self):
        errors = NetBoxSource().validate_config({"url": BASE, "role_map": {"core": "not_a_type"}})
        assert any("not a valid node type" in e for e in errors)

    def test_role_map_accepts_known_node_type(self):
        assert NetBoxSource().validate_config({"url": BASE, "role_map": {"core": "network_device"}}) == []

    def test_max_devices_must_be_positive_int(self):
        assert any("max_devices" in e for e in NetBoxSource().validate_config({"url": BASE, "max_devices": 0}))
        assert any("max_devices" in e for e in NetBoxSource().validate_config({"url": BASE, "max_devices": "x"}))

    def test_site_must_be_string(self):
        assert any("site" in e for e in NetBoxSource().validate_config({"url": BASE, "site": 123}))


# ── test_connection ────────────────────────────────────────────────


class TestConnection:
    def test_reaches_status_endpoint(self, netbox_env):
        _env, _project, _configure, plugin = netbox_env
        p = plugin({f"{BASE}/api/status/": {"netbox-version": "4.1.0"}})
        ok, msg = asyncio.run(p.test_connection({"url": BASE, "token": "x", "verify_ssl": False}))
        assert ok
        assert "4.1.0" in msg

    def test_reports_unreachable(self, netbox_env):
        _env, _project, _configure, plugin = netbox_env
        ok, msg = asyncio.run(plugin({}).test_connection({"url": BASE, "verify_ssl": False}))
        assert not ok
        assert "404" in msg

    def test_sends_token_header(self, netbox_env):
        _env, _project, _configure, plugin = netbox_env
        p = plugin({f"{BASE}/api/status/": {"netbox-version": "4.1.0"}})
        asyncio.run(p.test_connection({"url": BASE, "token": "sekret", "verify_ssl": False}))
        assert p._session.headers_seen[-1].get("Authorization") == "Token sekret"


# ── sync ───────────────────────────────────────────────────────────


class TestSync:
    def test_sites_racks_devices_create_nodes_and_edges(self, netbox_env):
        env, project, configure, plugin = netbox_env
        configure()
        host = _device(100, "server-01", "server", serial="SN-100", asset_tag="ASSET-100", primary_ip="10.0.0.5/24")
        routes = _routes(devices=[host])

        result = plugin(routes).sync("testproject", "nb-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.nodes_created == 3  # site + rack + device

        site = read_model(project.node_file("site", "dc1"), Node)
        assert site.type == "site"
        assert site.source_id == "netbox:nb-test:site:1"
        assert site.managed_by == "nb-test"
        assert site.first_seen == _utc_today()
        assert site.attributes["netbox"] == {"status": "active", "facility": "Equinix FR5"}

        rack = read_model(project.node_file("rack", "rack-a1"), Node)
        assert rack.type == "rack"
        assert rack.source_id == "netbox:nb-test:rack:10"
        assert rack.attributes["netbox"] == {"status": "active", "u_height": 42, "facility_id": "A1"}

        node = read_model(project.node_file("physical_host", "server-01"), Node)
        assert node.type == "physical_host"
        assert node.source_id == "netbox:nb-test:device:100"
        assert node.ip_addresses == ["10.0.0.5"]
        assert node.attributes["hardware"] == {
            "manufacturer": "Dell",
            "model": "PowerEdge R660",
            "serial": "SN-100",
            "asset_tag": "ASSET-100",
            "u_height": 1,
            "rack_position": 1,
            "rack_face": "front",
        }

        rels = read_model(project.relationships_yaml, RelationshipFile).relationships
        located = {(r.source, r.target) for r in rels if r.type == "located_in"}
        assert ("rack:rack-a1", "site:dc1") in located
        assert ("physical_host:server-01", "rack:rack-a1") in located
        assert all(r.managed_by == "nb-test" for r in rels if r.type == "located_in")

        records = load_run_records(env, project="testproject", source="nb-test")
        assert records
        assert "physical_host:server-01" in records[-1].created

    def test_role_mapping_defaults(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        configure()
        devices = [
            _device(1, "sw-01", "top-of-rack-switch"),
            _device(2, "fw-01", "firewall"),
            _device(3, "pdu-01", "rack-pdu"),
            _device(4, "ups-01", "ups"),
            _device(5, "srv-01", "server"),
            _device(6, "backups-01", "backups"),  # must NOT read as a UPS
        ]
        plugin(_routes(devices=devices)).sync("testproject", "nb-test")

        assert project.node_file("network_device", "sw-01").exists()
        assert project.node_file("network_device", "fw-01").exists()
        assert project.node_file("pdu", "pdu-01").exists()
        assert project.node_file("ups", "ups-01").exists()
        assert project.node_file("physical_host", "srv-01").exists()
        assert project.node_file("physical_host", "backups-01").exists()

    def test_role_map_override(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        configure(role_map={"server": "network_device"})
        plugin(_routes(devices=[_device(1, "appliance-01", "server")])).sync("testproject", "nb-test")

        assert project.node_file("network_device", "appliance-01").exists()
        assert not project.node_file("physical_host", "appliance-01").exists()

    def test_rename_via_pk_relocates_without_orphan(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        configure()
        plugin(_routes(devices=[_device(100, "server-01", "server")])).sync("testproject", "nb-test")
        assert project.node_file("physical_host", "server-01").exists()

        # Same NetBox PK, new name -> the device is relocated, not duplicated.
        result = plugin(_routes(devices=[_device(100, "server-01-renamed", "server")])).sync(
            "testproject", "nb-test"
        )

        assert result.status is SyncStatus.SUCCESS
        assert any("Renamed" in w for w in result.warnings)
        assert not project.node_file("physical_host", "server-01").exists()
        assert project.node_file("physical_host", "server-01-renamed").exists()

    def test_rename_rewrites_manual_references(self, netbox_env):
        # A rename deletes the old node file; manual relationships and chain
        # members referencing the old id must be repointed, not left dangling.
        _env, project, configure, plugin = netbox_env
        configure()
        plugin(_routes(devices=[_device(100, "server-01", "server")])).sync("testproject", "nb-test")

        _write_manual_node(project, "vm", "app-01")
        write_yaml(
            project.relationships_yaml,
            {
                "version": "2.0",
                "relationships": [
                    {"source": "vm:app-01", "target": "physical_host:server-01", "type": "runs_on"}
                ],
            },
        )
        write_yaml(
            project.chains_yaml,
            {
                "version": "2.0",
                "chains": [
                    {"name": "edge", "members": ["vm:app-01", "physical_host:server-01"]}
                ],
            },
        )

        result = plugin(_routes(devices=[_device(100, "server-01-renamed", "server")])).sync(
            "testproject", "nb-test"
        )

        assert result.status is SyncStatus.SUCCESS
        assert any("Rewrote" in w for w in result.warnings)
        rels = read_model(project.relationships_yaml, RelationshipFile).relationships
        manual = [r for r in rels if r.type == "runs_on"]
        assert manual[0].target == "physical_host:server-01-renamed"
        chains = read_yaml(project.chains_yaml)["chains"]
        assert chains[0]["members"][1] == "physical_host:server-01-renamed"
        # Nothing references the deleted id anymore.
        assert not any(
            "physical_host:server-01" in (r.source, r.target) for r in rels
        )

    def test_unracked_device_locates_in_site(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        configure()
        unracked = _device(200, "edge-01", "server", rack=None, position=None, face="")
        plugin(_routes(devices=[unracked])).sync("testproject", "nb-test")

        rels = read_model(project.relationships_yaml, RelationshipFile).relationships
        located = {(r.source, r.target) for r in rels if r.type == "located_in"}
        assert ("physical_host:edge-01", "site:dc1") in located
        node = read_model(project.node_file("physical_host", "edge-01"), Node)
        assert "rack_position" not in node.attributes.get("hardware", {})
        assert "rack_face" not in node.attributes.get("hardware", {})

    def test_pagination_is_followed(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        configure()
        page2 = f"{BASE}/api/dcim/devices/?limit=1&offset=1"
        routes = {
            f"{BASE}/api/dcim/sites/": _page([SITE_DC1]),
            f"{BASE}/api/dcim/racks/": _page([RACK_A1]),
            f"{BASE}/api/dcim/devices/": _page([_device(1, "srv-01", "server")], next_url=page2, count=2),
            page2: _page([_device(2, "srv-02", "server")], count=2),
        }
        p = plugin(routes)
        result = p.sync("testproject", "nb-test")

        assert result.status is SyncStatus.SUCCESS
        assert project.node_file("physical_host", "srv-01").exists()
        assert project.node_file("physical_host", "srv-02").exists()
        assert page2 in p._session.calls

    def test_device_cap_warns_and_stops_early(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        configure(max_devices=1)
        page2 = f"{BASE}/api/dcim/devices/?limit=1&offset=1"
        routes = {
            f"{BASE}/api/dcim/sites/": _page([]),
            f"{BASE}/api/dcim/racks/": _page([]),
            f"{BASE}/api/dcim/devices/": _page([_device(1, "srv-01", "server")], next_url=page2, count=2),
            page2: _page([_device(2, "srv-02", "server")], count=2),
        }
        p = plugin(routes)
        result = p.sync("testproject", "nb-test")

        assert result.status is SyncStatus.SUCCESS
        assert any("Device cap reached" in w and "of 2 devices" in w for w in result.warnings)
        assert project.node_file("physical_host", "srv-01").exists()
        assert not project.node_file("physical_host", "srv-02").exists()
        # Cap stops pagination before the next page is ever requested.
        assert page2 not in p._session.calls

    def test_site_filter_scopes_requests(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        configure(site="dc1")
        routes = {
            f"{BASE}/api/dcim/sites/?slug=dc1": _page([SITE_DC1]),
            f"{BASE}/api/dcim/racks/?site=dc1": _page([RACK_A1]),
            f"{BASE}/api/dcim/devices/?site=dc1": _page([_device(100, "server-01", "server")]),
        }
        p = plugin(routes)
        result = p.sync("testproject", "nb-test")

        assert result.status is SyncStatus.SUCCESS
        assert project.node_file("physical_host", "server-01").exists()
        assert f"{BASE}/api/dcim/devices/?site=dc1" in p._session.calls
        assert f"{BASE}/api/dcim/devices/" not in p._session.calls

    def test_dedup_warns_against_existing_manual_node(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        _write_manual_node(project, "vm", "legacy", ip_addresses=["10.0.0.5"])
        configure()
        host = _device(100, "server-01", "server", primary_ip="10.0.0.5/24")

        result = plugin(_routes(devices=[host])).sync("testproject", "nb-test")

        assert result.status is SyncStatus.SUCCESS
        assert any("overlaps vm:legacy" in w and "consolidate" in w for w in result.warnings)

    def test_foreign_source_id_collision_is_guarded(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        _write_manual_node(project, "physical_host", "server-01", source_id="snmp:other:server-01")
        configure()

        result = plugin(_routes(devices=[_device(100, "server-01", "server")])).sync("testproject", "nb-test")

        assert result.status is SyncStatus.PARTIAL
        assert any("refusing to overwrite" in e for e in result.errors)
        node = read_model(project.node_file("physical_host", "server-01"), Node)
        assert node.source_id == "snmp:other:server-01"

    def test_resync_is_unchanged_and_preserves_manual_ssh_alias(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        configure()
        routes = _routes(devices=[_device(100, "server-01", "server", primary_ip="10.0.0.5/24")])
        plugin(routes).sync("testproject", "nb-test")

        node_file = project.node_file("physical_host", "server-01")
        node = read_model(node_file, Node)
        write_model(node_file, node.model_copy(update={"ssh_alias": "server-ssh"}))

        result = plugin(routes).sync("testproject", "nb-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.nodes_created == 0
        assert result.nodes_updated == 0
        assert result.nodes_unchanged == 3  # site + rack + device all unchanged
        assert read_model(node_file, Node).ssh_alias == "server-ssh"

    def test_unchanged_resync_does_not_rewrite_relationships(self, netbox_env):
        """An unchanged located_in edge set must not rewrite relationships.yaml."""
        _env, project, configure, plugin = netbox_env
        configure()
        routes = _routes(devices=[_device(100, "server-01", "server")])
        plugin(routes).sync("testproject", "nb-test")

        os.utime(project.relationships_yaml, (100, 100))  # sentinel
        result = plugin(routes).sync("testproject", "nb-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.relationships_created == 0  # guard: edge set unchanged
        assert project.relationships_yaml.stat().st_mtime == 100  # no churn

    def test_empty_result_holds_guard(self, netbox_env):
        env, project, configure, plugin = netbox_env
        configure()

        result = plugin(_routes(sites=[], racks=[], devices=[])).sync("testproject", "nb-test")

        assert result.status is SyncStatus.SUCCESS
        assert "empty-sync guard" in result.message
        assert not project.node_file("site", "dc1").exists()
        records = load_run_records(env, project="testproject", source="nb-test")
        assert records and records[-1].status == "success"

    def test_pull_failure_is_failed_and_records_run(self, netbox_env):
        env, project, configure, plugin = netbox_env
        configure()

        result = plugin({}).sync("testproject", "nb-test")  # empty routes -> 404 on /sites/

        assert result.status is SyncStatus.FAILED
        assert any("404" in e for e in result.errors)
        assert not project.node_file("site", "dc1").exists()
        records = load_run_records(env, project="testproject", source="nb-test")
        assert records and records[-1].status == "failed"

    def test_invalid_config_is_failed(self, netbox_env):
        _env, _project, configure, plugin = netbox_env
        configure(url="")

        result = plugin({}).sync("testproject", "nb-test")

        assert result.status is SyncStatus.FAILED
        assert "url" in result.message

    def test_missing_source_is_failed(self, netbox_env):
        _env, _project, _configure, plugin = netbox_env

        result = plugin({}).sync("testproject", "does-not-exist")

        assert result.status is SyncStatus.FAILED
        assert "not found" in result.message
