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

    def test_components_must_be_bool(self):
        assert any("components" in e for e in NetBoxSource().validate_config({"url": BASE, "components": "yes"}))
        assert NetBoxSource().validate_config({"url": BASE, "components": True}) == []

    def test_max_components_must_be_positive_int(self):
        for bad in (0, "x", True):
            assert any(
                "max_components" in e
                for e in NetBoxSource().validate_config({"url": BASE, "max_components": bad})
            )
        assert NetBoxSource().validate_config({"url": BASE, "max_components": 25000}) == []


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


class TestModelHints:
    def test_generic_role_pdu_model_maps_to_pdu(self):
        from infracontext.models.node import NodeType
        from infracontext.sources.netbox import _model_type_hint

        assert _model_type_hint("8x Schuko 1U PDU") is NodeType.PDU
        assert _model_type_hint("Smart-UPS 3000") is NodeType.UPS
        assert _model_type_hint("12-port Copper Patch Panel Half Depth") is NodeType.NETWORK_DEVICE
        assert _model_type_hint("Blanking Panel 1U") is None      # not typed — excludable
        assert _model_type_hint("ProLiant DL380 Gen10") is None   # servers unaffected
        assert _model_type_hint("Backups Appliance") is None      # token match, not substring


class TestSlugMap:
    def test_slug_map_validation(self):
        from infracontext.sources.netbox import NetBoxSource

        src = NetBoxSource()
        base = {"url": "https://nb.example.com", "credential": "netbox:x"}
        assert src.validate_config({**base, "slug_map": {"Dev A": "pve-dev-a"}}) == []
        errors = src.validate_config({**base, "slug_map": {"Dev A": "Not A Slug!"}})
        assert any("slug_map" in e for e in errors)

    def test_slug_map_pins_device_onto_existing_slug(self):
        from infracontext.sources.netbox import NetBoxSource

        src = NetBoxSource()
        src._slug_map = {"Bergfex App-A": "pve-app-a"}
        device = {
            "id": 42,
            "name": "Bergfex App-A",
            "role": {"slug": "hypervisor"},
            "device_type": {"model": "DL380", "manufacturer": {"name": "HPE"}},
        }
        node = src._build_device_node(device, "nb", {"hypervisor": "physical_host"})
        assert node.slug == "pve-app-a"
        assert node.id == "physical_host:pve-app-a"
        assert node.name == "Bergfex App-A"

    def test_unmapped_device_keeps_derived_slug(self):
        from infracontext.sources.netbox import NetBoxSource

        src = NetBoxSource()
        src._slug_map = {}
        device = {"id": 7, "name": "Core SW 1", "role": {"slug": "core-switch"},
                  "device_type": {"model": "X"}}
        node = src._build_device_node(device, "nb", {})
        assert node.slug == "core-sw-1"


