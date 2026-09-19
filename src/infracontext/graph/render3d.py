"""Interactive 3D rendering of the infrastructure graph (``-f 3d``).

Self-contained HTML page built on the vendored `3d-force-graph`_ bundle
(three.js included). The page's centerpiece is **outage mode**: clicking a
node lights up its precomputed blast radius — every node that transitively
depends on it — with the same semantics as ``ic graph impact``
(:func:`infracontext.graph.analysis.calculate_impact`). Impact sets are
computed here in Python and embedded, so the page never re-derives dependency
direction in JavaScript and cannot drift from the CLI.

Node colors/categories and link colors reuse the 2D renderer's
:data:`~infracontext.graph.render.NODE_CATEGORIES` and
:data:`~infracontext.graph.render.EDGE_TYPE_STYLES`, so both views speak the
same visual language. Labels are drawn as an HTML overlay projected via
``graph2ScreenCoords`` — crisp text without mixing a second three.js
instance into the bundled one.

.. _3d-force-graph: https://github.com/vasturiano/3d-force-graph
"""

from __future__ import annotations

import html
import re
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from infracontext.graph.render import (
    _FALLBACK_EDGE_STYLE,
    EDGE_TYPE_STYLES,
    _display_label,
    _js_safe,
    _node_scope,
    _style_for,
)

if TYPE_CHECKING:
    import networkx as nx

_FG3D_VERSION = "1.80.0"


def _fg3d_bundle_js() -> str:
    """Return the vendored 3d-force-graph bundle for inline embedding."""
    bundle = (
        resources.files("infracontext.graph")
        / "assets"
        / f"3d-force-graph-{_FG3D_VERSION}.min.js"
    ).read_text(encoding="utf-8")
    if "</script>" in bundle:
        raise ValueError(
            "Vendored 3d-force-graph bundle contains '</script>' and cannot be "
            "inlined safely — re-vendor the asset (see graph/assets/README.md)."
        )
    return bundle


def _node_props(data: dict[str, Any]) -> list[list[str]]:
    """Type-aware property rows for the click panel — the "context-relevant
    information" a responder wants at a glance, extracted from the attribute
    soup the collectors wrote. Order = display order; capped to stay a panel,
    not a dossier (the YAML view has the rest)."""
    node_obj = data.get("node")
    a = dict(getattr(node_obj, "attributes", None) or {})
    hw = a.get("hardware") or {}
    rows: list[list[str]] = []

    def add(label: str, value: Any) -> None:
        if value not in (None, "", [], {}):
            rows.append([label, str(value)])

    add("OS", a.get("os_pretty"))
    add("Virtualization", a.get("virtualization"))
    if a.get("cpu_cores"):
        add("CPU", f"{a['cpu_cores']} cores")
    if a.get("memory_mb"):
        add("Memory", f"{round(a['memory_mb'] / 1024)} GB")
    if a.get("proxmox_node"):
        add("PVE placement",
            f"{a.get('proxmox_cluster', '?')} / {a['proxmox_node']}"
            + (f" (VMID {a['proxmox_vmid']})" if a.get("proxmox_vmid") else ""))
    if hw.get("model"):
        add("Hardware", f"{hw.get('manufacturer', '')} {hw['model']}".strip())
    add("Serial", a.get("dmi_serial") or hw.get("serial"))
    if hw.get("rack_position") is not None:
        face = f" ({hw['rack_face']})" if hw.get("rack_face") else ""
        add("Rack position", f"U{int(hw['rack_position'])}{face}")
    if a.get("u_height"):
        add("Height", f"{a['u_height']}U")
    add("NetBox name", a.get("netbox_name"))
    add("iLO", a.get("ilo_ip"))
    add("PVE version", a.get("pve_version"))
    add("Export", a.get("export_path"))
    if a.get("size"):
        add("Size", f"{a['size']} ({a.get('used_percent', '?')} used)")
    add("MAC", a.get("mac"))
    add("VLAN tag", a.get("vlan_tag"))
    triage = getattr(node_obj, "triage", None)
    if triage is not None and getattr(triage, "services", None):
        add("Services", ", ".join(triage.services[:6]))
    atts = getattr(node_obj, "attachments", None) or []
    if atts:
        titles = ", ".join((att.title or att.file.rsplit("/", 1)[-1]) for att in atts[:3])
        add("Attachments", f"{len(atts)} ({titles})")
    add("Checkmk folder", a.get("checkmk_folder"))
    add("Sources", a.get("sources"))
    add("Collected", a.get("ssh_collected_at") or a.get("collected_at"))
    # "Show more details if available": generous cap — the popover scrolls.
    return [[html.escape(k), html.escape(v)] for k, v in rows[:18]]


def _node_links(data: dict[str, Any]) -> list[dict[str, str]]:
    """Clickable endpoints for the node card: served domains, admin UIs (iLO,
    device web UIs), and observability dashboards. http(s) only — anything
    else has no meaningful browser target."""
    node_obj = data.get("node")
    a = getattr(node_obj, "attributes", None) or {}
    links: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(label: str, url: str) -> None:
        if not url.startswith(("http://", "https://")) or url in seen:
            return
        seen.add(url)
        links.append({"label": html.escape(label), "url": html.escape(url)})

    for dom in list(getattr(node_obj, "domains", None) or [])[:6]:
        if "." in dom and " " not in dom:
            add(dom, f"https://{dom}")
    if a.get("ilo_ip"):
        add(f"iLO ({a['ilo_ip']})", f"https://{a['ilo_ip']}")
    for obs in getattr(node_obj, "observability", None) or []:
        url = getattr(obs, "url", "") or ""
        if url:
            add(getattr(obs, "name", "") or getattr(obs, "type", "link"), url)
    for ep in getattr(node_obj, "endpoints", None) or []:
        proto = str(getattr(ep, "protocol", "") or "")
        if proto in ("http", "https"):
            port = getattr(ep, "port", None)
            for host in (getattr(ep, "domains", None) or [])[:2]:
                add(f"{getattr(ep, 'name', proto)} ({host}:{port})",
                    f"{proto}://{host}:{port}")
    return links[:8]


