"""Tests for infracontext.graph.render — HTML, SVG, GraphML output."""

from __future__ import annotations

import importlib.util

import networkx as nx
import pytest

from infracontext.graph.render import (
    MERMAID_EDGE_ARROWS,
    NODE_CATEGORIES,
    _build_vis_payload,
    _display_label,
    _mermaid_escape,
    _mermaid_ids,
    _node_scope,
    _primitive_attrs,
    _sanitize_for_graphml,
    _style_for,
    graph_to_mermaid,
    mermaid_size_warning,
    render_graphml,
    render_html,
    render_mermaid,
    render_svg,
)

_HAS_MATPLOTLIB = importlib.util.find_spec("matplotlib") is not None


# ─── helpers / pure functions ─────────────────────────────────────


class TestStyleFor:
    def test_known_type_returns_specific_style(self):
        color, shape, category = _style_for("vm")
        assert color == "#59A14F"
        assert shape == "box"
        assert category == "vm"

    def test_physical_and_virtual_compute_are_visually_distinct(self):
        """An infra graph is mostly compute — physical hosts and VMs must not
        collapse into one color/category blob."""
        phys_color, _, phys_cat = _style_for("physical_host")
        vm_color, _, vm_cat = _style_for("vm")
        assert phys_color != vm_color
        assert phys_cat != vm_cat

    def test_unknown_type_falls_back(self):
        color, shape, category = _style_for("totally-made-up")
        assert category == "other"
        assert shape == "dot"

    def test_none_falls_back(self):
        assert _style_for(None)[2] == "other"

    def test_every_node_type_in_constraint_table_has_style(self):
        # Every NodeType value the schema uses must produce a non-fallback
        # style — otherwise rendered diagrams quietly turn grey dots.
        from infracontext.models.node import NodeType

        for node_type in NodeType:
            assert node_type.value in NODE_CATEGORIES, (
                f"NodeType.{node_type.name} has no entry in NODE_CATEGORIES"
            )


class TestDisplayLabel:
    def test_uses_name_when_present(self):
        assert _display_label("vm:web-01", "Web Server") == "Web Server"

    def test_long_names_are_elided_but_tooltip_keeps_full_name(self):
        long_name = "192.168.0.24:/mnt/APPtank/media_downsized-clone"
        label = _display_label("nfs_share:x", long_name)
        assert len(label) <= 32
        assert label.endswith("…")
        g = nx.DiGraph()
        g.add_node("nfs_share:x", name=long_name, type="nfs_share")
        nodes, _e, _l = _build_vis_payload(g)
        assert long_name in nodes[0]["title"]

    def test_falls_back_to_unqualified_id(self):
        assert _display_label("prod/vm:web-01", None) == "vm:web-01"

    def test_external_root_unqualifies(self):
        assert _display_label("@fleet:prod/vm:web-01", None) == "vm:web-01"


class TestNodeScope:
    def test_local_root_with_project(self):
        assert _node_scope("prod/vm:web", {"project": "prod"}) == "prod"

    def test_external_root(self):
        assert (
            _node_scope("@fleet:prod/vm:web", {"project": "prod", "root": "fleet"})
            == "@fleet:prod"
        )

    def test_bare_id_returns_none(self):
        assert _node_scope("vm:web", {}) is None

    def test_falls_back_to_qualified_id(self):
        # Loader may not always set the project attr — derive from graph_id.
        assert _node_scope("prod/vm:web", {}) == "prod"

    def test_attr_takes_precedence_over_unqualified_id(self):
        # Unqualified graph ID (single-project load) but project attr set —
        # attr wins so federation rebuilds preserve scope information.
        assert _node_scope("vm:web", {"project": "prod"}) == "prod"


# ─── _build_vis_payload ───────────────────────────────────────────


def _sample_graph() -> nx.DiGraph:
    g = nx.DiGraph()
    g.add_node("vm:web", name="Web", type="vm")
    g.add_node("vm:db", name="DB", type="vm")
    g.add_node("physical_host:h1", name="Host 1", type="physical_host")
    g.add_edge("vm:web", "vm:db", type="depends_on", description="PostgreSQL")
    g.add_edge("vm:db", "physical_host:h1", type="runs_on")
    return g


