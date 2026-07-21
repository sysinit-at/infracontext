"""Tests for the Redfish (REST/JSON over HTTPS) source plugin.

The HTTP layer is faked with a session that maps full request URLs to canned
JSON responses (``_FakeSession``), mirroring the style of the existing HTTP
query-plugin tests. ``endpoint_routes`` builds a standard single-system,
single-chassis, single-manager Redfish tree for one BMC base URL.
"""

import os
import time

import pytest

from infracontext.models.node import Node
from infracontext.models.relationship import RelationshipFile
from infracontext.runs import load_run_records
from infracontext.sources.base import SyncStatus
from infracontext.sources.redfish import RedfishSource
from infracontext.storage import read_model, write_model, write_yaml


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

    def get(self, url, **kwargs):
        self.calls.append(url)
        entry = self.routes.get(url)
        if entry is None:
            return _FakeResp(404, text="not found")
        if isinstance(entry, _FakeResp):
            return entry
        return _FakeResp(200, json_data=entry)


SYSTEM_A = {
    "Manufacturer": "Dell Inc.",
    "Model": "PowerEdge R660",
    "SerialNumber": "ABC123",
    "SKU": "SKU-9",
    "UUID": "4c4c4544-0042-1234",
    "BiosVersion": "1.2.3",
    "HostName": "web-01",
    "Status": {"Health": "OK", "State": "Enabled"},
}


def endpoint_routes(
    base,
    *,
    system=None,
    systems=None,
    firmware="7.10.30",
    legacy_power=None,
    subsystem_power=None,
    thermal=None,
    include_managers=True,
    include_chassis=True,
):
    """Build a Redfish resource tree (URL -> JSON) for one endpoint."""
    base = base.rstrip("/")

    def u(path):
        return f"{base}{path}"

    routes = {}
    root = {"RedfishVersion": "1.6.0", "Systems": {"@odata.id": "/redfish/v1/Systems"}}
    if include_chassis:
        root["Chassis"] = {"@odata.id": "/redfish/v1/Chassis"}
    if include_managers:
        root["Managers"] = {"@odata.id": "/redfish/v1/Managers"}
    routes[u("/redfish/v1/")] = root

    sys_list = systems if systems is not None else ([system] if system is not None else [])
    members = []
    for i, sysdef in enumerate(sys_list, 1):
        sid = f"/redfish/v1/Systems/{i}"
        members.append({"@odata.id": sid})
        routes[u(sid)] = {"Id": str(i), **sysdef}
    routes[u("/redfish/v1/Systems")] = {"Members": members}

    if include_managers:
        mgr = {"Id": "BMC"}
        if firmware is not None:
            mgr["FirmwareVersion"] = firmware
        routes[u("/redfish/v1/Managers")] = {"Members": [{"@odata.id": "/redfish/v1/Managers/BMC"}]}
        routes[u("/redfish/v1/Managers/BMC")] = mgr

    if include_chassis:
        chassis = {"Id": "1"}
        if legacy_power is not None:
            chassis["Power"] = {"@odata.id": "/redfish/v1/Chassis/1/Power"}
            routes[u("/redfish/v1/Chassis/1/Power")] = {
                "PowerControl": [{"PowerConsumedWatts": legacy_power}]
            }
        if subsystem_power is not None:
            chassis["PowerSubsystem"] = {"@odata.id": "/redfish/v1/Chassis/1/PowerSubsystem"}
            chassis["EnvironmentMetrics"] = {"@odata.id": "/redfish/v1/Chassis/1/EnvironmentMetrics"}
            routes[u("/redfish/v1/Chassis/1/PowerSubsystem")] = {"Id": "PowerSubsystem"}
            routes[u("/redfish/v1/Chassis/1/EnvironmentMetrics")] = {
                "PowerWatts": {"Reading": subsystem_power}
            }
        if thermal is not None:
            chassis["Thermal"] = {"@odata.id": "/redfish/v1/Chassis/1/Thermal"}
            routes[u("/redfish/v1/Chassis/1/Thermal")] = thermal
        routes[u("/redfish/v1/Chassis")] = {"Members": [{"@odata.id": "/redfish/v1/Chassis/1"}]}
        routes[u("/redfish/v1/Chassis/1")] = chassis

    return routes