class TestAdoptionMerge:
    def test_adopting_a_manual_node_preserves_enrichment(self, tmp_project, monkeypatch_environment, monkeypatch):
        """slug_map adoption must merge, not replace: other collectors'
        attributes, the node's IPs, and its established name survive."""
        from infracontext.models.node import Node
        from infracontext.sources.netbox import NetBoxClient, NetBoxSource
        from infracontext.storage import read_model, write_model, write_yaml

        tmp_project.node_type_dir("physical_host").mkdir(parents=True, exist_ok=True)
        write_model(
            tmp_project.node_file("physical_host", "pve-app-a"),
            Node(id="physical_host:pve-app-a", slug="pve-app-a", type="physical_host",
                 name="PVE-APP-A", ssh_alias="b.proxmox-app-a",
                 ip_addresses=["192.168.20.31"],
                 attributes={"pve_version": "9.2", "dmi_serial": "CZJ123"}),
        )
        write_yaml(tmp_project.source_file("nb"), {
            "version": "2.0", "name": "nb", "type": "netbox", "status": "configured",
            "url": "https://nb.example.com", "token": "tok-inline",
            "slug_map": {"Bergfex App-A": "pve-app-a"},
        })
        def fake_get_all(self, path, *, max_items=None):
            if "sites" in path or "racks" in path:
                return [], None
            return [{"id": 42, "name": "Bergfex App-A", "role": {"slug": "hypervisor"},
                     "device_type": {"model": "DL380 Gen10 Plus",
                                     "manufacturer": {"name": "HPE"}},
                     "serial": "CZ456"}], None
        monkeypatch.setattr(NetBoxClient, "get_all", fake_get_all)

        result = NetBoxSource().sync("testproject", "nb")

        assert result.status.value == "success"
        node = read_model(tmp_project.node_file("physical_host", "pve-app-a"), Node)
        assert node.name == "PVE-APP-A"                       # established name kept
        assert node.ip_addresses == ["192.168.20.31"]          # IPs survive
        assert node.attributes["pve_version"] == "9.2"        # foreign enrichment kept
        assert node.attributes["dmi_serial"] == "CZJ123"
        assert node.attributes["hardware"]["model"] == "DL380 Gen10 Plus"  # netbox adds
        assert node.attributes["netbox_name"] == "Bergfex App-A"
        assert (node.source_id or "").startswith("netbox:")   # binding adopted
        assert node.ssh_alias == "b.proxmox-app-a"

    def test_second_sync_is_idempotent_and_nondestructive(self, tmp_project, monkeypatch_environment, monkeypatch):
        """The preservation rule must hold on EVERY sync, not only the
        adopting one — a flag keyed on prior ownership flips after adoption
        and wiped nodes on the second run (found against live data)."""
        from infracontext.models.node import Node
        from infracontext.sources.netbox import NetBoxClient, NetBoxSource
        from infracontext.storage import read_model, write_model, write_yaml

        tmp_project.node_type_dir("physical_host").mkdir(parents=True, exist_ok=True)
        write_model(
            tmp_project.node_file("physical_host", "pve-app-a"),
            Node(id="physical_host:pve-app-a", slug="pve-app-a", type="physical_host",
                 name="PVE-APP-A", ip_addresses=["192.168.20.31"],
                 attributes={"pve_version": "9.2"}),
        )
        write_yaml(tmp_project.source_file("nb"), {
            "version": "2.0", "name": "nb", "type": "netbox", "status": "configured",
            "url": "https://nb.example.com", "token": "tok-inline",
            "slug_map": {"Bergfex App-A": "pve-app-a"},
        })
        def fake_get_all(self, path, *, max_items=None):
            if "sites" in path or "racks" in path:
                return [], None
            return [{"id": 42, "name": "Bergfex App-A", "role": {"slug": "hypervisor"},
                     "device_type": {"model": "DL380", "manufacturer": {"name": "HPE"}}}], None
        monkeypatch.setattr(NetBoxClient, "get_all", fake_get_all)

        plugin = NetBoxSource()
        plugin.sync("testproject", "nb")
        second = plugin.sync("testproject", "nb")

        assert second.nodes_updated == 0
        assert second.nodes_unchanged >= 1
        node = read_model(tmp_project.node_file("physical_host", "pve-app-a"), Node)
        assert node.name == "PVE-APP-A"
        assert node.ip_addresses == ["192.168.20.31"]
        assert node.attributes["pve_version"] == "9.2"


# ── device components ──────────────────────────────────────────────


def _component_routes(devices=(), *, interfaces=(), inventory=(), modules=(), extra=None):
    """Device routes plus every component collection the plugin walks.

    Collections not exercised by a test still need a route: the plugin fetches
    all of them, and an unmapped URL would 404 into a skip-warning.
    """
    routes = _routes(devices=devices)
    routes[f"{BASE}/api/dcim/interfaces/"] = _page(interfaces)
    routes[f"{BASE}/api/dcim/inventory-items/"] = _page(inventory)
    routes[f"{BASE}/api/dcim/modules/"] = _page(modules)
    for path in ("console-ports", "front-ports", "rear-ports", "power-ports", "power-outlets"):
        routes[f"{BASE}/api/dcim/{path}/"] = _page([])
    routes.update(extra or {})
    return routes