def build_3d_payload(graph: nx.DiGraph) -> dict[str, Any]:
    """Build the JSON payload for the 3D page. Pure — no I/O.

    Returns a dict with ``nodes``, ``links``, ``impact`` (per-node blast
    radius: direct dependents + all transitive dependents, mirroring
    :func:`calculate_impact`), and ``legend``/``linkLegend`` rows.
    """
    import networkx as nx

    nodes: list[dict[str, Any]] = []
    degree = dict(graph.degree())
    category_counts: dict[str, dict[str, Any]] = {}
    hv_cluster_ids = {n for n, d in graph.nodes(data=True) if d.get("type") == "hypervisor_cluster"}
    hypervisors = {
        u for u, v, d in graph.edges(data=True)
        if d.get("type") == "member_of" and v in hv_cluster_ids
    }

    for node_id, data in graph.nodes(data=True):
        node_type = data.get("type")
        color, _shape, category = _style_for(node_type)
        label = _display_label(node_id, data.get("name"))
        node_obj = data.get("node")  # pydantic Node attached by the loader
        ips = list(getattr(node_obj, "ip_addresses", None) or [])
        domains = list(getattr(node_obj, "domains", None) or [])
        nodes.append(
            {
                "id": node_id,
                # Labels land in DOM text nodes / textContent only, but escape
                # anyway so a future innerHTML use cannot turn them into XSS.
                "label": html.escape(label),
                "name": html.escape(str(data.get("name") or label)),
                "type": html.escape(str(node_type or "unknown")),
                "category": category,
                "color": color,
                "val": 1 + degree.get(node_id, 0),
                "ips": [html.escape(ip) for ip in ips],
                "domains": [html.escape(dm) for dm in domains],
                "scope": html.escape(_node_scope(node_id, data) or ""),
                "ssh": html.escape(str(getattr(node_obj, "ssh_alias", None) or "")),
                "hv": node_id in hypervisors,
                "links": _node_links(data),
                "desc": html.escape(str(getattr(data.get("node"), "description", None) or ""))[:220],
                "props": _node_props(data),
            }
        )
        cc = category_counts.setdefault(category, {"color": color, "count": 0})
        cc["count"] += 1

    links: list[dict[str, Any]] = []
    link_counts: dict[str, dict[str, Any]] = {}
    for u, v, data in graph.edges(data=True):
        relation = str(data.get("type", "") or "other")
        color, dashed = EDGE_TYPE_STYLES.get(relation, _FALLBACK_EDGE_STYLE)
        # Links are emitted REVERSED (dependency -> dependent): directional
        # particles animate source->target, and in outage mode they must show
        # the failure spreading OUTWARD from the failed node to its
        # dependents — not dependents streaming into the failure.
        links.append(
            {
                "source": v,
                "target": u,
                "rel": relation,
                "color": color,
                "dashed": dashed,
                "desc": html.escape(str(data.get("description", "") or ""))[:200],
            }
        )
        lc = link_counts.setdefault(relation, {"color": color, "count": 0})
        lc["count"] += 1

    # Cluster assignment for spatial grouping: hypervisor clusters and their
    # members/guests get pulled toward per-cluster anchors in the page, so
    # e.g. APP/DB/DEV Proxmox clusters render as distinct constellations with
    # inter-cluster links visibly spanning between them.
    cluster_of: dict[str, str] = {}
    hv_name = {
        n: str(d.get("name") or n)
        for n, d in graph.nodes(data=True)
        if d.get("type") == "hypervisor_cluster"
    }
    for n, d in graph.nodes(data=True):
        if d.get("type") == "hypervisor_cluster":
            cluster_of[n] = hv_name[n]
            continue
        node_obj = d.get("node")
        attrs = getattr(node_obj, "attributes", None) or {}
        pc = attrs.get("proxmox_cluster")
        if pc:
            cluster_of[n] = str(pc)
    for u, v, data in graph.edges(data=True):
        if data.get("type") == "member_of" and v in hv_name:
            cluster_of.setdefault(u, hv_name[v])
    # Normalize hypervisor-cluster display names to their attribute form when
    # they differ (e.g. node name "APP-Cluster" == attribute value).
    for node in nodes:
        node["cluster"] = html.escape(cluster_of.get(node["id"], ""))

    # Blast radius per node — identical direction convention to
    # calculate_impact: predecessors depend on the node; ancestors are the
    # full transitive dependent set.
    impact: dict[str, dict[str, Any]] = {}
    for node_id in graph.nodes():
        direct = sorted(graph.predecessors(node_id))
        affected = sorted(nx.ancestors(graph, node_id))
        impact[node_id] = {"direct": direct, "all": affected}

    return {
        "nodes": nodes,
        "links": links,
        "impact": impact,
        "legend": [
            {"category": cat, "color": row["color"], "count": row["count"]}
            for cat, row in sorted(category_counts.items())
        ],
        "linkLegend": [
            {"rel": rel, "color": row["color"], "count": row["count"]}
            for rel, row in sorted(link_counts.items())
        ],
    }


def _renderer_version() -> str:
    """The infracontext version that produced the page, for provenance.

    Embedded in the artifact so a committed render is self-describing and
    reproducible: it records exactly which release to install to regenerate
    identical bytes. Deterministic (no timestamps), so it never adds churn.
    """
    try:
        from importlib.metadata import version

        return version("infracontext")
    except Exception:
        return "unknown"


def render_html_3d(graph: nx.DiGraph, output_path: Path, title: str = "Infrastructure") -> None:
    """Write the interactive 3D outage-explorer page to ``output_path``."""
    output_path = Path(output_path)
    payload = build_3d_payload(graph)
    stats = f"{graph.number_of_nodes()} nodes · {graph.number_of_edges()} links"

    # Single-pass substitution (same discipline as the 2D renderer): the
    # substituted JSON is never rescanned, and the bundle is inserted with a
    # non-interpreting replacement callable.
    script = re.sub(r"__DATA__", lambda _m: _js_safe(payload), _SCRIPT, count=1)
    page = _TEMPLATE.format(
        title=html.escape(title), stats=stats, styles=_STYLES,
        version=html.escape(_renderer_version()),
    )
    page = re.sub(r"__FG3D_BUNDLE__", lambda _m: _fg3d_bundle_js(), page, count=1)
    page = re.sub(r"__APP_SCRIPT__", lambda _m: script, page, count=1)
    output_path.write_text(page, encoding="utf-8")