BASE = "https://bmc-01.example.com"


@pytest.fixture()
def redfish_env(tmp_project, monkeypatch_environment):
    """Patched environment plus helpers to configure the source + fake HTTP."""

    def _configure(name="rf-test", endpoints=None, **overrides):
        config = {
            "version": "2.0",
            "name": name,
            "type": "redfish",
            "status": "configured",
            "endpoints": endpoints if endpoints is not None else [{"url": BASE}],
            # Inline creds keep the keychain out of the test path.
            "username": "admin",
            "password": "secret",
            "verify_ssl": False,
        }
        config.update(overrides)
        write_yaml(tmp_project.source_file(name), config)
        return name

    def _plugin(routes):
        plugin = RedfishSource()
        plugin._session = _FakeSession(routes)
        return plugin

    return monkeypatch_environment, tmp_project, _configure, _plugin


def _write_host(project, node_type, slug, serial, **extra):
    """Create a host node carrying attributes.hardware.serial."""
    project.node_type_dir(node_type).mkdir(parents=True, exist_ok=True)
    node = Node(
        id=f"{node_type}:{slug}",
        slug=slug,
        type=node_type,
        name=slug,
        attributes={"hardware": {"serial": serial}},
        **extra,
    )
    write_model(project.node_file(node_type, slug), node)
    return node


class TestValidateConfig:
    def test_requires_endpoints(self):
        errors = RedfishSource().validate_config({})
        assert any("endpoints" in e for e in errors)

    def test_endpoint_needs_url(self):
        errors = RedfishSource().validate_config({"endpoints": [{"name": "x"}]})
        assert any("url" in e for e in errors)

    def test_valid_config_passes(self):
        assert RedfishSource().validate_config({"endpoints": [{"url": "https://x"}]}) == []


