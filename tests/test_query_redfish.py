"""Tests for the Redfish query plugin (live health + power).

Reuses the fake HTTP session and ``endpoint_routes`` tree builder from the
source-plugin tests. The query plugin talks over ``plugin.session``, so the
fake is injected by setting ``plugin._session``.
"""

import logging

import pytest
from typer.testing import CliRunner

from infracontext.cli.query import app
from infracontext.models.node import Node, NodeType
from infracontext.query.base import QueryResult, resolve_basic_auth
from infracontext.query.redfish import RedfishQueryPlugin
from tests.test_sources_redfish import BASE, SYSTEM_A, _FakeResp, _FakeSession, endpoint_routes

runner = CliRunner()

CONFIG = {"name": "rf-test", "username": "admin", "password": "secret", "verify_ssl": False}


def _plugin(routes):
    plugin = RedfishQueryPlugin()
    plugin._session = _FakeSession(routes)
    return plugin


class TestStatus:
    def test_health_rollup_reports_worst(self):
        routes = endpoint_routes(
            BASE,
            system={**SYSTEM_A, "Status": {"Health": "Warning", "State": "Enabled"}},
            thermal={"Status": {"Health": "OK"}},
        )
        result = _plugin(routes).query(CONFIG, BASE, "status")

        assert result.success
        assert result.data["health"] == "Warning"
        assert result.data["systems"][0]["health"] == "Warning"
        assert result.data["systems"][0]["state"] == "Enabled"
        assert result.data["thermal"] == {"health": "OK", "source": "Thermal"}

    def test_critical_thermal_dominates_ok_system(self):
        routes = endpoint_routes(
            BASE,
            system={**SYSTEM_A, "Status": {"Health": "OK", "State": "Enabled"}},
            thermal={"Status": {"Health": "Critical"}},
        )
        result = _plugin(routes).query(CONFIG, BASE, "status")

        assert result.success
        assert result.data["health"] == "Critical"

    def test_health_unknown_when_nothing_reports(self):
        routes = endpoint_routes(BASE, system={"Manufacturer": "X"}, include_chassis=False)
        result = _plugin(routes).query(CONFIG, BASE, "status")

        assert result.success
        assert result.data["health"] == "Unknown"

    def test_thermal_sensor_rollup_when_no_top_level_health(self):
        """Legacy Thermal without a top-level Status.Health rolls up the worst
        per-sensor health across Temperatures and Fans."""
        routes = endpoint_routes(
            BASE,
            system={**SYSTEM_A, "Status": {"Health": "OK", "State": "Enabled"}},
            thermal={
                "Temperatures": [{"Status": {"Health": "OK"}}, {"Status": {"Health": "Warning"}}],
                "Fans": [{"Status": {"Health": "OK"}}],
            },
        )
        result = _plugin(routes).query(CONFIG, BASE, "status")

        assert result.success
        assert result.data["thermal"] == {"health": "Warning", "source": "Thermal"}
        assert result.data["health"] == "Warning"  # worst of system OK + thermal Warning

    def test_thermal_subsystem_without_health_yields_none(self):
        """A ThermalSubsystem lacking Status.Health surfaces health=None without
        overriding an otherwise-healthy rollup."""
        base = "https://bmc-8.example.com"
        routes = endpoint_routes(base, system={**SYSTEM_A, "Status": {"Health": "OK", "State": "Enabled"}})
        chassis = routes[f"{base}/redfish/v1/Chassis/1"]
        chassis["ThermalSubsystem"] = {"@odata.id": "/redfish/v1/Chassis/1/ThermalSubsystem"}
        routes[f"{base}/redfish/v1/Chassis/1/ThermalSubsystem"] = {"Id": "TS"}  # no Status.Health

        result = _plugin(routes).query(CONFIG, base, "status")

        assert result.success
        assert result.data["thermal"] == {"health": None, "source": "ThermalSubsystem"}
        assert result.data["health"] == "OK"  # None thermal health doesn't override system OK

    def test_thermal_subsystem_fallback(self):
        base = "https://bmc-9.example.com"
        routes = endpoint_routes(base, system=SYSTEM_A)
        # Swap the legacy Thermal link for the modern ThermalSubsystem.
        chassis = routes[f"{base}/redfish/v1/Chassis/1"]
        chassis["ThermalSubsystem"] = {"@odata.id": "/redfish/v1/Chassis/1/ThermalSubsystem"}
        routes[f"{base}/redfish/v1/Chassis/1/ThermalSubsystem"] = {"Status": {"Health": "OK"}}

        result = _plugin(routes).query(CONFIG, base, "status")

        assert result.success
        assert result.data["thermal"] == {"health": "OK", "source": "ThermalSubsystem"}


