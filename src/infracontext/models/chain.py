"""Request-path chains: one ordered entry describing lb -> app -> db.

A chain replaces N pairwise relationships with a single ordered member list.
It is stored in ``chains.yaml``, a per-project *sibling* of
``relationships.yaml``, and expanded at the parse boundary
(:func:`expand_chain`) into consecutive-pair :class:`Relationship` objects --
graph consumers never see chains, only ordinary pairwise edges.

Chains deliberately live in their own file: released infracontext versions
have ``extra="forbid"`` on ``RelationshipFile`` and skip the *entire*
relationships file on validation errors, so embedding chains in
``relationships.yaml`` would make all existing edges vanish for teammates on
older versions. A file those versions never read is safe by construction.
"""

import re
from itertools import pairwise

from pydantic import BaseModel, Field, field_validator, model_validator

from infracontext.models.relationship import Relationship, RelationshipType

# Attribute keys carried by chain-expanded relationships.
CHAIN_ATTR_NAME = "chain"
CHAIN_ATTR_POSITION = "chain_position"

_CHAIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ChainMember(BaseModel):
    """One hop in a chain.

    In YAML a member is either a plain string node ref (``vm:web-01``) or a
    mapping (``{id: vm:web-01, via: "port 8080"}``) -- the plain form keeps
    hand-edited files terse.
    """

    id: str = Field(..., description="Node ref: type:slug or @scope:type:slug")
    via: str | None = Field(
        default=None, description="Free text: how traffic reaches this hop (port, protocol, path)"
    )

    model_config = {"extra": "forbid"}


class Chain(BaseModel):
    """An ordered request path expanded into consecutive pairwise edges."""

    name: str = Field(..., description="Slug-like chain name, unique per project")
    description: str | None = None
    # Forward-compat: tolerate edge types from newer versions, mirroring
    # Relationship.type (`ic doctor` warns about unknown variants).
    type: RelationshipType | str = Field(
        default=RelationshipType.ROUTES_TO, union_mode="left_to_right"
    )
    members: list[ChainMember] = Field(..., min_length=2)

    model_config = {"extra": "forbid"}

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _CHAIN_NAME_RE.fullmatch(value):
            raise ValueError(
                "Chain name must be slug-like: lowercase letters, digits, and hyphens"
            )
        return value

    @field_validator("members", mode="before")
    @classmethod
    def coerce_members(cls, value: object) -> object:
        """Accept plain-string members: ``- vm:web`` == ``- {id: vm:web}``."""
        if isinstance(value, list):
            return [{"id": item} if isinstance(item, str) else item for item in value]
        return value

    @model_validator(mode="after")
    def validate_consecutive_members_differ(self) -> Chain:
        # A repeated consecutive member would expand into a self-edge, which
        # Relationship rejects -- fail here with a chain-level message instead.
        for a, b in pairwise(self.members):
            if a.id == b.id:
                raise ValueError(f"Chain '{self.name}' repeats consecutive member '{a.id}'")
        return self


class ChainFile(BaseModel):
    """Container for chains stored in chains.yaml."""

    version: str = Field(default="2.0")
    chains: list[Chain] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


def expand_chain(chain: Chain) -> list[Relationship]:
    """Expand a chain into consecutive-pair relationships.

    The single parse-boundary expansion shared by every consumer (graph
    loaders, doctor, render): each consecutive member pair becomes one edge of
    the chain's type, carrying the chain name and 0-based hop position in
    ``attributes`` and a human-readable description. A member's ``via`` text
    lands on the edge *into* that member.
    """
    edges: list[Relationship] = []
    hops = len(chain.members) - 1
    for position, (src, dst) in enumerate(pairwise(chain.members)):
        parts = [f"chain '{chain.name}' hop {position + 1}/{hops}"]
        if dst.via:
            parts.append(f"via {dst.via}")
        if chain.description:
            parts.append(chain.description)
        edges.append(
            Relationship(
                source=src.id,
                target=dst.id,
                type=chain.type,
                description="; ".join(parts),
                attributes={CHAIN_ATTR_NAME: chain.name, CHAIN_ATTR_POSITION: position},
            )
        )
    return edges