DEV_REF = {"id": 100, "name": "server-01"}

IFACE_MGMT = {
    "id": 1, "device": DEV_REF, "name": "iLO",
    "type": {"value": "1000base-t", "label": "1000BASE-T (1GE)"},
    "enabled": True, "mgmt_only": True, "mac_address": None, "mtu": None, "description": "",
}
IFACE_DATA = {
    "id": 2, "device": DEV_REF, "name": "eth/OCP3/a",
    "type": {"value": "40gbase-x-qsfpp", "label": "QSFP+ (40GE)"},
    "enabled": True, "mgmt_only": False, "mac_address": "AA:BB:CC:DD:EE:FF",
    "mtu": 9000, "description": "uplink", "lag": {"id": 9, "name": "bond10"},
}
INV_DIMM = {
    "id": 3, "device": DEV_REF, "name": "DIMM: PROC 1 DIMM 1",
    "role": {"id": 1, "name": "DIMM"}, "manufacturer": None, "part_id": "", "serial": "",
    "description": "64GB LRDIMM DDR4 @2666MHz [GoodInUse]", "discovered": False,
}
INV_DISK = {
    "id": 4, "device": DEV_REF, "name": "Disk 1: Micron_7450",
    "role": {"id": 2, "name": "Disk"}, "serial": "23324518E404", "description": "NVMe",
}
MODULE_PSU = {
    "id": 5, "device": DEV_REF, "module_bay": {"id": 7, "name": "PSU1"},
    "module_type": {"id": 8, "model": "720479-B21", "manufacturer": {"name": "HPE"}},
    "serial": "PSU-SN-1", "status": {"value": "active", "label": "Active"},
}


