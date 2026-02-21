"""Tier resolution for infracontext access control.

Configuration hierarchy:
    Project default → Node override → CLI restriction
         ↓                ↓               ↓
      baseline      can raise/lower    can only lower

The project's max_tier acts as a hard ceiling.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from infracontext.models.tier import AccessTier

if TYPE_CHECKING:
    from infracontext.models.node import Node
    from infracontext.models.project import ProjectConfig

# Default collector script path
DEFAULT_COLLECTOR_SCRIPT = "/usr/local/bin/ic-collect.sh"


def compute_effective_tier(
    project_config: ProjectConfig | None,
    node: Node,
    cli_tier: AccessTier | None = None,
) -> AccessTier:
    """Compute the effective access tier for a node.

    Resolution order:
    1. Start with project.access.default_tier (or UNPRIVILEGED if no project config)
    2. Apply node.triage.tier if set (can raise or lower)
    3. Apply cli_tier if set (can only restrict, not elevate)
    4. Clamp to project.access.max_tier ceiling (or PRIVILEGED if no project config)

    Args:
        project_config: Project configuration (may be None for backward compatibility)
        node: The node being accessed
        cli_tier: Tier override from CLI --tier flag (can only restrict)

    Returns:
        The effective AccessTier for this node
    """
    # Step 1: Start with project default
    if project_config:
        tier = project_config.access.default_tier
        max_tier = project_config.access.max_tier
    else:
        tier = AccessTier.UNPRIVILEGED
        max_tier = AccessTier.PRIVILEGED

    # Step 2: Apply node override (can raise or lower)
    if node.triage and node.triage.tier is not None:
        tier = AccessTier(node.triage.tier)

    # Step 3: Apply CLI override (can only restrict)
    if cli_tier is not None and cli_tier < tier:
        tier = cli_tier

    # Step 4: Clamp to max_tier ceiling
    if tier > max_tier:
        tier = max_tier

    return tier


def get_effective_tier(
    project_config: ProjectConfig | None,
    node: Node,
) -> AccessTier:
    """Get effective tier including CLI environment variable.

    Convenience wrapper that checks IC_TIER environment variable.
    """
    cli_tier = get_cli_tier()
    return compute_effective_tier(project_config, node, cli_tier)


def get_cli_tier() -> AccessTier | None:
    """Get tier override from IC_TIER environment variable."""
    tier_str = os.environ.get("IC_TIER")
    if not tier_str:
        return None

    # Allow both names (local_only) and values (0)
    tier_str = tier_str.upper().replace("-", "_")
    try:
        return AccessTier[tier_str]
    except KeyError:
        pass

    try:
        return AccessTier(int(tier_str))
    except (ValueError, KeyError):
        return None


def get_collector_script(project_config: ProjectConfig | None, node: Node) -> str:
    """Get the collector script path for a node.

    Priority: node override > project default > system default
    """
    # Check node override first
    if node.triage and node.triage.collector_script:
        return node.triage.collector_script

    # Then project config
    if project_config:
        return project_config.access.collector_script

    # Fall back to system default
    return DEFAULT_COLLECTOR_SCRIPT


def get_tier_capabilities(tier: AccessTier) -> list[str]:
    """Get the list of capabilities for a given tier.

    Returns human-readable capability names for context output.
    """
    capabilities = ["local_data"]

    if tier >= AccessTier.LOCAL_ONLY:
        capabilities.append("observability_api")

    if tier >= AccessTier.COLLECTOR:
        capabilities.append("collector_script")

    if tier >= AccessTier.UNPRIVILEGED:
        capabilities.append("ssh_readonly")

    if tier >= AccessTier.PRIVILEGED:
        capabilities.append("ssh_privileged")

    if tier >= AccessTier.REMEDIATE:
        capabilities.append("remediation")

    return capabilities
