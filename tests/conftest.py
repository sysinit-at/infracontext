"""Shared fixtures for infracontext tests."""

import networkx as nx
import pytest

from infracontext.models.node import Node, NodeType, Observability, TriageConfig
from infracontext.paths import INFRACONTEXT_DIR, EnvironmentPaths, ProjectPaths


@pytest.fixture()
def tmp_environment(tmp_path):
    """Create a temporary infracontext environment directory structure."""
    ic_dir = tmp_path / INFRACONTEXT_DIR
    ic_dir.mkdir()
    projects_dir = ic_dir / "projects"
    projects_dir.mkdir()

    return EnvironmentPaths.from_root(tmp_path)


@pytest.fixture()
def tmp_project(tmp_environment):
    """Create a ProjectPaths for 'testproject' inside the temp environment."""
    paths = ProjectPaths.for_project("testproject", tmp_environment)
    paths.ensure_dirs()
    return paths


@pytest.fixture()
def sample_node():
    """A minimal valid Node for testing."""
    return Node(
        id="vm:web-01",
        slug="web-01",
        type=NodeType.VM,
        name="Web Server 01",
        ssh_alias="web-01",
        ip_addresses=["10.0.0.1"],
        triage=TriageConfig(services=["nginx", "php-fpm"]),
        observability=[
            Observability(type="prometheus", instance="web-01:9100"),
        ],
    )


@pytest.fixture()
def sample_graph():
    """A directed graph: vm:web-01 → vm:db-01 → physical_host:host-01."""
    g = nx.DiGraph()
    g.add_node("vm:web-01", name="Web Server", type="vm")
    g.add_node("vm:db-01", name="Database", type="vm")
    g.add_node("physical_host:host-01", name="Host 01", type="physical_host")

    g.add_edge("vm:web-01", "vm:db-01", type="depends_on", description="PostgreSQL")
    g.add_edge("vm:db-01", "physical_host:host-01", type="runs_on", description="Hosted on")
    return g


@pytest.fixture()
def monkeypatch_environment(tmp_environment, monkeypatch):
    """Patch find_environment_root and require_environment_root to use tmp dir."""
    monkeypatch.setattr(
        "infracontext.paths.find_environment_root",
        lambda start=None: tmp_environment.root,  # noqa: ARG005
    )
    monkeypatch.setattr(
        "infracontext.paths.require_environment_root",
        lambda: tmp_environment.root,
    )
    return tmp_environment