class TestBuildVisPayload:
    def test_one_node_per_graph_node(self):
        nodes, _edges, _legend = _build_vis_payload(_sample_graph())
        assert len(nodes) == 3
        assert {n["id"] for n in nodes} == {"vm:web", "vm:db", "physical_host:h1"}

    def test_one_edge_per_graph_edge_with_relation_label(self):
        _nodes, edges, _legend = _build_vis_payload(_sample_graph())
        assert len(edges) == 2
        labels = {(e["from"], e["to"]): e["label"] for e in edges}
        assert labels[("vm:web", "vm:db")] == "depends_on"
        assert labels[("vm:db", "physical_host:h1")] == "runs_on"

    def test_legend_counts_per_category(self):
        _nodes, _edges, legend = _build_vis_payload(_sample_graph())
        by_cat = {row["category"]: row["count"] for row in legend}
        assert by_cat == {"physical": 1, "vm": 2}

    def test_each_node_carries_its_category(self):
        """The JS legend filter keys on each node's ``_category`` — make
        sure that field is set per node, not just summed into the legend.
        """
        g = nx.DiGraph()
        g.add_node("vm:web", name="Web", type="vm")
        g.add_node("nfs_share:data", name="Data", type="nfs_share")
        g.add_node("domain:example", name="example.com", type="domain")
        g.add_node("oci_container:c1", name="C1", type="oci_container")
        g.add_node("unknown:x", name="X", type="this_type_doesnt_exist")
        nodes, _e, _l = _build_vis_payload(g)
        by_id = {n["id"]: n["_category"] for n in nodes}
        assert by_id["vm:web"] == "vm"
        assert by_id["nfs_share:data"] == "storage"
        assert by_id["domain:example"] == "dns"
        assert by_id["oci_container:c1"] == "container"
        assert by_id["unknown:x"] == "other"

    def test_edge_tooltip_combines_relation_and_description(self):
        _nodes, edges, _legend = _build_vis_payload(_sample_graph())
        web_db = next(e for e in edges if e["from"] == "vm:web")
        assert "depends_on" in web_db["title"]
        assert "PostgreSQL" in web_db["title"]

    def test_edges_are_styled_by_relationship_type(self):
        _nodes, edges, _legend = _build_vis_payload(_sample_graph())
        by_rel = {e["label"]: e for e in edges}
        # placement edge: dashed; dependency edge: solid — and distinct colors
        assert by_rel["runs_on"]["dashes"] is True
        assert by_rel["depends_on"]["dashes"] is False
        assert by_rel["runs_on"]["color"]["color"] != by_rel["depends_on"]["color"]["color"]

    def test_only_outside_label_shapes_carry_scaling_value(self):
        """Label-sized shapes (box, database, ellipse) must NOT get `value`:
        vis-network would balloon long-named nodes past the scaling max."""
        g = _sample_graph()
        g.add_node("hypervisor_cluster:c1", name="C1", type="hypervisor_cluster")
        g.add_edge("physical_host:h1", "hypervisor_cluster:c1", type="member_of")
        nodes, _edges, _legend = _build_vis_payload(g)
        by_id = {n["id"]: n for n in nodes}
        assert "value" not in by_id["vm:db"]  # box shape
        assert by_id["hypervisor_cluster:c1"]["value"] == 1  # hexagon shape

    def test_edge_legend_counts_per_relation(self):
        from infracontext.graph.render import _build_edge_legend

        _nodes, edges, _legend = _build_vis_payload(_sample_graph())
        rows = {r["relation"]: r["count"] for r in _build_edge_legend(edges)}
        assert rows == {"depends_on": 1, "runs_on": 1}


class TestEdgeTypeStyles:
    def test_style_map_covers_every_relationship_type(self):
        """Every RelationshipType member needs an explicit edge style.

        A new enum value without one would silently fall back to the grey
        default; this test forces the decision when the enum grows.
        """
        from infracontext.graph.render import EDGE_TYPE_STYLES
        from infracontext.models.relationship import RelationshipType

        assert set(EDGE_TYPE_STYLES) == {t.value for t in RelationshipType}


# ─── render_html ──────────────────────────────────────────────────


