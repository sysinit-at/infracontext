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
