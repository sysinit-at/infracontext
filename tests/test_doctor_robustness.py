"""Doctor must always run to completion, reporting problems as issues
rather than crashing with a traceback.

Covers:
- A schema-invalid config.yaml -> ERROR issue, no traceback.
- A non-mapping top-level YAML -> "expected a mapping" issue.
- A node file whose declared id disagrees with its path -> ERROR issue.
"""

from __future__ import annotations

import pytest

from infracontext.cli.doctor import Severity, run_doctor
from infracontext.models.node import Node, NodeType
from infracontext.paths import INFRACONTEXT_DIR, EnvironmentPaths, ProjectPaths
from infracontext.storage import write_model

_MALFORMED_CONFIG = """\
active_project: prod
external_roots:
  - alias: fleet
    path: ../fleet
    mode: readonly
"""


@pytest.fixture()
def env_at(tmp_path, monkeypatch):
    """A temp environment wired so run_doctor's env discovery lands here."""
    (tmp_path / INFRACONTEXT_DIR).mkdir(parents=True)
    (tmp_path / INFRACONTEXT_DIR / "projects").mkdir()
    env = EnvironmentPaths.from_root(tmp_path)
    monkeypatch.setattr("infracontext.paths.find_environment_root", lambda start=None: env.root)  # noqa: ARG005
    monkeypatch.setattr("infracontext.paths.require_environment_root", lambda: env.root)
    return env


class TestDoctorMalformedConfig:
    def test_reports_invalid_config_without_crashing(self, env_at):
        env_at.config_yaml.write_text(_MALFORMED_CONFIG, encoding="utf-8")

        # Must NOT raise -- doctor always completes.
        report = run_doctor(env_at)

        config_errors = [
            i
            for i in report.issues
            if i.severity == Severity.ERROR and i.category == "config"
        ]
        assert len(config_errors) == 1
        msg = config_errors[0].message
        assert "external_roots[0].mode" in msg
        assert "readonly" in msg
        assert report.has_errors


class TestDoctorNonMappingYaml:
    def test_top_level_list_reported_as_mapping_error(self, env_at):
        proj = ProjectPaths.for_project("prod", env_at)
        proj.ensure_dirs()
        proj.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        # Valid YAML, but a top-level list rather than a mapping.
        (proj.node_file("vm", "bad")).write_text("- one\n- two\n", encoding="utf-8")

        report = run_doctor(env_at)

        syntax_errors = [
            i
            for i in report.issues
            if i.severity == Severity.ERROR and "mapping" in i.message.lower()
        ]
        assert len(syntax_errors) == 1
        assert "list" in syntax_errors[0].message.lower()

    def test_top_level_scalar_reported_as_mapping_error(self, env_at):
        proj = ProjectPaths.for_project("prod", env_at)
        proj.ensure_dirs()
        proj.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        (proj.node_file("vm", "scalar")).write_text("just a string\n", encoding="utf-8")

        report = run_doctor(env_at)
        assert any("mapping" in i.message.lower() for i in report.issues)


class TestDoctorIdPathMismatch:
    def test_id_not_matching_path_reported(self, env_at):
        proj = ProjectPaths.for_project("prod", env_at)
        proj.ensure_dirs()
        proj.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        # File lives at vm/web.yaml but declares id "vm:other".
        node = Node(id="vm:other", slug="other", type=NodeType.VM, name="Mislabeled")
        write_model(proj.node_file("vm", "web"), node)

        report = run_doctor(env_at)

        mismatches = [i for i in report.issues if i.category == "id_path_mismatch"]
        assert len(mismatches) == 1
        assert mismatches[0].severity == Severity.ERROR
        assert "vm:other" in mismatches[0].message
        assert "vm:web" in mismatches[0].message

    def test_matching_id_and_path_is_clean(self, env_at):
        proj = ProjectPaths.for_project("prod", env_at)
        proj.ensure_dirs()
        proj.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        node = Node(id="vm:web", slug="web", type=NodeType.VM, name="Web")
        write_model(proj.node_file("vm", "web"), node)

        report = run_doctor(env_at)
        assert not [i for i in report.issues if i.category == "id_path_mismatch"]