class TestRenderHTML:
    def test_writes_file(self, tmp_path):
        out = tmp_path / "graph.html"
        render_html(_sample_graph(), out)
        assert out.exists()
        body = out.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in body
        assert "vis-network" in body

    def test_includes_node_labels_and_relation(self, tmp_path):
        out = tmp_path / "graph.html"
        render_html(_sample_graph(), out, title="My Cluster")
        body = out.read_text(encoding="utf-8")
        assert "My Cluster" in body
        assert "Web" in body
        assert "depends_on" in body
        assert "runs_on" in body

    def test_user_supplied_name_cannot_inject_script_tag(self, tmp_path):
        """Two attacks the title field must resist:

        1. ``</script>`` in the JSON payload would break out of the embedded
           <script> tag — ``_js_safe`` escapes it to ``<\\/script>``.
        2. vis-network renders ``title`` as HTML in its tooltip, so a raw
           ``<script>`` inside the title would execute at hover time —
           ``_build_vis_payload`` html-escapes each tooltip fragment.
        """
        g = nx.DiGraph()
        g.add_node("vm:x", name="<script>alert(1)</script>", type="vm")
        out = tmp_path / "graph.html"
        render_html(g, out)
        body = out.read_text(encoding="utf-8")

        # Attack 1: no raw </script> closing tag (would terminate <script>).
        assert "</script>alert" not in body
        assert "<\\/script>" in body or "<\\/SCRIPT>" in body.upper()

        # Attack 2: the title field itself must not contain a raw <script.
        # Inspect the title in the vis-network JSON payload directly so we
        # don't get fooled by the label slot (which vis-network re-escapes
        # before rendering anyway).
        nodes, _edges, _legend = _build_vis_payload(g)
        title = nodes[0]["title"]
        assert "<script" not in title.lower()
        assert "&lt;script&gt;" in title

    def test_user_supplied_description_cannot_inject_html(self):
        """Same risk on edge tooltips — `description` is user-controlled."""
        g = nx.DiGraph()
        g.add_node("a", name="A", type="vm")
        g.add_node("b", name="B", type="vm")
        g.add_edge(
            "a", "b", type="depends_on", description='<img src=x onerror="alert(1)">'
        )
        _nodes, edges, _legend = _build_vis_payload(g)
        title = edges[0]["title"]
        assert "<img" not in title.lower()
        assert "&lt;img" in title

    def test_empty_graph_is_handled(self, tmp_path):
        out = tmp_path / "empty.html"
        render_html(nx.DiGraph(), out)
        body = out.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in body
        assert "0 nodes" in body

    def test_node_named_like_placeholder_does_not_corrupt_page(self, tmp_path):
        """Substitution is single-pass: a node literally named ``__EDGES__``
        must survive as data. A chained str.replace would re-substitute it
        with the edges JSON and corrupt the page.
        """
        g = nx.DiGraph()
        g.add_node("vm:__EDGES__", name="__EDGES__", type="vm")
        out = tmp_path / "placeholder.html"
        render_html(g, out)
        body = out.read_text(encoding="utf-8")

        # The label survives verbatim in the nodes payload...
        assert '"label": "__EDGES__"' in body
        # ...and the real edges payload was still substituted (empty list).
        assert "const RAW_EDGES = []" in body

    def test_federated_id_displays_unqualified_name(self, tmp_path):
        g = nx.DiGraph()
        g.add_node(
            "prod/vm:web", name="Web Server", type="vm", project="prod", root=""
        )
        out = tmp_path / "fed.html"
        render_html(g, out)
        body = out.read_text(encoding="utf-8")
        assert "Web Server" in body
        # The qualified ID still appears (it's the vis-network node ID),
        # but the human-readable label is just "Web Server".

    def test_cdn_pinned_to_exact_version(self, tmp_path):
        """The vis-network CDN URL must reference an exact version, not a
        floating major tag (vis-network@9). A floating tag can be silently
        republished; an exact pin can't. This guards the supply chain.
        """
        from infracontext.graph.render import _VIS_CDN, _VIS_VERSION

        # The constant carries the exact version, not a floating major.
        assert _VIS_VERSION in _VIS_CDN
        # Must look like X.Y.Z, not just '9'.
        parts = _VIS_VERSION.split(".")
        assert len(parts) == 3, f"{_VIS_VERSION} is not an exact semver pin"
        assert all(p.isdigit() for p in parts)

        # And CDN-mode HTML actually uses the pinned URL.
        out = tmp_path / "g.html"
        render_html(_sample_graph(), out, inline_js=False)
        assert _VIS_CDN in out.read_text(encoding="utf-8")
        # No floating '@9/' (the old insecure form).
        assert "vis-network@9/" not in out.read_text(encoding="utf-8")


class TestRenderHTMLSelfContained:
    """Default HTML output inlines the vendored vis-network bundle."""

    def test_vendored_bundle_matches_pinned_version_and_is_script_safe(self):
        """The bundle ships as package data, matches _VIS_VERSION, and must
        not contain '</script>' — it is embedded in an inline <script> tag,
        where that sequence would terminate the tag and corrupt the page.
        """
        from infracontext.graph.render import _VIS_VERSION, _vis_bundle_js

        bundle = _vis_bundle_js()
        assert len(bundle) > 100_000  # sanity: full library, not a stub
        assert _VIS_VERSION in bundle  # banner carries @version
        assert "</script>" not in bundle

    def test_default_output_inlines_bundle(self, tmp_path):
        from infracontext.graph.render import _vis_bundle_js

        out = tmp_path / "g.html"
        render_html(_sample_graph(), out)
        assert _vis_bundle_js() in out.read_text(encoding="utf-8")

    def test_default_output_references_no_external_urls(self, tmp_path):
        """Self-contained means offline: apart from license-comment URLs
        *inside* the inlined bundle, the page must contain no http(s)://
        at all — in particular no <script src> pointing at a CDN.
        """
        import re as _re

        from infracontext.graph.render import _vis_bundle_js

        out = tmp_path / "g.html"
        render_html(_sample_graph(), out)
        body = out.read_text(encoding="utf-8")

        assert 'src="http' not in body
        page_without_bundle = body.replace(_vis_bundle_js(), "")
        assert not _re.search(r"https?://", page_without_bundle)

    def test_cdn_mode_references_cdn_and_omits_bundle(self, tmp_path):
        from infracontext.graph.render import _VIS_CDN, _vis_bundle_js

        out = tmp_path / "g.html"
        render_html(_sample_graph(), out, inline_js=False)
        body = out.read_text(encoding="utf-8")
        assert f'<script src="{_VIS_CDN}"></script>' in body
        assert _vis_bundle_js() not in body

    def test_node_named_like_vis_placeholder_survives_as_data(self, tmp_path):
        """The bundle substitution is count=1 on a placeholder that precedes
        all user data — a node literally named __VIS_SCRIPT__ must not be
        replaced by the bundle.
        """
        g = nx.DiGraph()
        g.add_node("vm:x", name="__VIS_SCRIPT__", type="vm")
        out = tmp_path / "placeholder.html"
        render_html(g, out)
        body = out.read_text(encoding="utf-8")
        assert '"label": "__VIS_SCRIPT__"' in body
        # The placeholder in <head> was still substituted (no leftovers
        # outside the JSON payload).
        assert "\n__VIS_SCRIPT__\n" not in body


