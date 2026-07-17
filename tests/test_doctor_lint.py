"""Lint-tier doctor checks.

Covers the four checks added on top of the schema/orphan validation:

- Constraint re-validation: RELATIONSHIP_CONSTRAINTS is enforced at create
  time only; doctor re-runs the matrix over stored YAML -> WARNING.
- Duplicate identifiers: ssh_alias within a project -> WARNING (breaks
  'ic ssh' fuzzy resolution), across projects -> INFO; duplicate IP within
  a project -> WARNING.
- Application coverage: compute/service nodes not reachable from any
  application via contains/depends_on/uses -> INFO ('ungrouped').
- Blank learnings: whitespace-only context/finding -> INFO.

All of these are WARNING/INFO only: a repo that exited 0 before must keep
exiting 0 (doctor exits 1 on errors only).
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from infracontext.cli.doctor import Severity, run_doctor
from infracontext.cli.doctor import app as doctor_app
from infracontext.config import AppConfig, ExternalRoot, save_config
from infracontext.models.node import Learning, Node, NodeType
from infracontext.models.relationship import Relationship, RelationshipFile, RelationshipType
from infracontext.paths import INFRACONTEXT_DIR, EnvironmentPaths, ProjectPaths
from infracontext.storage import write_model

runner = CliRunner()


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


def _make_node(paths: ProjectPaths, node_type: NodeType, slug: str, **kwargs) -> Node:
    node = Node(id=f"{node_type}:{slug}", slug=slug, type=node_type, name=slug, **kwargs)
    paths.node_type_dir(str(node_type)).mkdir(parents=True, exist_ok=True)
    write_model(paths.node_file(str(node_type), slug), node)
    return node


def _write_relationships(paths: ProjectPaths, relationships: list[Relationship]) -> None:
    write_model(paths.relationships_yaml, RelationshipFile(relationships=relationships))


def _issues(report, category: str, severity: Severity | None = None):
    return [
        i
        for i in report.issues
        if i.category == category and (severity is None or i.severity == severity)
    ]


# ── (a) Constraint re-validation ───────────────────────────────────


class TestConstraintRevalidation:
    def test_invalid_type_for_known_pair_warned(self, env_at):
        proj = _project(env_at)
        _make_node(proj, NodeType.DOMAIN, "example")
        _make_node(proj, NodeType.VM, "web")
        # (domain, vm) only allows resolves_to.
        _write_relationships(
            proj,
            [Relationship(source="domain:example", target="vm:web", type=RelationshipType.RUNS_ON)],
        )

        report = run_doctor(env_at)

        constraint = _issues(report, "constraint", Severity.WARNING)
        assert len(constraint) == 1
        assert "resolves_to" in constraint[0].message
        assert "RELATIONSHIP_CONSTRAINTS" in constraint[0].suggestion
        assert not report.has_errors  # lint never flips the exit code

    def test_pair_absent_from_matrix_warned(self, env_at):
        proj = _project(env_at)
        _make_node(proj, NodeType.VM, "web")
        _make_node(proj, NodeType.DOMAIN, "example")
        # (vm, domain) has no entry in the matrix at all.
        _write_relationships(
            proj,
            [Relationship(source="vm:web", target="domain:example", type=RelationshipType.CONTAINS)],
        )

        report = run_doctor(env_at)

        constraint = _issues(report, "constraint", Severity.WARNING)
        assert len(constraint) == 1
        assert "no relationship types are defined" in constraint[0].message
        assert not report.has_errors

    def test_valid_relationships_are_clean(self, env_at):
        proj = _project(env_at)
        _make_node(proj, NodeType.DOMAIN, "example")
        _make_node(proj, NodeType.VM, "web")
        _make_node(proj, NodeType.PHYSICAL_HOST, "pve-01")
        _write_relationships(
            proj,
            [
                Relationship(source="domain:example", target="vm:web", type=RelationshipType.RESOLVES_TO),
                Relationship(source="vm:web", target="physical_host:pve-01", type=RelationshipType.RUNS_ON),
            ],
        )

        report = run_doctor(env_at)
        assert not _issues(report, "constraint")
        assert not report.has_errors

    def test_unresolved_endpoint_skipped(self, env_at):
        proj = _project(env_at)
        _make_node(proj, NodeType.VM, "web")
        # Target doesn't exist: the orphan check reports it; the constraint
        # check must stay silent rather than pile on.
        _write_relationships(
            proj,
            [Relationship(source="vm:web", target="vm:ghost", type=RelationshipType.CONTAINS)],
        )

        report = run_doctor(env_at)

        assert not _issues(report, "constraint")
        assert _issues(report, "orphan", Severity.ERROR)

    def test_unresolvable_external_root_skipped(self, env_at):
        proj = _project(env_at)
        _make_node(proj, NodeType.VM, "web")
        # '@fleet' is not a configured external root and not a local project:
        # the endpoint type can't be resolved -> skip silently.
        _write_relationships(
            proj,
            [
                Relationship(
                    source="vm:web",
                    target="@fleet:physical_host:pve-01",
                    type=RelationshipType.CONTAINS,
                )
            ],
        )

        report = run_doctor(env_at)
        assert not _issues(report, "constraint")

    def test_cross_root_endpoint_resolved_and_checked(self, tmp_path, monkeypatch):
        local = tmp_path / "local"
        (local / INFRACONTEXT_DIR / "projects").mkdir(parents=True)
        local_env = EnvironmentPaths.from_root(local)
        monkeypatch.setattr(
            "infracontext.paths.find_environment_root",
            lambda start=None: local_env.root,  # noqa: ARG005
        )
        monkeypatch.setattr("infracontext.paths.require_environment_root", lambda: local_env.root)

        fleet = tmp_path / "fleet"
        (fleet / INFRACONTEXT_DIR / "projects").mkdir(parents=True)
        fleet_env = EnvironmentPaths.from_root(fleet)
        save_config(AppConfig(active_project="default"), fleet_env)
        _make_node(_project(fleet_env, "default"), NodeType.PHYSICAL_HOST, "pve-01")

        save_config(
            AppConfig(
                active_project="prod",
                external_roots=[ExternalRoot(alias="fleet", path="../fleet")],
            ),
            local_env,
        )
        proj = _project(local_env)
        _make_node(proj, NodeType.VM, "web")
        # (vm, physical_host) allows runs_on/hosted_by -- contains is invalid.
        _write_relationships(
            proj,
            [
                Relationship(
                    source="vm:web",
                    target="@fleet:physical_host:pve-01",
                    type=RelationshipType.CONTAINS,
                )
            ],
        )

        report = run_doctor(local_env)

        constraint = _issues(report, "constraint", Severity.WARNING)
        assert len(constraint) == 1
        assert "runs_on" in constraint[0].message
        assert not report.has_errors


# ── (b) Duplicate identifiers ──────────────────────────────────────


class TestDuplicateIdentifiers:
    def test_duplicate_ssh_alias_within_project_warned(self, env_at):
        proj = _project(env_at)
        _make_node(proj, NodeType.VM, "web-01", ssh_alias="web-prod")
        _make_node(proj, NodeType.VM, "web-02", ssh_alias="web-prod")

        report = run_doctor(env_at)

        dupes = _issues(report, "duplicate", Severity.WARNING)
        assert len(dupes) == 1
        assert "web-prod" in dupes[0].message
        assert "ambiguous" in dupes[0].message  # breaks ic ssh fuzzy resolution
        assert not report.has_errors

    def test_duplicate_ip_within_project_warned_with_vip_hint(self, env_at):
        proj = _project(env_at)
        _make_node(proj, NodeType.VM, "web-01", ssh_alias="a", ip_addresses=["10.0.0.5"])
        _make_node(proj, NodeType.VM, "web-02", ssh_alias="b", ip_addresses=["10.0.0.5", "10.0.0.6"])

        report = run_doctor(env_at)

        dupes = _issues(report, "duplicate", Severity.WARNING)
        assert len(dupes) == 1
        assert "10.0.0.5" in dupes[0].message
        assert "VIP" in dupes[0].suggestion
        assert not report.has_errors

    def test_same_alias_across_projects_is_info(self, env_at):
        _make_node(_project(env_at, "prod"), NodeType.VM, "web", ssh_alias="web-host")
        _make_node(_project(env_at, "dev"), NodeType.VM, "web", ssh_alias="web-host")

        report = run_doctor(env_at)

        assert not _issues(report, "duplicate", Severity.WARNING)
        infos = _issues(report, "duplicate", Severity.INFO)
        assert len(infos) == 1
        assert "web-host" in infos[0].message
        assert "2 projects" in infos[0].message
        assert not report.has_errors

    def test_unique_identifiers_are_clean(self, env_at):
        proj = _project(env_at)
        _make_node(proj, NodeType.VM, "web-01", ssh_alias="web-1", ip_addresses=["10.0.0.5"])
        _make_node(proj, NodeType.VM, "web-02", ssh_alias="web-2", ip_addresses=["10.0.0.6"])

        report = run_doctor(env_at)
        assert not _issues(report, "duplicate")


# ── (c) Application coverage ───────────────────────────────────────


class TestApplicationCoverage:
    def test_unreached_compute_node_flagged(self, env_at):
        proj = _project(env_at)
        _make_node(proj, NodeType.APPLICATION, "shop")
        _make_node(proj, NodeType.SERVICE, "api")
        _make_node(proj, NodeType.VM, "web", ssh_alias="web")
        _write_relationships(
            proj,
            [
                Relationship(source="application:shop", target="service:api", type=RelationshipType.CONTAINS),
                # runs_on is not a coverage edge, so vm:web stays ungrouped.
                Relationship(source="service:api", target="vm:web", type=RelationshipType.RUNS_ON),
            ],
        )

        report = run_doctor(env_at)

        ungrouped = _issues(report, "ungrouped", Severity.INFO)
        assert [i for i in ungrouped if "vm:web" in i.message]
        assert not [i for i in ungrouped if "service:api" in i.message]  # reached via contains
        assert "orphans" in ungrouped[0].suggestion  # labeled distinct from ic graph orphans
        assert not report.has_errors

    def test_transitively_grouped_nodes_not_flagged(self, env_at):
        proj = _project(env_at)
        _make_node(proj, NodeType.APPLICATION, "shop")
        _make_node(proj, NodeType.SERVICE, "api")
        _make_node(proj, NodeType.SERVICE, "db")
        _write_relationships(
            proj,
            [
                Relationship(source="application:shop", target="service:api", type=RelationshipType.CONTAINS),
                Relationship(source="service:api", target="service:db", type=RelationshipType.DEPENDS_ON),
            ],
        )

        report = run_doctor(env_at)
        assert not _issues(report, "ungrouped")

    def test_skipped_entirely_without_application_nodes(self, env_at):
        proj = _project(env_at)
        _make_node(proj, NodeType.VM, "web-01", ssh_alias="a")
        _make_node(proj, NodeType.VM, "web-02", ssh_alias="b")

        report = run_doctor(env_at)
        assert not _issues(report, "ungrouped")


# ── (d) Blank learnings ────────────────────────────────────────────


class TestBlankLearnings:
    def test_whitespace_only_context_flagged(self, env_at):
        proj = _project(env_at)
        _make_node(
            proj,
            NodeType.VM,
            "web",
            ssh_alias="web",
            learnings=[Learning(date="2026-01-01", context="   ", finding="pool misconfigured")],
        )

        report = run_doctor(env_at)

        blanks = _issues(report, "blank_learning", Severity.INFO)
        assert len(blanks) == 1
        assert "context" in blanks[0].message
        assert not report.has_errors

    def test_empty_finding_flagged(self, env_at):
        proj = _project(env_at)
        _make_node(
            proj,
            NodeType.VM,
            "web",
            ssh_alias="web",
            learnings=[Learning(date="2026-01-01", context="cpu", finding="")],
        )

        report = run_doctor(env_at)

        blanks = _issues(report, "blank_learning", Severity.INFO)
        assert len(blanks) == 1
        assert "finding" in blanks[0].message

    def test_filled_learnings_are_clean(self, env_at):
        proj = _project(env_at)
        _make_node(
            proj,
            NodeType.VM,
            "web",
            ssh_alias="web",
            learnings=[Learning(date="2026-01-01", context="cpu", finding="pool misconfigured")],
        )

        report = run_doctor(env_at)
        assert not _issues(report, "blank_learning")


# ── Exit-code stability across all lint checks ─────────────────────


class TestExitCodeStability:
    def test_all_lint_findings_together_still_exit_zero(self, env_at):
        """A repo tripping every lint check at once must not exit 1."""
        proj = _project(env_at)
        _make_node(proj, NodeType.APPLICATION, "shop")
        _make_node(proj, NodeType.DOMAIN, "example")
        _make_node(
            proj,
            NodeType.VM,
            "web-01",
            ssh_alias="web",
            ip_addresses=["10.0.0.5"],
            learnings=[Learning(date="2026-01-01", context=" ", finding="x")],
        )
        _make_node(proj, NodeType.VM, "web-02", ssh_alias="web", ip_addresses=["10.0.0.5"])
        _write_relationships(
            proj,
            [Relationship(source="domain:example", target="vm:web-01", type=RelationshipType.RUNS_ON)],
        )

        report = run_doctor(env_at)
        for category in ("constraint", "duplicate", "ungrouped", "blank_learning"):
            assert _issues(report, category), f"expected {category} findings"
        assert not report.has_errors

        result = runner.invoke(doctor_app, [])
        assert result.exit_code == 0, result.output
