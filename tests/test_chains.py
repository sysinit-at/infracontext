"""Request-path chains: model, expansion, loader integration, doctor, CLI.

Chains live in ``chains.yaml`` -- a per-project sibling of
``relationships.yaml``, never inside it: released versions have
``extra="forbid"`` on RelationshipFile and skip the entire file on validation
errors, so embedding chains would erase all edges for older versions. The
tests below pin that separation (chain writes never touch relationships.yaml)
and the parse-boundary expansion every consumer shares.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from infracontext.cli.describe import app as describe_app
from infracontext.cli.doctor import Severity, run_doctor
from infracontext.models.chain import (
    CHAIN_ATTR_NAME,
    CHAIN_ATTR_POSITION,
    Chain,
    ChainFile,
    ChainMember,
    expand_chain,
)
from infracontext.models.node import Node, NodeType
from infracontext.models.relationship import Relationship, RelationshipFile, RelationshipType
from infracontext.paths import INFRACONTEXT_DIR, EnvironmentPaths, ProjectPaths
from infracontext.storage import write_model

runner = CliRunner()


# ── model ──────────────────────────────────────────────────────────


class TestChainModel:
    def test_plain_string_members_coerced(self):
        chain = Chain(name="edge", members=["vm:lb", "vm:app"])
        assert chain.members == [ChainMember(id="vm:lb"), ChainMember(id="vm:app")]

    def test_mapping_members_carry_via(self):
        chain = Chain(name="edge", members=["vm:lb", {"id": "vm:app", "via": "port 8080"}])
        assert chain.members[1].via == "port 8080"

    def test_default_type_is_routes_to(self):
        chain = Chain(name="edge", members=["vm:lb", "vm:app"])
        assert chain.type == RelationshipType.ROUTES_TO

    def test_fewer_than_two_members_rejected(self):
        with pytest.raises(ValidationError):
            Chain(name="edge", members=["vm:lb"])

    def test_consecutive_duplicate_member_rejected(self):
        # Would expand into a self-edge, which Relationship forbids.
        with pytest.raises(ValidationError, match="consecutive"):
            Chain(name="edge", members=["vm:lb", "vm:lb", "vm:db"])

    def test_non_consecutive_repeat_allowed(self):
        # A loop through a proxy and back is a legitimate path.
        chain = Chain(name="loop", members=["vm:a", "vm:b", "vm:a"])
        assert len(chain.members) == 3

    def test_non_slug_name_rejected(self):
        with pytest.raises(ValidationError, match="slug"):
            Chain(name="Web Path", members=["vm:lb", "vm:app"])

    def test_unknown_type_preserved_as_string(self):
        # Forward-compat mirror of Relationship.type.
        chain = Chain(name="edge", type="teleports_to", members=["vm:lb", "vm:app"])
        assert chain.type == "teleports_to"


# ── expansion ──────────────────────────────────────────────────────


class TestExpandChain:
    def test_pairs_and_ordering(self):
        chain = Chain(name="edge", members=["vm:lb", "vm:app", "vm:db"])
        edges = expand_chain(chain)

        assert [(e.source, e.target) for e in edges] == [("vm:lb", "vm:app"), ("vm:app", "vm:db")]
        assert all(e.type == RelationshipType.ROUTES_TO for e in edges)

    def test_chain_metadata_in_attributes(self):
        chain = Chain(name="edge", members=["vm:lb", "vm:app", "vm:db"])
        edges = expand_chain(chain)

        assert [e.attributes[CHAIN_ATTR_NAME] for e in edges] == ["edge", "edge"]
        assert [e.attributes[CHAIN_ATTR_POSITION] for e in edges] == [0, 1]

    def test_via_lands_on_edge_into_member(self):
        chain = Chain(
            name="edge",
            members=["vm:lb", {"id": "vm:app", "via": "port 8080"}, "vm:db"],
        )
        edges = expand_chain(chain)

        assert "via port 8080" in edges[0].description  # edge INTO vm:app
        assert "via" not in edges[1].description  # vm:db has no via

    def test_description_and_type_propagated(self):
        chain = Chain(
            name="edge",
            description="Customer traffic",
            type=RelationshipType.DEPENDS_ON,
            members=["vm:lb", "vm:app"],
        )
        edges = expand_chain(chain)

        assert edges[0].type == RelationshipType.DEPENDS_ON
        assert "chain 'edge' hop 1/1" in edges[0].description
        assert "Customer traffic" in edges[0].description


# ── loader integration ─────────────────────────────────────────────


def _write_nodes(tmp_project: ProjectPaths, *slugs: str) -> None:
    tmp_project.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
    for slug in slugs:
        node = Node(id=f"vm:{slug}", slug=slug, type=NodeType.VM, name=slug)
        write_model(tmp_project.node_file("vm", slug), node)


@pytest.fixture()
def chain_project(tmp_project, monkeypatch_environment, monkeypatch):
    """A project with lb/app/db nodes and a chain, loader paths patched."""
    _write_nodes(tmp_project, "lb", "app", "db")
    write_model(
        tmp_project.chains_yaml,
        ChainFile(chains=[Chain(name="edge", members=["vm:lb", "vm:app", "vm:db"])]),
    )
    monkeypatch.setattr(
        "infracontext.graph.loader.ProjectPaths.for_project",
        lambda _slug, _env=None: tmp_project,
    )
    return tmp_project


class TestLoaderChains:
    def test_load_graph_sees_chain_edges(self, chain_project):
        from infracontext.graph.loader import load_graph

        graph = load_graph("testproject")

        assert graph.has_edge("vm:lb", "vm:app")
        assert graph.has_edge("vm:app", "vm:db")
        edge = graph.edges["vm:lb", "vm:app"]
        assert edge["type"] == RelationshipType.ROUTES_TO
        assert edge["relationship"].attributes[CHAIN_ATTR_NAME] == "edge"

    def test_load_node_neighborhood_sees_chain_edges(self, chain_project):
        from infracontext.graph.loader import load_node_neighborhood

        graph = load_node_neighborhood("testproject", "vm:app", depth=1)

        assert graph.has_edge("vm:lb", "vm:app")
        assert graph.has_edge("vm:app", "vm:db")
        assert graph.edges["vm:app", "vm:db"]["relationship"].attributes[CHAIN_ATTR_POSITION] == 1

    def test_load_relationships_includes_chain_edges(self, chain_project):
        from infracontext.graph.loader import load_relationships

        rels = load_relationships("testproject")

        assert [(r.source, r.target) for r in rels] == [("vm:lb", "vm:app"), ("vm:app", "vm:db")]

    def test_chain_edges_merge_with_relationships_file(self, chain_project):
        from infracontext.graph.loader import load_graph

        write_model(
            chain_project.relationships_yaml,
            RelationshipFile(
                relationships=[
                    Relationship(source="vm:app", target="vm:lb", type=RelationshipType.FRONTED_BY)
                ]
            ),
        )

        graph = load_graph("testproject")

        assert graph.has_edge("vm:app", "vm:lb")  # from relationships.yaml
        assert graph.has_edge("vm:lb", "vm:app")  # from chains.yaml
        assert graph.has_edge("vm:app", "vm:db")

    def test_absent_chains_file_is_noop(self, tmp_project, monkeypatch_environment, monkeypatch):
        """No chains.yaml (every pre-chains repo) must load exactly as before."""
        from infracontext.graph.loader import load_graph

        _write_nodes(tmp_project, "web", "db")
        write_model(
            tmp_project.relationships_yaml,
            RelationshipFile(
                relationships=[
                    Relationship(source="vm:web", target="vm:db", type=RelationshipType.DEPENDS_ON)
                ]
            ),
        )
        monkeypatch.setattr(
            "infracontext.graph.loader.ProjectPaths.for_project",
            lambda _slug, _env=None: tmp_project,
        )

        graph = load_graph("testproject")

        assert graph.number_of_edges() == 1
        assert graph.has_edge("vm:web", "vm:db")

    def test_corrupt_chains_file_degrades_gracefully(
        self, chain_project, monkeypatch_environment, caplog
    ):
        """A malformed chains.yaml drops chain edges only -- relationships
        survive, with a warning pointing at `ic doctor`."""
        import logging

        from infracontext.graph.loader import load_graph

        write_model(
            chain_project.relationships_yaml,
            RelationshipFile(
                relationships=[
                    Relationship(source="vm:app", target="vm:db", type=RelationshipType.DEPENDS_ON)
                ]
            ),
        )
        chain_project.chains_yaml.write_text("chains: [unclosed\n")

        with caplog.at_level(logging.WARNING):
            graph = load_graph("testproject")

        assert graph.has_edge("vm:app", "vm:db")  # relationships intact
        assert not graph.has_edge("vm:lb", "vm:app")  # chain edges dropped
        assert "chains" in caplog.text.lower()
        assert "doctor" in caplog.text.lower()

    def test_plain_string_yaml_members_load(self, chain_project):
        """Hand-written YAML with mixed member forms round-trips the loader."""
        from infracontext.graph.loader import load_graph

        chain_project.chains_yaml.write_text(
            "version: '2.0'\n"
            "chains:\n"
            "  - name: edge\n"
            "    members:\n"
            "      - vm:lb\n"
            "      - id: vm:app\n"
            "        via: port 8080\n"
            "      - vm:db\n"
        )

        graph = load_graph("testproject")

        assert graph.has_edge("vm:lb", "vm:app")
        assert "via port 8080" in graph.edges["vm:lb", "vm:app"]["description"]


class TestMergedGraphChains:
    def test_cross_project_chain_member_qualified(
        self, tmp_environment, monkeypatch_environment, monkeypatch
    ):
        """@project-qualified chain members resolve across projects."""
        from infracontext.graph.loader import load_merged_graph

        proj_a = ProjectPaths.for_project("proj-a", tmp_environment)
        proj_a.ensure_dirs()
        proj_b = ProjectPaths.for_project("proj-b", tmp_environment)
        proj_b.ensure_dirs()

        _write_nodes(proj_a, "lb", "app")
        _write_nodes(proj_b, "db")

        write_model(
            proj_a.chains_yaml,
            ChainFile(chains=[Chain(name="edge", members=["vm:lb", "vm:app", "@proj-b:vm:db"])]),
        )

        _orig_for_project = ProjectPaths.for_project.__func__
        monkeypatch.setattr(
            "infracontext.graph.loader.list_projects",
            lambda **_kw: ["proj-a", "proj-b"],
        )
        monkeypatch.setattr(
            "infracontext.graph.loader.ProjectPaths.for_project",
            lambda slug, _env=None: _orig_for_project(ProjectPaths, slug, tmp_environment),
        )

        graph = load_merged_graph()

        assert graph.has_edge("proj-a/vm:lb", "proj-a/vm:app")
        assert graph.has_edge("proj-a/vm:app", "proj-b/vm:db")
        edge = graph.edges["proj-a/vm:app", "proj-b/vm:db"]
        assert edge["relationship"].attributes[CHAIN_ATTR_NAME] == "edge"


# ── doctor ─────────────────────────────────────────────────────────


@pytest.fixture()
def env_at(tmp_path, monkeypatch):
    """A temp environment wired so run_doctor's env discovery lands here."""
    (tmp_path / INFRACONTEXT_DIR).mkdir(parents=True)
    (tmp_path / INFRACONTEXT_DIR / "projects").mkdir()
    env = EnvironmentPaths.from_root(tmp_path)
    monkeypatch.setattr("infracontext.paths.find_environment_root", lambda start=None: env.root)  # noqa: ARG005
    monkeypatch.setattr("infracontext.paths.require_environment_root", lambda: env.root)
    return env


