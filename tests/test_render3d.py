"""Tests for the 3D outage-explorer renderer."""

from __future__ import annotations

import networkx as nx

from infracontext.graph.analysis import calculate_impact
from infracontext.graph.render3d import build_3d_payload, render_html_3d


def _graph():
    g = nx.DiGraph()
    g.add_node("vm:web", name="Web", type="vm")
    g.add_node("vm:db", name="DB", type="vm")
    g.add_node("nfs_share:data", name="192.168.0.1:/tank/data", type="nfs_share")
    g.add_node("physical_host:h1", name="Host 1", type="physical_host")
    g.add_edge("vm:web", "vm:db", type="depends_on")
    g.add_edge("vm:web", "nfs_share:data", type="mounts")
    g.add_edge("nfs_share:data", "physical_host:h1", type="hosted_by")
    g.add_edge("vm:db", "physical_host:h1", type="runs_on")
    return g


class TestPayload:
    def test_impact_sets_match_calculate_impact(self):
        """The embedded blast radius must be exactly what `ic graph impact`
        reports — the page must never drift from the CLI."""
        g = _graph()
        payload = build_3d_payload(g)
        for node_id in g.nodes():
            expected = calculate_impact(g, node_id)
            imp = payload["impact"][node_id]
            assert len(imp["direct"]) == expected["direct_dependents"], node_id
            assert len(imp["all"]) == expected["total_affected"], node_id

    def test_h1_outage_affects_everything(self):
        payload = build_3d_payload(_graph())
        assert set(payload["impact"]["physical_host:h1"]["all"]) == {
            "vm:web", "vm:db", "nfs_share:data",
        }

    def test_leaf_outage_affects_nothing(self):
        payload = build_3d_payload(_graph())
        assert payload["impact"]["vm:web"]["all"] == []

    def test_links_carry_relationship_style(self):
        payload = build_3d_payload(_graph())
        rels = {link["rel"] for link in payload["links"]}
        assert rels == {"depends_on", "mounts", "hosted_by", "runs_on"}
        for link in payload["links"]:
            assert link["color"].startswith("#")

    def test_links_are_emitted_dependency_to_dependent(self):
        """Graph edge u->v means 'u depends on v'; the page emits v->u so
        directional particles animate failure spreading outward."""
        payload = build_3d_payload(_graph())
        pairs = {(link["source"], link["target"]) for link in payload["links"]}
        assert ("vm:db", "vm:web") in pairs        # web depends_on db
        assert ("physical_host:h1", "vm:db") in pairs  # db runs_on h1

    def test_cluster_assignment_from_membership_and_attributes(self):
        from infracontext.models.node import Node

        g = _graph()
        g.add_node("hypervisor_cluster:c1", name="APP-Cluster", type="hypervisor_cluster")
        g.add_edge("physical_host:h1", "hypervisor_cluster:c1", type="member_of")
        g.nodes["vm:db"]["node"] = Node(
            id="vm:db", slug="db", type="vm", name="DB",
            attributes={"proxmox_cluster": "APP-Cluster"},
        )
        payload = build_3d_payload(g)
        by_id = {n["id"]: n["cluster"] for n in payload["nodes"]}
        assert by_id["hypervisor_cluster:c1"] == "APP-Cluster"
        assert by_id["physical_host:h1"] == "APP-Cluster"
        assert by_id["vm:db"] == "APP-Cluster"
        assert by_id["vm:web"] == ""  # unclustered stays centered

    def test_names_are_escaped(self):
        g = nx.DiGraph()
        g.add_node("vm:x", name="<img src=x onerror=alert(1)>", type="vm")
        payload = build_3d_payload(g)
        assert "<img" not in payload["nodes"][0]["name"]
        assert "&lt;img" in payload["nodes"][0]["name"]


class TestRender:
    def test_writes_selfcontained_page(self, tmp_path):
        out = tmp_path / "g.3d.html"
        render_html_3d(_graph(), out, title="My <Estate>")
        body = out.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in body
        assert "ForceGraph3D" in body
        assert "My &lt;Estate&gt;" in body
        assert "selectOrigin" in body
        # self-contained: no external script/style references
        assert 'src="http' not in body

    def test_selection_and_url_stay_in_sync(self, tmp_path):
        """Selecting/clearing must rewrite the hash, else a bookmarked or
        copied URL captures a different node than the one on screen."""
        out = tmp_path / "g.3d.html"
        render_html_3d(_graph(), out)
        body = out.read_text(encoding="utf-8")
        assert "function syncHash(" in body
        # both directions wired
        select_block = body.split("function selectOrigin(")[1][:200]
        clear_block = body.split("function clearOrigin(")[1][:200]
        assert "syncHash(id)" in select_block
        assert "syncHash(null)" in clear_block
        # replaceState, not location.hash= (no history spam, no hashchange loop)
        assert "history.replaceState" in body

    def test_empty_graph_renders(self, tmp_path):
        out = tmp_path / "empty.3d.html"
        render_html_3d(nx.DiGraph(), out)
        assert "0 nodes" in out.read_text(encoding="utf-8")
