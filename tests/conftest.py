"""Shared fixtures for infracontext tests."""

import networkx as nx
import pytest

from infracontext.models.node import Node, NodeType, Observability, TriageConfig
from infracontext.paths import INFRACONTEXT_DIR, EnvironmentPaths, ProjectPaths


@pytest.fixture(autouse=True)
def _isolate_environment_discovery(tmp_path_factory, monkeypatch):
    """Keep global environment discovery out of the test suite by default.

    ``find_environment_root`` consults ``IC_ROOT`` and the user-level
    environment registry (under ``$XDG_CONFIG_HOME``) whenever it is called
    with no explicit start. Neither should bleed into tests that rely on the
    cwd walk-up or on ``monkeypatch_environment``, and a stray ``IC_ROOT`` in
    the developer's shell must not make the suite non-deterministic. Tests that
    exercise those paths opt in explicitly by setting the env var / XDG dir
    themselves (this fixture only establishes a clean baseline).
    """
    monkeypatch.delenv("IC_ROOT", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config")))


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


@pytest.fixture()
def hotpath_env(tmp_environment, monkeypatch_environment, monkeypatch):
    """Environment with two VMs in an active 'prod' project, discovery patched.

    Shared by the hot-path CLI tests (resolver, ic ssh, ic learn, aliases,
    completion). ``vm:web-01`` carries an ssh_alias, domain, IP, triage hints,
    and a learning; ``vm:db-01`` is bare (no SSH target, no observability).
    """
    from infracontext.models.node import Learning, Node, NodeType, TriageConfig
    from infracontext.paths import ProjectPaths
    from infracontext.storage import write_model

    paths = ProjectPaths.for_project("prod", tmp_environment)
    paths.ensure_dirs()
    paths.node_type_dir("vm").mkdir(parents=True, exist_ok=True)

    web = Node(
        id="vm:web-01",
        slug="web-01",
        type=NodeType.VM,
        name="Web Server 01",
        ssh_alias="web-prod",
        domains=["web01.example.com"],
        ip_addresses=["10.0.0.5"],
        triage=TriageConfig(services=["nginx", "php-fpm"], context="check php-fpm first"),
        learnings=[
            Learning(date="2026-01-01", context="cpu", finding="pool misconfigured", source="human"),
        ],
    )
    db = Node(id="vm:db-01", slug="db-01", type=NodeType.VM, name="DB Server")
    write_model(paths.node_file("vm", "web-01"), web)
    write_model(paths.node_file("vm", "db-01"), db)

    monkeypatch.setenv("IC_PROJECT", "prod")
    return tmp_environment
