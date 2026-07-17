"""Import-time duplicate detection: helper guards + importer warning emission.

Detection is warn-only: importers still create the node, they never
auto-attach across source boundaries. Guards: loopback IPs are ignored, and
so is any identifier already shared by >1 existing node (floating IP / VIP /
shared jump alias -- shared identity never implies same box).
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from infracontext.cli.import_cmd import app as import_app
from infracontext.models.node import Node, NodeType
from infracontext.sources.base import SyncStatus
from infracontext.sources.dedup import (
    IdentifierOverlap,
    find_duplicate_candidates,
    load_existing_nodes,
    overlap_warning,
)
from infracontext.sources.proxmox import ProxmoxSource
from infracontext.sources.proxmox import SyncStats as ProxmoxSyncStats
from infracontext.sources.ssh_config import SSHConfigSource
from infracontext.storage import write_model, write_yaml

runner = CliRunner()


def _node(node_id: str, **kwargs) -> Node:
    node_type, slug = node_id.split(":", 1)
    return Node(id=node_id, slug=slug, type=NodeType(node_type), name=kwargs.pop("name", slug), **kwargs)


# ── find_duplicate_candidates ─────────────────────────────────────


class TestFindDuplicateCandidates:
    def test_overlap_on_ip(self):
        existing = [_node("vm:web-prod", ip_addresses=["10.0.0.5"])]
        overlaps = find_duplicate_candidates(existing, ips=["10.0.0.5"])
        assert overlaps == [IdentifierOverlap(existing_id="vm:web-prod", identifier="10.0.0.5", kind="ip")]

    def test_overlap_on_domain(self):
        existing = [_node("vm:web-prod", domains=["web.example.com"])]
        overlaps = find_duplicate_candidates(existing, domains=["web.example.com"])
        assert overlaps[0].kind == "domain"
        assert overlaps[0].existing_id == "vm:web-prod"

    def test_overlap_on_ssh_alias(self):
        existing = [_node("vm:web-prod", ssh_alias="web-prod")]
        overlaps = find_duplicate_candidates(existing, ssh_alias="web-prod")
        assert overlaps[0].kind == "ssh_alias"

    def test_no_overlap_for_unknown_identifiers(self):
        existing = [_node("vm:web-prod", ip_addresses=["10.0.0.5"])]
        assert find_duplicate_candidates(existing, ips=["10.9.9.9"], domains=["x.y"], ssh_alias="z") == []

    def test_loopback_ips_ignored(self):
        existing = [_node("vm:web-prod", ip_addresses=["127.0.0.1", "::1"])]
        assert find_duplicate_candidates(existing, ips=["127.0.0.1", "::1"]) == []

    def test_loopback_range_ignored(self):
        # Anything in 127.0.0.0/8, not just 127.0.0.1.
        existing = [_node("vm:web-prod", ip_addresses=["127.1.2.3"])]
        assert find_duplicate_candidates(existing, ips=["127.1.2.3"]) == []

    def test_shared_identifier_ignored(self):
        # A floating IP on two existing nodes never implies "same box".
        existing = [
            _node("vm:web-a", ip_addresses=["10.0.0.5"]),
            _node("vm:web-b", ip_addresses=["10.0.0.5"]),
        ]
        assert find_duplicate_candidates(existing, ips=["10.0.0.5"]) == []

    def test_shared_ssh_alias_ignored(self):
        existing = [
            _node("vm:jump-a", ssh_alias="jump"),
            _node("vm:jump-b", ssh_alias="jump"),
        ]
        assert find_duplicate_candidates(existing, ssh_alias="jump") == []

    def test_multiple_identifiers_yield_one_overlap_each(self):
        existing = [_node("vm:web-prod", ip_addresses=["10.0.0.5"], domains=["web.example.com"])]
        overlaps = find_duplicate_candidates(
            existing, ips=["10.0.0.5"], domains=["web.example.com"]
        )
        assert len(overlaps) == 2
        assert {o.kind for o in overlaps} == {"ip", "domain"}

    def test_empty_identifiers_ignored(self):
        existing = [_node("vm:web-prod", ip_addresses=["10.0.0.5"])]
        assert find_duplicate_candidates(existing, ips=[""], domains=[""], ssh_alias=None) == []

    def test_warning_text_carries_consolidate_hint(self):
        overlap = IdentifierOverlap(existing_id="vm:web-prod", identifier="10.0.0.5", kind="ip")
        text = overlap_warning("vm:new-box", overlap)
        assert "incoming 'vm:new-box' overlaps vm:web-prod on 10.0.0.5 (ip)" in text
        assert "ic describe node consolidate vm:web-prod vm:new-box" in text


# ── load_existing_nodes ───────────────────────────────────────────


class TestLoadExistingNodes:
    def test_loads_all_nodes(self, tmp_project):
        write_model(tmp_project.node_file("vm", "a"), _node("vm:a"))
        write_model(tmp_project.node_file("physical_host", "b"), _node("physical_host:b"))
        assert {n.id for n in load_existing_nodes(tmp_project)} == {"vm:a", "physical_host:b"}

    def test_missing_nodes_dir_is_empty(self, tmp_environment):
        from infracontext.paths import ProjectPaths

        paths = ProjectPaths.for_project("emptyproject", tmp_environment)
        assert load_existing_nodes(paths) == []

    def test_broken_file_skipped(self, tmp_project):
        write_model(tmp_project.node_file("vm", "a"), _node("vm:a"))
        tmp_project.node_file("vm", "broken").write_text("id: [not: valid: {")
        assert [n.id for n in load_existing_nodes(tmp_project)] == ["vm:a"]


# ── ssh-config sync emission ──────────────────────────────────────


@pytest.fixture()
def ssh_env(tmp_project, monkeypatch_environment, tmp_path):
    """Patched environment plus a helper to (re)write the SSH source config."""
    ssh_cfg = tmp_path / "ssh-config-file"

    def _configure(text: str, name: str = "ssh-test"):
        ssh_cfg.write_text(text)
        write_yaml(
            tmp_project.source_file(name),
            {
                "version": "2.0",
                "name": name,
                "type": "ssh_config",
                "status": "configured",
                "config_path": str(ssh_cfg),
            },
        )

    return tmp_project, _configure


class TestSSHConfigSyncDuplicateWarning:
    def test_created_node_overlapping_existing_ip_warns(self, ssh_env):
        project, configure = ssh_env
        write_model(project.node_file("vm", "web-prod"), _node("vm:web-prod", ip_addresses=["10.0.0.5"]))
        configure("Host new-box\n    HostName 10.0.0.5\n")

        result = SSHConfigSource().sync("testproject", "ssh-test")

        assert result.status is SyncStatus.SUCCESS
        assert len(result.warnings) == 1
        assert "incoming 'vm:new-box' overlaps vm:web-prod on 10.0.0.5" in result.warnings[0]
        assert "ic describe node consolidate vm:web-prod vm:new-box" in result.warnings[0]
        # Detection only: the node is still created.
        assert project.node_file("vm", "new-box").exists()

    def test_shared_ip_across_existing_nodes_stays_silent(self, ssh_env):
        project, configure = ssh_env
        write_model(project.node_file("vm", "web-a"), _node("vm:web-a", ip_addresses=["10.0.0.5"]))
        write_model(project.node_file("vm", "web-b"), _node("vm:web-b", ip_addresses=["10.0.0.5"]))
        configure("Host new-box\n    HostName 10.0.0.5\n")

        result = SSHConfigSource().sync("testproject", "ssh-test")

        assert result.warnings == []
        assert project.node_file("vm", "new-box").exists()

    def test_loopback_hostname_stays_silent(self, ssh_env):
        project, configure = ssh_env
        write_model(project.node_file("vm", "local-a"), _node("vm:local-a", ip_addresses=["127.0.0.1"]))
        configure("Host local-b\n    HostName 127.0.0.1\n")

        result = SSHConfigSource().sync("testproject", "ssh-test")

        assert result.warnings == []

    def test_resync_update_does_not_warn(self, ssh_env):
        _, configure = ssh_env
        configure("Host web-01\n    HostName 10.0.0.1\n")
        plugin = SSHConfigSource()
        assert plugin.sync("testproject", "ssh-test").warnings == []

        # Second run updates/confirms the same node -> no creation, no warning.
        result = plugin.sync("testproject", "ssh-test")
        assert result.warnings == []


# ── proxmox plan emission ─────────────────────────────────────────


class TestProxmoxPlanNodeDuplicateWarning:
    def _plugin_with(self, existing: list[Node]) -> ProxmoxSource:
        plugin = ProxmoxSource()
        plugin._source_id_index = {}
        plugin._existing_nodes = existing
        return plugin

    def test_created_node_overlapping_existing_ip_warns(self, tmp_project):
        plugin = self._plugin_with([_node("vm:web-prod", ip_addresses=["10.0.0.9"])])
        stats = ProxmoxSyncStats()
        incoming = _node("vm:vm-100", source_id="proxmox:c1:qemu:100", ip_addresses=["10.0.0.9"])

        planned: list = []
        merged = plugin._plan_node(tmp_project, incoming, stats, "vms", planned)

        assert merged is not None
        assert len(stats.warnings) == 1
        assert "overlaps vm:web-prod on 10.0.0.9" in stats.warnings[0]
        assert len(planned) == 1  # detection only, the write is still planned

    def test_update_of_existing_node_does_not_warn(self, tmp_project):
        existing = _node("vm:vm-100", source_id="proxmox:c1:qemu:100", ip_addresses=["10.0.0.9"])
        write_model(tmp_project.node_file("vm", "vm-100"), existing)
        plugin = self._plugin_with([existing])
        stats = ProxmoxSyncStats()
        incoming = _node("vm:vm-100", source_id="proxmox:c1:qemu:100", ip_addresses=["10.0.0.9"])

        plugin._plan_node(tmp_project, incoming, stats, "vms", planned=[])

        assert stats.warnings == []


# ── kubectl / sos import emission ─────────────────────────────────


class TestKubectlImportDuplicateWarning:
    def test_new_k8s_node_overlapping_existing_ip_warns(self, tmp_project, monkeypatch_environment, monkeypatch):
        monkeypatch.setenv("IC_PROJECT", "testproject")
        write_model(
            tmp_project.node_file("vm", "web-prod"),
            _node("vm:web-prod", ip_addresses=["10.0.0.9"]),
        )

        nodes_json = json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "worker-1", "labels": {}},
                        "status": {
                            "addresses": [{"type": "InternalIP", "address": "10.0.0.9"}],
                            "capacity": {},
                            "nodeInfo": {},
                            "conditions": [],
                        },
                    }
                ]
            }
        )

        def fake_run_cmd(cmd: list[str], description: str) -> str | None:  # noqa: ARG001
            if "current-context" in cmd:
                return "test-ctx\n"
            if "nodes" in cmd:
                return nodes_json
            if "version" in cmd:
                return "{}"
            return None

        monkeypatch.setattr("infracontext.cli.import_cmd._run_cmd", fake_run_cmd)

        result = runner.invoke(import_app, ["kubectl"])

        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "overlaps vm:web-prod on 10.0.0.9" in flat
        assert "ic describe node consolidate vm:web-prod k8s_node:worker-1" in flat
        # Detection only: the node is still created.
        assert tmp_project.node_file("k8s_node", "worker-1").exists()


class TestSosImportDuplicateWarning:
    def test_new_node_overlapping_existing_domain_warns(
        self, tmp_project, monkeypatch_environment, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("IC_PROJECT", "testproject")
        write_model(
            tmp_project.node_file("vm", "web-prod"),
            _node("vm:web-prod", domains=["shared.example.com"]),
        )

        health_json = json.dumps(
            {
                "system": {"hostname": "shared.example.com", "os": "Debian 13", "kernel": "6.12"},
                "findings": [],
            }
        )

        def fake_run_cmd(cmd: list[str], description: str) -> str | None:  # noqa: ARG001
            return health_json

        monkeypatch.setattr("infracontext.cli.import_cmd._run_cmd", fake_run_cmd)

        result = runner.invoke(import_app, ["sos", str(tmp_path)])

        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "overlaps vm:web-prod on shared.example.com (domain)" in flat
        assert "ic describe node consolidate vm:web-prod vm:shared-example-com" in flat
        assert tmp_project.node_file("vm", "shared-example-com").exists()