_STYLES = """<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body { background: #04060f; color: #dfe3ee; overflow: hidden;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  #space-bg { position: absolute; inset: 0; }
  #graph { position: absolute; inset: 0; }
  #views { margin-top: 10px; padding: 6px; display: flex; flex-wrap: wrap; gap: 4px; }
  .view-btn { background: transparent; border: 1px solid #2b3557; color: #9aa5c4;
              font-size: 11px; padding: 4px 9px; border-radius: 6px; cursor: pointer; }
  .view-btn:hover { border-color: #4a5a8a; color: #dfe3ee; }
  .view-btn.active { background: #223058; border-color: #3d4f8a; color: #fff; }
  .ndesc { font-size: 12px; color: #a8b2cc; margin: 0 0 10px; line-height: 1.45; }
  .props { display: grid; grid-template-columns: auto 1fr; gap: 2px 12px; margin-bottom: 12px; }
  .props .pk { font-size: 10px; color: #7d88a6; text-transform: uppercase;
               letter-spacing: 0.05em; padding-top: 1px; }
  .props .pv { font-size: 12px; color: #dfe3ee; word-break: break-word; }
  #labels { position: absolute; inset: 0; pointer-events: none; overflow: hidden; }
  .nlabel { position: absolute; transform: translate(-50%, -160%); font-size: 11px;
            color: #cfd6e6; text-shadow: 0 1px 3px #000, 0 0 6px #000;
            white-space: nowrap; pointer-events: none; }
  .nlabel.hub { font-size: 12px; font-weight: 600; }
  .nlabel.hit { color: #ffb4b4; font-weight: 600; }
  .nlabel.origin { color: #ff6b6b; font-size: 13px; font-weight: 700; }
  .cluster-caption { position: absolute; transform: translate(-50%, -50%);
                     font-size: 17px; font-weight: 800; letter-spacing: 0.16em;
                     text-transform: uppercase; color: #3d4a70;
                     text-shadow: 0 1px 4px #000; pointer-events: auto;
                     cursor: pointer; user-select: none; }
  .cluster-caption:hover { color: #6b7cad; }
  .cluster-caption.off { opacity: 0.35; text-decoration: line-through; }

  #hud { position: absolute; top: 0; left: 0; right: 0; display: flex;
         justify-content: space-between; align-items: flex-start; padding: 14px 16px;
         pointer-events: none; }
  .panel { background: rgba(10, 14, 28, 0.82); border: 1px solid #232b45;
           border-radius: 10px; backdrop-filter: blur(8px); pointer-events: auto; }
  #title-card { padding: 10px 14px; }
  #title-card h1 { font-size: 15px; font-weight: 650; letter-spacing: 0.01em; }
  #title-card .stats { font-size: 11px; color: #6f7a96; margin-top: 2px; }

  #searchbox { margin-top: 10px; width: 300px; padding: 8px 12px; }
  #search { width: 100%; background: transparent; border: none; outline: none;
            color: #dfe3ee; font-size: 13px; }
  #search::placeholder { color: #5a648066; color: #5a6480; }
  #search-results { max-height: 220px; overflow-y: auto; margin-top: 4px; display: none; }
  .search-item { padding: 5px 8px; border-radius: 6px; font-size: 12px; cursor: pointer;
                 display: flex; gap: 8px; align-items: center; }
  .search-item:hover { background: #1b2340; }
  .sdot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
  .smeta { color: #66719000; color: #667190; font-size: 11px; margin-left: auto; }

  /* Two selection surfaces: #node-card is the popover attached to the star
     (identity + details), #side/#impact-card is the fixed right panel with
     the outage/impact analysis. */
  #side { position: absolute; top: 14px; right: 16px; width: 330px;
          max-height: calc(100% - 28px); display: flex; flex-direction: column;
          gap: 10px; z-index: 25; }
  #impact-card { padding: 14px; display: none; overflow-y: auto; }
  /* No left/top transition on the popover: it is repositioned every frame to
     track its star (a transition would lag and jitter). The spawn "pop" is a
     one-shot transform/opacity animation re-triggered on each open. */
  #node-card { position: fixed; width: 300px; max-height: 54vh; padding: 13px;
               display: none; overflow-y: auto; z-index: 30; pointer-events: auto;
               box-shadow: 0 12px 44px rgba(0, 0, 0, 0.55); }
  #node-card.pop { animation: cardPop 170ms cubic-bezier(0.2, 0.9, 0.3, 1.2); }
  @keyframes cardPop {
    from { opacity: 0; transform: scale(0.82) translateX(-6px); }
    to   { opacity: 1; transform: scale(1) translateX(0); }
  }
  /* Nub anchoring the card to its star. position:FIXED and a sibling of the
     card (not a child) so it is immune to the card's own scroll — an absolute
     child would drag with the scrolled content and slide off the node. JS
     places it in viewport coords at the node's screen-Y, and hides it when the
     node isn't beside the card's visible span. */
  .card-nub { position: fixed; width: 12px; height: 12px; z-index: 31;
              display: none; background: rgba(10, 14, 28, 0.82);
              transform: translate(-50%, -50%) rotate(45deg); }
  .card-nub.left  { border-left: 1px solid #232b45; border-bottom: 1px solid #232b45; }
  .card-nub.right { border-right: 1px solid #232b45; border-top: 1px solid #232b45; }
  #impact-card h2, #node-card h2 { font-size: 14px; }
  #impact-card .subtitle, #node-card .subtitle { font-size: 11px; color: #667190;
                 margin: 2px 0 10px; }
  .impact-stats { display: flex; gap: 8px; margin-bottom: 10px; }
  .stat { flex: 1; background: #131a31; border-radius: 8px; padding: 8px; text-align: center; }
  .stat b { display: block; font-size: 20px; font-weight: 700; }
  .stat span { font-size: 10px; color: #7d88a6; text-transform: uppercase; letter-spacing: 0.06em; }
  .stat.red b { color: #ff6b6b; }
  .stat.amber b { color: #ffc46b; }
  .impact-group { margin-top: 8px; }
  .impact-group h3 { font-size: 11px; color: #7d88a6; text-transform: uppercase;
                     letter-spacing: 0.05em; margin-bottom: 4px; }
  .impact-node { padding: 3px 8px; margin: 2px 0; border-left: 3px solid #333;
                 border-radius: 4px; font-size: 12px; cursor: pointer;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .impact-node:hover { background: #1b2340; }
  .hint { font-size: 11px; color: #667190; line-height: 1.5; }
  #impact-card .close, #node-card .close { float: right; cursor: pointer;
                        color: #667190; font-size: 16px;
                        line-height: 1; padding: 2px 4px; }
  #impact-card .close:hover, #node-card .close:hover { color: #fff; }
  .ipline { font-size: 11px; color: #8fa0c8; margin-bottom: 8px; word-break: break-all; }
  .nlinks { display: flex; flex-wrap: wrap; gap: 5px; margin: 2px 0 10px; }
  .nlink { font-size: 11px; color: #7fb2ff; border: 1px solid #2b3557;
           border-radius: 6px; padding: 2px 8px; text-decoration: none; }
  .nlink:hover { border-color: #4a5a8a; background: #17203c; }
  .nrel { margin: 4px 0; }
  .nrel summary { cursor: pointer; font-size: 11px; color: #7d88a6;
                  text-transform: uppercase; letter-spacing: 0.05em;
                  user-select: none; }
  .nrel summary:hover { color: #aab3cc; }
  .nrel-row { padding: 2px 8px; margin: 2px 0 2px 6px; font-size: 12px;
              cursor: pointer; border-left: 2px solid #2b3557; border-radius: 3px;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .nrel-row:hover { background: #1b2340; }

  #legend-card { position: absolute; bottom: 14px; left: 16px; padding: 12px 14px;
                 max-width: 300px; }
  #legend-card h3 { font-size: 10px; color: #7d88a6; text-transform: uppercase;
                    letter-spacing: 0.06em; margin: 6px 0 4px; }
  .lrow { display: flex; align-items: center; gap: 7px; padding: 2px 4px; font-size: 12px;
          cursor: pointer; border-radius: 4px; user-select: none; }
  .lrow:hover { background: #1b2340; }
  .lrow.off { opacity: 0.32; }
  .ldot { width: 10px; height: 10px; border-radius: 50%; }
  .ldash { width: 14px; height: 3px; border-radius: 2px; }
  .lcount { margin-left: auto; color: #566080; font-size: 10px; }

  #controls { position: absolute; bottom: 14px; right: 16px; padding: 10px 12px;
              display: flex; flex-direction: column; gap: 6px; }
  #controls label { display: flex; align-items: center; gap: 7px; font-size: 12px;
                    color: #aab3cc; cursor: pointer; user-select: none; }
  #controls .keyhint { font-size: 10px; color: #566080; margin-top: 4px; }
</style>"""


