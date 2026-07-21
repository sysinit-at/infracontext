"""`ic triage checklist` -- self-contained checker delivery for the inline fallback.

The triage skill's no-subagent fallback fetches checker checklists from the
installed CLI, so these must resolve in every install shape: dev checkout
(repo-root `agents/`) and wheel (`infracontext/data/agents`, force-included in
pyproject).
"""

from typer.testing import CliRunner

from infracontext.cli.main import app
from infracontext.skill_data import list_skill_files, skill_data_dir

runner = CliRunner()

EXPECTED_CHECKERS = {
    "ic-collector-checker",
    "ic-connectivity-checker",
    "ic-cpu-checker",
    "ic-memory-checker",
    "ic-service-checker",
    "ic-storage-capacity-checker",
    "ic-storage-io-checker",
}


class TestSkillData:
    def test_agents_dir_resolves(self):
        directory = skill_data_dir("agents")
        assert directory is not None
        assert directory.is_dir()

    def test_commands_dir_resolves(self):
        directory = skill_data_dir("commands")
        assert directory is not None
        assert (directory / "ic-triage.md").exists()

    def test_unknown_kind_is_none(self):
        assert skill_data_dir("nonsense") is None
        assert list_skill_files("nonsense") == []

    def test_all_checkers_listed(self):
        stems = {path.stem for path in list_skill_files("agents")}
        assert stems >= EXPECTED_CHECKERS


class TestChecklistCommand:
    def test_list_names(self):
        result = runner.invoke(app, ["triage", "checklist"])
        assert result.exit_code == 0
        listed = set(result.output.split())
        assert listed >= EXPECTED_CHECKERS

    def test_print_one_checker(self):
        result = runner.invoke(app, ["triage", "checklist", "ic-cpu-checker"])
        assert result.exit_code == 0
        assert "USE method" in result.output

    def test_md_suffix_accepted(self):
        result = runner.invoke(app, ["triage", "checklist", "ic-cpu-checker.md"])
        assert result.exit_code == 0
        assert "USE method" in result.output

    def test_unknown_checker_lists_available(self):
        result = runner.invoke(app, ["triage", "checklist", "ic-gpu-checker"])
        assert result.exit_code == 1
        assert "ic-cpu-checker" in result.output  # available list shown

    def test_broken_install_degrades_with_error(self, monkeypatch):
        monkeypatch.setattr("infracontext.cli.triage_cmd.list_skill_files", lambda kind: [])
        result = runner.invoke(app, ["triage", "checklist"])
        assert result.exit_code == 1
        assert "broken installation" in result.output
