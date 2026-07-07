"""Tests for the shared fuzzy node resolver (``resolve_node_or_exit``)."""

from __future__ import annotations

import pytest
import typer

from infracontext.cli.resolve import _suggest_nodes, resolve_node_or_exit
from infracontext.paths import ProjectPaths


class TestExactPassthrough:
    def test_exact_type_slug_is_not_scanned(self, hotpath_env):
        """A ``type:slug`` argument takes the fast path verbatim -- it is not
        matched against on-disk nodes, so even a non-existent ID passes
        through (existence is the caller's concern)."""
        target = resolve_node_or_exit("vm:ghost")
        assert target.node_id == "vm:ghost"
        assert target.project == "prod"

    def test_exact_existing_id_resolves(self, hotpath_env):
        target = resolve_node_or_exit("vm:web-01")
        assert target.node_id == "vm:web-01"


class TestFuzzyResolution:
    def test_single_fuzzy_hit_by_slug(self, hotpath_env):
        target = resolve_node_or_exit("web")
        assert target.node_id == "vm:web-01"

    def test_single_fuzzy_hit_by_domain(self, hotpath_env):
        target = resolve_node_or_exit("web01.example.com")
        assert target.node_id == "vm:web-01"

    def test_single_fuzzy_hit_by_ssh_alias(self, hotpath_env):
        target = resolve_node_or_exit("web-prod")
        assert target.node_id == "vm:web-01"

    def test_multiple_hits_exit_1_with_table(self, hotpath_env, capsys):
        # '01' is a substring of both web-01 and db-01.
        with pytest.raises(typer.Exit) as exc:
            resolve_node_or_exit("01")
        assert exc.value.exit_code == 1
        out = capsys.readouterr().out
        assert "vm:web-01" in out
        assert "vm:db-01" in out
        assert "specific" in out.lower()

    def test_zero_hits_exit_1_with_suggestion(self, hotpath_env, capsys):
        with pytest.raises(typer.Exit) as exc:
            resolve_node_or_exit("web-02")
        assert exc.value.exit_code == 1
        out = capsys.readouterr().out
        assert "No node matches" in out
        # 'web-02' is close to 'web-01' -> did-you-mean surfaces it.
        assert "vm:web-01" in out

    def test_zero_hits_no_close_match_lists_hint(self, hotpath_env, capsys):
        with pytest.raises(typer.Exit):
            resolve_node_or_exit("zzzzzz")
        out = capsys.readouterr().out
        assert "ic describe node list" in out


class TestSuggestNodes:
    def test_suggests_by_slug(self, hotpath_env):
        paths = ProjectPaths.for_project("prod", hotpath_env)
        suggestions = _suggest_nodes("web-02", paths, hotpath_env, "prod")
        assert "vm:web-01" in suggestions

    def test_no_suggestion_for_unrelated(self, hotpath_env):
        paths = ProjectPaths.for_project("prod", hotpath_env)
        assert _suggest_nodes("zzzzzz", paths, hotpath_env, "prod") == []
