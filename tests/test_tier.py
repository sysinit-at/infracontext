"""Tests for infracontext.tier — access tier computation and capabilities."""

from infracontext.models.node import Node, NodeType, TriageConfig
from infracontext.models.project import ProjectAccessConfig, ProjectConfig
from infracontext.models.tier import AccessTier
from infracontext.tier import (
    compute_effective_tier,
    get_cli_tier,
    get_collector_script,
    get_tier_capabilities,
)


def _node(tier: int | None = None) -> Node:
    """Helper: minimal node with optional triage tier."""
    triage = TriageConfig(tier=tier) if tier is not None else None
    return Node(id="vm:test", slug="test", type=NodeType.VM, name="Test", triage=triage)


def _project(default_tier: int = 2, max_tier: int = 3) -> ProjectConfig:
    return ProjectConfig(
        name="Test",
        slug="test",
        access=ProjectAccessConfig(
            default_tier=AccessTier(default_tier),
            max_tier=AccessTier(max_tier),
        ),
    )


# ── compute_effective_tier ────────────────────────────────────────


class TestComputeEffectiveTier:
    def test_project_default(self):
        tier = compute_effective_tier(_project(default_tier=2), _node())
        assert tier == AccessTier.UNPRIVILEGED

    def test_node_override_raises(self):
        tier = compute_effective_tier(_project(default_tier=2), _node(tier=3))
        assert tier == AccessTier.PRIVILEGED

    def test_node_override_lowers(self):
        tier = compute_effective_tier(_project(default_tier=3), _node(tier=1))
        assert tier == AccessTier.COLLECTOR

    def test_cli_restriction_lowers(self):
        tier = compute_effective_tier(_project(default_tier=3), _node(), cli_tier=AccessTier.COLLECTOR)
        assert tier == AccessTier.COLLECTOR

    def test_cli_restriction_cannot_elevate(self):
        """CLI tier is ignored if it would raise above current."""
        tier = compute_effective_tier(_project(default_tier=1), _node(), cli_tier=AccessTier.PRIVILEGED)
        assert tier == AccessTier.COLLECTOR

    def test_max_tier_ceiling(self):
        """Even with node override, max_tier caps the result."""
        tier = compute_effective_tier(_project(default_tier=2, max_tier=2), _node(tier=4))
        assert tier == AccessTier.UNPRIVILEGED

    def test_no_project_config(self):
        tier = compute_effective_tier(None, _node())
        assert tier == AccessTier.UNPRIVILEGED  # default when no config


# ── get_cli_tier ──────────────────────────────────────────────────


class TestGetCliTier:
    def test_name(self, monkeypatch):
        monkeypatch.setenv("IC_TIER", "collector")
        assert get_cli_tier() == AccessTier.COLLECTOR

    def test_name_uppercase(self, monkeypatch):
        monkeypatch.setenv("IC_TIER", "PRIVILEGED")
        assert get_cli_tier() == AccessTier.PRIVILEGED

    def test_name_with_dashes(self, monkeypatch):
        monkeypatch.setenv("IC_TIER", "local-only")
        assert get_cli_tier() == AccessTier.LOCAL_ONLY

    def test_number(self, monkeypatch):
        monkeypatch.setenv("IC_TIER", "3")
        assert get_cli_tier() == AccessTier.PRIVILEGED

    def test_empty(self, monkeypatch):
        monkeypatch.setenv("IC_TIER", "")
        assert get_cli_tier() is None

    def test_unset(self, monkeypatch):
        monkeypatch.delenv("IC_TIER", raising=False)
        assert get_cli_tier() is None

    def test_invalid(self, monkeypatch):
        monkeypatch.setenv("IC_TIER", "bogus")
        assert get_cli_tier() is None


# ── get_collector_script ──────────────────────────────────────────


class TestGetCollectorScript:
    def test_node_override(self):
        node = _node()
        node.triage = TriageConfig(collector_script="/custom/collect.sh")
        result = get_collector_script(_project(), node)
        assert result == "/custom/collect.sh"

    def test_project_default(self):
        result = get_collector_script(_project(), _node())
        assert result == "/usr/local/bin/ic-collect.sh"

    def test_system_default(self):
        result = get_collector_script(None, _node())
        assert result == "/usr/local/bin/ic-collect.sh"


# ── get_tier_capabilities ─────────────────────────────────────────


class TestGetTierCapabilities:
    def test_local_only(self):
        caps = get_tier_capabilities(AccessTier.LOCAL_ONLY)
        assert "local_data" in caps
        assert "observability_api" in caps
        assert "ssh_readonly" not in caps

    def test_collector(self):
        caps = get_tier_capabilities(AccessTier.COLLECTOR)
        assert "collector_script" in caps
        assert "ssh_readonly" not in caps

    def test_unprivileged(self):
        caps = get_tier_capabilities(AccessTier.UNPRIVILEGED)
        assert "ssh_readonly" in caps
        assert "ssh_privileged" not in caps

    def test_privileged(self):
        caps = get_tier_capabilities(AccessTier.PRIVILEGED)
        assert "ssh_privileged" in caps
        assert "remediation" not in caps

    def test_remediate(self):
        caps = get_tier_capabilities(AccessTier.REMEDIATE)
        assert "remediation" in caps

    def test_cumulative(self):
        """Higher tiers include all lower capabilities."""
        for tier in AccessTier:
            caps = get_tier_capabilities(tier)
            assert "local_data" in caps  # always present