_SCRIPT = """<script>
const DATA = __DATA__;
const byId = new Map(DATA.nodes.map(n => [n.id, n]));

// ── graph setup ───────────────────────────────────────────────────
const state = {
  origin: null,          // selected outage node id
  affected: new Set(),   // blast radius of origin
  direct: new Set(),
  hidden: new Set(),     // hidden categories
  hiddenRels: new Set(),
  hiddenClusters: new Set(),  // clusters toggled off via their caption
  labelsOn: true,
  particlesOn: true,
  hover: null,
};

// ── view presets: different lenses over the same graph ──
const VIEWS = {
  all:        { label: 'Everything', types: null, rels: null },
  datacenter: { label: 'Datacenter', types: ['site','rack','pdu','ups','physical_host',
                  'network_device','hypervisor_cluster'],
                rels: ['located_in','powered_by','manages','contains','member_of','connects_to'] },
  hypervisor: { label: 'Hypervisors',
                pred: n => n.type === 'hypervisor_cluster' || n.hv,
                rels: ['member_of','manages','connects_to'] },
  compute:    { label: 'VMs & Containers', types: ['vm','lxc_container','oci_container','docker_compose',
                  'podman_compose','podman_quadlet','k8s_cluster','service_cluster','nfs_share'],
                rels: ['connects_to','depends_on','routes_to','fronted_by','mounts','member_of','runs_on'] },
  services:   { label: 'Services & Apps', types: ['application','service_cluster','service','domain',
                  'dns_zone','external_service','cdn_endpoint','vm','lxc_container'],
                rels: ['depends_on','uses','routes_to','fronted_by','contains','resolves_to','connects_to'] },
};
let currentView = 'all';

function inView(n) {
  const v = VIEWS[currentView];
  if (v.pred) return v.pred(n);
  return !v.types || v.types.includes(n.type);
}

function nodeVisible(n) {
  return inView(n) && !state.hidden.has(n.category) && !state.hiddenClusters.has(n.cluster);
}

function relInView(rel) {
  const v = VIEWS[currentView];
  return !v.rels || v.rels.includes(rel);
}

const elGraph = document.getElementById('graph');
// Orbit controls: trackball (the default) has no autoRotate support.
const Graph = ForceGraph3D({ controlType: 'orbit' })(elGraph)
  .graphData({ nodes: DATA.nodes, links: DATA.links })
  .backgroundColor('#04060f')
  .showNavInfo(false)
  .nodeLabel(null)
  .nodeVal(n => n.val)
  .nodeResolution(16)
  .nodeColor(nodeColor)
  .nodeOpacity(0.92)
  .linkColor(linkColor)
  .linkOpacity(0.45)
  .linkWidth(l => inBlast(l) ? 1.6 : 0.4)
  .linkDirectionalParticles(l => (state.particlesOn && inBlast(l)) ? 3 : 0)
  .linkDirectionalParticleWidth(1.6)
  .linkDirectionalParticleSpeed(0.006)
  .nodeVisibility(n => nodeVisible(n))
  .linkVisibility(l => !state.hiddenRels.has(l.rel) && relInView(l.rel)
      && nodeVisible(nodeOf(l.source))
      && nodeVisible(nodeOf(l.target)))
  .onNodeClick(n => selectOrigin(n.id))
  .onNodeHover(n => { state.hover = n ? n.id : null; elGraph.style.cursor = n ? 'pointer' : null; })
  .onBackgroundClick(clearOrigin);

// ── stars: emissive-shell glow around the default node spheres. The
// vendored bundle does not export THREE (it only *checks* window.THREE), so
// no Sprite/Texture classes are reachable — instead we harvest constructors
// from the live meshes (n.__threeObj) and layer transparent, additively
// blended, emissive Lambert shells: a self-lit core plus two halo shells.
// Decoration runs in the RAF loop and is idempotent, so it survives object
// re-creation on visibility changes.
const ADDITIVE_BLENDING = 2; // THREE.AdditiveBlending — stable numeric constant
function starify(n, mesh) {
  mesh.__starred = true;
  // Materials are shared between same-colored nodes — clone before mutating.
  mesh.material = mesh.material.clone();
  mesh.material.transparent = true;
  const MeshCls = mesh.constructor;
  const MatCls = mesh.material.constructor;
  mesh.__shells = [1.9, 3.1].map((scale, i) => {
    const mat = new MatCls({
      transparent: true, opacity: i === 0 ? 0.22 : 0.09,
      depthWrite: false, blending: ADDITIVE_BLENDING,
    });
    const shell = new MeshCls(mesh.geometry, mat);
    shell.scale.setScalar(scale);
    shell.raycast = () => {}; // halos must not swallow clicks
    mesh.add(shell);
    return shell;
  });
  tintStar(n, mesh);
}
function tintStar(n, mesh) {
  const c = nodeColor(n);
  mesh.material.color.set(c);
  if (mesh.material.emissive) mesh.material.emissive.set(c);
  mesh.__shells.forEach(s => {
    s.material.color.set(c);
    if (s.material.emissive) s.material.emissive.set(c);
  });
}

// starfield backdrop behind a transparent scene
Graph.backgroundColor('rgba(2,3,10,0)');
(function starfield() {
  const c = document.getElementById('space-bg');
  const fit = () => { c.width = innerWidth; c.height = innerHeight; draw(); };
  function draw() {
    const g = c.getContext('2d');
    g.fillStyle = '#03040c';
    g.fillRect(0, 0, c.width, c.height);
    // deterministic pseudo-random so redraws don't twinkle on resize
    let seed = 42;
    const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    for (let i = 0; i < 420; i++) {
      const x = rnd() * c.width, y = rnd() * c.height, r = rnd();
      g.globalAlpha = 0.12 + r * 0.5;
      g.fillStyle = r > 0.92 ? '#aec6ff' : '#ffffff';
      g.beginPath();
      g.arc(x, y, r > 0.96 ? 1.5 : 0.7, 0, 7);
      g.fill();
    }
    g.globalAlpha = 1;
  }
  addEventListener('resize', fit);
  fit();
})();

Graph.d3Force('charge').strength(-130);
// Soft spherical containment: nodes with no (visible) edges — freshly
// imported racks/PDUs before their power topology is recorded — otherwise
// drift off under charge repulsion and wreck every zoom-to-fit.
Graph.d3Force('containment', alpha => {
  DATA.nodes.forEach(n => {
    if (n.x === undefined) return;
    const r = Math.hypot(n.x, n.y, n.z || 0);
    if (r > 480) {
      const k = 0.03 * alpha * (r - 480) / r;
      n.vx -= n.x * k;
      n.vy -= n.y * k;
      n.vz -= (n.z || 0) * k;
    }
  });
});
Graph.d3Force('link').distance(38);
let didFit = false;
Graph.onEngineStop(() => {
  if (didFit) return;
  didFit = true;
  // A deep link (#<node-id>) focuses that node instead of fitting the whole
  // graph. onEngineStop is a setter, so this stays the single handler.
  if (location.hash) selectFromHash();
  else Graph.zoomToFit(800, 60);
});

// ── cluster constellations ────────────────────────────────────────
// Named clusters (e.g. the Proxmox APP/DB/Dev clusters) are pulled toward
// anchors on a wide circle; shared/unclustered infrastructure stays in the
// middle, so inter-cluster links visibly span between constellations.
const clusterNames = [...new Set(DATA.nodes.map(n => n.cluster).filter(Boolean))].sort();
const anchors = {};
const R = 420;
clusterNames.forEach((c, i) => {
  const a = (i / clusterNames.length) * Math.PI * 2;
  anchors[c] = { x: Math.cos(a) * R, y: (i % 2 ? 70 : -70), z: Math.sin(a) * R };
});
Graph.d3Force('clusterAnchor', alpha => {
  const k = 0.09 * alpha;
  DATA.nodes.forEach(n => {
    const a = anchors[n.cluster];
    if (!a || n.x === undefined) return;
    n.vx += (a.x - n.x) * k;
    n.vy += (a.y - n.y) * k;
    n.vz += (a.z - n.z) * k;
  });
});

function nodeOf(end) { return typeof end === 'object' ? end : byId.get(end); }

function inBlast(l) {
  if (!state.origin) return false;
  // Links are emitted dependency -> dependent, so particles (source->target)
  // animate the failure spreading outward from the origin.
  const dep = nodeOf(l.source).id;   // the dependency (closer to the failure)
  const dependent = nodeOf(l.target).id;
  const set = state.affected;
  return (dep === state.origin || set.has(dep)) && set.has(dependent);
}

function nodeColor(n) {
  if (!state.origin) return n.color;
  if (n.id === state.origin) return '#ff2d2d';
  if (state.direct.has(n.id)) return '#ff6b6b';
  if (state.affected.has(n.id)) return '#ffa94d';
  return '#1c2338';
}

function linkColor(l) {
  if (!state.origin) return l.color;
  return inBlast(l) ? '#ff6b6b' : '#131a2e';
}

function refresh() {
  DATA.nodes.forEach(n => {
    if (n.__threeObj && n.__threeObj.__starred) tintStar(n, n.__threeObj);
  });
  Graph.linkColor(Graph.linkColor())
       .linkWidth(Graph.linkWidth())
       .linkDirectionalParticles(Graph.linkDirectionalParticles());
}

// ── outage mode ───────────────────────────────────────────────────
// The pointer nub lives OUTSIDE the scrollable card (fixed sibling on <body>)
// so scrolling the card never moves it; positionCard places it each frame.
const cardNub = document.createElement('div');
cardNub.className = 'card-nub left';
document.body.appendChild(cardNub);

// Keep the address bar in step with the selection so the URL is always
// bookmark/copy-accurate. replaceState (not location.hash=) avoids both a
// history entry per click and a hashchange loop back into selectFromHash.
function syncHash(id) {
  try {
    const url = id ? '#' + encodeURIComponent(id) : location.pathname + location.search;
    history.replaceState(null, '', url);
  } catch (e) { /* file:// in some browsers disallows replaceState — ignore */ }
}

function selectOrigin(id) {
  state.origin = id;
  syncHash(id);
  const imp = DATA.impact[id] || { direct: [], all: [] };
  state.direct = new Set(imp.direct);
  state.affected = new Set(imp.all);
  refresh();
  const nodeData = byId.get(id);
  if (nodeData) renderNodeCard(nodeData);
  renderImpactCard(id, imp);
  positionCard();
  const n = byId.get(id);
  if (n && n.x !== undefined) {
    const dist = 220;
    const r = 1 + dist / Math.hypot(n.x, n.y, n.z || 1);
    Graph.cameraPosition({ x: n.x * r, y: n.y * r, z: (n.z || 1) * r }, n, 900);
  }
}

// The info box spawns at its star and tracks it: projected to screen every
// frame (see tickLabels), offset clear of the node, and clamped to the
// viewport so it never runs off-screen. Placed to the node's right, flipping
// left near the right edge; the scroll-immune nub points back at the star.
function positionCard() {
  const card = document.getElementById('node-card');
  if (!state.origin || card.style.display !== 'block') { cardNub.style.display = 'none'; return; }
  const n = byId.get(state.origin);
  if (!n || n.x === undefined) { cardNub.style.display = 'none'; return; }
  const c = Graph.graph2ScreenCoords(n.x, n.y, n.z);
  // Reserve the fixed right impact panel's strip so the popover never slides
  // underneath it when clamped.
  const SIDE_W = 330 + 16 + 12;
  const W = window.innerWidth - SIDE_W, H = window.innerHeight, pad = 12, gap = 18;
  const cw = card.offsetWidth, ch = card.offsetHeight;
  const flip = c.x > W - cw - gap - pad;   // no room on the right → place left
  let left = flip ? c.x - cw - gap : c.x + gap;
  let top = c.y - 20;
  left = Math.max(pad, Math.min(left, W - cw - pad));
  top = Math.max(pad, Math.min(top, H - ch - pad));
  card.style.left = left + 'px';
  card.style.top = top + 'px';

  // Nub in VIEWPORT coords (fixed, sibling of the card) so the card's own
  // scroll can't move it. It sits on the card edge facing the node, at the
  // node's screen-Y, and hides when the node isn't beside the card's span —
  // never pointing at empty space.
  const nub = cardNub;
  if (c.y < top + 6 || c.y > top + ch - 6) {
    nub.style.display = 'none';
  } else {
    nub.style.display = 'block';
    nub.className = 'card-nub ' + (flip ? 'right' : 'left');
    nub.style.left = (flip ? left + cw : left) + 'px';
    nub.style.top = c.y + 'px';
  }
}

function clearOrigin() {
  state.origin = null;
  syncHash(null);
  state.affected.clear();
  state.direct.clear();
  document.getElementById('impact-card').style.display = 'none';
  document.getElementById('node-card').style.display = 'none';
  cardNub.style.display = 'none';
  refresh();
}

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

// Attached box at the star: identity + all available node details.
function renderNodeCard(n) {
  const card = document.getElementById('node-card');
  card.style.display = 'block';
  // Re-trigger the spawn pop on every open (reflow forces the restart).
  card.classList.remove('pop');
  void card.offsetWidth;
  card.classList.add('pop');
  card.scrollTop = 0;
  card.replaceChildren();
  const close = el('span', 'close', '×');
  close.addEventListener('click', clearOrigin);
  card.appendChild(close);
  card.appendChild(el('h2', null, n.name));
  card.appendChild(el('div', 'subtitle', n.type + (n.scope ? ` · ${n.scope}` : '')));
  const idline = [...n.ips, ...n.domains];
  if (n.ssh) idline.push(`ssh: ${n.ssh}`);
  if (idline.length) card.appendChild(el('div', 'ipline', idline.join(' · ')));
  if (n.desc) card.appendChild(el('div', 'ndesc', n.desc));
  if (n.props && n.props.length) {
    const dl = el('div', 'props');
    n.props.forEach(([k, v]) => {
      dl.appendChild(el('div', 'pk', k));
      dl.appendChild(el('div', 'pv', v));
    });
    card.appendChild(dl);
  }

  // Clickable endpoints (served domains, admin UIs, dashboards) — new window.
  if (n.links && n.links.length) {
    const box = el('div', 'nlinks');
    n.links.forEach(({ label, url }) => {
      const a = el('a', 'nlink', label + ' ↗');
      a.href = url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      box.appendChild(a);
    });
    card.appendChild(box);
  }

  // Type-specific relations, collapsed by default to save space.
  const related = (title, rows) => {
    if (!rows.length) return;
    const d = el('details', 'nrel');
    d.appendChild(el('summary', null, `${title} (${rows.length})`));
    rows.sort((a, b) => a.label.localeCompare(b.label)).forEach(({ id, label }) => {
      const row = el('div', 'nrel-row', label);
      row.addEventListener('click', () => selectOrigin(id));
      d.appendChild(row);
    });
    card.appendChild(d);
  };
  // Links are dependency -> dependent, so a host's guests are its runs_on
  // out-links; a switch's neighborhood is its connects_to/manages links.
  if (n.hv || n.type === 'hypervisor_cluster') {
    related('Guests', DATA.links
      .filter(l => nodeOf(l.source).id === n.id && l.rel === 'runs_on')
      .map(l => nodeOf(l.target)).filter(g => g.id !== n.id)
      .map(g => ({ id: g.id, label: g.name })));
  }
  if (n.type === 'network_device') {
    const near = DATA.links
      .filter(l => ['connects_to', 'manages'].includes(l.rel)
          && (nodeOf(l.source).id === n.id || nodeOf(l.target).id === n.id))
      .map(l => nodeOf(nodeOf(l.source).id === n.id ? l.target : l.source));
    related('Connected', [...new Map(near.map(x => [x.id, x])).values()]
      .filter(x => x.id !== n.id).map(x => ({ id: x.id, label: x.name })));
  }
  if (n.type === 'rack' || n.type === 'site') {
    related('Housed here', DATA.links
      .filter(l => nodeOf(l.source).id === n.id && ['located_in', 'contains'].includes(l.rel))
      .map(l => nodeOf(l.target)).filter(x => x.id !== n.id)
      .map(x => ({ id: x.id, label: x.name })));
  }
}

// Fixed right panel: the outage/impact analysis.
function renderImpactCard(id, imp) {
  const n = byId.get(id);
  const card = document.getElementById('impact-card');
  card.style.display = 'block';
  card.scrollTop = 0;
  const groups = {};
  imp.all.forEach(aid => {
    const a = byId.get(aid);
    if (!a) return;
    (groups[a.type] = groups[a.type] || []).push(a);
  });
  card.replaceChildren();
  const close = el('span', 'close', '×');
  close.addEventListener('click', clearOrigin);
  card.appendChild(close);
  card.appendChild(el('h2', null, 'Outage impact'));
  card.appendChild(el('div', 'subtitle', `if ${n.name} fails`));
  const stats = el('div', 'impact-stats');
  const stat = (num, lab, cls) => {
    const s = el('div', 'stat' + (cls ? ' ' + cls : ''));
    s.appendChild(el('b', null, String(num)));
    s.appendChild(el('span', null, lab));
    return s;
  };
  stats.appendChild(stat(imp.direct.length, 'direct', 'red'));
  stats.appendChild(stat(imp.all.length, 'affected', 'amber'));
  stats.appendChild(stat(Object.keys(groups).length, 'types'));
  card.appendChild(stats);
  if (!imp.all.length) {
    card.appendChild(el('div', 'hint', 'Nothing in the map depends on this node.'));
  }
  Object.keys(groups).sort().forEach(t => {
    const g = el('div', 'impact-group');
    g.appendChild(el('h3', null, `${t} (${groups[t].length})`));
    groups[t].sort((a, b) => a.name.localeCompare(b.name)).forEach(a => {
      const row = el('div', 'impact-node', a.name);
      row.style.borderLeftColor = state.direct.has(a.id) ? '#ff6b6b' : '#ffa94d';
      row.addEventListener('click', () => selectOrigin(a.id));
      g.appendChild(row);
    });
    card.appendChild(g);
  });
}

// ── HTML label overlay ────────────────────────────────────────────
const labelLayer = document.getElementById('labels');
const hubIds = [...DATA.nodes].sort((a, b) => b.val - a.val).slice(0, 16).map(n => n.id);
function labelSet() {
  const ids = new Set();
  if (state.labelsOn) hubIds.forEach(id => ids.add(id));
  if (state.hover) ids.add(state.hover);
  if (state.origin) {
    ids.add(state.origin);
    let c = 0;
    for (const id of state.direct) { if (c++ > 40) break; ids.add(id); }
  }
  return ids;
}
const labelPool = new Map();
// floating caption per cluster constellation, tracking its live centroid
const captionPool = new Map();
function tickCaptions() {
  const sums = new Map();
  DATA.nodes.forEach(n => {
    if (!n.cluster || n.x === undefined || state.hidden.has(n.category)) return;
    let s = sums.get(n.cluster);
    if (!s) { s = { x: 0, y: 0, z: 0, count: 0 }; sums.set(n.cluster, s); }
    s.x += n.x; s.y += n.y; s.z += n.z; s.count++;
  });
  for (const [c, e] of captionPool) {
    if (!sums.has(c)) { e.remove(); captionPool.delete(c); }
  }
  const W = elGraph.clientWidth, H = elGraph.clientHeight;
  for (const [c, s] of sums) {
    const p = Graph.graph2ScreenCoords(s.x / s.count, s.y / s.count + 120, s.z / s.count);
    let e = captionPool.get(c);
    if (!e) {
      e = document.createElement('div');
      e.className = 'cluster-caption';
      e.textContent = c;
      e.title = 'click to hide/show this cluster';
      e.addEventListener('click', () => {
        if (state.hiddenClusters.has(c)) state.hiddenClusters.delete(c);
        else state.hiddenClusters.add(c);
        Graph.nodeVisibility(Graph.nodeVisibility()).linkVisibility(Graph.linkVisibility());
      });
      labelLayer.appendChild(e);
      captionPool.set(c, e);
    }
    e.classList.toggle('off', state.hiddenClusters.has(c));
    const off = p.x < -80 || p.x > W + 80 || p.y < -40 || p.y > H + 40;
    e.style.display = off ? 'none' : 'block';
    e.style.left = p.x + 'px';
    e.style.top = p.y + 'px';
  }
}
(function tickLabels() {
  DATA.nodes.forEach(n => {
    const m = n.__threeObj;
    if (m && !m.__starred) starify(n, m);
  });
  // Filter by visibility BEFORE pool cleanup — otherwise labels of nodes
  // hidden by a view/category/cluster toggle linger with stale positions.
  const wanted = new Set([...labelSet()].filter(id => {
    const n = byId.get(id);
    return n && nodeVisible(n);
  }));
  for (const [id, e] of labelPool) {
    if (!wanted.has(id)) { e.remove(); labelPool.delete(id); }
  }
  const W = elGraph.clientWidth, H = elGraph.clientHeight;
  wanted.forEach(id => {
    const n = byId.get(id);
    if (!n || n.x === undefined || !nodeVisible(n)) return;
    const c = Graph.graph2ScreenCoords(n.x, n.y, n.z);
    let e = labelPool.get(id);
    if (!e) {
      e = document.createElement('div');
      labelLayer.appendChild(e);
      labelPool.set(id, e);
    }
    e.textContent = n.label;
    e.className = 'nlabel'
      + (hubIds.includes(id) ? ' hub' : '')
      + (state.origin === id ? ' origin' : (state.affected.has(id) ? ' hit' : ''));
    const off = c.x < -50 || c.x > W + 50 || c.y < -50 || c.y > H + 50;
    e.style.display = off ? 'none' : 'block';
    e.style.left = c.x + 'px';
    e.style.top = c.y + 'px';
  });
  tickCaptions();
  positionCard();
  requestAnimationFrame(tickLabels);
})();

// ── search ────────────────────────────────────────────────────────
const searchInput = document.getElementById('search');
const searchResults = document.getElementById('search-results');
searchInput.addEventListener('input', () => {
  const q = searchInput.value.toLowerCase().trim();
  searchResults.replaceChildren();
  if (!q) { searchResults.style.display = 'none'; return; }
  const hits = DATA.nodes.filter(n =>
    n.label.toLowerCase().includes(q) || n.id.toLowerCase().includes(q) ||
    n.ips.some(ip => ip.includes(q)) || n.domains.some(d => d.toLowerCase().includes(q))
  ).slice(0, 20);
  if (!hits.length) { searchResults.style.display = 'none'; return; }
  searchResults.style.display = 'block';
  hits.forEach(n => {
    const row = document.createElement('div');
    row.className = 'search-item';
    const dot = document.createElement('span');
    dot.className = 'sdot';
    dot.style.background = n.color;
    row.appendChild(dot);
    row.appendChild(document.createTextNode(n.label));
    const meta = document.createElement('span');
    meta.className = 'smeta';
    meta.textContent = n.ips[0] || n.type;
    row.appendChild(meta);
    row.addEventListener('click', () => {
      selectOrigin(n.id);
      searchResults.style.display = 'none';
      searchInput.value = '';
    });
    searchResults.appendChild(row);
  });
});

// ── legends ───────────────────────────────────────────────────────
const catBox = document.getElementById('cat-legend');
DATA.legend.forEach(c => {
  const row = document.createElement('div');
  row.className = 'lrow';
  const dot = document.createElement('span');
  dot.className = 'ldot';
  dot.style.background = c.color;
  row.appendChild(dot);
  row.appendChild(document.createTextNode(c.category));
  const cnt = document.createElement('span');
  cnt.className = 'lcount';
  cnt.textContent = c.count;
  row.appendChild(cnt);
  row.addEventListener('click', () => {
    if (state.hidden.has(c.category)) state.hidden.delete(c.category);
    else state.hidden.add(c.category);
    row.classList.toggle('off');
    Graph.nodeVisibility(Graph.nodeVisibility()).linkVisibility(Graph.linkVisibility());
  });
  catBox.appendChild(row);
});
const relBox = document.getElementById('rel-legend');
DATA.linkLegend.forEach(c => {
  const row = document.createElement('div');
  row.className = 'lrow';
  const dash = document.createElement('span');
  dash.className = 'ldash';
  dash.style.background = c.color;
  row.appendChild(dash);
  row.appendChild(document.createTextNode(c.rel));
  const cnt = document.createElement('span');
  cnt.className = 'lcount';
  cnt.textContent = c.count;
  row.appendChild(cnt);
  row.addEventListener('click', () => {
    if (state.hiddenRels.has(c.rel)) state.hiddenRels.delete(c.rel);
    else state.hiddenRels.add(c.rel);
    row.classList.toggle('off');
    Graph.linkVisibility(Graph.linkVisibility());
  });
  relBox.appendChild(row);
});

// ── view switcher ─────────────────────────────────────────────────
const viewsEl = document.getElementById('views');
Object.entries(VIEWS).forEach(([key, v]) => {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'view-btn' + (key === currentView ? ' active' : '');
  b.textContent = v.label;
  b.addEventListener('click', () => {
    currentView = key;
    viewsEl.querySelectorAll('.view-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    Graph.nodeVisibility(Graph.nodeVisibility()).linkVisibility(Graph.linkVisibility());
    // A hidden origin makes the outage panel misleading — reset it.
    if (state.origin && !nodeVisible(byId.get(state.origin))) clearOrigin();
    setTimeout(() => Graph.zoomToFit(700, 80, nodeVisible), 700);
  });
  viewsEl.appendChild(b);
});

// ── controls ──────────────────────────────────────────────────────
document.getElementById('ctl-labels').addEventListener('change', e => {
  state.labelsOn = e.target.checked;
});
document.getElementById('ctl-particles').addEventListener('change', e => {
  state.particlesOn = e.target.checked;
  refresh();
});
let spinning = false;
document.getElementById('ctl-spin').addEventListener('change', e => {
  spinning = e.target.checked;
  Graph.controls().autoRotate = spinning;
  Graph.controls().autoRotateSpeed = 0.6;
});
// ── deep links ────────────────────────────────────────────────────
// #<node-id> selects that node's outage (applied once the layout settles,
// see onEngineStop above). Embedders — an iframe in another app, a bookmark —
// can therefore point straight at a node. Declaration is hoisted, so the
// engine-stop handler above may reference it.
function selectFromHash() {
  const id = decodeURIComponent(location.hash.replace(/^#/, ''));
  if (id && byId.has(id)) selectOrigin(id);
  else if (!id) clearOrigin();
}
window.addEventListener('hashchange', selectFromHash);
// Fallback: onEngineStop can fire before or after this point depending on how
// fast the layout settles. selectOrigin is idempotent, so a delayed re-apply
// guarantees a deep link takes effect without racing the simulation.
if (location.hash) setTimeout(selectFromHash, 1500);

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') clearOrigin();
  if (e.key === '/' && document.activeElement !== searchInput) {
    e.preventDefault();
    searchInput.focus();
  }
});
window.addEventListener('resize', () =>
  Graph.width(elGraph.clientWidth).height(elGraph.clientHeight));
</script>"""


