"""Tests for infracontext.runs — run records, pruning, presence classification,
and the doctor presence warning built on top of them."""

from __future__ import annotations

import pytest

from infracontext.cli.doctor import Severity, run_doctor
from infracontext.models.node import Node, NodeType
from infracontext.paths import INFRACONTEXT_DIR, EnvironmentPaths, ProjectPaths
from infracontext.runs import (
    KEEP_RUNS_PER_SOURCE,
    REMOVAL_GRACE_SYNCS,
    NodePresence,
    Presence,
    RunRecord,
    classify_presence,
    load_run_records,
    runs_dir,
    write_run_record,
)
from infracontext.storage import write_model


def _ts(n: int) -> str:
    """Deterministic increasing ISO timestamps for test records."""
    return f"2026-07-{(n // 86400) + 1:02d}T{(n // 3600) % 24:02d}:{(n // 60) % 60:02d}:{n % 60:02d}Z"


# ── Run record write + prune ──────────────────────────────────────


class TestWriteRunRecord:
    def test_writes_record_with_expected_fields(self, tmp_environment):
        path = write_run_record(
            tmp_environment,
            project="prod",
            source="ssh-config",
            status="success",
            created=["vm:web-01"],
            updated=["vm:db-01"],
            confirmed_unchanged=["vm:cache-01"],
            timestamp="2026-07-16T10:15:30Z",
        )
        assert path.parent == runs_dir(tmp_environment)
        assert path.name == "20260716T101530Z-ssh-config.yaml"

        records = load_run_records(tmp_environment, project="prod", source="ssh-config")
        assert len(records) == 1
        record = records[0]
        assert record.timestamp == "2026-07-16T10:15:30Z"
        assert record.ic_version  # stamped with the running version
        assert record.status == "success"
        assert record.created == ["vm:web-01"]
        assert record.updated == ["vm:db-01"]
        assert record.confirmed_unchanged == ["vm:cache-01"]
        assert record.seen_node_ids == {"vm:web-01", "vm:db-01", "vm:cache-01"}

    def test_same_timestamp_does_not_clobber(self, tmp_environment):
        for _ in range(2):
            write_run_record(
                tmp_environment,
                project="prod",
                source="ssh-config",
                status="success",
                created=["vm:web-01"],
                timestamp="2026-07-16T10:15:30Z",
            )
        assert len(load_run_records(tmp_environment, project="prod", source="ssh-config")) == 2

    def test_prunes_to_keep_limit_per_source(self, tmp_environment):
        for n in range(KEEP_RUNS_PER_SOURCE + 5):
            write_run_record(
                tmp_environment,
                project="prod",
                source="ssh-config",
                status="success",
                created=[f"vm:web-{n:02d}"],
                timestamp=_ts(n),
            )
        records = load_run_records(tmp_environment, project="prod", source="ssh-config")
        assert len(records) == KEEP_RUNS_PER_SOURCE
        # Newest first; the oldest 5 records were pruned.
        assert records[0].created == [f"vm:web-{KEEP_RUNS_PER_SOURCE + 4:02d}"]
        assert records[-1].created == ["vm:web-05"]

    def test_prune_is_scoped_to_project_and_source(self, tmp_environment):
        for n in range(KEEP_RUNS_PER_SOURCE + 3):
            write_run_record(
                tmp_environment, project="prod", source="ssh-config", status="success",
                created=["vm:a"], timestamp=_ts(n),
            )
        # Other source and other project must be untouched by the pruning above.
        write_run_record(
            tmp_environment, project="prod", source="proxmox-prod", status="success",
            created=["vm:b"], timestamp=_ts(0),
        )
        write_run_record(
            tmp_environment, project="staging", source="ssh-config", status="success",
            created=["vm:c"], timestamp=_ts(0),
        )
        assert len(load_run_records(tmp_environment, project="prod", source="proxmox-prod")) == 1
        assert len(load_run_records(tmp_environment, project="staging", source="ssh-config")) == 1

    def test_load_returns_newest_first_and_skips_foreign_files(self, tmp_environment):
        directory = runs_dir(tmp_environment)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "not-a-record.yaml").write_text("just: junk\n")
        write_run_record(
            tmp_environment, project="prod", source="s", status="success",
            created=["vm:old"], timestamp=_ts(1),
        )
        write_run_record(
            tmp_environment, project="prod", source="s", status="success",
            created=["vm:new"], timestamp=_ts(2),
        )
        records = load_run_records(tmp_environment, project="prod", source="s")
        assert [r.created for r in records] == [["vm:new"], ["vm:old"]]