# ─── render_svg ───────────────────────────────────────────────────


@pytest.mark.skipif(not _HAS_MATPLOTLIB, reason="matplotlib not installed (viz extra)")
class TestRenderSVG:
    def test_writes_svg_file(self, tmp_path):
        out = tmp_path / "graph.svg"
        render_svg(_sample_graph(), out, title="Test")
        assert out.exists()
        body = out.read_text(encoding="utf-8")
        assert body.startswith("<?xml") or body.startswith("<svg")

    def test_empty_graph_writes_placeholder(self, tmp_path):
        out = tmp_path / "empty.svg"
        render_svg(nx.DiGraph(), out, title="Empty")
        body = out.read_text(encoding="utf-8")
        assert "<svg" in body
        assert "empty graph" in body

    def test_raises_above_node_limit(self, tmp_path, monkeypatch):
        """Spring layout is O(N^3)-ish; the cap is a safety rail.

        Setting IC_SVG_MAX_NODES=2 means a 3-node graph must trip it.
        """
        monkeypatch.setenv("IC_SVG_MAX_NODES", "2")
        with pytest.raises(ValueError, match="too large"):
            render_svg(_sample_graph(), tmp_path / "huge.svg")

    def test_cap_can_be_disabled(self, tmp_path, monkeypatch):
        """IC_SVG_MAX_NODES=0 disables the guard for power users."""
        monkeypatch.setenv("IC_SVG_MAX_NODES", "0")
        render_svg(_sample_graph(), tmp_path / "ok.svg")  # must not raise


@pytest.mark.skipif(_HAS_MATPLOTLIB, reason="only meaningful when matplotlib is missing")
class TestRenderSVGWithoutMatplotlib:
    def test_raises_clear_import_error(self, tmp_path):
        with pytest.raises(ImportError, match="matplotlib"):
            render_svg(_sample_graph(), tmp_path / "x.svg")


# ─── render_graphml ───────────────────────────────────────────────


