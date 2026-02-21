"""Tests for infracontext.paths — path validation, traversal protection, discovery."""

from unittest.mock import patch

import pytest

from infracontext.paths import (
    INFRACONTEXT_DIR,
    InvalidProjectSlugError,
    ProjectPaths,
    _validate_path_component,
    find_environment_root,
    list_projects,
    project_exists,
    validate_project_slug,
)

# ── validate_project_slug ──────────────────────────────────────────


class TestValidateProjectSlug:
    def test_simple_slug(self):
        assert validate_project_slug("production") == "production"

    def test_hierarchical_slug(self):
        assert validate_project_slug("acme/production") == "acme/production"

    def test_slug_with_dots_dashes_underscores(self):
        assert validate_project_slug("my-project_v2.0") == "my-project_v2.0"

    def test_empty_raises(self):
        with pytest.raises(InvalidProjectSlugError, match="cannot be empty"):
            validate_project_slug("")

    def test_whitespace_only_raises(self):
        with pytest.raises(InvalidProjectSlugError, match="cannot be empty"):
            validate_project_slug("   ")

    def test_traversal_dotdot_raises(self):
        with pytest.raises(InvalidProjectSlugError):
            validate_project_slug("../etc")

    def test_traversal_dotdot_slash_raises(self):
        with pytest.raises(InvalidProjectSlugError):
            validate_project_slug("foo/../../etc")

    def test_invalid_chars_raises(self):
        with pytest.raises(InvalidProjectSlugError):
            validate_project_slug("foo bar")

    def test_two_level_hierarchy_rejected(self):
        with pytest.raises(InvalidProjectSlugError):
            validate_project_slug("a/b/c")

    def test_starts_with_dot_rejected(self):
        with pytest.raises(InvalidProjectSlugError):
            validate_project_slug(".hidden")

    def test_strips_whitespace(self):
        assert validate_project_slug("  prod  ") == "prod"


# ── _validate_path_component ───────────────────────────────────────


class TestValidatePathComponent:
    def test_valid_component(self):
        assert _validate_path_component("vm", "node type") == "vm"

    def test_dot_rejected(self):
        with pytest.raises(ValueError, match="not allowed"):
            _validate_path_component(".", "node type")

    def test_dotdot_rejected(self):
        with pytest.raises(ValueError, match="not allowed"):
            _validate_path_component("..", "node type")

    def test_slash_rejected(self):
        with pytest.raises(ValueError, match="path separators"):
            _validate_path_component("a/b", "slug")

    def test_backslash_rejected(self):
        with pytest.raises(ValueError, match="path separators"):
            _validate_path_component("a\\b", "slug")

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            _validate_path_component("", "slug")


# ── ProjectPaths.for_project ──────────────────────────────────────


class TestProjectPathsForProject:
    def test_valid_project(self, tmp_environment):
        paths = ProjectPaths.for_project("myproject", tmp_environment)
        assert paths.root.name == "myproject"
        assert paths.nodes_dir == paths.root / "nodes"

    def test_hierarchical_project(self, tmp_environment):
        paths = ProjectPaths.for_project("acme/prod", tmp_environment)
        assert "acme" in str(paths.root)
        assert "prod" in str(paths.root)

    def test_traversal_literal_rejected(self, tmp_environment):
        with pytest.raises(InvalidProjectSlugError):
            ProjectPaths.for_project("../escape", tmp_environment)

    def test_traversal_resolved_rejected(self, tmp_environment):
        """Symlink-based escape is caught by post-resolve check."""
        # Create a symlink inside projects/ pointing outside
        escape_target = tmp_environment.root.parent / "outside"
        escape_target.mkdir(exist_ok=True)
        link = tmp_environment.projects_dir / "evil"
        link.symlink_to(escape_target)

        with pytest.raises(InvalidProjectSlugError, match="escapes"):
            ProjectPaths.for_project("evil", tmp_environment)

    def test_node_file_path(self, tmp_environment):
        paths = ProjectPaths.for_project("test", tmp_environment)
        nf = paths.node_file("vm", "web-01")
        assert nf.name == "web-01.yaml"
        assert nf.parent.name == "vm"


# ── find_environment_root ─────────────────────────────────────────


class TestFindEnvironmentRoot:
    def test_finds_in_current_dir(self, tmp_path):
        (tmp_path / INFRACONTEXT_DIR).mkdir()
        assert find_environment_root(tmp_path) == tmp_path

    def test_walks_up(self, tmp_path):
        (tmp_path / INFRACONTEXT_DIR).mkdir()
        child = tmp_path / "sub" / "deep"
        child.mkdir(parents=True)
        assert find_environment_root(child) == tmp_path

    def test_stops_at_git_root(self, tmp_path):
        """Should not find .infracontext above git root."""
        # Simulate git root at tmp_path/repo
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        # .infracontext is above repo
        (tmp_path / INFRACONTEXT_DIR).mkdir()

        with patch("infracontext.paths.find_git_root", return_value=repo):
            assert find_environment_root(repo) is None

    def test_returns_none_when_missing(self, tmp_path):
        with patch("infracontext.paths.find_git_root", return_value=tmp_path):
            assert find_environment_root(tmp_path) is None


# ── list_projects / project_exists ────────────────────────────────


class TestListProjects:
    def test_empty(self, tmp_environment):
        assert list_projects(tmp_environment) == []

    def test_single_project(self, tmp_project, tmp_environment):
        projects = list_projects(tmp_environment)
        assert "testproject" in projects

    def test_multiple_projects(self, tmp_environment):
        for name in ["alpha", "beta"]:
            p = ProjectPaths.for_project(name, tmp_environment)
            p.ensure_dirs()
        projects = list_projects(tmp_environment)
        assert set(projects) == {"alpha", "beta"}


class TestProjectExists:
    def test_exists(self, tmp_project, tmp_environment):
        assert project_exists("testproject", tmp_environment) is True

    def test_not_exists(self, tmp_environment):
        assert project_exists("nonexistent", tmp_environment) is False

    def test_invalid_slug_returns_false(self, tmp_environment):
        assert project_exists("../bad", tmp_environment) is False