# ── Presence classification ───────────────────────────────────────


def _record(n: int, seen: list[str], status: str = "success") -> RunRecord:
    return RunRecord(
        timestamp=_ts(n), source="s", project="p", status=status, confirmed_unchanged=seen,
    )


class TestClassifyPresence:
    def test_seen_in_latest_run_is_present(self):
        records = [_record(2, ["vm:web-01"]), _record(1, [])]  # newest first
        result = classify_presence(["vm:web-01"], records)
        assert result["vm:web-01"] == NodePresence(Presence.PRESENT, 0)

    def test_absent_within_grace_is_possibly_missing(self):
        for misses in range(1, REMOVAL_GRACE_SYNCS + 1):
            records = [_record(10 - i, ["vm:other"]) for i in range(misses)]
            records.append(_record(0, ["vm:web-01"]))
            result = classify_presence(["vm:web-01"], records)
            assert result["vm:web-01"] == NodePresence(Presence.POSSIBLY_MISSING, misses)

    def test_absent_beyond_grace_is_missing(self):
        records = [_record(10 - i, ["vm:other"]) for i in range(REMOVAL_GRACE_SYNCS + 1)]
        records.append(_record(0, ["vm:web-01"]))
        result = classify_presence(["vm:web-01"], records)
        assert result["vm:web-01"] == NodePresence(Presence.MISSING, REMOVAL_GRACE_SYNCS + 1)

    def test_never_seen_node_counts_all_runs_as_misses(self):
        records = [_record(10 - i, ["vm:other"]) for i in range(5)]
        result = classify_presence(["vm:gone"], records)
        assert result["vm:gone"].presence is Presence.MISSING
        assert result["vm:gone"].consecutive_misses == 5

    def test_failed_and_partial_runs_do_not_advance_presence(self):
        records = [
            _record(3, [], status="failed"),
            _record(2, [], status="partial"),
            _record(1, ["vm:web-01"]),
        ]
        result = classify_presence(["vm:web-01"], records)
        assert result["vm:web-01"].presence is Presence.PRESENT

    def test_empty_successful_run_is_ignored(self):
        # Zero nodes reported: far more likely a broken source than a vanished
        # fleet -- the empty run must not count as a missed observation.
        records = [_record(2, []), _record(1, ["vm:web-01"])]
        result = classify_presence(["vm:web-01"], records)
        assert result["vm:web-01"].presence is Presence.PRESENT

    def test_no_counting_runs_yields_no_classification(self):
        records = [_record(2, [], status="failed"), _record(1, [])]
        assert classify_presence(["vm:web-01"], records) == {}
        assert classify_presence(["vm:web-01"], []) == {}


# ── Doctor presence warnings ──────────────────────────────────────


@pytest.fixture()
def env_at(tmp_path, monkeypatch):
    """A temp environment wired so run_doctor's env discovery lands here."""
    (tmp_path / INFRACONTEXT_DIR).mkdir(parents=True)
    (tmp_path / INFRACONTEXT_DIR / "projects").mkdir()
    env = EnvironmentPaths.from_root(tmp_path)
    monkeypatch.setattr("infracontext.paths.find_environment_root", lambda start=None: env.root)  # noqa: ARG005
    monkeypatch.setattr("infracontext.paths.require_environment_root", lambda: env.root)
    return env