class TestComponents:
    def test_disabled_by_default_no_component_requests(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        configure()
        p = plugin(_component_routes(devices=[_device(100, "server-01", "server")]))

        result = p.sync("testproject", "nb-test")

        assert result.status is SyncStatus.SUCCESS
        assert not any("interfaces" in url for url in p._session.calls)
        node = read_model(project.node_file("physical_host", "server-01"), Node)
        assert "netbox_components" not in node.attributes

    def test_components_grouped_onto_their_device(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        configure(components=True)
        routes = _component_routes(
            devices=[_device(100, "server-01", "server")],
            interfaces=[IFACE_MGMT, IFACE_DATA],
            inventory=[INV_DIMM, INV_DISK],
            modules=[MODULE_PSU],
        )

        result = plugin(routes).sync("testproject", "nb-test")

        assert result.status is SyncStatus.SUCCESS
        assert "3 component(s)" not in result.message  # 5 rows, not a per-collection count
        assert "5 component(s)" in result.message
        components = read_model(project.node_file("physical_host", "server-01"), Node).attributes[
            "netbox_components"
        ]
        assert components["interfaces"] == [
            {"name": "iLO", "type": "1000BASE-T (1GE)", "enabled": True, "mgmt_only": True},
            {
                "name": "eth/OCP3/a", "type": "QSFP+ (40GE)", "enabled": True,
                "mac_address": "AA:BB:CC:DD:EE:FF", "mtu": 9000, "lag": "bond10",
                "description": "uplink",
            },
        ]
        assert components["inventory_items"][0] == {
            "name": "DIMM: PROC 1 DIMM 1", "role": "DIMM",
            "description": "64GB LRDIMM DDR4 @2666MHz [GoodInUse]",
        }
        assert components["inventory_items"][1]["serial"] == "23324518E404"
        assert components["modules"] == [
            {"bay": "PSU1", "type": "720479-B21", "manufacturer": "HPE",
             "serial": "PSU-SN-1", "status": "active"},
        ]
        assert "console_ports" not in components  # empty collections stay absent

    def test_components_of_other_devices_do_not_leak(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        configure(components=True)
        other = {**IFACE_DATA, "id": 9, "device": {"id": 200, "name": "server-02"}, "name": "eth9"}
        routes = _component_routes(
            devices=[_device(100, "server-01", "server"), _device(200, "server-02", "server")],
            interfaces=[IFACE_MGMT, other],
        )

        plugin(routes).sync("testproject", "nb-test")

        one = read_model(project.node_file("physical_host", "server-01"), Node)
        two = read_model(project.node_file("physical_host", "server-02"), Node)
        assert [i["name"] for i in one.attributes["netbox_components"]["interfaces"]] == ["iLO"]
        assert [i["name"] for i in two.attributes["netbox_components"]["interfaces"]] == ["eth9"]

    def test_orderless_rows_keep_netbox_order(self, netbox_env):
        """Natural port order must survive: sorting would put ge-0/0/10 second."""
        _env, project, configure, plugin = netbox_env
        configure(components=True)
        ports = [
            {**IFACE_DATA, "id": 100 + n, "name": f"ge-0/0/{n}", "lag": None, "mac_address": None}
            for n in (1, 2, 10, 11)
        ]
        plugin(_component_routes(devices=[_device(100, "server-01", "server")], interfaces=ports)).sync(
            "testproject", "nb-test"
        )

        node = read_model(project.node_file("physical_host", "server-01"), Node)
        assert [i["name"] for i in node.attributes["netbox_components"]["interfaces"]] == [
            "ge-0/0/1", "ge-0/0/2", "ge-0/0/10", "ge-0/0/11",
        ]

    def test_forbidden_collection_warns_but_sync_succeeds(self, netbox_env):
        """A 403 on one component endpoint must not fail the DCIM sync."""
        _env, project, configure, plugin = netbox_env
        configure(components=True)
        routes = _component_routes(
            devices=[_device(100, "server-01", "server")], interfaces=[IFACE_MGMT]
        )
        routes[f"{BASE}/api/dcim/inventory-items/"] = _FakeResp(403, text="forbidden")

        result = plugin(routes).sync("testproject", "nb-test")

        assert result.status is SyncStatus.SUCCESS
        assert any("could not read inventory_items" in w for w in result.warnings)
        node = read_model(project.node_file("physical_host", "server-01"), Node)
        assert node.attributes["netbox_components"]["interfaces"]  # the readable one landed

    def test_component_cap_warns(self, netbox_env):
        _env, _project, configure, plugin = netbox_env
        configure(components=True, max_components=1)
        routes = _component_routes(
            devices=[_device(100, "server-01", "server")], interfaces=[IFACE_MGMT, IFACE_DATA]
        )

        result = plugin(routes).sync("testproject", "nb-test")

        assert any("Component read incomplete for interfaces" in w for w in result.warnings)
        assert any("read 1 of 2" in w for w in result.warnings)

    def test_disabling_components_drops_them_on_resync(self, netbox_env):
        """netbox_components is an owned namespace: turning the flag off must
        clear it, not leave a stale snapshot behind forever."""
        _env, project, configure, plugin = netbox_env
        configure(components=True)
        routes = _component_routes(
            devices=[_device(100, "server-01", "server")], interfaces=[IFACE_MGMT]
        )
        plugin(routes).sync("testproject", "nb-test")
        assert "netbox_components" in read_model(
            project.node_file("physical_host", "server-01"), Node
        ).attributes

        configure(components=False)
        plugin(routes).sync("testproject", "nb-test")

        node = read_model(project.node_file("physical_host", "server-01"), Node)
        assert "netbox_components" not in node.attributes

    def test_components_survive_alongside_foreign_enrichment(self, netbox_env):
        """Components must not wipe another collector's attributes."""
        _env, project, configure, plugin = netbox_env
        configure(components=True, slug_map={"server-01": "srv-1"})
        _write_manual_node(project, "physical_host", "srv-1", attributes={"pve_version": "9.2"})
        routes = _component_routes(
            devices=[_device(100, "server-01", "server")], interfaces=[IFACE_MGMT]
        )

        plugin(routes).sync("testproject", "nb-test")

        node = read_model(project.node_file("physical_host", "srv-1"), Node)
        assert node.attributes["pve_version"] == "9.2"
        assert node.attributes["netbox_components"]["interfaces"][0]["name"] == "iLO"

    def test_second_sync_with_components_is_idempotent(self, netbox_env):
        _env, _project, configure, plugin = netbox_env
        configure(components=True)
        routes = _component_routes(
            devices=[_device(100, "server-01", "server")],
            interfaces=[IFACE_MGMT, IFACE_DATA],
            inventory=[INV_DIMM],
        )
        p = plugin(routes)
        p.sync("testproject", "nb-test")

        second = p.sync("testproject", "nb-test")

        assert second.nodes_updated == 0
        assert second.nodes_unchanged == 3  # site + rack + device


class TestDeviceFields:
    def test_platform_tenant_description_comments_land_in_netbox_namespace(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        configure()
        device = _device(100, "server-01", "server")
        device.update(
            {
                "platform": {"id": 7, "name": "Proxmox", "slug": "proxmox"},
                "tenant": {"id": 2, "name": "Bergfex", "slug": "bergfex"},
                "description": "App-Cluster Server A",
                "comments": "CPU: 2x Xeon Gold 6148 | RAM: 1536 GB",
            }
        )

        plugin(_routes(devices=[device])).sync("testproject", "nb-test")

        netbox = read_model(project.node_file("physical_host", "server-01"), Node).attributes["netbox"]
        assert netbox["platform"] == "Proxmox"
        assert netbox["tenant"] == "Bergfex"
        assert netbox["site"] == "DC1"
        assert netbox["description"] == "App-Cluster Server A"
        assert netbox["comments"].startswith("CPU: 2x Xeon Gold 6148")

    def test_absent_fields_are_omitted(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        configure()
        plugin(_routes(devices=[_device(100, "server-01", "server")])).sync("testproject", "nb-test")

        netbox = read_model(project.node_file("physical_host", "server-01"), Node).attributes["netbox"]
        assert set(netbox) == {"status", "role", "site"}

    def test_device_description_does_not_touch_node_description(self, netbox_env):
        """node.description is a manual field (CheckMK/SSH collectors write it);
        NetBox's own description belongs in the owned namespace only."""
        _env, project, configure, plugin = netbox_env
        configure(slug_map={"server-01": "srv-1"})
        _write_manual_node(project, "physical_host", "srv-1", description="Proxmox VE node in cluster")
        device = _device(100, "server-01", "server")
        device["description"] = "NetBox blurb"

        plugin(_routes(devices=[device])).sync("testproject", "nb-test")

        node = read_model(project.node_file("physical_host", "srv-1"), Node)
        assert node.description == "Proxmox VE node in cluster"
        assert node.attributes["netbox"]["description"] == "NetBox blurb"


class TestComponentFailureDoesNotErase:
    """A collection that could not be read in full must never blank the rows
    already on disk: netbox_components is replaced wholesale as an owned
    namespace, so one transient 403 on inventory-items would otherwise erase
    every DIMM, disk and card on the fleet while still reporting success."""

    def _seed(self, configure, plugin, project):
        configure(components=True)
        routes = _component_routes(
            devices=[_device(100, "server-01", "server")],
            interfaces=[IFACE_MGMT],
            inventory=[INV_DIMM, INV_DISK],
        )
        plugin(routes).sync("testproject", "nb-test")
        seeded = read_model(project.node_file("physical_host", "server-01"), Node)
        assert len(seeded.attributes["netbox_components"]["inventory_items"]) == 2
        return routes

    def test_failed_collection_keeps_previous_rows(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        routes = self._seed(configure, plugin, project)
        routes[f"{BASE}/api/dcim/inventory-items/"] = _FakeResp(403, text="forbidden")

        result = plugin(routes).sync("testproject", "nb-test")

        assert result.status is SyncStatus.SUCCESS
        assert any("kept the previously imported rows" in w for w in result.warnings)
        components = read_model(project.node_file("physical_host", "server-01"), Node).attributes[
            "netbox_components"
        ]
        assert [i["name"] for i in components["inventory_items"]] == [
            "DIMM: PROC 1 DIMM 1", "Disk 1: Micron_7450",
        ]
        assert components["interfaces"][0]["name"] == "iLO"  # readable one still refreshed

    def test_every_collection_failing_leaves_the_node_untouched(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        routes = self._seed(configure, plugin, project)
        for path in ("interfaces", "inventory-items", "modules", "console-ports",
                     "front-ports", "rear-ports", "power-ports", "power-outlets"):
            routes[f"{BASE}/api/dcim/{path}/"] = _FakeResp(503, text="upstream down")
        before = read_model(project.node_file("physical_host", "server-01"), Node)

        result = plugin(routes).sync("testproject", "nb-test")

        assert result.nodes_updated == 0
        assert read_model(project.node_file("physical_host", "server-01"), Node) == before

    def test_carried_rows_keep_canonical_collection_order(self, netbox_env):
        """A carried-over key must not reshuffle the YAML into a fake diff."""
        _env, project, configure, plugin = netbox_env
        routes = self._seed(configure, plugin, project)
        routes[f"{BASE}/api/dcim/interfaces/"] = _FakeResp(403, text="forbidden")

        plugin(routes).sync("testproject", "nb-test")

        components = read_model(project.node_file("physical_host", "server-01"), Node).attributes[
            "netbox_components"
        ]
        assert list(components) == ["interfaces", "inventory_items"]

    def test_successful_empty_collection_still_deletes(self, netbox_env):
        """The guard must not freeze data: a collection read fine that no
        longer lists the device means the components are genuinely gone."""
        _env, project, configure, plugin = netbox_env
        routes = self._seed(configure, plugin, project)
        routes[f"{BASE}/api/dcim/inventory-items/"] = _page([])

        plugin(routes).sync("testproject", "nb-test")

        components = read_model(project.node_file("physical_host", "server-01"), Node).attributes[
            "netbox_components"
        ]
        assert "inventory_items" not in components

    def test_capped_collection_keeps_previous_but_seeds_new_devices(self, netbox_env):
        """Truncation is incomplete knowledge too -- writing it would blank
        every device past the cut-off. A device with nothing to lose still
        gets whatever was read."""
        _env, project, configure, plugin = netbox_env
        self._seed(configure, plugin, project)

        configure(components=True, max_components=1)
        fresh_inv = {**INV_DISK, "id": 77, "device": {"id": 200, "name": "server-02"},
                     "name": "Disk 9: New"}
        routes = _component_routes(
            devices=[_device(100, "server-01", "server"), _device(200, "server-02", "server")],
            interfaces=[IFACE_MGMT],
            inventory=[fresh_inv, INV_DIMM],
        )
        result = plugin(routes).sync("testproject", "nb-test")

        assert any("Component read incomplete" in w for w in result.warnings)
        one = read_model(project.node_file("physical_host", "server-01"), Node)
        two = read_model(project.node_file("physical_host", "server-02"), Node)
        # server-01 had rows -> keeps its complete set, not the truncated view.
        assert [i["name"] for i in one.attributes["netbox_components"]["inventory_items"]] == [
            "DIMM: PROC 1 DIMM 1", "Disk 1: Micron_7450",
        ]
        # server-02 is new -> partial beats nothing.
        assert [i["name"] for i in two.attributes["netbox_components"]["inventory_items"]] == [
            "Disk 9: New",
        ]

    def test_failure_does_not_corrupt_the_shared_fetch_map(self, netbox_env):
        """Carry-over must copy, never mutate the per-device dict the fetch
        map hands out -- two devices share collection lists by reference."""
        _env, project, configure, plugin = netbox_env
        configure(components=True)
        routes = _component_routes(
            devices=[_device(100, "server-01", "server")], interfaces=[IFACE_MGMT]
        )
        p = plugin(routes)
        p.sync("testproject", "nb-test")

        routes[f"{BASE}/api/dcim/inventory-items/"] = _FakeResp(403, text="forbidden")
        p2 = plugin(routes)
        p2.sync("testproject", "nb-test")

        assert "inventory_items" not in p2._components.get(100, {})


class TestComponentCompletenessIsProven:
    """Truncation must be detected even when NetBox's count cannot prove it:
    get_all returns (rows, None) when the paginator omits 'count', and stops
    silently on a page whose body has no results list. Assuming completeness
    in either case writes a partial view and blanks every device past the cut."""

    def _seed(self, configure, plugin, project):
        configure(components=True)
        routes = _component_routes(
            devices=[_device(100, "server-01", "server")],
            interfaces=[IFACE_MGMT],
            inventory=[INV_DIMM, INV_DISK],
        )
        plugin(routes).sync("testproject", "nb-test")
        assert len(
            read_model(project.node_file("physical_host", "server-01"), Node)
            .attributes["netbox_components"]["inventory_items"]
        ) == 2
        return routes

    def test_missing_count_is_not_treated_as_complete(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        routes = self._seed(configure, plugin, project)
        # Paginator omitted "count": truncation would be undetectable.
        routes[f"{BASE}/api/dcim/inventory-items/"] = {"next": None, "results": [INV_DIMM]}

        result = plugin(routes).sync("testproject", "nb-test")

        assert any("omitted the total count" in w for w in result.warnings)
        components = read_model(project.node_file("physical_host", "server-01"), Node).attributes[
            "netbox_components"
        ]
        assert [i["name"] for i in components["inventory_items"]] == [
            "DIMM: PROC 1 DIMM 1", "Disk 1: Micron_7450",
        ]

    def test_page_breaking_mid_walk_keeps_previous_rows(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        routes = self._seed(configure, plugin, project)
        page2 = f"{BASE}/api/dcim/inventory-items/?page=2"
        routes[f"{BASE}/api/dcim/inventory-items/"] = _page([INV_DIMM], next_url=page2, count=2)
        routes[page2] = {"count": 2, "next": None, "results": "not-a-list"}

        result = plugin(routes).sync("testproject", "nb-test")

        assert any("walk ended early" in w for w in result.warnings)
        components = read_model(project.node_file("physical_host", "server-01"), Node).attributes[
            "netbox_components"
        ]
        assert len(components["inventory_items"]) == 2

    def test_cap_hit_without_count_still_flags(self, netbox_env):
        """Both blind spots at once: capped read, no count to compare against."""
        _env, project, configure, plugin = netbox_env
        self._seed(configure, plugin, project)
        configure(components=True, max_components=1)
        routes = _component_routes(
            devices=[_device(100, "server-01", "server")], interfaces=[IFACE_MGMT]
        )
        routes[f"{BASE}/api/dcim/inventory-items/"] = {"next": None, "results": [INV_DIMM]}

        result = plugin(routes).sync("testproject", "nb-test")

        assert any("Component read incomplete for inventory_items" in w for w in result.warnings)
        components = read_model(project.node_file("physical_host", "server-01"), Node).attributes[
            "netbox_components"
        ]
        assert len(components["inventory_items"]) == 2  # previous rows survived

    def test_exact_count_match_still_replaces(self, netbox_env):
        """The guard must not freeze data: a proven-complete read wins."""
        _env, project, configure, plugin = netbox_env
        routes = self._seed(configure, plugin, project)
        routes[f"{BASE}/api/dcim/inventory-items/"] = _page([INV_DISK])  # count == 1 == rows

        result = plugin(routes).sync("testproject", "nb-test")

        assert not any("incomplete" in w for w in result.warnings)
        components = read_model(project.node_file("physical_host", "server-01"), Node).attributes[
            "netbox_components"
        ]
        assert [i["name"] for i in components["inventory_items"]] == ["Disk 1: Micron_7450"]


class TestComponentRowsMustBeRetained:
    """Reading every row is not keeping every row. Rows dropped during
    grouping -- no usable device reference, or nothing left after building --
    leave the count check satisfied, so the survivors would be written over a
    node's full inventory with nothing to notice."""

    def _seed(self, configure, plugin, project):
        configure(components=True)
        routes = _component_routes(
            devices=[_device(100, "server-01", "server")],
            interfaces=[IFACE_MGMT],
            inventory=[INV_DIMM, INV_DISK],
        )
        plugin(routes).sync("testproject", "nb-test")
        return routes

    def test_unattributable_rows_do_not_erase(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        routes = self._seed(configure, plugin, project)
        # Device ref present but without an id -- count still matches len().
        orphan = {**INV_DISK, "id": 88, "device": {"url": "/api/dcim/devices/100/"}}
        routes[f"{BASE}/api/dcim/inventory-items/"] = _page([orphan])

        result = plugin(routes).sync("testproject", "nb-test")

        assert any("no usable device reference" in w for w in result.warnings)
        components = read_model(project.node_file("physical_host", "server-01"), Node).attributes[
            "netbox_components"
        ]
        assert [i["name"] for i in components["inventory_items"]] == [
            "DIMM: PROC 1 DIMM 1", "Disk 1: Micron_7450",
        ]

    def test_bare_device_pk_is_attributed_not_dropped(self, netbox_env):
        """A brief serialization hands back the pk itself; .get would raise."""
        _env, project, configure, plugin = netbox_env
        configure(components=True)
        brief = {**INV_DISK, "device": 100}
        routes = _component_routes(
            devices=[_device(100, "server-01", "server")], inventory=[brief]
        )

        result = plugin(routes).sync("testproject", "nb-test")

        assert result.status is SyncStatus.SUCCESS
        assert not any("incomplete" in w for w in result.warnings)
        components = read_model(project.node_file("physical_host", "server-01"), Node).attributes[
            "netbox_components"
        ]
        assert [i["name"] for i in components["inventory_items"]] == ["Disk 1: Micron_7450"]

    def test_rows_building_to_nothing_do_not_erase(self, netbox_env):
        _env, project, configure, plugin = netbox_env
        routes = self._seed(configure, plugin, project)
        empty = {"id": 89, "device": DEV_REF, "name": "", "role": None, "description": ""}
        routes[f"{BASE}/api/dcim/inventory-items/"] = _page([empty])

        result = plugin(routes).sync("testproject", "nb-test")

        assert any("Component read incomplete for inventory_items" in w for w in result.warnings)
        components = read_model(project.node_file("physical_host", "server-01"), Node).attributes[
            "netbox_components"
        ]
        assert len(components["inventory_items"]) == 2

    def test_partial_drop_still_protects_every_device(self, netbox_env):
        """One bad row poisons the collection, not just its own device: we
        cannot tell which devices the unattributable rows belonged to."""
        _env, project, configure, plugin = netbox_env
        routes = self._seed(configure, plugin, project)
        orphan = {**INV_DISK, "id": 88, "device": {}}
        routes[f"{BASE}/api/dcim/inventory-items/"] = _page([INV_DIMM, orphan])

        plugin(routes).sync("testproject", "nb-test")

        components = read_model(project.node_file("physical_host", "server-01"), Node).attributes[
            "netbox_components"
        ]
        assert len(components["inventory_items"]) == 2  # previous set, not the 1 survivor
