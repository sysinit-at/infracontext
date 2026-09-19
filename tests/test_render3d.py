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

    def test_props_are_type_aware_and_escaped(self):
        from infracontext.models.node import Node

        g = _graph()
        g.nodes["vm:db"]["node"] = Node(
            id="vm:db", slug="db", type="vm", name="DB",
            attributes={"os_pretty": "Debian <13>", "cpu_cores": 8, "memory_mb": 16384,
                        "proxmox_cluster": "DB-Cluster", "proxmox_node": "pve-db-a",
                        "proxmox_vmid": 121,
                        "hardware": {"manufacturer": "HPE", "model": "DL380",
                                     "rack_position": 16.0, "rack_face": "front"}},
        )
        payload = build_3d_payload(g)
        props = dict(map(tuple, next(n for n in payload["nodes"] if n["id"] == "vm:db")["props"]))
        assert props["OS"] == "Debian &lt;13&gt;"
        assert props["CPU"] == "8 cores"
        assert props["Memory"] == "16 GB"
        assert props["PVE placement"] == "DB-Cluster / pve-db-a (VMID 121)"
        assert props["Rack position"] == "U16 (front)"
        # untyped nodes still get an (empty) list, never a KeyError
        assert all("props" in n for n in payload["nodes"])

    def test_view_presets_and_stars_in_page(self, tmp_path):
        out = tmp_path / "g.3d.html"
        render_html_3d(_graph(), out)
        body = out.read_text(encoding="utf-8")
        assert "const VIEWS" in body
        for label in ("Datacenter", "Hypervisors", "Services & Apps"):
            assert label in body  # view labels are JS strings, not HTML-escaped
        assert "SpriteMaterial" in body      # star rendering
        assert "space-bg" in body            # starfield backdrop
        assert "AdditiveBlending" in body

    def test_node_details_attached_and_impact_on_the_right(self, tmp_path):
        """Split surfaces: node identity/details in a popover anchored at the
        star (#node-card), the outage analysis in the fixed right panel
        (#impact-card)."""
        out = tmp_path / "g.3d.html"
        render_html_3d(_graph(), out)
        body = out.read_text(encoding="utf-8")
        assert "function positionCard(" in body
        assert "graph2ScreenCoords" in body
        assert "#node-card { position: fixed" in body       # attached popover
        assert "#side { position: absolute; top: 14px; right: 16px" in body  # right panel
        assert "function renderNodeCard(" in body
        assert "function renderImpactCard(" in body
        assert "'Outage impact'" in body
        assert "cardPop" in body                    # spawn animation
        # positionCard is driven every frame so it tracks the star
        assert body.count("positionCard()") >= 2

    def test_nub_tracks_node_and_hides_out_of_range(self, tmp_path):
        """The pointer nub is JS-positioned (not a static ::before), so under
        clamping it follows the node's screen-Y and hides when out of range —
        never pointing at empty space."""
        out = tmp_path / "g.3d.html"
        render_html_3d(_graph(), out)
        body = out.read_text(encoding="utf-8")
        # nub is a fixed-position sibling of the card (scroll-immune), placed
        # in viewport coords at the node's screen-Y
        assert ".card-nub { position: fixed" in body
        assert "document.body.appendChild(cardNub)" in body
        assert "nub.style.top = c.y" in body
        assert "cardNub.style.display = 'none'" in body   # hidden with the card
        assert "::before" not in body.split("</style>")[0]  # no static pseudo nub

    def test_artifact_records_renderer_version(self, tmp_path):
        from importlib.metadata import version
        out = tmp_path / "g.3d.html"
        render_html_3d(_graph(), out)
        body = out.read_text(encoding="utf-8")
        assert f"infracontext-renderer: {version('infracontext')}" in body

    def test_hv_flag_and_links_in_payload(self):
        from infracontext.models.node import Node, Observability

        g = _graph()
        g.add_node("hypervisor_cluster:c1", name="APP", type="hypervisor_cluster")
        g.add_edge("physical_host:h1", "hypervisor_cluster:c1", type="member_of")
        g.nodes["physical_host:h1"]["node"] = Node(
            id="physical_host:h1", slug="h1", type="physical_host", name="Host 1",
            domains=["h1.example.com"],
            attributes={"ilo_ip": "10.0.9.1"},
            observability=[Observability(type="dashboard", name="Grafana",
                                         url="https://grafana.example.com/d/h1")],
        )
        payload = build_3d_payload(g)
        by_id = {n["id"]: n for n in payload["nodes"]}
        assert by_id["physical_host:h1"]["hv"] is True
        assert by_id["vm:web"]["hv"] is False
        urls = [link["url"] for link in by_id["physical_host:h1"]["links"]]
        assert "https://h1.example.com" in urls
        assert "https://10.0.9.1" in urls
        assert "https://grafana.example.com/d/h1" in urls

    def test_hypervisor_view_is_predicate_filtered(self, tmp_path):
        out = tmp_path / "g.3d.html"
        render_html_3d(_graph(), out)
        body = out.read_text(encoding="utf-8")
        assert "pred: n => n.type === 'hypervisor_cluster' || n.hv" in body
        assert "if (v.pred) return v.pred(n);" in body
        assert "related('Guests'" in body
        assert "related('Connected'" in body
        assert "rel = 'noopener noreferrer'" in body or "noopener noreferrer" in body

    def test_empty_graph_renders(self, tmp_path):
        out = tmp_path / "empty.3d.html"
        render_html_3d(nx.DiGraph(), out)
        assert "0 nodes" in out.read_text(encoding="utf-8")
