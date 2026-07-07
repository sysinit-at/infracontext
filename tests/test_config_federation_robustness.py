"""Robustness of config loading and federation against broken input.

- A schema-invalid config.yaml raises a typed, actionable ConfigError
  (not a raw pydantic traceback).
- Federation must never let one broken external root -- or an unparseable
  local config -- break local-only commands.
"""

from __future__ import annotations

import pytest

from infracontext.config import (
    AppConfig,
    ConfigError,
    ExternalRoot,
    load_config,
    save_config,
)
from infracontext.federation import (
    LOCAL_ROOT_ALIAS,
    all_roots,
    load_external_roots,
)
from infracontext.paths import INFRACONTEXT_DIR, EnvironmentPaths

_MALFORMED_CONFIG = """\
active_project: prod
external_roots:
  - alias: fleet
    path: ../fleet
    mode: readonly
"""


def _env_with_config(tmp_path, text: str) -> EnvironmentPaths:
    (tmp_path / INFRACONTEXT_DIR).mkdir(parents=True)
    env = EnvironmentPaths.from_root(tmp_path)
    env.config_yaml.write_text(text, encoding="utf-8")
    return env


class TestLoadConfigErrors:
    def test_invalid_mode_raises_config_error(self, tmp_path):
        env = _env_with_config(tmp_path, _MALFORMED_CONFIG)
        with pytest.raises(ConfigError) as excinfo:
            load_config(env)
        msg = str(excinfo.value)
        # Actionable: names the file, the offending key path, and the value.
        assert "config.yaml" in msg
        assert "external_roots[0].mode" in msg
        assert "readonly" in msg

    def test_config_error_is_valueerror(self):
        # Inherits ValueError to land in existing `except ValueError` handlers.
        assert issubclass(ConfigError, ValueError)

    def test_valid_config_still_loads(self, tmp_path):
        env = EnvironmentPaths.from_root(tmp_path)
        (tmp_path / INFRACONTEXT_DIR).mkdir(parents=True)
        save_config(AppConfig(active_project="prod"), env)
        assert load_config(env).active_project == "prod"

    def test_missing_config_returns_default(self, tmp_path):
        (tmp_path / INFRACONTEXT_DIR).mkdir(parents=True)
        env = EnvironmentPaths.from_root(tmp_path)
        assert load_config(env).active_project is None


class TestFederationRobustness:
    def test_broken_local_config_skips_external_roots(self, tmp_path, caplog):
        """An unparseable local config must not raise from federation --
        local commands keep working with no external roots contributed."""
        env = _env_with_config(tmp_path, _MALFORMED_CONFIG)
        with caplog.at_level("WARNING"):
            assert load_external_roots(env) == {}
        assert "local config" in caplog.text.lower()

    def test_all_roots_survives_broken_config(self, tmp_path):
        env = _env_with_config(tmp_path, _MALFORMED_CONFIG)
        roots = all_roots(env)
        # Local root still present and usable; no external roots.
        assert set(roots) == {LOCAL_ROOT_ALIAS}
        assert roots[LOCAL_ROOT_ALIAS].writable is True

    def test_external_root_oserror_is_skipped(self, tmp_path, caplog, monkeypatch):
        """If resolving a root raises OSError (symlink loop, permission
        denied), that root is skipped, not fatal."""
        local = tmp_path / "local"
        (local / INFRACONTEXT_DIR).mkdir(parents=True)
        local_env = EnvironmentPaths.from_root(local)
        save_config(
            AppConfig(external_roots=[ExternalRoot(alias="fleet", path="../fleet")]),
            local_env,
        )

        def _boom(*_args, **_kwargs):
            raise OSError("simulated symlink loop")

        monkeypatch.setattr("infracontext.federation.resolve_external_root", _boom)
        with caplog.at_level("WARNING"):
            assert load_external_roots(local_env) == {}
        assert "fleet" in caplog.text

    def test_external_root_runtimeerror_is_skipped(self, tmp_path, monkeypatch):
        local = tmp_path / "local"
        (local / INFRACONTEXT_DIR).mkdir(parents=True)
        local_env = EnvironmentPaths.from_root(local)
        save_config(
            AppConfig(external_roots=[ExternalRoot(alias="fleet", path="../fleet")]),
            local_env,
        )

        def _boom(*_args, **_kwargs):
            raise RuntimeError("cannot determine home")

        monkeypatch.setattr("infracontext.federation.resolve_external_root", _boom)
        assert load_external_roots(local_env) == {}