def _project(env: EnvironmentPaths, slug: str = "prod") -> ProjectPaths:
    paths = ProjectPaths.for_project(slug, env)
    paths.ensure_dirs()
    return paths


def _make_node(paths: ProjectPaths, node_type: NodeType, slug: str) -> Node:
    node = Node(id=f"{node_type}:{slug}", slug=slug, type=node_type, name=slug)
    paths.node_type_dir(str(node_type)).mkdir(parents=True, exist_ok=True)
    write_model(paths.node_file(str(node_type), slug), node)
    return node


def _issues(report, category: str, severity: Severity | None = None):
    return [
        i
        for i in report.issues
        if i.category == category and (severity is None or i.severity == severity)
    ]


class TestDoctorChains:
    def test_valid_chain_is_clean(self, env_at):
        proj = _project(env_at)
        for slug in ("lb", "app", "db"):
            _make_node(proj, NodeType.VM, slug)
        # (vm, vm) allows routes_to, so the whole chain passes the matrix.
        write_model(
            proj.chains_yaml,
            ChainFile(chains=[Chain(name="edge", members=["vm:lb", "vm:app", "vm:db"])]),
        )

        report = run_doctor(env_at)

        assert not _issues(report, "chain")
        assert not _issues(report, "constraint")
        assert not report.has_errors
        # Expanded pairs are counted as relationships (the edges consumers see).
        assert report.relationships_checked == 2

    def test_dangling_member_warned_not_error(self, env_at):
        proj = _project(env_at)
        _make_node(proj, NodeType.VM, "lb")
        write_model(
            proj.chains_yaml,
            ChainFile(chains=[Chain(name="edge", members=["vm:lb", "vm:ghost"])]),
        )

        report = run_doctor(env_at)

        dangling = _issues(report, "chain", Severity.WARNING)
        assert len(dangling) == 1
        assert "vm:ghost" in dangling[0].message
        assert not report.has_errors  # chains never flip the exit code

    def test_dangling_cross_project_member_warned(self, env_at):
        proj = _project(env_at)
        _make_node(proj, NodeType.VM, "lb")
        write_model(
            proj.chains_yaml,
            ChainFile(chains=[Chain(name="edge", members=["vm:lb", "@otherproj:vm:db"])]),
        )

        report = run_doctor(env_at)

        dangling = _issues(report, "chain", Severity.WARNING)
        assert len(dangling) == 1
        assert "otherproj" in dangling[0].message
        assert not report.has_errors

    def test_duplicate_chain_names_warned(self, env_at):
        proj = _project(env_at)
        for slug in ("lb", "app"):
            _make_node(proj, NodeType.VM, slug)
        write_model(
            proj.chains_yaml,
            ChainFile(
                chains=[
                    Chain(name="edge", members=["vm:lb", "vm:app"]),
                    Chain(name="edge", members=["vm:app", "vm:lb"]),
                ]
            ),
        )

        report = run_doctor(env_at)

        duplicates = [i for i in _issues(report, "chain", Severity.WARNING) if "Duplicate" in i.message]
        assert len(duplicates) == 1
        assert not report.has_errors

    def test_consecutive_pairs_revalidated_against_constraints(self, env_at):
        proj = _project(env_at)
        for slug in ("lb", "app"):
            _make_node(proj, NodeType.VM, slug)
        # (vm, vm) does not allow mounts -> constraint warning per pair.
        write_model(
            proj.chains_yaml,
            ChainFile(
                chains=[
                    Chain(name="edge", type=RelationshipType.MOUNTS, members=["vm:lb", "vm:app"])
                ]
            ),
        )

        report = run_doctor(env_at)

        constraint = _issues(report, "constraint", Severity.WARNING)
        assert len(constraint) == 1
        assert str(proj.chains_yaml) == str(constraint[0].file)
        assert not report.has_errors

    def test_first_member_via_warned(self, env_at):
        # `via` describes the edge INTO a member; the first member has no
        # inbound hop, so a via there is silently lost from every graph view.
        proj = _project(env_at)
        for slug in ("lb", "app"):
            _make_node(proj, NodeType.VM, slug)
        write_model(
            proj.chains_yaml,
            ChainFile(
                chains=[
                    Chain(name="edge", members=[{"id": "vm:lb", "via": "port 443"}, "vm:app"])
                ]
            ),
        )

        report = run_doctor(env_at)

        warnings = [i for i in _issues(report, "chain", Severity.WARNING) if "via" in i.message]
        assert len(warnings) == 1
        assert "vm:lb" in warnings[0].message
        assert not report.has_errors

    def test_via_on_later_member_stays_quiet(self, env_at):
        proj = _project(env_at)
        for slug in ("lb", "app"):
            _make_node(proj, NodeType.VM, slug)
        write_model(
            proj.chains_yaml,
            ChainFile(
                chains=[
                    Chain(name="edge", members=["vm:lb", {"id": "vm:app", "via": "port 8080"}])
                ]
            ),
        )

        report = run_doctor(env_at)

        assert not [i for i in _issues(report, "chain") if "via" in i.message]

    def test_absent_chains_file_reports_nothing(self, env_at):
        proj = _project(env_at)
        _make_node(proj, NodeType.VM, "lb")

        report = run_doctor(env_at)

        assert not _issues(report, "chain")
        assert report.relationships_checked == 0


