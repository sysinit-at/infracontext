"""Tests for `ic init` — environment bootstrap."""

from typer.testing import CliRunner

from infracontext.cli.main import app

runner = CliRunner()


class TestInit:
    def test_creates_structure_and_gitignore(self, tmp_path):
        result = runner.invoke(app, ["init", str(tmp_path)])

        assert result.exit_code == 0, result.output
        ic_dir = tmp_path / ".infracontext"
        assert ic_dir.is_dir()
        assert (ic_dir / "config.yaml").exists()

        # Lock/temp litter from the atomic writers must be pre-ignored so it
        # never shows up in the user's `git status`.
        gitignore = (ic_dir / ".gitignore").read_text()
        assert "**/.*.lock" in gitignore
        assert "**/.*.tmp" in gitignore

    def test_existing_gitignore_is_not_clobbered(self, tmp_path):
        """A user-customized .gitignore inside .infracontext survives re-init
        of a sibling directory structure (init writes it only when missing).
        """
        ic_dir = tmp_path / ".infracontext"
        ic_dir.mkdir()
        custom = "# mine\nprojects/secrets/\n"
        (ic_dir / ".gitignore").write_text(custom)

        # init refuses on an already-initialized root; call the writer path
        # via a fresh sibling to keep this focused on the no-clobber rule.
        result = runner.invoke(app, ["init", str(tmp_path)])

        # Already initialized -> exit 1, and the custom file is untouched.
        assert result.exit_code == 1
        assert (ic_dir / ".gitignore").read_text() == custom
