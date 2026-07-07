"""Tests for the hot-path surface: top-level aliases, shell completion, the
``import ssh-config`` rename, and clean config-error surfacing."""

from __future__ import annotations

from typer.testing import CliRunner

from infracontext.cli.completion import complete_node_id, complete_project
from infracontext.cli.import_cmd import app as import_app
from infracontext.cli.main import app as main_app

runner = CliRunner()


class TestAliases:
    def test_ctx_delegates_to_node_context(self, hotpath_env):
        result = runner.invoke(main_app, ["ctx", "web", "-f", "json"])
        assert result.exit_code == 0, result.output
        assert '"id": "vm:web-01"' in result.output

    def test_find_delegates_to_node_find(self, hotpath_env):
        result = runner.invoke(main_app, ["find", "web01.example.com"])
        assert result.exit_code == 0, result.output
        assert "vm:web-01" in result.output

    def test_status_delegates_and_resolves_fuzzily(self, hotpath_env):
        # db-01 has no sources -> deterministic "nothing configured" message,
        # proving the fuzzy query reached query_status with the right node.
        result = runner.invoke(main_app, ["status", "db"])
        assert result.exit_code == 0, result.output
        assert "No monitoring sources configured for vm:db-01" in result.output

    def test_ctx_fuzzy_multi_hit_exits_1(self, hotpath_env):
        result = runner.invoke(main_app, ["ctx", "01"])
        assert result.exit_code == 1
        assert "vm:web-01" in result.output and "vm:db-01" in result.output


class TestCompletion:
    def test_complete_node_id_from_dir_structure(self, hotpath_env):
        ids = complete_node_id("")
        assert "vm:web-01" in ids
        assert "vm:db-01" in ids

    def test_complete_node_id_prefix_filter(self, hotpath_env):
        assert complete_node_id("web") == ["vm:web-01"]

    def test_complete_node_id_swallows_errors(self, monkeypatch):
        def _boom():
            raise RuntimeError("no environment")

        monkeypatch.setattr("infracontext.paths.require_environment_root", _boom)
        assert complete_node_id("x") == []

    def test_complete_project(self, hotpath_env):
        assert "prod" in complete_project("")

    def test_complete_project_swallows_errors(self, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr("infracontext.paths.list_projects", _boom)
        assert complete_project("x") == []


class TestImportSshConfigRename:
    def test_ssh_config_command_registered(self):
        result = runner.invoke(import_app, ["--help"])
        assert "ssh-config" in result.output

    def test_deprecated_ssh_alias_warns_and_delegates(self, hotpath_env):
        # `prod` is not hierarchical, so the delegated import bails with an
        # error -- but the deprecation warning must appear first.
        result = runner.invoke(import_app, ["ssh"])
        assert "deprecated" in result.output.lower()


class TestConfigErrorSurface:
    def test_malformed_config_shows_one_clean_line(self, hotpath_env, monkeypatch):
        # Force config.yaml to actually be read (no IC_PROJECT shortcut).
        monkeypatch.delenv("IC_PROJECT", raising=False)
        hotpath_env.config_yaml.write_text(
            "active_project: prod\n"
            "external_roots:\n"
            "  - alias: fleet\n"
            "    path: ../fleet\n"
            "    mode: bogus\n",
            encoding="utf-8",
        )

        result = runner.invoke(main_app, ["describe", "node", "show", "vm:web-01"])

        assert result.exit_code == 1
        assert "config.yaml" in result.output
        # No pretty-traceback / locals dump.
        assert "Traceback" not in result.output
        assert "external_roots[0].mode" in " ".join(result.output.split())