class TestRenderGraphML:
    def test_roundtrip_preserves_nodes_and_edges(self, tmp_path):
        out = tmp_path / "graph.graphml"
        render_graphml(_sample_graph(), out)

        loaded = nx.read_graphml(out)
        assert loaded.number_of_nodes() == 3
        assert loaded.number_of_edges() == 2
        assert loaded.has_edge("vm:web", "vm:db")
        # Primitive attrs survive
        assert loaded.nodes["vm:web"].get("name") == "Web"
        assert loaded.nodes["vm:web"].get("type") == "vm"
        assert loaded.edges["vm:web", "vm:db"].get("type") == "depends_on"

    def test_strips_non_primitive_attributes(self):
        from infracontext.models.node import Node, NodeType
        from infracontext.models.relationship import Relationship, RelationshipType

        g = nx.DiGraph()
        node = Node(id="vm:web", slug="web", type=NodeType.VM, name="Web")
        rel = Relationship(
            source="vm:web", target="vm:db", type=RelationshipType.DEPENDS_ON
        )
        g.add_node("vm:web", node=node, name="Web", type="vm")
        g.add_node("vm:db", name="DB", type="vm")
        g.add_edge("vm:web", "vm:db", relationship=rel, type="depends_on")

        sanitized = _sanitize_for_graphml(g)
        assert "node" not in sanitized.nodes["vm:web"]
        assert "name" in sanitized.nodes["vm:web"]
        assert "relationship" not in sanitized.edges["vm:web", "vm:db"]
        assert sanitized.edges["vm:web", "vm:db"]["type"] == "depends_on"

    def test_primitive_attrs_drops_none(self):
        assert _primitive_attrs({"a": 1, "b": None, "c": "x"}) == {"a": 1, "c": "x"}

    def test_primitive_attrs_keeps_bool_and_float(self):
        assert _primitive_attrs({"flag": True, "ratio": 0.5}) == {
            "flag": True,
            "ratio": 0.5,
        }

    def test_primitive_attrs_coerces_strenum_to_plain_str(self):
        """NetworkX's GraphML writer checks ``type(v)``, not ``isinstance``,
        so StrEnum values must be collapsed to plain str — otherwise
        write_graphml raises ``GraphML writer does not support <enum>``.
        """
        from infracontext.models.node import NodeType

        result = _primitive_attrs({"type": NodeType.VM})
        assert result == {"type": "vm"}
        assert type(result["type"]) is str  # noqa: E721 — exact type check is the point

    def test_roundtrip_with_real_loader_attributes(self, tmp_path):
        """End-to-end: a graph carrying NodeType / RelationshipType enums
        (as load_graph attaches) must be GraphML-writable without error.
        """
        from infracontext.models.node import NodeType
        from infracontext.models.relationship import RelationshipType

        g = nx.DiGraph()
        g.add_node("vm:web", name="Web", type=NodeType.VM)
        g.add_node("vm:db", name="DB", type=NodeType.VM)
        g.add_edge("vm:web", "vm:db", type=RelationshipType.DEPENDS_ON)

        out = tmp_path / "enum.graphml"
        render_graphml(g, out)
        loaded = nx.read_graphml(out)
        assert loaded.nodes["vm:web"]["type"] == "vm"
        assert loaded.edges["vm:web", "vm:db"]["type"] == "depends_on"


# ─── mermaid ──────────────────────────────────────────────────────


class TestMermaidArrows:
    def test_arrow_map_covers_every_relationship_type(self):
        """Every RelationshipType member needs an explicit arrow choice.

        A new enum value without one would silently fall back to '-->';
        this test forces the decision when the enum grows.
        """
        from infracontext.models.relationship import RelationshipType

        assert set(MERMAID_EDGE_ARROWS) == {t.value for t in RelationshipType}

    def test_placement_edges_are_dashed(self):
        for rel in ("runs_on", "hosted_by", "member_of", "contains", "mounts"):
            assert MERMAID_EDGE_ARROWS[rel] == "-.->"

    def test_identity_edges_are_plain_links(self):
        for rel in ("resolves_to", "replicates_to"):
            assert MERMAID_EDGE_ARROWS[rel] == "---"

    def test_unknown_relationship_falls_back_to_solid_arrow(self):
        g = nx.DiGraph()
        g.add_node("a", name="A", type="vm")
        g.add_node("b", name="B", type="vm")
        g.add_edge("a", "b", type="quantum_entangled_with")
        assert "    a -->|quantum_entangled_with| b" in graph_to_mermaid(g)

    def test_untyped_edge_has_no_label(self):
        g = nx.DiGraph()
        g.add_node("a", name="A", type="vm")
        g.add_node("b", name="B", type="vm")
        g.add_edge("a", "b")
        assert "    a --> b" in graph_to_mermaid(g)


# ─── physical / datacenter layer (ic 0.4.0) ───────────────────────


def _physical_graph() -> nx.DiGraph:
    """A minimal site > rack > host placement graph with both edge
    directions: host/rack located_in their container, site contains its rack.
    """
    g = nx.DiGraph()
    g.add_node("site:dc1", name="DC1", type="site")
    g.add_node("rack:r1", name="Rack 1", type="rack")
    g.add_node("physical_host:h1", name="Host 1", type="physical_host")
    g.add_edge("physical_host:h1", "rack:r1", type="located_in")
    g.add_edge("rack:r1", "site:dc1", type="located_in")
    g.add_edge("site:dc1", "rack:r1", type="contains")
    return g


class TestPhysicalLayerStyles:
    def test_site_and_rack_join_the_physical_family(self):
        assert _style_for("site")[2] == "physical"
        assert _style_for("rack")[2] == "physical"

    def test_pdu_and_ups_form_the_power_family(self):
        assert _style_for("pdu")[2] == "power"
        assert _style_for("ups")[2] == "power"

    def test_power_family_is_distinct_from_physical(self):
        assert _style_for("pdu")[0] != _style_for("site")[0]
        assert _style_for("pdu")[2] != _style_for("site")[2]

    def test_located_in_edge_is_dashed_powered_by_and_manages_solid(self):
        from infracontext.graph.render import EDGE_TYPE_STYLES

        assert EDGE_TYPE_STYLES["located_in"][1] is True
        assert EDGE_TYPE_STYLES["powered_by"][1] is False
        assert EDGE_TYPE_STYLES["manages"][1] is False

    def test_mermaid_arrows_for_physical_edges(self):
        assert MERMAID_EDGE_ARROWS["located_in"] == "-.->"
        assert MERMAID_EDGE_ARROWS["powered_by"] == "-->"
        assert MERMAID_EDGE_ARROWS["manages"] == "-->"


