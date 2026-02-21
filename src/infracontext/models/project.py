"""Project configuration model."""

from pydantic import BaseModel, Field

from infracontext.models.tier import AccessTier


class ProjectAccessConfig(BaseModel):
    """Access tier configuration for a project."""

    default_tier: AccessTier = Field(
        default=AccessTier.UNPRIVILEGED,
        description="Default access tier for nodes without explicit tier",
    )
    max_tier: AccessTier = Field(
        default=AccessTier.PRIVILEGED,
        description="Maximum allowed tier (hard ceiling)",
    )
    collector_script: str = Field(
        default="/usr/local/bin/ic-collect.sh",
        description="Path to collector script on target hosts",
    )

    model_config = {"extra": "forbid"}


class ProjectLinks(BaseModel):
    """Project-level links shared across all nodes."""

    issue_tracker: str | None = Field(
        default=None,
        description="Issue tracker URL (e.g., Jira, GitHub Issues)",
    )
    communication_channel: str | None = Field(
        default=None,
        description="Team communication channel (e.g., Slack channel, Teams)",
    )

    model_config = {"extra": "forbid"}


class ProjectConfig(BaseModel):
    """Project-level configuration stored in project.yaml."""

    version: str = Field(default="2.0", description="Schema version")
    name: str = Field(..., description="Human-readable project name")
    slug: str = Field(..., description="URL-safe project identifier")
    description: str | None = Field(default=None, description="Project description")
    access: ProjectAccessConfig = Field(
        default_factory=ProjectAccessConfig,
        description="Access tier configuration",
    )
    links: ProjectLinks = Field(
        default_factory=ProjectLinks,
        description="Project-level links (issue tracker, communication)",
    )

    model_config = {"extra": "forbid"}