# __FG3D_BUNDLE__ stays above the <title> (nothing user-controlled precedes it).
_TEMPLATE = """<!DOCTYPE html>
<!-- infracontext-renderer: {version} — regenerate with `ic graph render -f 3d`
     from infracontext {version}; the render is deterministic so identical bytes
     require this version. -->
<html lang="en">
<head>
<meta charset="UTF-8">
<script>__FG3D_BUNDLE__</script>
<title>infracontext 3D — {title}</title>
{styles}
</head>
<body>
<canvas id="space-bg"></canvas>
<div id="graph"></div>
<div id="labels"></div>
<div id="hud">
  <div>
    <div id="title-card" class="panel">
      <h1>{title}</h1>
      <div class="stats">{stats} · click a node to simulate its outage</div>
    </div>
    <div id="searchbox" class="panel">
      <input id="search" type="text" placeholder="Search name, IP, domain…  ( / )" autocomplete="off">
      <div id="search-results"></div>
    </div>
    <div id="views" class="panel"></div>
  </div>
</div>
<div id="node-card" class="panel"></div>
<div id="side">
  <div id="impact-card" class="panel"></div>
</div>
<div id="legend-card" class="panel">
  <h3>Categories</h3>
  <div id="cat-legend"></div>
  <h3>Relationships</h3>
  <div id="rel-legend"></div>
</div>
<div id="controls" class="panel">
  <label><input type="checkbox" id="ctl-labels" checked> labels</label>
  <label><input type="checkbox" id="ctl-particles" checked> failure particles</label>
  <label><input type="checkbox" id="ctl-spin"> auto-rotate</label>
  <div class="keyhint">Esc resets · / searches</div>
</div>
__APP_SCRIPT__
</body>
</html>"""