class TestPhysicalLayerRender:
    def test_html_renders_site_rack_host(self, tmp_path):
        out = tmp_path / "physical.html"
        render_html(_physical_graph(), out)
        body = out.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in body
        assert "DC1" in body
        assert "Rack 1" in body
        assert "located_in" in body
        assert "contains" in body

    def test_vis_payload_styles_physical_nodes_and_edges(self):
        nodes, edges, _legend = _build_vis_payload(_physical_graph())
        by_id = {n["id"]: n for n in nodes}
        assert by_id["site:dc1"]["_category"] == "physical"
        assert by_id["rack:r1"]["_category"] == "physical"
        by_rel = {e["label"]: e for e in edges}
        assert by_rel["located_in"]["dashes"] is True
        assert by_rel["contains"]["dashes"] is True

    def test_mermaid_renders_site_rack_host_with_correct_arrows(self):
        text = graph_to_mermaid(_physical_graph())
        assert "physical_host_h1 -.->|located_in| rack_r1" in text
        assert "rack_r1 -.->|located_in| site_dc1" in text
        assert "site_dc1 -.->|contains| rack_r1" in text


class TestMermaidIds:
    def test_sanitizes_ic_ids(self):
        g = nx.DiGraph()
        g.add_node("vm:web-01")
        g.add_node("@fleet:prod/vm:pve-01")
        ids = _mermaid_ids(g)
        assert ids["vm:web-01"] == "vm_web_01"
        assert ids["@fleet:prod/vm:pve-01"] == "_fleet_prod_vm_pve_01"

    def test_collisions_get_numeric_suffix(self):
        """Distinct IDs may sanitize to the same string — each must still
        map to a unique mermaid identifier, deterministically.
        """
        g = nx.DiGraph()
        g.add_node("vm:web-01")
        g.add_node("vm:web_01")
        g.add_node("vm:web.01")
        ids = _mermaid_ids(g)
        assert ids["vm:web-01"] == "vm_web_01"
        assert ids["vm:web_01"] == "vm_web_01_2"
        assert ids["vm:web.01"] == "vm_web_01_3"
        assert len(set(ids.values())) == 3


class TestMermaidEscape:
    def test_escapes_label_breaking_characters(self):
        assert (
            _mermaid_escape('A "B" & <b>[x]')
            == "A &quot;B&quot; &amp; &lt;b&gt;&#91;x&#93;"
        )

    def test_ampersand_escaped_first(self):
        # '&' must not double-escape entities produced by later replacements.
        assert _mermaid_escape("<") == "&lt;"
        assert _mermaid_escape("&lt;") == "&amp;lt;"

    def test_label_with_quotes_survives_in_output(self):
        g = nx.DiGraph()
        g.add_node("vm:x", name='Say "hi" [now]', type="vm")
        text = graph_to_mermaid(g)
        assert 'vm_x["Say &quot;hi&quot; &#91;now&#93; (vm)"]' in text


class TestGraphToMermaid:
    def test_golden_single_project(self):
        """Exact serialization of the small fixture graph — the format
        contract for `ic graph render -f mermaid`.
        """
        expected = (
            "flowchart TD\n"
            '    vm_web["Web (vm)"]\n'
            '    vm_db["DB (vm)"]\n'
            '    physical_host_h1["Host 1 (physical_host)"]\n'
            "    vm_web -->|depends_on| vm_db\n"
            "    vm_db -.->|runs_on| physical_host_h1\n"
        )
        assert graph_to_mermaid(_sample_graph()) == expected

    def test_empty_graph_is_valid_flowchart(self):
        assert graph_to_mermaid(nx.DiGraph()) == "flowchart TD\n"

    def test_merged_graph_groups_scopes_into_subgraphs(self):
        g = nx.DiGraph()
        g.add_node("prod/vm:web", name="Web", type="vm", project="prod")
        g.add_node(
            "@fleet:prod/vm:pve-01",
            name="PVE 01",
            type="physical_host",
            project="prod",
            root="fleet",
        )
        g.add_edge("prod/vm:web", "@fleet:prod/vm:pve-01", type="runs_on")

        expected = (
            "flowchart TD\n"
            '    subgraph scope_prod["prod"]\n'
            '        prod_vm_web["Web (vm)"]\n'
            "    end\n"
            '    subgraph scope__fleet_prod["@fleet:prod"]\n'
            '        _fleet_prod_vm_pve_01["PVE 01 (physical_host)"]\n'
            "    end\n"
            "    prod_vm_web -.->|runs_on| _fleet_prod_vm_pve_01\n"
        )
        assert graph_to_mermaid(g) == expected

    def test_single_project_graph_stays_flat(self):
        text = graph_to_mermaid(_sample_graph())
        assert "subgraph" not in text

    def test_node_without_name_or_type_uses_unqualified_id(self):
        g = nx.DiGraph()
        g.add_node("vm:mystery")
        assert '    vm_mystery["vm:mystery"]' in graph_to_mermaid(g)

    def test_strenum_edge_types_map_to_arrows(self):
        """The loader attaches RelationshipType StrEnums, not plain strings —
        the arrow lookup must work for both.
        """
        from infracontext.models.relationship import RelationshipType

        g = nx.DiGraph()
        g.add_node("vm:a", name="A", type="vm")
        g.add_node("vm:b", name="B", type="vm")
        g.add_edge("vm:a", "vm:b", type=RelationshipType.RUNS_ON)
        assert "    vm_a -.->|runs_on| vm_b" in graph_to_mermaid(g)


