"""Tests for `ic query status` — concurrent fan-out across monitoring sources."""

from typer.testing import CliRunner

from infracontext.cli.query import app
from infracontext.models.node import Node, NodeType
from infracontext.query.base import QueryResult

runner = CliRunner()


def _fake_node() -> Node:
    return Node(id="vm:web", slug="web", type=NodeType.VM, name="Web")


def _patch_env(monkeypatch, *, prom=False, cmk=False):
    """Wire the CLI helpers so only the requested sources are configured."""
    from infracontext.overrides import NodeOverrides

    monkeypatch.setattr("infracontext.cli.query.require_project", lambda: "demo")
    monkeypatch.setattr(
        "infracontext.cli.query.require_node", lambda _p, _n: _fake_node()
    )
    # query_status reads the node once and threads it through the helpers, so
    # the patched helpers accept (but ignore) the pre-loaded node / sources.
    monkeypatch.setattr(
        "infracontext.cli.query.get_node_observability", lambda _p, _n, _t, node=None: None
    )
    monkeypatch.setattr(
        "infracontext.cli.query.get_node_ssh_target", lambda _p, _n, node=None: None
    )
    monkeypatch.setattr(
        "infracontext.cli.query.get_node_overrides", lambda *_a, **_k: NodeOverrides()
    )

    configs = {}
    if prom:
        # Real plugin config key is 'addr' (not 'address'); keep the fixture
        # faithful even though these tests swap in fake plugins.
        configs["prometheus"] = {"addr": "http://prom.example:9090"}
    if cmk:
        configs["checkmk"] = {"api_url": "http://cmk.example"}
    monkeypatch.setattr(
        "infracontext.cli.query.get_source_config",
        lambda _p, source_type, _name=None, sources=None: configs.get(source_type),
    )


class TestQueryStatus:
    def test_one_failing_source_does_not_hide_the_others(self, monkeypatch):
        """A raising plugin is reported inline; other sections still print,
        and section order is stable regardless of which fetch finishes first.
        """
        _patch_env(monkeypatch, prom=True, cmk=True)

        class _BoomPrometheus:
            def query(self, *_a, **_k):
                raise RuntimeError("boom")

        class _OkCheckMK:
            def query(self, *_a, **_k):
                return QueryResult(
                    success=True, source_type="checkmk", source_name="cmk",
                    data={"alerts": []},
                )

        monkeypatch.setattr(
            "infracontext.query.prometheus.PrometheusPlugin", _BoomPrometheus
        )
        monkeypatch.setattr("infracontext.query.checkmk.CheckMKPlugin", _OkCheckMK)

        result = runner.invoke(app, ["status", "vm:web"])

        assert result.exit_code == 0, result.output
        assert "Error: boom" in result.output
        assert "No active alerts" in result.output
        # Prometheus section prints before CheckMK even though it errored.
        assert result.output.index("Prometheus") < result.output.index("CheckMK")

    def test_failed_query_result_is_reported(self, monkeypatch):
        """A QueryResult with success=False surfaces its error message."""
        _patch_env(monkeypatch, prom=True)

        class _FailingPrometheus:
            def query(self, *_a, **_k):
                return QueryResult(
                    success=False, source_type="prometheus", source_name="prom",
                    error="connection refused",
                )

        monkeypatch.setattr(
            "infracontext.query.prometheus.PrometheusPlugin", _FailingPrometheus
        )

        result = runner.invoke(app, ["status", "vm:web"])

        assert result.exit_code == 0, result.output
        assert "connection refused" in result.output

    def test_no_sources_configured_prints_guidance(self, monkeypatch):
        """With nothing configured, the user gets a next-step hint instead of
        a bare header followed by silence.
        """
        _patch_env(monkeypatch)

        result = runner.invoke(app, ["status", "vm:web"])

        assert result.exit_code == 0, result.output
        assert "No monitoring sources configured" in result.output
        assert "observability" in result.output


class TestMonitTlsWiring:
    """The node's monit observability `tls_skip_verify` must reach the plugin."""

    def _patch_monit_env(self, monkeypatch, obs: dict):
        from infracontext.overrides import NodeOverrides

        monkeypatch.setattr("infracontext.cli.query.require_project", lambda: "demo")
        monkeypatch.setattr("infracontext.cli.query.require_node", lambda _p, _n: _fake_node())
        monkeypatch.setattr(
            "infracontext.cli.query.get_node_observability",
            lambda _p, _n, obs_type, node=None: obs if obs_type == "monit" else None,
        )
        monkeypatch.setattr("infracontext.cli.query.get_node_ssh_target", lambda _p, _n, node=None: None)
        monkeypatch.setattr("infracontext.cli.query.get_node_overrides", lambda *_a, **_k: NodeOverrides())
        monkeypatch.setattr(
            "infracontext.cli.query.get_source_config",
            lambda _p, _t, _name=None, sources=None: None,
        )

        captured = {}

        class _CaptureMonit:
            def query(self, **kwargs):
                captured.update(kwargs)
                return QueryResult(
                    success=True, source_type="monit", source_name="monit",
                    data={"services": []},
                )

        monkeypatch.setattr("infracontext.query.monit.MonitPlugin", _CaptureMonit)
        return captured

    def test_status_passes_tls_skip_verify_from_observability(self, monkeypatch):
        captured = self._patch_monit_env(
            monkeypatch,
            {"monit_url": "https://monit.example:2812", "tls_skip_verify": True},
        )

        result = runner.invoke(app, ["status", "vm:web"])

        assert result.exit_code == 0, result.output
        assert captured["tls_skip_verify"] is True

    def test_query_monit_passes_tls_skip_verify_from_observability(self, monkeypatch):
        captured = self._patch_monit_env(
            monkeypatch,
            {"monit_url": "https://monit.example:2812", "tls_skip_verify": True},
        )

        result = runner.invoke(app, ["monit", "vm:web"])

        assert result.exit_code == 0, result.output
        assert captured["tls_skip_verify"] is True

    def test_tls_verification_defaults_on(self, monkeypatch):
        captured = self._patch_monit_env(
            monkeypatch, {"monit_url": "https://monit.example:2812"}
        )

        result = runner.invoke(app, ["status", "vm:web"])

        assert result.exit_code == 0, result.output
        assert captured["tls_skip_verify"] is False

    def test_tls_skip_verify_is_accepted_by_the_node_schema(self, monkeypatch):
        """Regression: the Observability model must accept tls_skip_verify —
        the CLI wiring is useless if the schema rejects the key (extra=forbid).
        Goes through the real model + get_node_observability, not a patched dict.
        """
        from infracontext.cli.query import get_node_observability
        from infracontext.models.node import Observability

        node = _fake_node()
        node.observability = [
            Observability(
                type="monit",
                monit_url="https://monit.example:2812",
                tls_skip_verify=True,
            )
        ]

        obs = get_node_observability("demo", "vm:web", "monit", node=node)

        assert obs is not None
        assert obs["tls_skip_verify"] is True