# ── CLI ────────────────────────────────────────────────────────────


class TestChainCli:
    def test_add_and_list(self, hotpath_env):
        result = runner.invoke(
            describe_app,
            [
                "relationship",
                "chain",
                "add",
                "edge",
                "--member",
                "vm:web-01",
                "--member",
                "vm:db-01",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "edge" in result.output

        paths = ProjectPaths.for_project("prod", hotpath_env)
        assert paths.chains_yaml.exists()
        # The federation constraint: chains NEVER land in relationships.yaml.
        assert not paths.relationships_yaml.exists()

        result = runner.invoke(describe_app, ["relationship", "chain", "list"])
        assert result.exit_code == 0, result.output
        assert "edge" in result.output
        assert "vm:web-01" in result.output

    def test_add_custom_type(self, hotpath_env):
        result = runner.invoke(
            describe_app,
            [
                "relationship",
                "chain",
                "add",
                "edge",
                "--member",
                "vm:web-01",
                "--member",
                "vm:db-01",
                "--type",
                "depends_on",
            ],
        )
        assert result.exit_code == 0, result.output

        paths = ProjectPaths.for_project("prod", hotpath_env)
        assert "depends_on" in paths.chains_yaml.read_text()

    def test_add_duplicate_name_rejected(self, hotpath_env):
        args = [
            "relationship",
            "chain",
            "add",
            "edge",
            "--member",
            "vm:web-01",
            "--member",
            "vm:db-01",
        ]
        assert runner.invoke(describe_app, args).exit_code == 0
        result = runner.invoke(describe_app, args)
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_add_missing_node_rejected(self, hotpath_env):
        result = runner.invoke(
            describe_app,
            ["relationship", "chain", "add", "edge", "--member", "vm:web-01", "--member", "vm:ghost"],
        )
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_add_single_member_rejected(self, hotpath_env):
        result = runner.invoke(
            describe_app,
            ["relationship", "chain", "add", "edge", "--member", "vm:web-01"],
        )
        assert result.exit_code == 1
        assert "at least two" in result.output

    def test_add_invalid_type_rejected(self, hotpath_env):
        result = runner.invoke(
            describe_app,
            [
                "relationship",
                "chain",
                "add",
                "edge",
                "--member",
                "vm:web-01",
                "--member",
                "vm:db-01",
                "--type",
                "teleports_to",
            ],
        )
        assert result.exit_code == 1
        assert "Invalid relationship type" in result.output

    def test_add_invalid_name_rejected(self, hotpath_env):
        result = runner.invoke(
            describe_app,
            [
                "relationship",
                "chain",
                "add",
                "Not A Slug",
                "--member",
                "vm:web-01",
                "--member",
                "vm:db-01",
            ],
        )
        assert result.exit_code == 1
        assert "Invalid chain" in result.output

    def test_list_empty(self, hotpath_env):
        result = runner.invoke(describe_app, ["relationship", "chain", "list"])
        assert result.exit_code == 0, result.output
        assert "No chains defined" in result.output