class TestMermaidSizeWarning:
    def test_small_graph_no_warning(self):
        assert mermaid_size_warning(_sample_graph()) is None

    def test_warns_above_limit_but_rendering_still_works(self, monkeypatch):
        """Mirror of the IC_SVG_MAX_NODES pattern — except mermaid only
        warns; the text format has no hard failure mode.
        """
        monkeypatch.setenv("IC_MERMAID_MAX_NODES", "2")
        warning = mermaid_size_warning(_sample_graph())
        assert warning is not None
        assert "3 nodes" in warning
        # Warn, don't fail: serialization must still succeed.
        assert graph_to_mermaid(_sample_graph()).startswith("flowchart TD")

    def test_zero_disables_warning(self, monkeypatch):
        monkeypatch.setenv("IC_MERMAID_MAX_NODES", "0")
        assert mermaid_size_warning(_sample_graph()) is None

    def test_garbage_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("IC_MERMAID_MAX_NODES", "lots")
        assert mermaid_size_warning(_sample_graph()) is None


class TestRenderMermaid:
    def test_writes_file(self, tmp_path):
        out = tmp_path / "graph.mmd"
        render_mermaid(_sample_graph(), out)
        assert out.read_text(encoding="utf-8") == graph_to_mermaid(_sample_graph())


# ─── CLI: `ic graph render` ───────────────────────────────────────