class TestPower:
    def test_legacy_power_control(self):
        routes = endpoint_routes(BASE, system=SYSTEM_A, legacy_power=210)
        result = _plugin(routes).query(CONFIG, BASE, "power")

        assert result.success
        assert result.data["power_watts"] == 210.0
        assert result.data["chassis"][0]["power_watts"] == 210.0

    def test_powersubsystem_environment_metrics(self):
        routes = endpoint_routes(BASE, system=SYSTEM_A, subsystem_power=488)
        result = _plugin(routes).query(CONFIG, BASE, "power")

        assert result.success
        assert result.data["power_watts"] == 488.0

    def test_no_power_reading_is_none(self):
        routes = endpoint_routes(BASE, system=SYSTEM_A)  # no power resources
        result = _plugin(routes).query(CONFIG, BASE, "power")

        assert result.success
        assert result.data["power_watts"] is None

    def test_legacy_power_falls_back_to_subsystem_when_no_numeric_reading(self):
        """A legacy Power resource that yields no numeric PowerConsumedWatts must
        fall through to PowerSubsystem/EnvironmentMetrics (the mixed BMC case)."""
        routes = endpoint_routes(BASE, system=SYSTEM_A, subsystem_power=488)
        chassis = routes[f"{BASE}/redfish/v1/Chassis/1"]
        chassis["Power"] = {"@odata.id": "/redfish/v1/Chassis/1/Power"}
        routes[f"{BASE}/redfish/v1/Chassis/1/Power"] = {"PowerControl": [{"PowerConsumedWatts": None}]}

        result = _plugin(routes).query(CONFIG, BASE, "power")

        assert result.success
        assert result.data["power_watts"] == 488.0


class TestErrors:
    def test_auth_failure_is_shaped(self):
        routes = {f"{BASE}/redfish/v1/": _FakeResp(401, text="Unauthorized")}
        result = _plugin(routes).query(CONFIG, BASE, "status")

        assert not result.success
        assert "HTTP 401" in result.error
        assert "Unauthorized" in result.error

    def test_server_error_is_shaped(self):
        routes = {f"{BASE}/redfish/v1/": _FakeResp(500, text="boom")}
        result = _plugin(routes).query(CONFIG, BASE, "power")

        assert not result.success
        assert "HTTP 500" in result.error
        assert "boom" in result.error

    def test_unknown_query_type(self):
        routes = endpoint_routes(BASE, system=SYSTEM_A)
        result = _plugin(routes).query(CONFIG, BASE, "temperatures")

        assert not result.success
        assert "Unknown query_type" in result.error

    def test_missing_url_errors_without_request(self):
        plugin = _plugin({})
        result = plugin.query(CONFIG, "", "status")

        assert not result.success
        assert "No Redfish URL" in result.error
        assert plugin._session.calls == []  # never hit the network


class TestBasicAuthResolution:
    def test_keychain_user_password(self, monkeypatch):
        monkeypatch.setattr(
            "infracontext.credentials.keychain.get_credential",
            lambda account: "root:calvin" if account == "redfish:prod" else None,
        )
        assert resolve_basic_auth({"credential": "redfish:prod"}) == ("root", "calvin")

    def test_password_may_contain_colon(self, monkeypatch):
        monkeypatch.setattr(
            "infracontext.credentials.keychain.get_credential",
            lambda account: "root:a:b:c",
        )
        assert resolve_basic_auth({"credential": "x"}) == ("root", "a:b:c")

    def test_inline_fallback(self):
        assert resolve_basic_auth({"username": "u", "password": "p"}) == ("u", "p")

    def test_none_when_unset(self):
        assert resolve_basic_auth({}) is None

    def test_colonless_secret_is_ignored_with_warning(self, monkeypatch, caplog):
        """A keychain secret without a ':' is malformed for basic auth; it is
        dropped but logged at WARNING (not silently swallowed)."""
        monkeypatch.setattr(
            "infracontext.credentials.keychain.get_credential",
            lambda account: "bare-token-no-colon",
        )
        with caplog.at_level(logging.WARNING, logger="infracontext.query.base"):
            result = resolve_basic_auth({"credential": "redfish:prod"})

        assert result is None  # no inline fallback -> anonymous
        assert any("user:password" in r.getMessage() for r in caplog.records)
        assert any("redfish:prod" in r.getMessage() for r in caplog.records)

    def test_colonless_secret_still_allows_inline_fallback(self, monkeypatch):
        """A malformed keychain secret must not shadow inline username/password."""
        monkeypatch.setattr(
            "infracontext.credentials.keychain.get_credential",
            lambda account: "bare-token-no-colon",
        )
        assert resolve_basic_auth(
            {"credential": "x", "username": "u", "password": "p"}
        ) == ("u", "p")