def _make_node(paths: ProjectPaths, slug: str, **kwargs) -> Node:
    node = Node(id=f"vm:{slug}", slug=slug, type=NodeType.VM, name=slug, **kwargs)
    paths.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
    write_model(paths.node_file("vm", slug), node)
    return node


def _presence_issues(report):
    return [i for i in report.issues if i.category == "presence"]


class TestDoctorPresence:
    def _successful_runs(self, env, *, count: int, seen: list[str], project="prod", source="ssh-test"):
        for n in range(count):
            write_run_record(
                env, project=project, source=source, status="success",
                confirmed_unchanged=seen, timestamp=_ts(n + 1),
            )

    def test_warns_for_managed_node_absent_from_recent_syncs(self, env_at):
        paths = ProjectPaths.for_project("prod", env_at)
        paths.ensure_dirs()
        _make_node(paths, "web-01", managed_by="ssh-test", source="ssh-test")
        self._successful_runs(env_at, count=4, seen=["vm:other"])

        report = run_doctor(env_at)
        issues = _presence_issues(report)
        assert len(issues) == 1
        assert issues[0].severity == Severity.WARNING
        assert "vm:web-01" in issues[0].message
        assert "ssh-test" in issues[0].message
        assert "4 syncs" in issues[0].message
        assert "missing" in issues[0].message

    def test_possibly_missing_within_grace_window(self, env_at):
        paths = ProjectPaths.for_project("prod", env_at)
        paths.ensure_dirs()
        _make_node(paths, "web-01", managed_by="ssh-test", source="ssh-test")
        self._successful_runs(env_at, count=2, seen=["vm:other"])

        report = run_doctor(env_at)
        issues = _presence_issues(report)
        assert len(issues) == 1
        assert "possibly-missing" in issues[0].message

    def test_present_node_does_not_warn(self, env_at):
        paths = ProjectPaths.for_project("prod", env_at)
        paths.ensure_dirs()
        _make_node(paths, "web-01", managed_by="ssh-test", source="ssh-test")
        self._successful_runs(env_at, count=4, seen=["vm:web-01"])

        assert _presence_issues(run_doctor(env_at)) == []

    def test_manual_nodes_never_warn(self, env_at):
        """Nodes without managed_by have no confirmation path -- never warn."""
        paths = ProjectPaths.for_project("prod", env_at)
        paths.ensure_dirs()
        _make_node(paths, "manual-01")  # no managed_by
        self._successful_runs(env_at, count=6, seen=["vm:other"])

        assert _presence_issues(run_doctor(env_at)) == []

    def test_managed_node_without_any_run_records_does_not_warn(self, env_at):
        paths = ProjectPaths.for_project("prod", env_at)
        paths.ensure_dirs()
        _make_node(paths, "web-01", managed_by="ssh-test", source="ssh-test")

        assert _presence_issues(run_doctor(env_at)) == []

    def test_failed_and_empty_runs_alone_do_not_warn(self, env_at):
        paths = ProjectPaths.for_project("prod", env_at)
        paths.ensure_dirs()
        _make_node(paths, "web-01", managed_by="ssh-test", source="ssh-test")
        write_run_record(env_at, project="prod", source="ssh-test", status="failed", timestamp=_ts(1))
        write_run_record(env_at, project="prod", source="ssh-test", status="success", timestamp=_ts(2))  # empty

        assert _presence_issues(run_doctor(env_at)) == []

    def test_records_from_other_project_are_not_evidence(self, env_at):
        paths = ProjectPaths.for_project("prod", env_at)
        paths.ensure_dirs()
        _make_node(paths, "web-01", managed_by="ssh-test", source="ssh-test")
        self._successful_runs(env_at, count=5, seen=["vm:other"], project="staging")

        assert _presence_issues(run_doctor(env_at)) == []
