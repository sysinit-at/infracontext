"""Perf follow-ups: targeted 2-hop context load, single-read ``query status``.

Two hot incident commands used to over-read the filesystem:

- ``ic ctx`` / ``ic describe node context`` parsed *every* node in the project
  to compute one node's 2-hop neighborhood.
- ``ic query status`` re-parsed the same node YAML ~6x (require_node + four
  observability lookups + the ssh-target lookup) and re-globbed the sources
  dir per source type.

These tests pin the fixed behavior by spying on the YAML read layer and
asserting untouched node files are never parsed, plus an equivalence check
that the targeted context matches the full-graph context byte-for-byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import infracontext.storage as storage
from infracontext.cli.describe import app as describe_app
from infracontext.models.node import Node, NodeType, Observability
from infracontext.models.relationship import Relationship, RelationshipFile, RelationshipType
from infracontext.paths import ProjectPaths
from infracontext.query.base import QueryResult
from infracontext.storage import write_model, write_yaml

runner = CliRunner()


def _spy_node_reads(monkeypatch) -> list[Path]:
    """Record the path of every *node* YAML parsed via the storage layer.

    All node parses funnel through ``storage.read_model`` -> the storage
    module's ``read_yaml`` global, regardless of which caller module holds the
    ``read_model`` reference. Patching that single global therefore counts
    every node parse; filtering on ``/nodes/`` excludes relationships/sources.
    """
    reads: list[Path] = []
    original = storage.read_yaml

    def _counting_read_yaml(path):  # type: ignore[no-untyped-def]
        if "/nodes/" in str(path):
            reads.append(Path(path))
        return original(path)

    monkeypatch.setattr(storage, "read_yaml", _counting_read_yaml)
    return reads


# ── node context: targeted 2-hop neighborhood load ────────────────────


@pytest.fixture()
def chain_env(tmp_environment, monkeypatch_environment, monkeypatch):
    """A 'prod' project with a depends_on chain a -> b -> c -> d, plus lone e.

    Edge convention mirrors the graph loader (source depends on target), so
    from ``vm:a`` the 2-hop *upstream* ball is {b, c}; ``vm:d`` (3 hops) and
    the unconnected ``vm:e`` must never be parsed when building a's context.
    """
    paths = ProjectPaths.for_project("prod", tmp_environment)
    paths.ensure_dirs()

    for slug in ("a", "b", "c", "d", "e"):
        write_model(
            paths.node_file("vm", slug),
            Node(id=f"vm:{slug}", slug=slug, type=NodeType.VM, name=f"Node {slug.upper()}"),
        )

    write_model(
        paths.relationships_yaml,
        RelationshipFile(
            relationships=[
                Relationship(source="vm:a", target="vm:b", type=RelationshipType.DEPENDS_ON),
                Relationship(source="vm:b", target="vm:c", type=RelationshipType.DEPENDS_ON),
                Relationship(source="vm:c", target="vm:d", type=RelationshipType.DEPENDS_ON),
            ]
        ),
    )

    monkeypatch.setenv("IC_PROJECT", "prod")
    return tmp_environment


class TestNodeContextNeighborhood:
    def test_context_parses_only_the_two_hop_neighborhood(self, chain_env, monkeypatch):
        reads = _spy_node_reads(monkeypatch)

        result = runner.invoke(describe_app, ["node", "context", "vm:a"])
        assert result.exit_code == 0, result.output

        parsed = {p.stem for p in reads}
        # a's 2-hop upstream is {b, c}; a itself is read to build context.
        assert {"a", "b", "c"} <= parsed
        # 3-hop (d) and the unconnected node (e) are never touched.
        assert "d" not in parsed
        assert "e" not in parsed

    def test_targeted_context_equals_full_graph_context(self, chain_env, monkeypatch):
        from infracontext.cli.describe import _build_node_context
        from infracontext.graph import loader

        paths = ProjectPaths.for_project("prod", chain_env)
        node_b = storage.read_model(paths.node_file("vm", "b"), Node)
        assert node_b is not None

        # Real (targeted) path.
        ctx_targeted = _build_node_context(node_b, "prod", True, True)

        # Force the full-graph path and rebuild identical inputs.
        monkeypatch.setattr(
            loader,
            "load_node_neighborhood",
            lambda project, node_id, depth=2, root_alias="": loader.load_graph(
                project, root_alias=root_alias
            ),
        )
        ctx_full = _build_node_context(node_b, "prod", True, True)

        assert ctx_targeted == ctx_full
        # Sanity: b sees both directions (upstream c, d; downstream a).
        deps = ctx_targeted["dependencies"]
        assert [d["id"] for d in deps["depends_on"]] == ["vm:c", "vm:d"]
        assert [d["id"] for d in deps["depended_on_by"]] == ["vm:a"]


# ── query status: single node read ────────────────────────────────────


@pytest.fixture()
def status_env(tmp_environment, monkeypatch_environment, monkeypatch):
    """A 'prod' project with one node (ssh_alias + prometheus obs) and a source.

    The node carries enough config to spin up the Prometheus and (ssh-mode)
    Monit sections, which is exactly the shape that used to re-read the node
    file once per observability lookup plus once for the ssh target.
    """
    paths = ProjectPaths.for_project("prod", tmp_environment)
    paths.ensure_dirs()

    write_model(
        paths.node_file("vm", "web"),
        Node(
            id="vm:web",
            slug="web",
            type=NodeType.VM,
            name="Web",
            ssh_alias="web-prod",
            observability=[Observability(type="prometheus", instance="web:9100")],
        ),
    )
    write_yaml(paths.source_file("prom"), {"type": "prometheus", "addr": "http://p:9090"})

    monkeypatch.setenv("IC_PROJECT", "prod")
    return tmp_environment


class TestQueryStatusSingleRead:
    def test_status_parses_node_file_exactly_once(self, status_env, monkeypatch):
        # Keep the source plugins off the network — we only care about I/O to
        # the node YAML, not real Prometheus/Monit calls.
        class _OkProm:
            def query(self, *_a, **_k):
                return QueryResult(success=True, source_type="prometheus", source_name="p", data={"up": 1})

        class _OkMonit:
            def query(self, *_a, **_k):
                return QueryResult(
                    success=True, source_type="monit", source_name="m",
                    data={"services": [], "summary": {}},
                )

        monkeypatch.setattr("infracontext.query.prometheus.PrometheusPlugin", _OkProm)
        monkeypatch.setattr("infracontext.query.monit.MonitPlugin", _OkMonit)

        reads = _spy_node_reads(monkeypatch)

        from infracontext.cli.query import app as query_app

        result = runner.invoke(query_app, ["status", "vm:web"])
        assert result.exit_code == 0, result.output
        # Both sections must have been assembled (proving the lookups ran)...
        assert "Prometheus" in result.output
        assert "Monit" in result.output
        # ...yet the node YAML is parsed exactly once.
        assert [p.stem for p in reads] == ["web"]