class TestRenderCLI:
    """Exercise the Typer command, mocking the loaders so the test does
    not need a full environment on disk. The point is to lock the routing
    between flags and loaders — a regression that called load_graph instead
    of load_merged_graph under -A would otherwise pass everything else.
    """

    def _runner(self):
        from typer.testing import CliRunner

        from infracontext.cli.graph import app

        return CliRunner(), app

    def test_all_projects_uses_merged_loader(self, tmp_path, monkeypatch):
        runner, app = self._runner()
        called: dict[str, bool] = {}

        def fake_merged():
            called["merged"] = True
            return _sample_graph()

        def fake_single(_slug):
            called["single"] = True
            return _sample_graph()

        # Stub the loaders and the env/project guards so the test runs
        # without `.infracontext/` on disk.
        monkeypatch.setattr("infracontext.cli.graph.load_merged_graph", fake_merged)
        monkeypatch.setattr("infracontext.cli.graph.load_graph", fake_single)
        monkeypatch.setattr("infracontext.cli.graph.require_environment", lambda: None)
        monkeypatch.setattr("infracontext.cli.graph.require_project", lambda: "demo")

        out = tmp_path / "all.graphml"
        result = runner.invoke(app, ["render", "-A", "-f", "graphml", "-o", str(out)])

        assert result.exit_code == 0, result.output
        assert called.get("merged") is True
        assert "single" not in called
        assert out.exists()

    def test_single_project_uses_load_graph(self, tmp_path, monkeypatch):
        runner, app = self._runner()
        called: dict[str, bool] = {}

        monkeypatch.setattr(
            "infracontext.cli.graph.load_merged_graph",
            lambda: (_ for _ in ()).throw(AssertionError("merged loader called")),
        )

        def fake_single(_slug):
            called["single"] = True
            return _sample_graph()

        monkeypatch.setattr("infracontext.cli.graph.load_graph", fake_single)
        monkeypatch.setattr("infracontext.cli.graph.require_environment", lambda: None)
        monkeypatch.setattr("infracontext.cli.graph.require_project", lambda: "demo")

        out = tmp_path / "one.graphml"
        result = runner.invoke(app, ["render", "-f", "graphml", "-o", str(out)])

        assert result.exit_code == 0, result.output
        assert called.get("single") is True
        assert out.exists()

    def test_overwrite_existing_file_emits_warning(self, tmp_path, monkeypatch):
        runner, app = self._runner()
        monkeypatch.setattr(
            "infracontext.cli.graph.load_graph", lambda _slug: _sample_graph()
        )
        monkeypatch.setattr("infracontext.cli.graph.require_environment", lambda: None)
        monkeypatch.setattr("infracontext.cli.graph.require_project", lambda: "demo")

        out = tmp_path / "graph.graphml"
        out.write_text("pre-existing")
        result = runner.invoke(app, ["render", "-f", "graphml", "-o", str(out)])

        assert result.exit_code == 0, result.output
        assert "Overwrote" in result.output

    def test_open_flag_opens_rendered_file(self, tmp_path, monkeypatch):
        """--open hands the rendered file to the default application."""
        runner, app = self._runner()
        monkeypatch.setattr(
            "infracontext.cli.graph.load_graph", lambda _slug: _sample_graph()
        )
        monkeypatch.setattr("infracontext.cli.graph.require_environment", lambda: None)
        monkeypatch.setattr("infracontext.cli.graph.require_project", lambda: "demo")

        opened: list[str] = []
        import webbrowser

        monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

        out = tmp_path / "graph.html"
        result = runner.invoke(app, ["render", "-o", str(out), "--open"])

        assert result.exit_code == 0, result.output
        assert opened == [out.resolve().as_uri()]

    def test_no_open_flag_does_not_open(self, tmp_path, monkeypatch):
        runner, app = self._runner()
        monkeypatch.setattr(
            "infracontext.cli.graph.load_graph", lambda _slug: _sample_graph()
        )
        monkeypatch.setattr("infracontext.cli.graph.require_environment", lambda: None)
        monkeypatch.setattr("infracontext.cli.graph.require_project", lambda: "demo")

        opened: list[str] = []
        import webbrowser

        monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))

        result = runner.invoke(
            app, ["render", "-o", str(tmp_path / "graph.html")]
        )

        assert result.exit_code == 0, result.output
        assert opened == []

    def _patch_loaders(self, monkeypatch):
        monkeypatch.setattr(
            "infracontext.cli.graph.load_graph", lambda _slug: _sample_graph()
        )
        monkeypatch.setattr("infracontext.cli.graph.require_environment", lambda: None)
        monkeypatch.setattr("infracontext.cli.graph.require_project", lambda: "demo")

    def test_mermaid_format_writes_mmd_default_filename(self, tmp_path, monkeypatch):
        """`-f mermaid` without -o writes <name>.mmd, not <name>.mermaid."""
        runner, app = self._runner()
        self._patch_loaders(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["render", "-f", "mermaid"])

        assert result.exit_code == 0, result.output
        out = tmp_path / "infracontext-graph.mmd"
        assert out.exists()
        assert out.read_text(encoding="utf-8").startswith("flowchart TD")

    def test_mermaid_output_dash_writes_to_stdout(self, tmp_path, monkeypatch):
        """`-o -` pipes the mermaid text to stdout, with nothing else mixed in."""
        runner, app = self._runner()
        self._patch_loaders(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["render", "-f", "mermaid", "-o", "-"])

        assert result.exit_code == 0, result.output
        assert result.stdout == graph_to_mermaid(_sample_graph())
        assert not list(tmp_path.iterdir())  # no file written

    def test_output_dash_rejected_for_non_mermaid_formats(self, tmp_path, monkeypatch):
        runner, app = self._runner()
        self._patch_loaders(monkeypatch)
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["render", "-f", "html", "-o", "-"])

        assert result.exit_code == 1
        assert "mermaid" in result.output

    def test_mermaid_size_warning_goes_to_stderr_not_stdout(self, tmp_path, monkeypatch):
        """Piping `-o -` into a markdown file must never capture the warning."""
        runner, app = self._runner()
        self._patch_loaders(monkeypatch)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IC_MERMAID_MAX_NODES", "2")

        result = runner.invoke(app, ["render", "-f", "mermaid", "-o", "-"])

        assert result.exit_code == 0, result.output
        assert result.stdout == graph_to_mermaid(_sample_graph())
        assert "mermaid renderers get slow" in result.stderr

    def test_html_default_is_self_contained(self, tmp_path, monkeypatch):
        runner, app = self._runner()
        self._patch_loaders(monkeypatch)

        out = tmp_path / "graph.html"
        result = runner.invoke(app, ["render", "-o", str(out)])

        assert result.exit_code == 0, result.output
        assert 'src="http' not in out.read_text(encoding="utf-8")

    def test_html_cdn_flag_restores_script_src(self, tmp_path, monkeypatch):
        runner, app = self._runner()
        self._patch_loaders(monkeypatch)

        out = tmp_path / "graph.html"
        result = runner.invoke(app, ["render", "-o", str(out), "--cdn"])

        assert result.exit_code == 0, result.output
        from infracontext.graph.render import _VIS_CDN

        assert f'<script src="{_VIS_CDN}"></script>' in out.read_text(encoding="utf-8")
