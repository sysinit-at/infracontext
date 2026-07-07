"""Tests for ``ic describe node add`` -- one-command bootstrap from an SSH alias."""

from __future__ import annotations

from typer.testing import CliRunner

from infracontext.cli.describe import app as describe_app
from infracontext.models.node import Node
from infracontext.paths import ProjectPaths
from infracontext.storage import read_model

runner = CliRunner()


def _read_node(env, slug: str, node_type: str = "vm") -> Node | None:
    paths = ProjectPaths.for_project("prod", env)
    return read_model(paths.node_file(node_type, slug), Node)


class TestNodeAddSanitization:
    def test_dot_alias_becomes_hyphen_slug(self, hotpath_env):
        result = runner.invoke(describe_app, ["node", "add", "s.myserver"])
        assert result.exit_code == 0, result.output

        node = _read_node(hotpath_env, "s-myserver")
        assert node is not None
        assert node.id == "vm:s-myserver"
        # ssh_alias keeps the verbatim alias; name defaults to it too.
        assert node.ssh_alias == "s.myserver"
        assert node.name == "s.myserver"
        # The "next: ic ssh <slug>" hint points at the sanitized slug.
        assert "ic ssh s-myserver" in result.output

    def test_underscore_and_case_are_normalized(self, hotpath_env):
        result = runner.invoke(describe_app, ["node", "add", "Web_Server"])
        assert result.exit_code == 0, result.output

        node = _read_node(hotpath_env, "web-server")
        assert node is not None
        assert node.id == "vm:web-server"
        assert node.ssh_alias == "Web_Server"


class TestNodeAddGuards:
    def test_collision_errors_with_slug_hint(self, hotpath_env):
        # vm:web-01 already exists in the fixture; the alias slugifies to it.
        result = runner.invoke(describe_app, ["node", "add", "web-01"])
        assert result.exit_code == 1
        assert "already exists" in result.output
        assert "--slug" in result.output

    def test_slug_override(self, hotpath_env):
        result = runner.invoke(describe_app, ["node", "add", "prod-box", "--slug", "custom-box"])
        assert result.exit_code == 0, result.output

        node = _read_node(hotpath_env, "custom-box")
        assert node is not None
        assert node.ssh_alias == "prod-box"


class TestNodeAddModel:
    def test_created_node_round_trips_through_validation(self, hotpath_env):
        result = runner.invoke(describe_app, ["node", "add", "db.internal", "--name", "Primary DB"])
        assert result.exit_code == 0, result.output

        node = _read_node(hotpath_env, "db-internal")
        assert node is not None
        assert node.name == "Primary DB"
        # read_model re-runs the model validators (slug regex, id == type:slug).
        assert node.id == f"{node.type}:{node.slug}"

    def test_type_override(self, hotpath_env):
        result = runner.invoke(describe_app, ["node", "add", "edge", "--type", "physical_host"])
        assert result.exit_code == 0, result.output

        node = _read_node(hotpath_env, "edge", node_type="physical_host")
        assert node is not None
        assert str(node.type) == "physical_host"
