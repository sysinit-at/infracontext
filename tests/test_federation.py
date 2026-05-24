"""Tests for federation: external_roots, qualified IDs, cross-root graph."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from infracontext.config import (
    AppConfig,
    ExternalRoot,
    ExternalRootMode,
    save_config,
)
from infracontext.federation import (
    LOCAL_ROOT_ALIAS,
    ExternalRootError,
    ReadOnlyRootError,
    all_roots,
    get_root,
    load_external_roots,
    require_writable_root,
    resolve_external_root,
    resolve_node_ref,
)
from infracontext.models.node import Node, NodeType
from infracontext.models.relationship import Relationship, RelationshipFile, RelationshipType
from infracontext.paths import INFRACONTEXT_DIR, EnvironmentPaths, ProjectPaths
from infracontext.storage import write_model

# ── ExternalRoot config model ──────────────────────────────────────


class TestExternalRootModel:
    def test_minimal_valid(self):
        root = ExternalRoot(alias="fleet", path="../fleet")
        assert root.alias == "fleet"
        assert root.mode == ExternalRootMode.READ_ONLY

    def test_read_write_mode(self):
        root = ExternalRoot(alias="fleet", path="../fleet", mode=ExternalRootMode.READ_WRITE)
        assert root.mode == ExternalRootMode.READ_WRITE

    def test_invalid_alias_rejected(self):
        with pytest.raises(ValidationError):
            ExternalRoot(alias="Fleet-WithCaps", path=".")  # uppercase

    def test_alias_with_dot_rejected(self):
        with pytest.raises(ValidationError):
            ExternalRoot(alias="fleet.prod", path=".")  # dot not allowed

    def test_unique_aliases_enforced(self):
        with pytest.raises(ValidationError):
            AppConfig(
                external_roots=[
                    ExternalRoot(alias="fleet", path="a"),
                    ExternalRoot(alias="fleet", path="b"),
                ]
            )


# ── resolve_external_root ──────────────────────────────────────────


class TestResolveExternalRoot:
    def test_resolves_existing_path(self, tmp_path):
        # Build a sibling environment
        sibling = tmp_path / "fleet"
        (sibling / INFRACONTEXT_DIR).mkdir(parents=True)
        entry = ExternalRoot(alias="fleet", path="fleet")  # relative to anchor
        resolved = resolve_external_root(entry, anchor=tmp_path)
        assert resolved.alias == "fleet"
        assert resolved.writable is False
        assert resolved.environment.root == sibling

    def test_missing_path_raises(self, tmp_path):
        entry = ExternalRoot(alias="fleet", path="nowhere")
        with pytest.raises(ExternalRootError):
            resolve_external_root(entry, anchor=tmp_path)

    def test_read_write_mode_carries_through(self, tmp_path):
        sibling = tmp_path / "fleet"
        (sibling / INFRACONTEXT_DIR).mkdir(parents=True)
        entry = ExternalRoot(alias="fleet", path="fleet", mode=ExternalRootMode.READ_WRITE)
        resolved = resolve_external_root(entry, anchor=tmp_path)
        assert resolved.writable is True


# ── resolve_node_ref ──────────────────────────────────────────────


class TestResolveNodeRef:
    @pytest.fixture()
    def two_roots(self, tmp_path):
        """Set up a local env and an external 'fleet' root."""
        local = tmp_path / "local"
        (local / INFRACONTEXT_DIR).mkdir(parents=True)
        local_env = EnvironmentPaths.from_root(local)

        fleet = tmp_path / "fleet"
        (fleet / INFRACONTEXT_DIR).mkdir(parents=True)
        fleet_env = EnvironmentPaths.from_root(fleet)
        save_config(AppConfig(active_project="default"), fleet_env)

        save_config(
            AppConfig(
                active_project="prod",
                external_roots=[ExternalRoot(alias="fleet", path="../fleet")],
            ),
            local_env,
        )
        return local_env, fleet_env

    def test_unqualified_ref_local(self, two_roots):
        local_env, _ = two_roots
        roots = all_roots(local_env)
        res = resolve_node_ref("vm:web-01", default_project="prod", roots=roots)
        assert res.root_alias == LOCAL_ROOT_ALIAS
        assert res.project == "prod"
        assert res.node_id == "vm:web-01"

    def test_external_root_qualified(self, two_roots):
        local_env, _ = two_roots
        roots = all_roots(local_env)
        res = resolve_node_ref(
            "@fleet:physical_host:pve-01", default_project="prod", roots=roots
        )
        assert res.root_alias == "fleet"
        assert res.project == "default"  # fleet's active_project
        assert res.node_id == "physical_host:pve-01"

    def test_local_cross_project_when_no_matching_alias(self, two_roots):
        local_env, _ = two_roots
        roots = all_roots(local_env)
        # 'staging' is not an external root -> treated as local cross-project
        res = resolve_node_ref("@staging:vm:web-01", default_project="prod", roots=roots)
        assert res.root_alias == LOCAL_ROOT_ALIAS
        assert res.project == "staging"
        assert res.node_id == "vm:web-01"


# ── all_roots / get_root / require_writable_root ─────────────────


class TestRootRegistry:
    @pytest.fixture()
    def federated(self, tmp_path):
        local = tmp_path / "local"
        (local / INFRACONTEXT_DIR).mkdir(parents=True)
        local_env = EnvironmentPaths.from_root(local)

        ro = tmp_path / "ro"
        (ro / INFRACONTEXT_DIR).mkdir(parents=True)

        rw = tmp_path / "rw"
        (rw / INFRACONTEXT_DIR).mkdir(parents=True)

        save_config(
            AppConfig(
                external_roots=[
                    ExternalRoot(alias="ro", path="../ro", mode=ExternalRootMode.READ_ONLY),
                    ExternalRoot(alias="rw", path="../rw", mode=ExternalRootMode.READ_WRITE),
                ]
            ),
            local_env,
        )
        return local_env

    def test_all_roots_includes_local(self, federated):
        roots = all_roots(federated)
        assert LOCAL_ROOT_ALIAS in roots
        assert roots[LOCAL_ROOT_ALIAS].writable is True

    def test_all_roots_includes_externals(self, federated):
        roots = all_roots(federated)
        assert set(roots) == {LOCAL_ROOT_ALIAS, "ro", "rw"}

    def test_get_root_by_alias(self, federated):
        ro = get_root("ro", federated)
        assert ro is not None
        assert ro.writable is False

    def test_get_root_unknown_returns_none(self, federated):
        assert get_root("nope", federated) is None

    def test_require_writable_root_allows_rw(self, federated):
        root = require_writable_root("rw", federated)
        assert root.writable is True

    def test_require_writable_root_refuses_ro(self, federated):
        with pytest.raises(ReadOnlyRootError):
            require_writable_root("ro", federated)

    def test_load_external_roots_skips_missing(self, tmp_path, caplog):
        local = tmp_path / "local"
        (local / INFRACONTEXT_DIR).mkdir(parents=True)
        local_env = EnvironmentPaths.from_root(local)
        save_config(
            AppConfig(
                external_roots=[ExternalRoot(alias="gone", path="../missing")],
            ),
            local_env,
        )
        roots = load_external_roots(local_env)
        assert roots == {}  # missing root silently skipped


# ── graph loading across roots ───────────────────────────────────


class TestFederatedGraph:
    @pytest.fixture()
    def federated_with_nodes(self, tmp_path, monkeypatch):
        """Two roots, each with one node. Local relationship references the
        node in the external root."""
        # Local root with a VM.
        local = tmp_path / "local"
        (local / INFRACONTEXT_DIR).mkdir(parents=True)
        local_env = EnvironmentPaths.from_root(local)
        local_proj = ProjectPaths.for_project("prod", local_env)
        local_proj.ensure_dirs()
        local_proj.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(
            local_proj.node_file("vm", "web-01"),
            Node(id="vm:web-01", slug="web-01", type=NodeType.VM, name="Web"),
        )

        # Fleet root with a hypervisor.
        fleet = tmp_path / "fleet"
        (fleet / INFRACONTEXT_DIR).mkdir(parents=True)
        fleet_env = EnvironmentPaths.from_root(fleet)
        save_config(AppConfig(active_project="default"), fleet_env)
        fleet_proj = ProjectPaths.for_project("default", fleet_env)
        fleet_proj.ensure_dirs()
        fleet_proj.node_type_dir("physical_host").mkdir(parents=True, exist_ok=True)
        write_model(
            fleet_proj.node_file("physical_host", "pve-01"),
            Node(
                id="physical_host:pve-01",
                slug="pve-01",
                type=NodeType.PHYSICAL_HOST,
                name="PVE-01",
            ),
        )

        # Local relationship -> external root.
        write_model(
            local_proj.relationships_yaml,
            RelationshipFile(
                relationships=[
                    Relationship(
                        source="vm:web-01",
                        target="@fleet:physical_host:pve-01",
                        type=RelationshipType.RUNS_ON,
                    )
                ]
            ),
        )

        save_config(
            AppConfig(
                active_project="prod",
                external_roots=[ExternalRoot(alias="fleet", path="../fleet")],
            ),
            local_env,
        )

        # Point auto-discovery at the local env.
        monkeypatch.setattr(
            "infracontext.paths.find_environment_root",
            lambda start=None: local_env.root,
        )
        monkeypatch.setattr(
            "infracontext.paths.require_environment_root",
            lambda: local_env.root,
        )
        return local_env, fleet_env

    def test_load_graph_resolves_cross_root_target(self, federated_with_nodes):
        from infracontext.graph.loader import load_graph

        graph = load_graph("prod")
        assert graph.has_node("vm:web-01")
        # Cross-root target gets qualified as @fleet:default/physical_host:pve-01
        assert graph.has_node("@fleet:default/physical_host:pve-01")
        assert graph.has_edge("vm:web-01", "@fleet:default/physical_host:pve-01")

    def test_merged_graph_spans_roots(self, federated_with_nodes):
        from infracontext.graph.loader import load_merged_graph

        graph = load_merged_graph()
        node_ids = set(graph.nodes())
        assert "prod/vm:web-01" in node_ids
        assert "@fleet:default/physical_host:pve-01" in node_ids


# ── doctor: external root validation ─────────────────────────────


class TestDoctorExternalRoots:
    def test_alias_collision_reported(self, tmp_path, monkeypatch):
        from infracontext.cli.doctor import run_doctor

        local = tmp_path / "local"
        (local / INFRACONTEXT_DIR).mkdir(parents=True)
        local_env = EnvironmentPaths.from_root(local)
        # Local project named 'fleet'
        local_proj = ProjectPaths.for_project("fleet", local_env)
        local_proj.ensure_dirs()
        # External root also aliased 'fleet'
        ext = tmp_path / "external"
        (ext / INFRACONTEXT_DIR).mkdir(parents=True)
        save_config(
            AppConfig(
                active_project="fleet",
                external_roots=[ExternalRoot(alias="fleet", path="../external")],
            ),
            local_env,
        )

        monkeypatch.setattr(
            "infracontext.paths.find_environment_root",
            lambda start=None: local_env.root,
        )
        monkeypatch.setattr(
            "infracontext.paths.require_environment_root",
            lambda: local_env.root,
        )

        report = run_doctor(local_env)
        errors = [i for i in report.issues if i.category == "external_root"]
        assert any("collides" in i.message for i in errors)

    def test_duplicate_node_id_warns(self, tmp_path, monkeypatch):
        from infracontext.cli.doctor import run_doctor

        local = tmp_path / "local"
        (local / INFRACONTEXT_DIR).mkdir(parents=True)
        local_env = EnvironmentPaths.from_root(local)
        local_proj = ProjectPaths.for_project("prod", local_env)
        local_proj.ensure_dirs()
        local_proj.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(
            local_proj.node_file("vm", "shared"),
            Node(id="vm:shared", slug="shared", type=NodeType.VM, name="Shared"),
        )

        ext = tmp_path / "external"
        (ext / INFRACONTEXT_DIR).mkdir(parents=True)
        ext_env = EnvironmentPaths.from_root(ext)
        save_config(AppConfig(active_project="default"), ext_env)
        ext_proj = ProjectPaths.for_project("default", ext_env)
        ext_proj.ensure_dirs()
        ext_proj.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(
            ext_proj.node_file("vm", "shared"),
            Node(id="vm:shared", slug="shared", type=NodeType.VM, name="Other Shared"),
        )

        save_config(
            AppConfig(
                active_project="prod",
                external_roots=[ExternalRoot(alias="fleet", path="../external")],
            ),
            local_env,
        )

        monkeypatch.setattr(
            "infracontext.paths.find_environment_root",
            lambda start=None: local_env.root,
        )
        monkeypatch.setattr(
            "infracontext.paths.require_environment_root",
            lambda: local_env.root,
        )

        report = run_doctor(local_env)
        dupes = [
            i for i in report.issues if i.category == "external_root" and "shared" in i.message
        ]
        assert len(dupes) == 1
        assert dupes[0].severity.value == "warning"


# ── describe node commands accept qualified IDs ───────────────────


class TestFederatedNodeCommands:
    @pytest.fixture()
    def federated(self, tmp_path, monkeypatch):
        """Build a local root with a prod project + an external 'fleet' root
        with a default project containing a hypervisor."""
        local = tmp_path / "local"
        (local / INFRACONTEXT_DIR).mkdir(parents=True)
        local_env = EnvironmentPaths.from_root(local)
        local_proj = ProjectPaths.for_project("prod", local_env)
        local_proj.ensure_dirs()
        local_proj.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(
            local_proj.node_file("vm", "web-01"),
            Node(id="vm:web-01", slug="web-01", type=NodeType.VM, name="Web"),
        )

        fleet = tmp_path / "fleet"
        (fleet / INFRACONTEXT_DIR).mkdir(parents=True)
        fleet_env = EnvironmentPaths.from_root(fleet)
        save_config(AppConfig(active_project="default"), fleet_env)
        fleet_proj = ProjectPaths.for_project("default", fleet_env)
        fleet_proj.ensure_dirs()
        fleet_proj.node_type_dir("physical_host").mkdir(parents=True, exist_ok=True)
        write_model(
            fleet_proj.node_file("physical_host", "pve-01"),
            Node(
                id="physical_host:pve-01",
                slug="pve-01",
                type=NodeType.PHYSICAL_HOST,
                name="PVE-01",
            ),
        )

        save_config(
            AppConfig(
                active_project="prod",
                external_roots=[ExternalRoot(alias="fleet", path="../fleet")],
            ),
            local_env,
        )

        monkeypatch.setattr(
            "infracontext.paths.find_environment_root",
            lambda start=None: local_env.root,
        )
        monkeypatch.setattr(
            "infracontext.paths.require_environment_root",
            lambda: local_env.root,
        )
        return local_env, fleet_env

    def test_resolve_local_plain_ref(self, federated):
        from infracontext.cli.describe import _resolve_node_target

        target = _resolve_node_target("vm:web-01")
        assert target.root_alias == ""
        assert target.project == "prod"
        assert target.node_id == "vm:web-01"
        assert target.writable is True

    def test_resolve_external_qualified_ref(self, federated):
        from infracontext.cli.describe import _resolve_node_target

        target = _resolve_node_target("@fleet:physical_host:pve-01")
        assert target.root_alias == "fleet"
        assert target.project == "default"  # fleet's active_project
        assert target.node_id == "physical_host:pve-01"
        assert target.writable is False  # read-only by default
        # Paths resolve into the fleet env, not the local one.
        local_env, fleet_env = federated
        assert target.paths.root.is_relative_to(fleet_env.root)

    def test_resolve_write_against_readonly_root_exits(self, federated):
        import typer

        from infracontext.cli.describe import _resolve_node_target

        with pytest.raises(typer.Exit):
            _resolve_node_target("@fleet:physical_host:pve-01", require_writable=True)

    def test_resolve_write_against_readwrite_root_succeeds(self, tmp_path, monkeypatch):
        """A root explicitly marked read-write must allow writes."""
        from infracontext.cli.describe import _resolve_node_target

        local = tmp_path / "local"
        (local / INFRACONTEXT_DIR).mkdir(parents=True)
        local_env = EnvironmentPaths.from_root(local)
        ProjectPaths.for_project("prod", local_env).ensure_dirs()

        fleet = tmp_path / "fleet"
        (fleet / INFRACONTEXT_DIR).mkdir(parents=True)
        fleet_env = EnvironmentPaths.from_root(fleet)
        save_config(AppConfig(active_project="default"), fleet_env)
        fleet_proj = ProjectPaths.for_project("default", fleet_env)
        fleet_proj.ensure_dirs()
        fleet_proj.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(
            fleet_proj.node_file("vm", "writable-vm"),
            Node(id="vm:writable-vm", slug="writable-vm", type=NodeType.VM, name="W"),
        )

        save_config(
            AppConfig(
                active_project="prod",
                external_roots=[
                    ExternalRoot(
                        alias="fleet",
                        path="../fleet",
                        mode=ExternalRootMode.READ_WRITE,
                    )
                ],
            ),
            local_env,
        )

        monkeypatch.setattr(
            "infracontext.paths.find_environment_root",
            lambda start=None: local_env.root,
        )
        monkeypatch.setattr(
            "infracontext.paths.require_environment_root",
            lambda: local_env.root,
        )

        target = _resolve_node_target("@fleet:vm:writable-vm", require_writable=True)
        assert target.writable is True

    def test_node_find_all_roots_finds_external(self, federated, capsys):
        """`ic describe node find -A` must reach into external roots and
        report matches using qualified IDs."""
        from infracontext.cli.describe import node_find

        # Typer normally injects args; call the inner function directly.
        node_find("pve-01", show_all=False, all_roots_flag=True)
        out = capsys.readouterr().out
        assert "@fleet:physical_host:pve-01" in out

    def test_node_find_default_stays_local(self, federated, capsys):
        """Without --all-roots, the search must NOT reach external roots."""
        from infracontext.cli.describe import node_find

        node_find("pve-01", show_all=False, all_roots_flag=False)
        out = capsys.readouterr().out
        assert "No nodes found" in out

    def test_node_find_all_roots_restricts_to_active_project(self, tmp_path, monkeypatch, capsys):
        """External-root scope is the root's active project only.

        Regression for the addressing-bug Codex caught: emitting an ID for a
        non-active project (where `@alias:type:slug` cannot reach) would let
        users paste it into `node show/context` and silently address the
        wrong node. We instead only surface matches from the addressable
        space — the root's active project.
        """
        from infracontext.cli.describe import node_find

        # Local root.
        local = tmp_path / "local"
        (local / INFRACONTEXT_DIR).mkdir(parents=True)
        local_env = EnvironmentPaths.from_root(local)
        ProjectPaths.for_project("prod", local_env).ensure_dirs()

        # Fleet root with TWO projects, the same slug in each, but different
        # node names. Only the active project's match should appear.
        fleet = tmp_path / "fleet"
        (fleet / INFRACONTEXT_DIR).mkdir(parents=True)
        fleet_env = EnvironmentPaths.from_root(fleet)
        save_config(AppConfig(active_project="default"), fleet_env)

        active_proj = ProjectPaths.for_project("default", fleet_env)
        active_proj.ensure_dirs()
        active_proj.node_type_dir("physical_host").mkdir(parents=True, exist_ok=True)
        write_model(
            active_proj.node_file("physical_host", "pve-01"),
            Node(
                id="physical_host:pve-01",
                slug="pve-01",
                type=NodeType.PHYSICAL_HOST,
                name="PVE-01 ACTIVE",
            ),
        )

        inactive_proj = ProjectPaths.for_project("dr-site", fleet_env)
        inactive_proj.ensure_dirs()
        inactive_proj.node_type_dir("physical_host").mkdir(parents=True, exist_ok=True)
        write_model(
            inactive_proj.node_file("physical_host", "pve-01"),
            Node(
                id="physical_host:pve-01",
                slug="pve-01",
                type=NodeType.PHYSICAL_HOST,
                name="PVE-01 DR-SITE",
            ),
        )

        save_config(
            AppConfig(
                active_project="prod",
                external_roots=[ExternalRoot(alias="fleet", path="../fleet")],
            ),
            local_env,
        )

        monkeypatch.setattr(
            "infracontext.paths.find_environment_root",
            lambda start=None: local_env.root,
        )
        monkeypatch.setattr(
            "infracontext.paths.require_environment_root",
            lambda: local_env.root,
        )

        node_find("pve-01", show_all=True, all_roots_flag=True)
        out = capsys.readouterr().out
        # The addressable match (active project) is shown.
        assert "@fleet:physical_host:pve-01" in out
        # The non-addressable match (dr-site) must NOT be advertised: paste-back
        # would silently resolve to the active-project node instead.
        assert "DR-SITE" not in out

    def test_node_find_default_emits_bare_local_id(self, tmp_path, monkeypatch, capsys):
        """Default `node find` (no -A) must emit bare IDs for local current-
        project matches even when an external root alias shares the project's
        name.

        Regression for the off-by-one Codex caught: search_targets is
        (alias, env, project, paths). A stale [0][1] read used the env as
        ``here_project``, which never compared equal to any string, so local
        hits got qualified as `@prod:vm:foo`. Federation then resolves @prod:
        as the external root, sending follow-up commands to the wrong root.
        ``ic doctor`` already flags the alias/project collision, but `find`
        must not produce paste-broken output before doctor runs.
        """
        from infracontext.cli.describe import node_find

        # Local root: project "prod" with vm:web-01.
        local = tmp_path / "local"
        (local / INFRACONTEXT_DIR).mkdir(parents=True)
        local_env = EnvironmentPaths.from_root(local)
        local_proj = ProjectPaths.for_project("prod", local_env)
        local_proj.ensure_dirs()
        local_proj.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(
            local_proj.node_file("vm", "web-01"),
            Node(id="vm:web-01", slug="web-01", type=NodeType.VM, name="Web LOCAL"),
        )

        # External root deliberately aliased "prod" (same as local project)
        # to provoke the resolution conflict.
        external = tmp_path / "external-prod"
        (external / INFRACONTEXT_DIR).mkdir(parents=True)
        external_env = EnvironmentPaths.from_root(external)
        save_config(AppConfig(active_project="default"), external_env)

        save_config(
            AppConfig(
                active_project="prod",
                external_roots=[ExternalRoot(alias="prod", path="../external-prod")],
            ),
            local_env,
        )

        monkeypatch.setattr(
            "infracontext.paths.find_environment_root",
            lambda start=None: local_env.root,
        )
        monkeypatch.setattr(
            "infracontext.paths.require_environment_root",
            lambda: local_env.root,
        )

        node_find("web-01", show_all=False, all_roots_flag=False)
        out = capsys.readouterr().out
        # Bare ID is the only form that round-trips cleanly to a follow-up
        # command in the local current project.
        assert "vm:web-01" in out
        # Qualified form here would resolve via federation to the external
        # root (alias 'prod' wins over local project name 'prod').
        assert "@prod:vm:web-01" not in out

    def test_node_context_uses_external_root_for_relationships(self, tmp_path, monkeypatch):
        """node_context for an external node must build dependencies from the
        external root's graph, not the local one.

        Regression for the silent-mix-up Codex caught: local and external
        roots can have a project of the same slug ("default" here) but
        unrelated relationships. The pre-fix code called
        ``load_graph(project)`` without the external environment, which
        defaults to the local root and silently merged the wrong
        dependencies into LLM-facing context.
        """
        from infracontext.cli.describe import _build_node_context

        # Local root: project "default" with vm:foo and vm:LOCAL-DB. The
        # local graph has a relationship vm:foo -> vm:LOCAL-DB.
        local = tmp_path / "local"
        (local / INFRACONTEXT_DIR).mkdir(parents=True)
        local_env = EnvironmentPaths.from_root(local)
        local_proj = ProjectPaths.for_project("default", local_env)
        local_proj.ensure_dirs()
        local_proj.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(
            local_proj.node_file("vm", "foo"),
            Node(id="vm:foo", slug="foo", type=NodeType.VM, name="Foo LOCAL"),
        )
        write_model(
            local_proj.node_file("vm", "local-db"),
            Node(id="vm:local-db", slug="local-db", type=NodeType.VM, name="LOCAL DB"),
        )
        write_model(
            local_proj.relationships_yaml,
            RelationshipFile(
                relationships=[
                    Relationship(
                        source="vm:foo",
                        target="vm:local-db",
                        type=RelationshipType.DEPENDS_ON,
                    )
                ]
            ),
        )

        # Fleet root: also project "default" with its own vm:foo pointing
        # at vm:FLEET-DB. Different dependencies entirely.
        fleet = tmp_path / "fleet"
        (fleet / INFRACONTEXT_DIR).mkdir(parents=True)
        fleet_env = EnvironmentPaths.from_root(fleet)
        save_config(AppConfig(active_project="default"), fleet_env)
        fleet_proj = ProjectPaths.for_project("default", fleet_env)
        fleet_proj.ensure_dirs()
        fleet_proj.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        fleet_foo = Node(id="vm:foo", slug="foo", type=NodeType.VM, name="Foo FLEET")
        write_model(fleet_proj.node_file("vm", "foo"), fleet_foo)
        write_model(
            fleet_proj.node_file("vm", "fleet-db"),
            Node(id="vm:fleet-db", slug="fleet-db", type=NodeType.VM, name="FLEET DB"),
        )
        write_model(
            fleet_proj.relationships_yaml,
            RelationshipFile(
                relationships=[
                    Relationship(
                        source="vm:foo",
                        target="vm:fleet-db",
                        type=RelationshipType.DEPENDS_ON,
                    )
                ]
            ),
        )

        save_config(
            AppConfig(
                active_project="default",
                external_roots=[ExternalRoot(alias="fleet", path="../fleet")],
            ),
            local_env,
        )

        monkeypatch.setattr(
            "infracontext.paths.find_environment_root",
            lambda start=None: local_env.root,
        )
        monkeypatch.setattr(
            "infracontext.paths.require_environment_root",
            lambda: local_env.root,
        )

        # Build context for the *external* vm:foo. Dependencies must come
        # from the fleet root only.
        ctx = _build_node_context(
            fleet_foo,
            project="default",
            include_relationships=True,
            include_learnings=False,
            environment=fleet_env,
            root_alias="fleet",
        )
        deps = ctx.get("dependencies", {})
        upstream_ids = {d["id"] for d in deps.get("depends_on", [])}
        # External fleet-db must be in the upstream set.
        assert "vm:fleet-db" in upstream_ids
        # Local LOCAL-DB must NOT leak in. Pre-fix this would be present
        # because load_graph defaulted to the local root.
        assert "vm:local-db" not in upstream_ids
