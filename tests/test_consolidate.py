"""Duplicate-node consolidation: merge semantics, reference rewrite, CLI verb.

Covers the pure logic in infracontext.consolidate (fill-only scalars, unions,
dedupe appends, edge/chain/override rewrite) and the CLI refusal paths of
``ic describe node consolidate`` (same node, missing nodes, external roots,
cross-project, source-ownership conflicts, --force, --dry-run).
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from infracontext.cli.describe import app as describe_app
from infracontext.config import AppConfig, save_config
from infracontext.consolidate import (
    apply_override_remap,
    merge_nodes,
    plan_override_remap,
    rewrite_chain_members,
    rewrite_relationship_refs,
)
from infracontext.models.chain import Chain, ChainFile
from infracontext.models.node import Learning, Node, NodeType, Observability, TriageConfig
from infracontext.models.relationship import Relationship, RelationshipFile, RelationshipType
from infracontext.paths import INFRACONTEXT_DIR, EnvironmentPaths, ProjectPaths
from infracontext.storage import read_model, read_yaml, write_model, write_yaml

runner = CliRunner()


def _node(node_id: str, **kwargs) -> Node:
    node_type, slug = node_id.split(":", 1)
    return Node(id=node_id, slug=slug, type=NodeType(node_type), name=kwargs.pop("name", slug), **kwargs)


# ── merge_nodes ────────────────────────────────────────────────────


class TestMergeNodes:
    def test_scalar_fill_only_src_fills_empty(self):
        dest = _node("vm:a")
        src = _node("vm:b", description="from src", notes="src notes", ssh_alias="src-alias")

        merged, changed = merge_nodes(dest, src)

        assert merged.description == "from src"
        assert merged.notes == "src notes"
        assert merged.ssh_alias == "src-alias"
        assert {"description", "notes", "ssh_alias"} <= set(changed)

    def test_scalar_fill_only_dest_wins(self):
        dest = _node("vm:a", description="dest desc", ssh_alias="dest-alias")
        src = _node("vm:b", description="src desc", ssh_alias="src-alias")

        merged, changed = merge_nodes(dest, src)

        assert merged.description == "dest desc"
        assert merged.ssh_alias == "dest-alias"
        assert changed == []

    def test_identity_fields_stay_dest(self):
        dest = _node("vm:a", name="Dest")
        src = _node("vm:b", name="Src", description="x")

        merged, _ = merge_nodes(dest, src)

        assert merged.id == "vm:a"
        assert merged.slug == "a"
        assert merged.name == "Dest"

    def test_first_seen_keeps_earlier_date(self):
        dest = _node("vm:a", first_seen="2024-06-01")
        src = _node("vm:b", first_seen="2020-01-15")
        merged, changed = merge_nodes(dest, src)
        assert merged.first_seen == "2020-01-15"
        assert "first_seen" in changed

        # And the other direction: dest already earlier -> unchanged.
        merged, changed = merge_nodes(src, dest)
        assert merged.first_seen == "2020-01-15"
        assert changed == []

    def test_first_seen_filled_when_dest_empty(self):
        merged, _ = merge_nodes(_node("vm:a"), _node("vm:b", first_seen="2022-02-02"))
        assert merged.first_seen == "2022-02-02"

    def test_list_unions_dedupe_and_preserve_order(self):
        dest = _node("vm:a", ip_addresses=["10.0.0.1"], domains=["a.example.com"])
        src = _node("vm:b", ip_addresses=["10.0.0.1", "10.0.0.2"], domains=["b.example.com"])

        merged, changed = merge_nodes(dest, src)

        assert merged.ip_addresses == ["10.0.0.1", "10.0.0.2"]
        assert merged.domains == ["a.example.com", "b.example.com"]
        assert "ip_addresses (+1)" in changed
        assert "domains (+1)" in changed

    def test_attributes_fill_only(self):
        dest = _node("vm:a", attributes={"os": "debian"})
        src = _node("vm:b", attributes={"os": "ubuntu", "cpu_cores": 4})

        merged, changed = merge_nodes(dest, src)

        assert merged.attributes == {"os": "debian", "cpu_cores": 4}
        assert "attributes (+1)" in changed

    def test_learnings_append_dedupe_by_date_and_finding(self):
        shared = Learning(date="2026-01-01", context="cpu", finding="pool misconfigured")
        dest = _node("vm:a", learnings=[shared])
        src = _node(
            "vm:b",
            learnings=[
                # Same (date, finding) but different context: still a duplicate.
                Learning(date="2026-01-01", context="other investigation", finding="pool misconfigured"),
                Learning(date="2026-02-02", context="disk", finding="raid degraded"),
            ],
        )

        merged, changed = merge_nodes(dest, src)

        assert len(merged.learnings) == 2
        assert merged.learnings[1].finding == "raid degraded"
        assert "learnings (+1)" in changed

    def test_observability_append_dedupe_by_equality(self):
        obs = Observability(type="prometheus", instance="a:9100")
        dest = _node("vm:a", observability=[obs])
        src = _node(
            "vm:b",
            observability=[
                Observability(type="prometheus", instance="a:9100"),  # duplicate
                Observability(type="loki", selector='{service_name="a"}'),
            ],
        )

        merged, changed = merge_nodes(dest, src)

        assert len(merged.observability) == 2
        assert merged.observability[1].type == "loki"
        assert "observability (+1)" in changed

    def test_triage_taken_from_src_when_dest_has_none(self):
        src = _node("vm:b", triage=TriageConfig(services=["nginx"], tier=2))
        merged, changed = merge_nodes(_node("vm:a"), src)
        assert merged.triage is not None
        assert merged.triage.services == ["nginx"]
        assert merged.triage.tier == 2
        assert "triage" in changed

    def test_triage_services_union_and_fill_only_rest(self):
        dest = _node("vm:a", triage=TriageConfig(services=["nginx"], context="dest ctx"))
        src = _node("vm:b", triage=TriageConfig(services=["nginx", "redis"], context="src ctx", tier=3))

        merged, changed = merge_nodes(dest, src)

        assert merged.triage.services == ["nginx", "redis"]
        assert merged.triage.context == "dest ctx"  # dest wins
        assert merged.triage.tier == 3  # filled from src
        assert any(item.startswith("triage (") for item in changed)

    def test_no_changes_returns_empty_summary(self):
        dest = _node("vm:a", description="d", ip_addresses=["10.0.0.1"])
        src = _node("vm:b", description="other", ip_addresses=["10.0.0.1"])
        merged, changed = merge_nodes(dest, src)
        assert changed == []
        assert merged == dest


# ── rewrite_relationship_refs ─────────────────────────────────────


def _rel_file(*rels: Relationship) -> RelationshipFile:
    return RelationshipFile(relationships=list(rels))


class TestRewriteRelationshipRefs:
    def test_source_position_rewritten(self):
        rel_file = _rel_file(Relationship(source="vm:src", target="vm:db", type=RelationshipType.DEPENDS_ON))
        result = rewrite_relationship_refs(rel_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.rewritten == 1
        assert rel_file.relationships[0].source == "vm:dest"
        assert rel_file.relationships[0].target == "vm:db"

    def test_target_position_rewritten(self):
        rel_file = _rel_file(Relationship(source="vm:db", target="vm:src", type=RelationshipType.CONNECTS_TO))
        result = rewrite_relationship_refs(rel_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.rewritten == 1
        assert rel_file.relationships[0].target == "vm:dest"

    def test_self_qualified_ref_rewritten(self):
        rel_file = _rel_file(
            Relationship(source="@prod:vm:src", target="vm:db", type=RelationshipType.DEPENDS_ON)
        )
        result = rewrite_relationship_refs(rel_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.rewritten == 1
        assert rel_file.relationships[0].source == "vm:dest"

    def test_other_project_ref_untouched(self):
        rel_file = _rel_file(
            Relationship(source="@staging:vm:src", target="vm:db", type=RelationshipType.DEPENDS_ON)
        )
        result = rewrite_relationship_refs(rel_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.rewritten == 0
        assert rel_file.relationships[0].source == "@staging:vm:src"

    def test_duplicate_after_rewrite_removed(self):
        rel_file = _rel_file(
            Relationship(source="vm:src", target="vm:db", type=RelationshipType.DEPENDS_ON),
            Relationship(source="vm:dest", target="vm:db", type=RelationshipType.DEPENDS_ON),
        )
        result = rewrite_relationship_refs(rel_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.duplicates_removed == 1
        assert len(rel_file.relationships) == 1
        assert rel_file.relationships[0].source == "vm:dest"

    def test_different_type_is_not_a_duplicate(self):
        rel_file = _rel_file(
            Relationship(source="vm:src", target="vm:db", type=RelationshipType.DEPENDS_ON),
            Relationship(source="vm:dest", target="vm:db", type=RelationshipType.CONNECTS_TO),
        )
        result = rewrite_relationship_refs(rel_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.duplicates_removed == 0
        assert len(rel_file.relationships) == 2

    def test_edge_between_src_and_dest_dropped_as_self_edge(self):
        rel_file = _rel_file(
            Relationship(source="vm:src", target="vm:dest", type=RelationshipType.DEPENDS_ON),
            Relationship(source="vm:dest", target="vm:src", type=RelationshipType.CONNECTS_TO),
        )
        result = rewrite_relationship_refs(rel_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.self_edges_dropped == 2
        assert rel_file.relationships == []

    def test_pre_existing_duplicates_between_untouched_edges_survive(self):
        # Dedup is scoped to rewritten edges: hand-edited duplicates between
        # nodes unrelated to the consolidation are ic doctor's business.
        rel_file = _rel_file(
            Relationship(source="vm:c", target="vm:d", type=RelationshipType.DEPENDS_ON),
            Relationship(source="vm:c", target="vm:d", type=RelationshipType.DEPENDS_ON),
            Relationship(source="vm:src", target="vm:db", type=RelationshipType.DEPENDS_ON),
        )
        result = rewrite_relationship_refs(rel_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.rewritten == 1
        assert result.duplicates_removed == 0
        assert len(rel_file.relationships) == 3

    def test_inbound_cross_project_ref_rewritten_keeps_qualified_form(self):
        # A sibling project's relationships.yaml referencing the src node as
        # `@prod:vm:src` must be rewritten -- to the qualified dest form.
        rel_file = _rel_file(
            Relationship(source="vm:local", target="@prod:vm:src", type=RelationshipType.DEPENDS_ON),
            Relationship(source="@prod:vm:src", target="vm:local", type=RelationshipType.CONNECTS_TO),
            Relationship(source="vm:local", target="vm:src", type=RelationshipType.USES),
        )
        result = rewrite_relationship_refs(
            rel_file, project="prod", src_id="vm:src", dest_id="vm:dest", file_project="other"
        )

        assert result.rewritten == 2
        assert rel_file.relationships[0].target == "@prod:vm:dest"
        assert rel_file.relationships[1].source == "@prod:vm:dest"
        # A bare ref in the sibling file addresses the sibling's OWN vm:src.
        assert rel_file.relationships[2].target == "vm:src"


# ── rewrite_chain_members ─────────────────────────────────────────


class TestRewriteChainMembers:
    def test_member_rewritten(self):
        chain_file = ChainFile(chains=[Chain(name="edge", members=["vm:lb", "vm:src", "vm:db"])])
        result = rewrite_chain_members(chain_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.members_rewritten == 1
        assert [m.id for m in chain_file.chains[0].members] == ["vm:lb", "vm:dest", "vm:db"]

    def test_consecutive_duplicate_collapsed(self):
        chain_file = ChainFile(chains=[Chain(name="edge", members=["vm:src", "vm:dest", "vm:db"])])
        result = rewrite_chain_members(chain_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.members_rewritten == 1
        assert [m.id for m in chain_file.chains[0].members] == ["vm:dest", "vm:db"]
        assert result.chains_removed == 0

    def test_chain_collapsing_below_two_members_removed(self):
        chain_file = ChainFile(
            chains=[
                Chain(name="gone", members=["vm:src", "vm:dest"]),
                Chain(name="stays", members=["vm:lb", "vm:db"]),
            ]
        )
        result = rewrite_chain_members(chain_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.chains_removed == 1
        assert [c.name for c in chain_file.chains] == ["stays"]

    def test_non_consecutive_repeat_survives(self):
        # A loop through dest and back is still a valid path after rewrite.
        chain_file = ChainFile(chains=[Chain(name="loop", members=["vm:src", "vm:db", "vm:dest"])])
        rewrite_chain_members(chain_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert [m.id for m in chain_file.chains[0].members] == ["vm:dest", "vm:db", "vm:dest"]

    def test_inbound_cross_project_member_rewritten_keeps_qualified_form(self):
        chain_file = ChainFile(
            chains=[Chain(name="edge", members=["vm:local", "@prod:vm:src", "vm:src"])]
        )
        result = rewrite_chain_members(
            chain_file, project="prod", src_id="vm:src", dest_id="vm:dest", file_project="other"
        )

        assert result.members_rewritten == 1
        # Qualified inbound ref rewritten; the sibling's own bare vm:src untouched.
        assert [m.id for m in chain_file.chains[0].members] == ["vm:local", "@prod:vm:dest", "vm:src"]

    def test_preexisting_duplicate_hops_inside_touched_chain_preserved(self):
        # Codex stop-gate repro: a touched chain may also contain a
        # pre-existing spelling-duplicate pair unrelated to the rewrite
        # (vm:a next to @prod:vm:a). Only duplicates the rewrite CREATED may
        # collapse -- the unrelated pair stays.
        chain_file = ChainFile(
            chains=[Chain(name="edge", members=["vm:a", "@prod:vm:a", "vm:src", "vm:b"])]
        )
        result = rewrite_chain_members(chain_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.members_rewritten == 1
        assert result.chains_removed == 0
        assert [m.id for m in chain_file.chains[0].members] == ["vm:a", "@prod:vm:a", "vm:dest", "vm:b"]

    def test_rewrite_created_duplicate_still_collapses(self):
        # The rewrite makes vm:src adjacent-equal to vm:dest -> collapse, and
        # the first hop keeps its via.
        chain_file = ChainFile(
            chains=[
                Chain(name="edge", members=[{"id": "vm:src", "via": "port 443"}, "vm:dest", "vm:b"])
            ]
        )
        result = rewrite_chain_members(chain_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.members_rewritten == 1
        members = chain_file.chains[0].members
        assert [m.id for m in members] == ["vm:dest", "vm:b"]
        assert members[0].via == "port 443"

    def test_preexisting_run_bordering_rewrite_not_swallowed(self):
        # Codex stop-gate repro (boundary overlap): the rewrite creates ONE
        # duplicate pair (src->dest next to dest), but the following
        # dest/@prod:dest pair pre-existed. Rewritten status must not
        # propagate through the dropped member and swallow the whole run.
        chain_file = ChainFile(
            chains=[Chain(name="edge", members=["vm:src", "vm:dest", "@prod:vm:dest", "vm:b"])]
        )
        result = rewrite_chain_members(chain_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.members_rewritten == 1
        assert result.chains_removed == 0
        assert [m.id for m in chain_file.chains[0].members] == ["vm:dest", "@prod:vm:dest", "vm:b"]
        ChainFile.model_validate(chain_file.model_dump())

    def test_preexisting_run_at_chain_end_not_swallowed(self):
        # Same shape without a trailing member: only the rewrite-created pair
        # collapses; the chain must not degenerate below two members.
        chain_file = ChainFile(
            chains=[Chain(name="edge", members=["vm:src", "vm:dest", "@prod:vm:dest"])]
        )
        result = rewrite_chain_members(chain_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.chains_removed == 0
        assert [m.id for m in chain_file.chains[0].members] == ["vm:dest", "@prod:vm:dest"]

    def test_rewritten_member_sandwiched_between_dest_spellings_collapses_both_pairs(self):
        # Both original adjacencies of the rewritten middle member became
        # equal BECAUSE of the rewrite -- both collapse, judged pair-by-pair
        # on the original sequence (no propagation involved: the rewritten
        # member itself sits in each pair).
        chain_file = ChainFile(
            chains=[Chain(name="loop", members=["vm:dest", "vm:src", "@prod:vm:dest"])]
        )
        result = rewrite_chain_members(chain_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.members_rewritten == 1
        assert result.chains_removed == 1
        assert chain_file.chains == []

    def test_reversed_qualifier_run_never_writes_identical_adjacency(self):
        # Codex stop-gate repro: dropping index 1 glues the rewritten member
        # onto a STRING-identical index 2 ([vm:src, @prod:vm:dest, vm:dest]
        # -> [vm:dest, vm:dest]) -- an adjacency the Chain validator rejects,
        # so the written file would fail its next load. The artifact must
        # collapse; here the chain degenerates entirely (it was one node).
        chain_file = ChainFile(
            chains=[Chain(name="edge", members=["vm:src", "@prod:vm:dest", "vm:dest"])]
        )
        result = rewrite_chain_members(chain_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.members_rewritten == 1
        assert result.chains_removed == 1
        assert chain_file.chains == []

    def test_reversed_qualifier_run_with_tail_stays_valid(self):
        chain_file = ChainFile(
            chains=[Chain(name="edge", members=["vm:src", "@prod:vm:dest", "vm:dest", "vm:b"])]
        )
        result = rewrite_chain_members(chain_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.chains_removed == 0
        assert [m.id for m in chain_file.chains[0].members] == ["vm:dest", "vm:b"]
        # The mutated file must round-trip through full model validation --
        # write_model serializes without revalidating.
        ChainFile.model_validate(chain_file.model_dump())

    def test_untouched_chain_with_preexisting_duplicates_never_altered(self):
        # Collapse/removal is scoped to chains the rewrite touched. The model
        # validator only rejects string-identical consecutive members, so a
        # bare/self-qualified spelling pair (`vm:a` vs `@prod:vm:a`) is valid
        # YAML on disk yet _ref_key-identical -- the old unscoped collapse
        # silently rewrote (or deleted) such unrelated chains.
        chain_file = ChainFile(
            chains=[
                Chain(name="untouched-dup", members=["vm:a", "@prod:vm:a", "vm:b"]),
                Chain(name="untouched-degenerate", members=["vm:c", "@prod:vm:c"]),
                Chain(name="touched", members=["vm:src", "vm:db"]),
            ]
        )
        result = rewrite_chain_members(chain_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.members_rewritten == 1
        assert result.chains_removed == 0
        assert [c.name for c in chain_file.chains] == ["untouched-dup", "untouched-degenerate", "touched"]
        assert [m.id for m in chain_file.chains[0].members] == ["vm:a", "@prod:vm:a", "vm:b"]
        assert [m.id for m in chain_file.chains[1].members] == ["vm:c", "@prod:vm:c"]
        assert [m.id for m in chain_file.chains[2].members] == ["vm:dest", "vm:db"]

    def test_no_match_leaves_every_chain_untouched(self):
        chain_file = ChainFile(chains=[Chain(name="degenerate", members=["vm:c", "@prod:vm:c"])])
        result = rewrite_chain_members(chain_file, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert result.members_rewritten == 0
        assert result.chains_removed == 0
        assert len(chain_file.chains) == 1


# ── override remap ────────────────────────────────────────────────


class TestOverrideRemap:
    def test_scoped_entry_is_effective_global_deleted(self):
        # Lookup prefers the scoped entry WHOLLY, so only the scoped entry
        # transfers; the shadowed global entry is a dead key once src is gone
        # (no other project has the ID) and is deleted, never merged.
        nodes = {
            "vm:src": {"ssh_alias": "global-alias"},
            "prod/vm:src": {"ssh_alias": "scoped-alias"},
            "other/vm:src": {"ssh_alias": "other-project"},
        }
        remap = plan_override_remap(nodes, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert remap == [("move", "prod/vm:src", "prod/vm:dest"), ("delete", "vm:src", None)]

        apply_override_remap(nodes, remap)
        assert nodes == {
            "prod/vm:dest": {"ssh_alias": "scoped-alias"},
            "other/vm:src": {"ssh_alias": "other-project"},
        }

    def test_shadowed_global_fields_never_activate(self):
        # Codex stop-gate repro: global-only fields (source_paths) were
        # INACTIVE for this project pre-consolidation (scoped entry shadows
        # them at lookup, overrides.py get_node_overrides). Field-merging both
        # entries would activate them post-consolidation, leaking another
        # machine's values -- only the effective (scoped) entry may transfer.
        nodes = {
            "vm:src": {"ssh_alias": "other-machine", "source_paths": ["/other/checkout"]},
            "prod/vm:src": {"ssh_alias": "this-machine"},
        }
        remap = plan_override_remap(
            nodes, project="prod", src_id="vm:src", dest_id="vm:dest", dest_in_other_projects=True
        )
        apply_override_remap(nodes, remap)

        assert nodes == {"prod/vm:dest": {"ssh_alias": "this-machine"}}

    def test_shadowed_global_kept_when_other_projects_have_src(self):
        # Other projects' vm:src nodes still resolve the global entry -- it
        # must stay in place (untouched, not copied onto dest).
        nodes = {
            "vm:src": {"ssh_alias": "shared"},
            "prod/vm:src": {"ssh_alias": "scoped"},
        }
        remap = plan_override_remap(
            nodes, project="prod", src_id="vm:src", dest_id="vm:dest", src_in_other_projects=True
        )
        assert remap == [("move", "prod/vm:src", "prod/vm:dest")]

        apply_override_remap(nodes, remap)
        assert nodes == {
            "vm:src": {"ssh_alias": "shared"},
            "prod/vm:dest": {"ssh_alias": "scoped"},
        }

    def test_global_key_copied_when_src_id_lives_in_other_projects(self):
        # No scoped entry: the global one IS effective and transfers -- but as
        # a copy, because other projects' vm:src still need it.
        nodes = {"vm:src": {"ssh_alias": "shared"}}
        remap = plan_override_remap(
            nodes, project="prod", src_id="vm:src", dest_id="vm:dest", src_in_other_projects=True
        )
        assert remap == [("copy", "vm:src", "vm:dest")]

        apply_override_remap(nodes, remap)
        assert nodes == {"vm:src": {"ssh_alias": "shared"}, "vm:dest": {"ssh_alias": "shared"}}
        # Deep copy: mutating one entry must not leak into the other
        # (and ruamel must never see a shared object it would anchor).
        nodes["vm:dest"]["ssh_alias"] = "changed"
        assert nodes["vm:src"]["ssh_alias"] == "shared"

    def test_global_entry_redirected_to_scoped_when_dest_id_lives_in_other_projects(self):
        # A global dest key would apply src's ssh_alias to the OTHER project's
        # own vm:dest (a different machine) -- redirect to the scoped key so
        # the effect stays confined to the consolidation project.
        nodes = {"vm:src": {"ssh_alias": "src-alias"}}
        remap = plan_override_remap(
            nodes, project="prod", src_id="vm:src", dest_id="vm:dest", dest_in_other_projects=True
        )
        assert remap == [("move", "vm:src", "prod/vm:dest")]

        apply_override_remap(nodes, remap)
        assert nodes == {"prod/vm:dest": {"ssh_alias": "src-alias"}}

    def test_existing_global_dest_entry_seeds_scoped_key_and_wins(self):
        # Scoped keys shadow global ones at lookup. When the transfer lands on
        # a not-yet-existing scoped dest key while the dest node already has a
        # GLOBAL override, that entry is seeded onto the scoped key first --
        # otherwise src's values would silently take precedence over dest's.
        nodes = {
            "vm:src": {"ssh_alias": "src-alias"},
            "vm:dest": {"ssh_alias": "dest-alias"},
        }
        remap = plan_override_remap(
            nodes, project="prod", src_id="vm:src", dest_id="vm:dest", dest_in_other_projects=True
        )
        assert remap[0] == ("copy", "vm:dest", "prod/vm:dest")

        apply_override_remap(nodes, remap)
        # dest's alias wins on the scoped key; the global dest entry survives
        # for the other projects that see it.
        assert nodes == {
            "vm:dest": {"ssh_alias": "dest-alias"},
            "prod/vm:dest": {"ssh_alias": "dest-alias"},
        }

    def test_scoped_move_also_seeds_from_global_dest_entry(self):
        # Same precedence hazard without any redirect: moving prod/vm:src to
        # prod/vm:dest creates a scoped key that would shadow the dest node's
        # existing global entry.
        nodes = {
            "prod/vm:src": {"ssh_alias": "src-alias", "source_paths": ["/src"]},
            "vm:dest": {"ssh_alias": "dest-alias"},
        }
        remap = plan_override_remap(nodes, project="prod", src_id="vm:src", dest_id="vm:dest")
        apply_override_remap(nodes, remap)

        # dest's alias wins; src still fills the field dest never set.
        assert nodes == {
            "vm:dest": {"ssh_alias": "dest-alias"},
            "prod/vm:dest": {"ssh_alias": "dest-alias", "source_paths": ["/src"]},
        }

    def test_no_seed_when_scoped_dest_key_already_exists(self):
        # An existing scoped dest key already wins fill-only merges natively.
        nodes = {
            "prod/vm:src": {"ssh_alias": "src-alias"},
            "prod/vm:dest": {"ssh_alias": "dest-alias"},
            "vm:dest": {"ssh_alias": "global-dest"},
        }
        remap = plan_override_remap(nodes, project="prod", src_id="vm:src", dest_id="vm:dest")
        assert remap == [("move", "prod/vm:src", "prod/vm:dest")]

        apply_override_remap(nodes, remap)
        assert nodes["prod/vm:dest"] == {"ssh_alias": "dest-alias"}

    def test_apply_merges_fill_only_into_existing_dest_key(self):
        nodes = {
            "vm:src": {"ssh_alias": "src-alias", "source_paths": ["/src"]},
            "vm:dest": {"source_paths": ["/dest"]},
        }
        remap = plan_override_remap(nodes, project="prod", src_id="vm:src", dest_id="vm:dest")
        apply_override_remap(nodes, remap)

        assert nodes == {"vm:dest": {"source_paths": ["/dest"], "ssh_alias": "src-alias"}}

    def test_other_project_scoped_key_untouched(self):
        nodes = {"other/vm:src": {"ssh_alias": "x"}}
        remap = plan_override_remap(nodes, project="prod", src_id="vm:src", dest_id="vm:dest")

        assert remap == []


# ── CLI: happy path, dry-run, refusals ────────────────────────────


@pytest.fixture()
def consolidate_env(hotpath_env):
    """hotpath_env plus a duplicate vm:web-02, edges, chains, and overrides.

    hotpath_env provides prod with vm:web-01 (ssh_alias, domain, IP, triage,
    one learning) and a bare vm:db-01.
    """
    paths = ProjectPaths.for_project("prod", hotpath_env)
    web2 = Node(
        id="vm:web-02",
        slug="web-02",
        type=NodeType.VM,
        name="Web Server 02 (dup)",
        description="duplicate of web-01",
        ssh_alias="web-prod-old",
        first_seen="2020-01-01",
        ip_addresses=["10.0.0.5", "10.0.0.6"],
        domains=["web01.example.com", "web02.example.com"],
        triage=TriageConfig(services=["nginx", "redis"], tier=2),
        observability=[Observability(type="prometheus", instance="web-02:9100")],
        learnings=[
            # Duplicate of web-01's learning (same date+finding, other context).
            Learning(date="2026-01-01", context="load", finding="pool misconfigured", source="agent"),
            Learning(date="2026-03-03", context="disk", finding="raid degraded", source="agent"),
        ],
    )
    write_model(paths.node_file("vm", "web-02"), web2)

    write_model(
        paths.relationships_yaml,
        RelationshipFile(
            relationships=[
                Relationship(source="vm:web-02", target="vm:db-01", type=RelationshipType.DEPENDS_ON),
                Relationship(source="vm:db-01", target="vm:web-02", type=RelationshipType.CONNECTS_TO),
                Relationship(source="vm:web-01", target="vm:db-01", type=RelationshipType.DEPENDS_ON),
                Relationship(source="vm:web-02", target="vm:web-01", type=RelationshipType.DEPENDS_ON),
            ]
        ),
    )
    write_model(
        paths.chains_yaml,
        ChainFile(
            chains=[
                Chain(name="path-a", members=["vm:web-02", "vm:db-01", "vm:web-01"]),
                Chain(name="path-b", members=["vm:web-02", "vm:web-01"]),
            ]
        ),
    )
    write_yaml(
        hotpath_env.local_overrides,
        {
            "nodes": {
                "vm:web-02": {"ssh_alias": "local-w2"},
                "prod/vm:web-02": {"ssh_alias": "scoped-w2"},
                "other/vm:web-02": {"ssh_alias": "other-w2"},
                "vm:web-01": {"source_paths": ["/checkout/web"]},
            }
        },
    )
    return hotpath_env, paths


class TestConsolidateCli:
    def test_happy_path_merges_and_rewrites_everything(self, consolidate_env):
        env, paths = consolidate_env

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "vm:web-02"])
        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "Consolidated 'vm:web-02' into 'vm:web-01'" in flat

        # src file deleted, dest merged.
        assert not paths.node_file("vm", "web-02").exists()
        merged = read_model(paths.node_file("vm", "web-01"), Node)
        assert merged.description == "duplicate of web-01"
        assert merged.ssh_alias == "web-prod"  # dest wins (and no override baked in)
        assert merged.first_seen == "2020-01-01"
        assert merged.ip_addresses == ["10.0.0.5", "10.0.0.6"]
        assert merged.domains == ["web01.example.com", "web02.example.com"]
        assert [ln.finding for ln in merged.learnings] == ["pool misconfigured", "raid degraded"]
        assert [obs.instance for obs in merged.observability] == ["web-02:9100"]
        assert merged.triage.services == ["nginx", "php-fpm", "redis"]
        assert merged.triage.tier == 2
        assert merged.triage.context == "check php-fpm first"

        # Relationships: rewritten, deduped, self-edges dropped.
        rel_file = read_model(paths.relationships_yaml, RelationshipFile)
        edges = {(r.source, str(r.type), r.target) for r in rel_file.relationships}
        assert edges == {
            ("vm:web-01", "depends_on", "vm:db-01"),
            ("vm:db-01", "connects_to", "vm:web-01"),
        }
        assert "3 rewritten" in flat
        assert "1 duplicate(s) removed" in flat
        assert "1 self-edge(s) dropped" in flat

        # Chains: member rewritten; degenerate chain removed.
        chain_file = read_model(paths.chains_yaml, ChainFile)
        assert [c.name for c in chain_file.chains] == ["path-a"]
        assert [m.id for m in chain_file.chains[0].members] == ["vm:web-01", "vm:db-01", "vm:web-01"]

        # Overrides: the effective (scoped) src entry transferred, the
        # shadowed global src entry was deleted (dead key, its fields were
        # inactive here), other project's scoped key untouched.
        overrides = read_yaml(env.local_overrides)["nodes"]
        assert "vm:web-02" not in overrides
        assert "prod/vm:web-02" not in overrides
        # The scoped dest key was seeded from dest's existing global entry
        # (its values keep winning under scoped-shadows-global lookup), then
        # src's scoped alias filled the gap.
        assert overrides["prod/vm:web-01"] == {"source_paths": ["/checkout/web"], "ssh_alias": "scoped-w2"}
        assert overrides["other/vm:web-02"] == {"ssh_alias": "other-w2"}
        # The global dest entry is untouched -- the shadowed global src alias
        # (local-w2) must NOT have merged into it.
        assert overrides["vm:web-01"] == {"source_paths": ["/checkout/web"]}
        assert "3 key(s) remapped" in flat

    def test_fuzzy_queries_resolve(self, consolidate_env):
        _, paths = consolidate_env
        result = runner.invoke(describe_app, ["node", "consolidate", "web-01", "web-02"])
        assert result.exit_code == 0, result.output
        assert not paths.node_file("vm", "web-02").exists()

    def test_dry_run_touches_nothing(self, consolidate_env):
        env, paths = consolidate_env
        before = {
            path: path.read_text()
            for path in (
                paths.node_file("vm", "web-01"),
                paths.node_file("vm", "web-02"),
                paths.relationships_yaml,
                paths.chains_yaml,
                env.local_overrides,
            )
        }

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "vm:web-02", "--dry-run"])
        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "Would consolidate 'vm:web-02' into 'vm:web-01'" in flat
        assert "3 rewritten" in flat
        assert "3 key(s) remapped" in flat
        assert "would delete" in flat

        for path, text in before.items():
            assert path.exists(), path
            assert path.read_text() == text, path

    def test_refuses_same_node(self, hotpath_env):
        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "vm:web-01"])
        assert result.exit_code == 1
        assert "same node" in result.output

    def test_refuses_missing_dest(self, hotpath_env):
        result = runner.invoke(describe_app, ["node", "consolidate", "vm:ghost", "vm:db-01"])
        assert result.exit_code == 1
        assert "Destination node 'vm:ghost' not found" in " ".join(result.output.split())

    def test_refuses_missing_src(self, hotpath_env):
        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "vm:ghost"])
        assert result.exit_code == 1
        assert "Source node 'vm:ghost' not found" in " ".join(result.output.split())

    def test_refuses_cross_project(self, hotpath_env):
        staging = ProjectPaths.for_project("staging", hotpath_env)
        staging.ensure_dirs()
        staging.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(
            staging.node_file("vm", "other"),
            Node(id="vm:other", slug="other", type=NodeType.VM, name="Other"),
        )

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "@staging:vm:other"])
        assert result.exit_code == 1
        assert "different projects" in " ".join(result.output.split())
        assert staging.node_file("vm", "other").exists()

    def test_refuses_external_root(self, tmp_path, monkeypatch):
        from infracontext.config import ExternalRoot

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
            Node(id="physical_host:pve-01", slug="pve-01", type=NodeType.PHYSICAL_HOST, name="PVE"),
        )

        save_config(
            AppConfig(
                active_project="prod",
                external_roots=[ExternalRoot(alias="fleet", path="../fleet")],
            ),
            local_env,
        )
        monkeypatch.setattr("infracontext.paths.find_environment_root", lambda start=None: local_env.root)  # noqa: ARG005
        monkeypatch.setattr("infracontext.paths.require_environment_root", lambda: local_env.root)

        result = runner.invoke(
            describe_app, ["node", "consolidate", "vm:web-01", "@fleet:physical_host:pve-01"]
        )
        assert result.exit_code == 1
        assert "external root" in " ".join(result.output.split())
        assert fleet_proj.node_file("physical_host", "pve-01").exists()

    def test_refuses_managed_by_conflict_without_force(self, hotpath_env):
        paths = ProjectPaths.for_project("prod", hotpath_env)
        write_model(
            paths.node_file("vm", "owned-a"),
            _node("vm:owned-a", managed_by="source-a"),
        )
        write_model(
            paths.node_file("vm", "owned-b"),
            _node("vm:owned-b", managed_by="source-b"),
        )

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:owned-a", "vm:owned-b"])
        assert result.exit_code == 1
        flat = " ".join(result.output.split())
        assert "Refusing to consolidate" in flat
        assert "--force" in flat
        assert paths.node_file("vm", "owned-b").exists()

    def test_refuses_source_id_conflict_without_force(self, hotpath_env):
        paths = ProjectPaths.for_project("prod", hotpath_env)
        write_model(paths.node_file("vm", "sid-a"), _node("vm:sid-a", source_id="proxmox:c1:qemu:100"))
        write_model(paths.node_file("vm", "sid-b"), _node("vm:sid-b", source_id="ssh_config:s:host"))

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:sid-a", "vm:sid-b"])
        assert result.exit_code == 1
        assert "source_id" in result.output

    def test_force_overrides_ownership_conflict(self, hotpath_env):
        paths = ProjectPaths.for_project("prod", hotpath_env)
        write_model(
            paths.node_file("vm", "owned-a"),
            _node("vm:owned-a", managed_by="source-a"),
        )
        write_model(
            paths.node_file("vm", "owned-b"),
            _node("vm:owned-b", managed_by="source-b", description="from b"),
        )

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:owned-a", "vm:owned-b", "--force"])
        assert result.exit_code == 0, result.output
        assert "Warning" in result.output
        assert not paths.node_file("vm", "owned-b").exists()
        merged = read_model(paths.node_file("vm", "owned-a"), Node)
        assert merged.managed_by == "source-a"  # dest's ownership wins
        assert merged.description == "from b"

    def test_same_ownership_needs_no_force(self, hotpath_env):
        paths = ProjectPaths.for_project("prod", hotpath_env)
        write_model(paths.node_file("vm", "same-a"), _node("vm:same-a", managed_by="src", source_id="s:1"))
        write_model(paths.node_file("vm", "same-b"), _node("vm:same-b", managed_by="src"))

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:same-a", "vm:same-b"])
        assert result.exit_code == 0, result.output
        assert not paths.node_file("vm", "same-b").exists()

    def test_works_without_relationships_chains_or_overrides(self, hotpath_env):
        paths = ProjectPaths.for_project("prod", hotpath_env)

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "vm:db-01"])
        assert result.exit_code == 0, result.output
        assert not paths.node_file("vm", "db-01").exists()
        assert "0 rewritten" in " ".join(result.output.split())

    def test_refuses_source_managed_src_into_manual_dest(self, hotpath_env):
        # The fill-only merge would make the manual dest adopt src's source
        # binding, and the next sync of that source would rename or re-create
        # the merged node, undoing the consolidation.
        paths = ProjectPaths.for_project("prod", hotpath_env)
        write_model(
            paths.node_file("vm", "managed"),
            _node("vm:managed", managed_by="pve-test", source_id="proxmox:c1:qemu:100"),
        )

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "vm:managed"])
        assert result.exit_code == 1
        flat = " ".join(result.output.split())
        assert "Refusing to consolidate" in flat
        assert "source-managed" in flat
        assert "Swap the arguments" in flat
        assert paths.node_file("vm", "managed").exists()
        # Nothing merged onto dest.
        dest = read_model(paths.node_file("vm", "web-01"), Node)
        assert dest.managed_by is None
        assert dest.source_id is None

    def test_force_lets_manual_dest_adopt_source_binding_with_warning(self, hotpath_env):
        paths = ProjectPaths.for_project("prod", hotpath_env)
        write_model(
            paths.node_file("vm", "managed"),
            _node("vm:managed", managed_by="pve-test", source_id="proxmox:c1:qemu:100"),
        )

        result = runner.invoke(
            describe_app, ["node", "consolidate", "vm:web-01", "vm:managed", "--force"]
        )
        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "Warning" in flat
        assert "adopts" in flat
        assert not paths.node_file("vm", "managed").exists()
        merged = read_model(paths.node_file("vm", "web-01"), Node)
        assert merged.managed_by == "pve-test"
        assert merged.source_id == "proxmox:c1:qemu:100"

    def test_source_managed_dest_absorbing_manual_src_needs_no_force(self, hotpath_env):
        # The safe direction (the refusal message suggests exactly this swap):
        # dest keeps its own binding, src contributes content only.
        paths = ProjectPaths.for_project("prod", hotpath_env)
        write_model(
            paths.node_file("vm", "managed"),
            _node("vm:managed", managed_by="pve-test", source_id="proxmox:c1:qemu:100"),
        )
        write_model(paths.node_file("vm", "manual"), _node("vm:manual", description="hand-made"))

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:managed", "vm:manual"])
        assert result.exit_code == 0, result.output
        merged = read_model(paths.node_file("vm", "managed"), Node)
        assert merged.managed_by == "pve-test"
        assert merged.description == "hand-made"

    def test_inbound_refs_from_sibling_project_rewritten(self, consolidate_env):
        env, paths = consolidate_env
        other = ProjectPaths.for_project("other", env)
        other.ensure_dirs()
        write_model(
            other.relationships_yaml,
            RelationshipFile(
                relationships=[
                    Relationship(
                        source="vm:local", target="@prod:vm:web-02", type=RelationshipType.DEPENDS_ON
                    ),
                ]
            ),
        )
        write_model(
            other.chains_yaml,
            ChainFile(chains=[Chain(name="inbound", members=["vm:local", "@prod:vm:web-02"])]),
        )

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "vm:web-02"])
        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "inbound refs from 'other': 1 edge(s) and 1 chain member(s) rewritten" in flat

        sib_rels = read_model(other.relationships_yaml, RelationshipFile)
        assert sib_rels.relationships[0].target == "@prod:vm:web-01"
        sib_chains = read_model(other.chains_yaml, ChainFile)
        assert [m.id for m in sib_chains.chains[0].members] == ["vm:local", "@prod:vm:web-01"]

    def test_global_override_key_copied_when_other_project_has_same_id(self, consolidate_env):
        env, paths = consolidate_env
        other = ProjectPaths.for_project("other", env)
        other.ensure_dirs()
        # `other` has its own vm:web-02 -- the global override key applies to
        # it too, so consolidating prod's vm:web-02 must copy, not move.
        other.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(other.node_file("vm", "web-02"), _node("vm:web-02"))
        # Global-only src entry: with a scoped entry present the global one
        # would be shadowed (and simply left in place); here it IS effective.
        write_yaml(
            env.local_overrides,
            {"nodes": {"vm:web-02": {"ssh_alias": "local-w2"}}},
        )

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "vm:web-02"])
        assert result.exit_code == 0, result.output
        assert "copied, not moved" in " ".join(result.output.split())

        overrides = read_yaml(env.local_overrides)["nodes"]
        assert overrides["vm:web-02"] == {"ssh_alias": "local-w2"}  # kept for `other`
        assert overrides["vm:web-01"] == {"ssh_alias": "local-w2"}  # dest gained the copy

    def test_shadowed_global_src_entry_left_when_other_project_has_same_id(self, consolidate_env):
        env, paths = consolidate_env
        other = ProjectPaths.for_project("other", env)
        other.ensure_dirs()
        other.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(other.node_file("vm", "web-02"), _node("vm:web-02"))

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "vm:web-02"])
        assert result.exit_code == 0, result.output

        overrides = read_yaml(env.local_overrides)["nodes"]
        # The scoped entry was effective and transferred; the global entry
        # stays in place purely for `other`'s vm:web-02 -- its alias must not
        # have merged anywhere.
        assert overrides["vm:web-02"] == {"ssh_alias": "local-w2"}
        assert overrides["prod/vm:web-01"] == {"source_paths": ["/checkout/web"], "ssh_alias": "scoped-w2"}
        assert overrides["vm:web-01"] == {"source_paths": ["/checkout/web"]}

    def test_global_override_redirected_when_other_project_owns_dest_id(self, consolidate_env):
        env, paths = consolidate_env
        other = ProjectPaths.for_project("other", env)
        other.ensure_dirs()
        # `other` has its own vm:web-01 (a different machine): the remapped
        # global entry must not leak src's ssh_alias onto it.
        other.node_type_dir("vm").mkdir(parents=True, exist_ok=True)
        write_model(other.node_file("vm", "web-01"), _node("vm:web-01"))

        # Global-only src entry so the global entry is the effective one and
        # actually takes the redirect path (a scoped entry would transfer
        # scoped-to-scoped anyway).
        write_yaml(
            env.local_overrides,
            {
                "nodes": {
                    "vm:web-02": {"ssh_alias": "local-w2"},
                    "vm:web-01": {"source_paths": ["/checkout/web"]},
                }
            },
        )

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "vm:web-02"])
        assert result.exit_code == 0, result.output
        assert "written project-scoped" in " ".join(result.output.split())

        overrides = read_yaml(env.local_overrides)["nodes"]
        assert "vm:web-02" not in overrides  # src not in other projects: moved
        # Redirected to the scoped key: seeded from dest's global entry, then
        # src's alias filled the gap. The global vm:web-01 entry, which
        # `other` legitimately sees, is untouched.
        assert overrides["prod/vm:web-01"] == {"source_paths": ["/checkout/web"], "ssh_alias": "local-w2"}
        assert overrides["vm:web-01"] == {"source_paths": ["/checkout/web"]}

    def test_sibling_chain_with_preexisting_duplicate_survives_rewrite(self, consolidate_env):
        env, paths = consolidate_env
        other = ProjectPaths.for_project("other", env)
        other.ensure_dirs()
        write_model(
            other.chains_yaml,
            ChainFile(
                chains=[
                    # Unrelated chain whose consecutive members are the same
                    # node under different spellings (valid on disk, but
                    # _ref_key-identical -- the old unscoped collapse ate it).
                    Chain(name="unrelated", members=["vm:a", "@other:vm:a", "vm:b"]),
                    # Chain the consolidation legitimately rewrites.
                    Chain(name="inbound", members=["vm:local", "@prod:vm:web-02"]),
                ]
            ),
        )

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "vm:web-02"])
        assert result.exit_code == 0, result.output

        sib_chains = read_model(other.chains_yaml, ChainFile)
        by_name = {c.name: c for c in sib_chains.chains}
        # The rewrite landed...
        assert [m.id for m in by_name["inbound"].members] == ["vm:local", "@prod:vm:web-01"]
        # ...and the unrelated chain kept its spelling-duplicate untouched.
        assert [m.id for m in by_name["unrelated"].members] == ["vm:a", "@other:vm:a", "vm:b"]

    def test_written_chains_file_loads_after_reversed_qualifier_consolidate(self, hotpath_env):
        # End-to-end guard for the Codex repro: the consolidated chains.yaml
        # must parse on the next load -- an invalid file would silently drop
        # ALL chains from the graph.
        paths = ProjectPaths.for_project("prod", hotpath_env)
        write_model(paths.node_file("vm", "web-03"), _node("vm:web-03"))
        write_model(
            paths.chains_yaml,
            ChainFile(
                chains=[
                    Chain(name="degenerates", members=["vm:db-01", "@prod:vm:web-01", "vm:web-01"]),
                    Chain(
                        name="survives",
                        members=["vm:db-01", "@prod:vm:web-01", "vm:web-01", "vm:web-03"],
                    ),
                ]
            ),
        )

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "vm:db-01"])
        assert result.exit_code == 0, result.output

        chain_file = read_model(paths.chains_yaml, ChainFile)
        assert chain_file is not None, "written chains.yaml failed to load"
        assert [c.name for c in chain_file.chains] == ["survives"]
        assert [m.id for m in chain_file.chains[0].members] == ["vm:web-01", "vm:web-03"]

    def test_untouched_dest_chain_file_not_rewritten(self, hotpath_env):
        # A consolidation matching no chain member must leave chains.yaml
        # byte-identical -- even when it contains a degenerate chain (same
        # node twice under different spellings) that the old unscoped
        # collapse would have deleted.
        paths = ProjectPaths.for_project("prod", hotpath_env)
        write_model(
            paths.chains_yaml,
            ChainFile(chains=[Chain(name="degenerate", members=["vm:web-01", "@prod:vm:web-01"])]),
        )
        before = paths.chains_yaml.read_text()

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "vm:db-01"])
        assert result.exit_code == 0, result.output
        assert paths.chains_yaml.read_text() == before

    def test_malformed_overrides_file_warns_and_completes(self, consolidate_env):
        env, paths = consolidate_env
        env.local_overrides.write_text("nodes: [unclosed\n")

        result = runner.invoke(describe_app, ["node", "consolidate", "vm:web-01", "vm:web-02"])
        assert result.exit_code == 0, result.output
        flat = " ".join(result.output.split())
        assert "malformed" in flat
        assert "0 key(s) remapped" in flat
        # The consolidation itself still ran to completion.
        assert not paths.node_file("vm", "web-02").exists()
        rel_file = read_model(paths.relationships_yaml, RelationshipFile)
        assert all("web-02" not in (r.source, r.target) for r in rel_file.relationships)
        # The malformed file is left untouched for the human to fix.
        assert env.local_overrides.read_text() == "nodes: [unclosed\n"
