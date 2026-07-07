"""Tests for the top-level ``ic learn`` command (``run_learn``)."""

from __future__ import annotations

import pytest
import typer

from infracontext.cli.learn import _parse_template, run_learn
from infracontext.models.node import Node
from infracontext.paths import ProjectPaths
from infracontext.storage import read_model


def _read(env, node_id: str) -> Node:
    node_type, slug = node_id.split(":", 1)
    node = read_model(ProjectPaths.for_project("prod", env).node_file(node_type, slug), Node)
    assert node is not None
    return node


class TestLearnDirect:
    def test_adds_learning_with_source_human(self, hotpath_env):
        run_learn("db", "cache pool tuned", "manual note")
        node = _read(hotpath_env, "vm:db-01")
        assert node.learnings[-1].finding == "cache pool tuned"
        assert node.learnings[-1].context == "manual note"
        # The asymmetry with `describe node learning` (agent default) is the point.
        assert node.learnings[-1].source == "human"

    def test_custom_context(self, hotpath_env):
        run_learn("db", "disk swapped", "hardware maintenance")
        assert _read(hotpath_env, "vm:db-01").learnings[-1].context == "hardware maintenance"


class TestLearnEditor:
    def test_editor_path_writes_finding_and_context(self, hotpath_env, monkeypatch, tmp_path):
        editor = tmp_path / "fake_editor.sh"
        editor.write_text(
            "#!/bin/sh\nprintf 'edited via editor\\n# context: from editor\\n' > \"$1\"\n"
        )
        editor.chmod(0o755)
        monkeypatch.setenv("EDITOR", str(editor))

        run_learn("db", None, "manual note")

        node = _read(hotpath_env, "vm:db-01")
        assert node.learnings[-1].finding == "edited via editor"
        assert node.learnings[-1].context == "from editor"
        assert node.learnings[-1].source == "human"

    def test_empty_editor_result_aborts_without_writing(self, hotpath_env, monkeypatch, tmp_path):
        editor = tmp_path / "empty_editor.sh"
        # Writes only a comment -> no finding.
        editor.write_text("#!/bin/sh\nprintf '# nothing here\\n' > \"$1\"\n")
        editor.chmod(0o755)
        monkeypatch.setenv("EDITOR", str(editor))

        before = len(_read(hotpath_env, "vm:db-01").learnings)
        with pytest.raises(typer.Exit) as exc:
            run_learn("db", None, "manual note")
        assert exc.value.exit_code == 0
        assert len(_read(hotpath_env, "vm:db-01").learnings) == before


class TestParseTemplate:
    def test_finding_from_noncomment_lines(self):
        finding, context = _parse_template("the finding\n# context: probing\n")
        assert finding == "the finding"
        assert context == "probing"

    def test_default_context_when_absent(self):
        finding, context = _parse_template("just a finding\n")
        assert finding == "just a finding"
        assert context == "manual note"

    def test_empty_when_only_comments(self):
        finding, _context = _parse_template("# a\n# b\n")
        assert finding == ""