class TestSync:
    def test_creates_bmc_node_with_hardware_attrs_and_run_record(self, redfish_env):
        env, project, configure, plugin = redfish_env
        configure(endpoints=[{"url": BASE, "name": "web-01-bmc"}])
        routes = endpoint_routes(BASE, system=SYSTEM_A, firmware="7.10.30", legacy_power=210)

        result = plugin(routes).sync("testproject", "rf-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.nodes_created == 1

        node = read_model(project.node_file("network_device", "web-01-bmc"), Node)
        assert node.type == "network_device"
        assert node.managed_by == "rf-test"
        assert node.source_id == "redfish:rf-test:bmc-01.example.com"
        assert node.domains == ["bmc-01.example.com"]
        assert node.first_seen == _utc_today()
        assert node.attributes["hardware"] == {
            "manufacturer": "Dell Inc.",
            "model": "PowerEdge R660",
            "serial": "ABC123",
            "sku": "SKU-9",
            "uuid": "4c4c4544-0042-1234",
        }
        assert node.attributes["redfish"] == {
            "bios_version": "1.2.3",
            "bmc_firmware": "7.10.30",
        }
        obs = [o for o in node.observability if o.type == "redfish"]
        assert obs and obs[0].instance == BASE and obs[0].source == "rf-test"

        records = load_run_records(env, project="testproject", source="rf-test")
        assert records and records[-1].created == ["network_device:web-01-bmc"]

    def test_slug_derived_from_url_host_when_no_name(self, redfish_env):
        _env, project, configure, plugin = redfish_env
        configure(endpoints=[{"url": BASE}])
        routes = endpoint_routes(BASE, system={**SYSTEM_A, "HostName": None})

        plugin(routes).sync("testproject", "rf-test")

        assert project.node_file("network_device", "bmc-01-example-com").exists()

    def test_live_power_not_persisted_in_node(self, redfish_env):
        """Live power draw is a query-time metric; the sync must not persist it
        (it fluctuates continuously and would churn the node on every resync)."""
        _env, project, configure, plugin = redfish_env
        configure(endpoints=[{"url": BASE, "name": "bmc-x"}])
        routes = endpoint_routes(BASE, system=SYSTEM_A, subsystem_power=355)

        plugin(routes).sync("testproject", "rf-test")

        node = read_model(project.node_file("network_device", "bmc-x"), Node)
        assert "power_watts_at_sync" not in node.attributes.get("redfish", {})

    def test_power_drift_does_not_rewrite_node_on_resync(self, redfish_env):
        """A changed BMC power reading alone must not rewrite the node file."""
        _env, project, configure, plugin = redfish_env
        configure(endpoints=[{"url": BASE, "name": "bmc-x"}])
        plugin(endpoint_routes(BASE, system=SYSTEM_A, legacy_power=210)).sync("testproject", "rf-test")

        node_file = project.node_file("network_device", "bmc-x")
        os.utime(node_file, (100, 100))  # sentinel: any rewrite bumps mtime

        result = plugin(endpoint_routes(BASE, system=SYSTEM_A, legacy_power=999)).sync(
            "testproject", "rf-test"
        )

        assert result.nodes_unchanged == 1
        assert result.nodes_updated == 0
        assert node_file.stat().st_mtime == 100  # power drift must not churn

    def test_unchanged_resync_does_not_rewrite_relationships(self, redfish_env):
        """An unchanged managed-edge set must not rewrite relationships.yaml."""
        _env, project, configure, plugin = redfish_env
        _write_host(project, "physical_host", "host-01", "ABC123")
        configure(endpoints=[{"url": BASE, "name": "bmc-x"}])
        routes = endpoint_routes(BASE, system=SYSTEM_A)
        plugin(routes).sync("testproject", "rf-test")

        os.utime(project.relationships_yaml, (100, 100))  # sentinel
        result = plugin(routes).sync("testproject", "rf-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.relationships_created == 0  # guard: edge set unchanged
        assert project.relationships_yaml.stat().st_mtime == 100  # no churn

    def test_serial_match_plans_manages_edge(self, redfish_env):
        _env, project, configure, plugin = redfish_env
        _write_host(project, "physical_host", "host-01", "ABC123")
        configure(endpoints=[{"url": BASE, "name": "bmc-x"}])
        routes = endpoint_routes(BASE, system=SYSTEM_A)

        result = plugin(routes).sync("testproject", "rf-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.relationships_created == 1
        rels = read_model(project.relationships_yaml, RelationshipFile).relationships
        edge = next(r for r in rels if r.type == "manages")
        assert edge.source == "network_device:bmc-x"
        assert edge.target == "physical_host:host-01"
        assert edge.managed_by == "rf-test"

    def test_serial_match_is_case_insensitive(self, redfish_env):
        _env, project, configure, plugin = redfish_env
        _write_host(project, "physical_host", "host-01", "abc123")
        configure(endpoints=[{"url": BASE, "name": "bmc-x"}])
        routes = endpoint_routes(BASE, system={**SYSTEM_A, "SerialNumber": "ABC123"})

        result = plugin(routes).sync("testproject", "rf-test")

        assert result.relationships_created == 1

    def test_ambiguous_serial_warns_and_plans_no_edge(self, redfish_env):
        _env, project, configure, plugin = redfish_env
        _write_host(project, "physical_host", "host-01", "ABC123")
        _write_host(project, "physical_host", "host-02", "ABC123")
        configure(endpoints=[{"url": BASE, "name": "bmc-x"}])
        routes = endpoint_routes(BASE, system=SYSTEM_A)

        result = plugin(routes).sync("testproject", "rf-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.relationships_created == 0
        assert any("matches 2 nodes" in w and "host-01" in w and "host-02" in w for w in result.warnings)
        rels = read_model(project.relationships_yaml, RelationshipFile)
        assert rels is None or not any(r.type == "manages" for r in rels.relationships)

    def test_no_serial_match_warns_and_plans_no_edge(self, redfish_env):
        _env, project, configure, plugin = redfish_env
        _write_host(project, "physical_host", "host-01", "DIFFERENT")
        configure(endpoints=[{"url": BASE, "name": "bmc-x"}])
        routes = endpoint_routes(BASE, system=SYSTEM_A)

        result = plugin(routes).sync("testproject", "rf-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.relationships_created == 0
        assert any("no host node matches serial" in w for w in result.warnings)

    def test_bmc_does_not_self_match_on_resync(self, redfish_env):
        """A re-sync must not link the BMC to itself (BMC and host share the
        serial, but the BMC is managed by this source and is excluded)."""
        _env, project, configure, plugin = redfish_env
        configure(endpoints=[{"url": BASE, "name": "bmc-x"}])
        routes = endpoint_routes(BASE, system=SYSTEM_A)
        plugin(routes).sync("testproject", "rf-test")

        result = plugin(routes).sync("testproject", "rf-test")

        assert result.relationships_created == 0
        assert any("no host node matches serial" in w for w in result.warnings)

    def test_resync_is_unchanged_and_preserves_manual_ssh_alias(self, redfish_env):
        _env, project, configure, plugin = redfish_env
        configure(endpoints=[{"url": BASE, "name": "bmc-x"}])
        routes = endpoint_routes(BASE, system=SYSTEM_A, legacy_power=210)
        plugin(routes).sync("testproject", "rf-test")

        node_file = project.node_file("network_device", "bmc-x")
        node = read_model(node_file, Node)
        write_model(node_file, node.model_copy(update={"ssh_alias": "bmc-ssh"}))

        result = plugin(routes).sync("testproject", "rf-test")

        assert result.status is SyncStatus.SUCCESS
        assert result.nodes_unchanged == 1
        assert result.nodes_updated == 0
        assert read_model(node_file, Node).ssh_alias == "bmc-ssh"

    def test_endpoint_rename_relocates_without_orphan(self, redfish_env):
        """Changing the endpoint 'name' moves the BMC to a new slug; the old
        file must be removed (source_id is the stable identity)."""
        _env, project, configure, plugin = redfish_env
        configure(endpoints=[{"url": BASE, "name": "old-name"}])
        routes = endpoint_routes(BASE, system=SYSTEM_A)
        plugin(routes).sync("testproject", "rf-test")
        assert project.node_file("network_device", "old-name").exists()

        configure(endpoints=[{"url": BASE, "name": "new-name"}])
        result = plugin(routes).sync("testproject", "rf-test")

        assert result.status is SyncStatus.SUCCESS
        assert any("Renamed" in w for w in result.warnings)
        assert not project.node_file("network_device", "old-name").exists()
        assert project.node_file("network_device", "new-name").exists()

    def test_endpoint_rename_rewrites_manual_references(self, redfish_env):
        # The rename deletes the old BMC file; a manual edge referencing the
        # old id (e.g. a hand-added manages edge) must be repointed.
        _env, project, configure, plugin = redfish_env
        configure(endpoints=[{"url": BASE, "name": "old-name"}])
        routes = endpoint_routes(BASE, system=SYSTEM_A)
        plugin(routes).sync("testproject", "rf-test")

        write_yaml(
            project.relationships_yaml,
            {
                "version": "2.0",
                "relationships": [
                    {
                        "source": "network_device:old-name",
                        "target": "physical_host:srv-01",
                        "type": "manages",
                    }
                ],
            },
        )

        configure(endpoints=[{"url": BASE, "name": "new-name"}])
        result = plugin(routes).sync("testproject", "rf-test")

        assert result.status is SyncStatus.SUCCESS
        rels = read_model(project.relationships_yaml, RelationshipFile).relationships
        manual = [r for r in rels if r.managed_by is None]
        assert manual[0].source == "network_device:new-name"

    def test_changed_url_updates_source_owned_observability(self, redfish_env):
        # Scheme/port change on the endpoint: the source's own observability
        # entry must follow, or ic query redfish keeps hitting the old URL.
        _env, project, configure, plugin = redfish_env
        configure(endpoints=[{"url": BASE, "name": "bmc-01"}])
        plugin(endpoint_routes(BASE, system=SYSTEM_A)).sync("testproject", "rf-test")

        node = read_model(project.node_file("network_device", "bmc-01"), Node)
        assert [o.instance for o in node.observability if o.type == "redfish"] == [BASE]

        # Same hostname (=> same source_id / same node), different port.
        new_base = BASE + ":8443"
        configure(endpoints=[{"url": new_base, "name": "bmc-01"}])
        result = plugin(endpoint_routes(new_base, system=SYSTEM_A)).sync("testproject", "rf-test")

        assert result.status is SyncStatus.SUCCESS
        node = read_model(project.node_file("network_device", "bmc-01"), Node)
        redfish_entries = [o for o in node.observability if o.type == "redfish"]
        assert [o.instance for o in redfish_entries] == [new_base]  # updated, not duplicated

    def test_manual_redfish_observability_entry_left_alone(self, redfish_env):
        # An entry NOT owned by this source (different/absent source field) is
        # manual and must never be touched; the source adds nothing alongside.
        _env, project, configure, plugin = redfish_env
        project.node_type_dir("network_device").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("network_device", "bmc-01"),
            Node(
                id="network_device:bmc-01",
                slug="bmc-01",
                type="network_device",
                name="bmc-01",
                observability=[{"type": "redfish", "instance": "https://manual.example.com"}],
            ),
        )
        configure(endpoints=[{"url": BASE, "name": "bmc-01"}])
        result = plugin(endpoint_routes(BASE, system=SYSTEM_A)).sync("testproject", "rf-test")

        assert result.status is SyncStatus.SUCCESS
        node = read_model(project.node_file("network_device", "bmc-01"), Node)
        redfish_entries = [o for o in node.observability if o.type == "redfish"]
        assert [o.instance for o in redfish_entries] == ["https://manual.example.com"]
        assert redfish_entries[0].source is None

    def test_two_endpoints_same_slug_is_guarded_error(self, redfish_env):
        _env, project, configure, plugin = redfish_env
        base2 = "https://bmc-02.example.com"
        configure(endpoints=[{"url": BASE, "name": "dup"}, {"url": base2, "name": "dup"}])
        routes = {**endpoint_routes(BASE, system=SYSTEM_A), **endpoint_routes(base2, system=SYSTEM_A)}

        result = plugin(routes).sync("testproject", "rf-test")

        assert result.status is SyncStatus.PARTIAL
        assert any("Slug collision within sync" in e for e in result.errors)
        assert not project.node_file("network_device", "dup").exists()  # sync guard held

    def test_foreign_source_id_collision_is_guarded(self, redfish_env):
        _env, project, configure, plugin = redfish_env
        project.node_type_dir("network_device").mkdir(parents=True, exist_ok=True)
        write_model(
            project.node_file("network_device", "bmc-x"),
            Node(
                id="network_device:bmc-x",
                slug="bmc-x",
                type="network_device",
                name="bmc-x",
                source_id="snmp:other:bmc-x",
            ),
        )
        configure(endpoints=[{"url": BASE, "name": "bmc-x"}])
        routes = endpoint_routes(BASE, system=SYSTEM_A)

        result = plugin(routes).sync("testproject", "rf-test")

        assert result.status is SyncStatus.PARTIAL
        assert any("refusing to overwrite" in e for e in result.errors)
        assert read_model(project.node_file("network_device", "bmc-x"), Node).source_id == "snmp:other:bmc-x"

    def test_fetch_failure_holds_sync_guard_and_records_run(self, redfish_env):
        """An unreachable endpoint (404 on the service root) yields a PARTIAL
        run that writes nothing, but still records the run."""
        env, project, configure, plugin = redfish_env
        configure(endpoints=[{"url": BASE, "name": "bmc-x"}])

        result = plugin({}).sync("testproject", "rf-test")  # empty routes -> 404

        assert result.status is SyncStatus.PARTIAL
        assert any("failed" in e.lower() for e in result.errors)
        assert not project.node_file("network_device", "bmc-x").exists()
        records = load_run_records(env, project="testproject", source="rf-test")
        assert records and records[-1].status == "partial"

    def test_invalid_config_is_failed(self, redfish_env):
        _env, _project, configure, plugin = redfish_env
        configure(endpoints=[])

        result = plugin({}).sync("testproject", "rf-test")

        assert result.status is SyncStatus.FAILED
        assert "endpoints" in result.message

    def test_missing_source_is_failed(self, redfish_env):
        _env, _project, _configure, plugin = redfish_env

        result = plugin({}).sync("testproject", "does-not-exist")

        assert result.status is SyncStatus.FAILED
        assert "not found" in result.message
