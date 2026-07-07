"""Tests for the environment registry and IC_ROOT / registry-based discovery.

The autouse ``_isolate_environment_discovery`` fixture (see conftest) clears
IC_ROOT and points ``$XDG_CONFIG_HOME`` at a fresh temp dir, so each test here
starts from an empty registry and controls IC_ROOT / cwd explicitly.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from typer.testing import CliRunner

from infracontext import envregistry
from infracontext.cli.config import app as config_app
from infracontext.paths import INFRACONTEXT_DIR, find_environment_root

runner = CliRunner()


def _make_env(root: Path) -> Path:
    """Create a minimal environment (a ``.infracontext/`` directory) at root."""
    (root / INFRACONTEXT_DIR).mkdir(parents=True)
    return root


# ── IC_ROOT resolution in find_environment_root ────────────────────


class TestIcRoot:
    def test_valid_ic_root_wins(self, tmp_path, monkeypatch):
        env = _make_env(tmp_path / "envx")
        monkeypatch.setenv("IC_ROOT", str(env))
        assert find_environment_root() == env

    def test_invalid_ic_root_warns_and_falls_through(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("IC_ROOT", str(tmp_path / "nope"))
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)

        assert find_environment_root() is None
        assert "IC_ROOT" in capsys.readouterr().err

    def test_explicit_start_ignores_ic_root(self, tmp_path, monkeypatch):
        env = _make_env(tmp_path / "envy")
        other = _make_env(tmp_path / "envz")
        monkeypatch.setenv("IC_ROOT", str(env))
        # An explicit start must not be overridden by IC_ROOT (scoped lookups).
        assert find_environment_root(other) == other


# ── registry helpers ───────────────────────────────────────────────


class TestRegistryHelpers:
    def test_add_default_and_resolve(self, tmp_path):
        env = _make_env(tmp_path / "home")
        resolved = envregistry.add_environment("home", env, make_default=True)
        assert resolved == env.resolve()

        registry = envregistry.load_registry()
        assert registry.environments["home"] == str(env.resolve())
        assert registry.default == "home"
        assert envregistry.default_environment_root() == env.resolve()

    def test_first_add_becomes_default_without_flag(self, tmp_path):
        env = _make_env(tmp_path / "home")
        envregistry.add_environment("home", env)
        assert envregistry.load_registry().default == "home"

    def test_set_default_and_remove_round_trip(self, tmp_path):
        home = _make_env(tmp_path / "home")
        work = _make_env(tmp_path / "work")
        envregistry.add_environment("home", home, make_default=True)
        envregistry.add_environment("work", work)

        envregistry.set_default("work")
        assert envregistry.load_registry().default == "work"

        assert envregistry.remove_environment("home") is True
        assert "home" not in envregistry.load_registry().environments
        # Removing a non-existent env is a no-op signalled by False.
        assert envregistry.remove_environment("home") is False

        # Removing the current default clears it.
        assert envregistry.remove_environment("work") is True
        assert envregistry.load_registry().default is None

    def test_malformed_registry_schema_is_ignored(self, capsys):
        path = envregistry.registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("environments: [this, is, a, list]\n")  # wrong type

        registry = envregistry.load_registry()
        assert registry.environments == {}
        assert registry.default is None
        assert "malformed" in capsys.readouterr().err

    def test_broken_yaml_registry_is_ignored(self, capsys):
        path = envregistry.registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("environments: {unterminated\n")

        assert envregistry.load_registry().environments == {}
        assert "malformed" in capsys.readouterr().err

    def test_default_missing_path_is_ignored(self, tmp_path, capsys):
        env = _make_env(tmp_path / "home")
        envregistry.add_environment("home", env, make_default=True)
        shutil.rmtree(env)

        assert envregistry.default_environment_root() is None
        assert "no longer contains" in capsys.readouterr().err


# ── registry-based discovery ───────────────────────────────────────


class TestRegistryDiscovery:
    def test_registry_default_resolves_from_unrelated_cwd(self, tmp_path, monkeypatch):
        env = _make_env(tmp_path / "home")
        envregistry.add_environment("home", env, make_default=True)

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.delenv("IC_ROOT", raising=False)

        assert find_environment_root() == env.resolve()

    def test_no_registry_no_ic_root_from_empty_cwd_is_none(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)
        assert find_environment_root() is None


# ── `ic config env` CLI ────────────────────────────────────────────


class TestEnvCli:
    def test_add_rejects_path_without_infracontext(self, tmp_path):
        result = runner.invoke(config_app, ["env", "add", "home", str(tmp_path / "missing")])
        assert result.exit_code == 1
        assert ".infracontext" in result.output

    def test_add_force_allows_missing_path(self, tmp_path):
        result = runner.invoke(config_app, ["env", "add", "home", str(tmp_path / "missing"), "--force"])
        assert result.exit_code == 0, result.output
        assert "home" in envregistry.load_registry().environments

    def test_add_list_default_remove_round_trip(self, tmp_path):
        env = _make_env(tmp_path / "home")

        add = runner.invoke(config_app, ["env", "add", "home", str(env), "--default"])
        assert add.exit_code == 0, add.output

        listing = runner.invoke(config_app, ["env", "list"])
        assert listing.exit_code == 0
        assert "home" in listing.output

        default = runner.invoke(config_app, ["env", "default", "home"])
        assert default.exit_code == 0

        remove = runner.invoke(config_app, ["env", "remove", "home"])
        assert remove.exit_code == 0
        assert "home" not in envregistry.load_registry().environments

    def test_default_unknown_errors(self, tmp_path):
        result = runner.invoke(config_app, ["env", "default", "ghost"])
        assert result.exit_code == 1
        assert "No environment named" in result.output

    def test_remove_unknown_errors(self, tmp_path):
        result = runner.invoke(config_app, ["env", "remove", "ghost"])
        assert result.exit_code == 1

    def test_add_rejects_invalid_name(self, tmp_path):
        env = _make_env(tmp_path / "home")
        result = runner.invoke(config_app, ["env", "add", "bad name", str(env)])
        assert result.exit_code == 1
        assert "Invalid environment name" in result.output
