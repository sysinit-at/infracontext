"""Regression tests for query-plugin robustness fixes.

Covers:
  1. Prometheus DEFAULT_QUERIES render without str.format crashing on braces.
  2. HTTP >= 400 error bodies surface as real errors, not "Invalid JSON".
  3. Monit SSH argument-injection guard (`--` + leading-dash rejection).
  4. Monit direct-HTTPS tls_skip_verify + distinct 401/404/5xx error classes.
  5. Narrowed catch-alls: network errors are inline, programming bugs propagate.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from infracontext.query.loki import LokiPlugin
from infracontext.query.monit import MonitPlugin
from infracontext.query.prometheus import PrometheusPlugin


class _FakeResponse:
    """Minimal stand-in for requests.Response used by the plugins."""

    def __init__(self, status_code, *, text="", json_data=None, json_raises=False):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json_data


def _session_returning(response):
    """A plugin session whose .get() yields the given fake response."""
    session = Mock()
    session.get.return_value = response
    return session


def _session_raising(exc):
    """A plugin session whose .get() raises the given exception."""
    session = Mock()
    session.get.side_effect = exc
    return session


# ── Fix 1: Prometheus template substitution ───────────────────────


class TestPrometheusTemplateSubstitution:
    @pytest.mark.parametrize("query_type", list(PrometheusPlugin.DEFAULT_QUERIES.keys()))
    def test_default_query_renders_without_crashing(self, query_type):
        """Every DEFAULT_QUERIES template must embed the real instance value
        with literal PromQL braces intact and no leftover sentinel -- the old
        str.format() raised ValueError on the '{' in the label matchers.
        """
        plugin = PrometheusPlugin()
        captured = {}

        def fake_execute(addr, query, source_config):
            captured["query"] = query
            return {"status": "success", "data": {"resultType": "vector", "result": []}}

        plugin._execute_query = fake_execute  # type: ignore[method-assign]

        result = plugin.query({"addr": "http://prom:9090"}, "web-server:9100", query_type)

        assert result.success
        rendered = captured["query"]
        assert "__INSTANCE__" not in rendered
        assert '{instance="web-server:9100"' in rendered

    def test_status_renders_every_template(self):
        """The status fan-out substitutes the instance in all five templates."""
        plugin = PrometheusPlugin()
        captured = []

        def fake_execute(addr, query, source_config):
            captured.append(query)
            return {"status": "success", "data": {"resultType": "vector", "result": [{"value": [0, "1"]}]}}

        plugin._execute_query = fake_execute  # type: ignore[method-assign]

        result = plugin.query({"addr": "http://prom:9090"}, "web:9100", "status")

        assert result.success
        assert len(captured) == len(PrometheusPlugin.DEFAULT_QUERIES)
        for rendered in captured:
            assert "__INSTANCE__" not in rendered
            assert '{instance="web:9100"' in rendered


# ── Fix 2: HTTP error bodies not masked as "Invalid JSON" ─────────


class TestPrometheusHttpErrors:
    def test_5xx_html_body_is_reported(self):
        plugin = PrometheusPlugin()
        plugin._session = _session_returning(
            _FakeResponse(500, text="<html>502 Bad Gateway</html>", json_raises=True)
        )

        result = plugin.query({"addr": "http://prom:9090"}, "web:9100", "up")

        assert not result.success
        assert "HTTP 500" in result.error
        assert "Bad Gateway" in result.error
        assert "Invalid JSON" not in result.error

    def test_401_empty_body_is_reported(self):
        plugin = PrometheusPlugin()
        plugin._session = _session_returning(_FakeResponse(401, text="", json_raises=True))

        result = plugin.query({"addr": "http://prom:9090"}, "web:9100", "up")

        assert not result.success
        assert "HTTP 401" in result.error
        assert "Invalid JSON" not in result.error

    def test_structured_json_error_is_preserved(self):
        """A Prometheus 400 with a JSON {"error": ...} body keeps its message."""
        plugin = PrometheusPlugin()
        plugin._session = _session_returning(
            _FakeResponse(400, json_data={"status": "error", "error": "parse error: bad query"})
        )

        result = plugin.query({"addr": "http://prom:9090"}, "web:9100", "up")

        assert not result.success
        assert "parse error: bad query" in result.error


class TestLokiHttpErrors:
    def test_5xx_html_body_is_reported(self):
        plugin = LokiPlugin()
        plugin._session = _session_returning(
            _FakeResponse(503, text="<html>service unavailable</html>", json_raises=True)
        )

        result = plugin.query({"addr": "http://loki:3100"}, '{job="x"}')

        assert not result.success
        assert "HTTP 503" in result.error
        assert "service unavailable" in result.error
        assert "Invalid JSON" not in result.error

    def test_401_empty_body_is_reported(self):
        plugin = LokiPlugin()
        plugin._session = _session_returning(_FakeResponse(401, text="", json_raises=True))

        result = plugin.query({"addr": "http://loki:3100"}, '{job="x"}')

        assert not result.success
        assert "HTTP 401" in result.error
        assert "Invalid JSON" not in result.error

    def test_labels_5xx_body_is_reported(self):
        plugin = LokiPlugin()
        plugin._session = _session_returning(
            _FakeResponse(500, text="boom", json_raises=True)
        )

        result = plugin.query({"addr": "http://loki:3100"}, "", query_type="labels")

        assert not result.success
        assert "HTTP 500" in result.error
        assert "boom" in result.error


# ── Fix 3: Monit SSH argument-injection guard ─────────────────────


class TestMonitSshInjectionGuard:
    def test_argv_places_double_dash_before_target(self, monkeypatch):
        plugin = MonitPlugin()
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="<monit></monit>", stderr="")

        monkeypatch.setattr("infracontext.query.monit.subprocess.run", fake_run)

        plugin.query(ssh_target="web-prod", port=2812)

        cmd = captured["cmd"]
        assert "--" in cmd
        assert cmd.index("--") < cmd.index("web-prod")

    def test_leading_dash_target_is_rejected_without_running_ssh(self, monkeypatch):
        plugin = MonitPlugin()
        ran = {"called": False}

        def fake_run(cmd, **kwargs):
            ran["called"] = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("infracontext.query.monit.subprocess.run", fake_run)

        result = plugin.query(ssh_target="-oProxyCommand=touch /tmp/pwned", port=2812)

        assert not result.success
        assert ran["called"] is False
        assert "-oProxyCommand" in result.error


# ── Fix 4: Monit direct-HTTPS verify + error classes ──────────────


class TestMonitDirectHttps:
    def test_tls_skip_verify_disables_verification(self):
        plugin = MonitPlugin()
        session = _session_returning(_FakeResponse(200, text="<monit></monit>"))
        plugin._session = session

        plugin.query(url="https://monit.example:2812", tls_skip_verify=True)

        _, kwargs = session.get.call_args
        assert kwargs["verify"] is False

    def test_https_verifies_by_default(self):
        plugin = MonitPlugin()
        session = _session_returning(_FakeResponse(200, text="<monit></monit>"))
        plugin._session = session

        plugin.query(url="https://monit.example:2812")

        _, kwargs = session.get.call_args
        assert kwargs["verify"] is True

    def test_auth_error_class(self):
        plugin = MonitPlugin()
        plugin._session = _session_returning(_FakeResponse(401, text="denied"))

        result = plugin.query(url="http://monit.example:2812")

        assert not result.success
        assert "HTTP 401" in result.error
        assert "auth" in result.error.lower()

    def test_not_found_error_class(self):
        plugin = MonitPlugin()
        plugin._session = _session_returning(_FakeResponse(404, text="nope"))

        result = plugin.query(url="http://monit.example:2812")

        assert not result.success
        assert "HTTP 404" in result.error
        assert "not found" in result.error.lower()

    def test_server_error_class_includes_body_snippet(self):
        plugin = MonitPlugin()
        plugin._session = _session_returning(_FakeResponse(500, text="stack trace here"))

        result = plugin.query(url="http://monit.example:2812")

        assert not result.success
        assert "HTTP 500" in result.error
        assert "stack trace here" in result.error


# ── Fix 5: narrowed catch-alls ────────────────────────────────────


class TestNarrowedCatchAlls:
    def test_prometheus_network_error_is_inline(self):
        plugin = PrometheusPlugin()
        plugin._session = _session_raising(requests.ConnectionError("no route to host"))

        result = plugin.query({"addr": "http://prom:9090"}, "web:9100", "up")

        assert not result.success
        assert "Request failed" in result.error

    def test_prometheus_programming_error_propagates(self):
        plugin = PrometheusPlugin()
        plugin._session = _session_raising(TypeError("latent bug"))

        with pytest.raises(TypeError):
            plugin.query({"addr": "http://prom:9090"}, "web:9100", "up")

    def test_loki_network_error_is_inline(self):
        plugin = LokiPlugin()
        plugin._session = _session_raising(OSError("socket exploded"))

        result = plugin.query({"addr": "http://loki:3100"}, '{job="x"}')

        assert not result.success
        assert "Request failed" in result.error

    def test_loki_programming_error_propagates(self):
        plugin = LokiPlugin()
        plugin._session = _session_raising(KeyError("latent bug"))

        with pytest.raises(KeyError):
            plugin.query({"addr": "http://loki:3100"}, '{job="x"}')

    def test_monit_direct_network_error_is_inline(self):
        plugin = MonitPlugin()
        plugin._session = _session_raising(OSError("connection reset"))

        result = plugin.query(url="http://monit.example:2812")

        assert not result.success
        assert "Request failed" in result.error

    def test_monit_direct_programming_error_propagates(self):
        plugin = MonitPlugin()
        plugin._session = _session_raising(AttributeError("latent bug"))

        with pytest.raises(AttributeError):
            plugin.query(url="http://monit.example:2812")

    def test_monit_ssh_programming_error_propagates(self, monkeypatch):
        plugin = MonitPlugin()
        monkeypatch.setattr(
            "infracontext.query.monit.subprocess.run",
            Mock(side_effect=TypeError("latent bug")),
        )

        with pytest.raises(TypeError):
            plugin.query(ssh_target="web-prod", port=2812)

    def test_monit_ssh_oserror_is_inline(self, monkeypatch):
        plugin = MonitPlugin()
        monkeypatch.setattr(
            "infracontext.query.monit.subprocess.run",
            Mock(side_effect=FileNotFoundError("ssh: command not found")),
        )

        result = plugin.query(ssh_target="web-prod", port=2812)

        assert not result.success
        assert "SSH command failed" in result.error