@pytest.mark.parametrize("query_type", ["status", "power"])
def test_auth_header_is_sent(query_type):
    """The resolved basic-auth credential reaches the HTTP call."""
    routes = endpoint_routes(BASE, system=SYSTEM_A, legacy_power=100)
    plugin = RedfishQueryPlugin()

    captured = {}
    fake = _FakeSession(routes)
    real_get = fake.get

    def spy(url, **kwargs):
        captured["auth"] = kwargs.get("auth")
        captured["verify"] = kwargs.get("verify")
        return real_get(url, **kwargs)

    fake.get = spy
    plugin._session = fake

    result = plugin.query(CONFIG, BASE, query_type)

    assert result.success
    assert captured["auth"] == ("admin", "secret")
    assert captured["verify"] is False


# ── CLI wiring ─────────────────────────────────────────────────────


def _fake_node() -> Node:
    return Node(id="network_device:bmc-x", slug="bmc-x", type=NodeType.NETWORK_DEVICE, name="BMC")


class TestCliCommand:
    def _patch(self, monkeypatch, obs, config):
        monkeypatch.setattr("infracontext.cli.query.require_project", lambda: "demo")
        monkeypatch.setattr("infracontext.cli.query.require_node", lambda _p, _n: _fake_node())
        monkeypatch.setattr(
            "infracontext.cli.query.get_node_observability",
            lambda _p, _n, obs_type, node=None: obs if obs_type == "redfish" else None,
        )
        monkeypatch.setattr(
            "infracontext.cli.query.get_source_config",
            lambda _p, source_type, _name=None, sources=None: config if source_type == "redfish" else None,
        )

    def test_redfish_status_renders(self, monkeypatch):
        self._patch(monkeypatch, {"instance": BASE, "source": "rf"}, CONFIG)

        class _Ok(RedfishQueryPlugin):
            def query(self, *_a, **_k):
                return QueryResult(
                    success=True, source_type="redfish", source_name="rf",
                    data={"health": "Warning", "systems": [{"id": "1", "health": "Warning", "state": "Enabled"}], "thermal": None},
                )

        monkeypatch.setattr("infracontext.query.redfish.RedfishQueryPlugin", _Ok)

        result = runner.invoke(app, ["redfish", "network_device:bmc-x"])

        assert result.exit_code == 0, result.output
        assert "Warning" in result.output

    def test_redfish_missing_instance_errors(self, monkeypatch):
        self._patch(monkeypatch, {"source": "rf"}, CONFIG)  # no 'instance'

        result = runner.invoke(app, ["redfish", "network_device:bmc-x"])

        assert result.exit_code == 1
        assert "no redfish url" in result.output.lower()

    def test_redfish_no_source_errors(self, monkeypatch):
        self._patch(monkeypatch, {"instance": BASE}, None)  # no source config

        result = runner.invoke(app, ["redfish", "network_device:bmc-x"])

        assert result.exit_code == 1
        assert "No Redfish source configured" in result.output

    def test_status_help_lists_redfish(self):
        """`ic query status --help` must advertise Redfish among its sources."""
        result = runner.invoke(app, ["status", "--help"])

        assert result.exit_code == 0
        assert "Redfish" in result.output

    def test_status_fanout_includes_redfish(self, monkeypatch):
        """The aggregated `ic query status` picks up a redfish observability
        entry and renders a Redfish section."""
        from infracontext.overrides import NodeOverrides

        monkeypatch.setattr("infracontext.cli.query.require_project", lambda: "demo")
        monkeypatch.setattr("infracontext.cli.query.require_node", lambda _p, _n: _fake_node())
        monkeypatch.setattr(
            "infracontext.cli.query.get_node_observability",
            lambda _p, _n, obs_type, node=None: (
                {"instance": BASE, "source": "rf"} if obs_type == "redfish" else None
            ),
        )
        monkeypatch.setattr("infracontext.cli.query.get_node_ssh_target", lambda _p, _n, node=None: None)
        monkeypatch.setattr("infracontext.cli.query.get_node_overrides", lambda *_a, **_k: NodeOverrides())
        monkeypatch.setattr(
            "infracontext.cli.query.get_source_config",
            lambda _p, source_type, _name=None, sources=None: (CONFIG if source_type == "redfish" else None),
        )

        class _Ok(RedfishQueryPlugin):
            def query(self, *_a, **_k):
                return QueryResult(
                    success=True, source_type="redfish", source_name="rf",
                    data={"health": "OK", "systems": [], "thermal": None},
                )

        monkeypatch.setattr("infracontext.query.redfish.RedfishQueryPlugin", _Ok)

        result = runner.invoke(app, ["status", "network_device:bmc-x"])

        assert result.exit_code == 0, result.output
        assert "Redfish" in result.output
