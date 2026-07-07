"""Tests for ``ic init`` gitignoring the local-overrides file."""

from __future__ import annotations

from typer.testing import CliRunner

from infracontext.cli.main import _ensure_gitignored
from infracontext.cli.main import app as main_app
from infracontext.paths import LOCAL_OVERRIDES_FILE

runner = CliRunner()


class TestEnsureGitignored:
    def test_creates_file_when_missing(self, tmp_path):
        _ensure_gitignored(tmp_path, LOCAL_OVERRIDES_FILE)
        assert (tmp_path / ".gitignore").read_text() == f"{LOCAL_OVERRIDES_FILE}\n"

    def test_appends_preserving_existing_content(self, tmp_path):
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("node_modules/\n*.log\n")

        _ensure_gitignored(tmp_path, LOCAL_OVERRIDES_FILE)

        text = gitignore.read_text()
        assert "node_modules/" in text
        assert text.endswith(f"{LOCAL_OVERRIDES_FILE}\n")

    def test_adds_newline_when_file_lacks_trailing_one(self, tmp_path):
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.log")  # no trailing newline

        _ensure_gitignored(tmp_path, LOCAL_OVERRIDES_FILE)

        assert gitignore.read_text() == f"*.log\n{LOCAL_OVERRIDES_FILE}\n"

    def test_idempotent_on_exact_line_match(self, tmp_path):
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text(f"# header\n{LOCAL_OVERRIDES_FILE}\nother\n")

        _ensure_gitignored(tmp_path, LOCAL_OVERRIDES_FILE)

        # No duplicate line was appended.
        assert gitignore.read_text().count(LOCAL_OVERRIDES_FILE) == 1


class TestInitGitignores:
    def test_init_gitignores_local_overrides(self, tmp_path):
        result = runner.invoke(main_app, ["init", str(tmp_path)])
        assert result.exit_code == 0, result.output

        gitignore = (tmp_path / ".gitignore").read_text()
        assert LOCAL_OVERRIDES_FILE in gitignore
        # Rich may wrap under CliRunner; normalize before the substring check.
        normalized = " ".join(result.output.split())
        assert "to .gitignore" in normalized

    def test_init_gitignore_is_idempotent_with_existing_entry(self, tmp_path):
        (tmp_path / ".gitignore").write_text(f"{LOCAL_OVERRIDES_FILE}\n")

        result = runner.invoke(main_app, ["init", str(tmp_path)])
        assert result.exit_code == 0, result.output

        assert (tmp_path / ".gitignore").read_text().count(LOCAL_OVERRIDES_FILE) == 1
