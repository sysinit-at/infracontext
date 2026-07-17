"""Render the infrastructure graph as HTML, SVG, GraphML, or mermaid.

The exported functions take a NetworkX DiGraph (as built by
:mod:`infracontext.graph.loader`) and write a file:

- :func:`render_html`    — interactive vis-network page (self-contained by
  default; ``inline_js=False`` loads the pinned CDN URL instead).
- :func:`render_svg`     — static SVG via matplotlib (requires ``infracontext[viz]``).
- :func:`render_graphml` — GraphML for Gephi/yEd (pure networkx, no extra deps).
- :func:`render_mermaid` — mermaid ``flowchart TD`` text for markdown embedding
  (:func:`graph_to_mermaid` is the pure DiGraph → str core).

Coloring and shapes are derived from the node ``type`` attribute via
:data:`NODE_CATEGORIES`. Federated qualified IDs (``project/type:slug`` or
``@alias:project/type:slug``) are unqualified for display labels.
"""

from __future__ import annotations

import html
import json
import os
import re
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from infracontext.graph.loader import unqualify_node_id

if TYPE_CHECKING:
    import networkx as nx

# Soft cap on SVG rendering. spring_layout is O(N^3) in practice and gets
# unusable above a few thousand nodes; matplotlib's renderer slows further.
# Override with IC_SVG_MAX_NODES for unusual cases. HTML and GraphML scale
# fine and don't need a cap.
_DEFAULT_SVG_MAX_NODES = 1500


def _svg_node_limit() -> int:
    """Return the SVG node cap, honoring IC_SVG_MAX_NODES.

    Set to 0 to disable the cap. Falls back to the default on parse errors.
    """
    raw = os.environ.get("IC_SVG_MAX_NODES")
    if raw is None or not raw.strip():
        return _DEFAULT_SVG_MAX_NODES
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_SVG_MAX_NODES

# Visual category for each NodeType. Keys are the StrEnum *values* from
# infracontext.models.node.NodeType. Values are (color, shape, category_label).
# Shapes are vis-network names (matplotlib renders all as colored circles).
NODE_CATEGORIES: dict[str, tuple[str, str, str]] = {
    # business
    "application": ("#4E79A7", "ellipse", "application"),
    # service
    "service": ("#F28E2B", "ellipse", "service"),
    "service_cluster": ("#F28E2B", "ellipse", "service"),
    # compute (bare metal + virtualization)
    "physical_host": ("#59A14F", "box", "compute"),
    "hypervisor_cluster": ("#59A14F", "box", "compute"),
    "vm": ("#59A14F", "box", "compute"),
    # containers
    "lxc_container": ("#8CD17D", "box", "container"),
    "oci_container": ("#8CD17D", "box", "container"),
    "docker_compose": ("#8CD17D", "box", "container"),
    "podman_compose": ("#8CD17D", "box", "container"),
    "podman_quadlet": ("#8CD17D", "box", "container"),
    # kubernetes
    "k8s_cluster": ("#B07AA1", "hexagon", "kubernetes"),
    "k8s_node": ("#B07AA1", "hexagon", "kubernetes"),
    "k8s_namespace": ("#B07AA1", "hexagon", "kubernetes"),
    "k8s_pod": ("#B07AA1", "hexagon", "kubernetes"),
    "k8s_service": ("#B07AA1", "hexagon", "kubernetes"),
    "k8s_deployment": ("#B07AA1", "hexagon", "kubernetes"),
    # storage
    "storage": ("#76B7B2", "database", "storage"),
    "filesystem": ("#76B7B2", "database", "storage"),
    "nfs_share": ("#76B7B2", "database", "storage"),
    "ceph_cluster": ("#76B7B2", "database", "storage"),
    "block_storage": ("#76B7B2", "database", "storage"),
    "object_storage": ("#76B7B2", "database", "storage"),
    # network
    "network": ("#D37295", "diamond", "network"),
    "subnet": ("#D37295", "diamond", "network"),
    # dns
    "domain": ("#EDC948", "diamond", "dns"),
    "dns_zone": ("#EDC948", "diamond", "dns"),
    # external
    "external_service": ("#E15759", "star", "external"),
    "cdn_endpoint": ("#E15759", "star", "external"),
}

_FALLBACK_STYLE: tuple[str, str, str] = ("#BAB0AC", "dot", "other")


def _style_for(node_type: str | None) -> tuple[str, str, str]:
    """Return (color, shape, category) for a node type, falling back gracefully."""
    if not node_type:
        return _FALLBACK_STYLE
    return NODE_CATEGORIES.get(node_type, _FALLBACK_STYLE)


def _display_label(graph_id: str, name: str | None) -> str:
    """Build the human-facing label for a node.

    Prefers the ``name`` attribute but falls back to the unqualified node ID
    (``type:slug``) so federated graph IDs like ``project/vm:web-01`` don't
    leak the qualification into the diagram.
    """
    if name:
        return name
    _, node_id = unqualify_node_id(graph_id)
    return node_id


def _node_scope(graph_id: str, data: dict[str, Any]) -> str | None:
    """Return the project/root label for federated graphs, or None for a bare ID."""
    project = data.get("project")
    root = data.get("root")
    if root:
        return f"@{root}:{project}" if project else f"@{root}"
    if project:
        return project
    scope, _ = unqualify_node_id(graph_id)
    return scope or None


# ─── HTML (vis-network) ───────────────────────────────────────────

# Pinned to an exact version rather than the floating major tag
# (vis-network@9) so a compromised or republished tag can't silently change
# the JavaScript loaded by CDN-mode HTML files. The default (inline) mode
# embeds the vendored bundle shipped in graph/assets/, which must match this
# version. Bump deliberately: re-vendor the asset, re-verify its hash and
# license, and update assets/README.md (see that file for the procedure).
_VIS_VERSION = "9.1.9"
_VIS_CDN = f"https://unpkg.com/vis-network@{_VIS_VERSION}/standalone/umd/vis-network.min.js"


def _vis_bundle_js() -> str:
    """Return the vendored vis-network bundle for inline embedding.

    Shipped as package data — see ``graph/assets/README.md`` for origin URL,
    version, sha256, and license. The bundle lands inside an inline
    ``<script>`` tag, where a literal ``</script>`` would terminate the tag
    early and corrupt the page; refuse loudly instead of emitting a broken file.
    """
    bundle = (
        resources.files("infracontext.graph") / "assets" / f"vis-network-{_VIS_VERSION}.min.js"
    ).read_text(encoding="utf-8")
    if "</script>" in bundle:
        raise ValueError(
            "Vendored vis-network bundle contains '</script>' and cannot be inlined "
            "safely — re-vendor the asset (see src/infracontext/graph/assets/README.md)."
        )
    return bundle


def render_html(
    graph: nx.DiGraph,
    output_path: Path,
    title: str = "Infrastructure",
    *,
    inline_js: bool = True,
) -> None:
    """Write an interactive vis-network HTML page to ``output_path``.

    The page bundles its own JSON node/edge data. By default the vendored
    vis-network bundle is inlined too, so the file is fully self-contained
    and opens offline. With ``inline_js=False`` the page loads vis-network
    from the pinned CDN URL instead — smaller file, but the first view
    requires internet.

    Features: node shape/color by type, edge labels show relationship type,
    sidebar with search box, type-filter legend, and click-to-inspect panel.
    The DOM is built via ``createElement`` + ``textContent`` (no ``innerHTML``)
    so user-supplied names cannot inject markup.
    """
    output_path = Path(output_path)

    vis_nodes, vis_edges, legend = _build_vis_payload(graph)
    stats = (
        f"{graph.number_of_nodes()} node{'s' if graph.number_of_nodes() != 1 else ''} · "
        f"{graph.number_of_edges()} edge{'s' if graph.number_of_edges() != 1 else ''}"
    )

    # Single-pass substitution: substituted JSON is never rescanned, so node
    # data that happens to contain a placeholder string (e.g. a node named
    # "__EDGES__") can't be re-substituted and corrupt the page.
    payloads = {
        "__NODES__": _js_safe(vis_nodes),
        "__EDGES__": _js_safe(vis_edges),
        "__LEGEND__": _js_safe(legend),
    }
    script = re.sub(
        r"__NODES__|__EDGES__|__LEGEND__", lambda m: payloads[m.group(0)], _HTML_SCRIPT
    )

    page = _HTML_TEMPLATE.format(
        title=html.escape(title),
        styles=_HTML_STYLES,
        stats=stats,
        script_block=script,
    )

    vis_script = (
        f"<script>\n{_vis_bundle_js()}\n</script>"
        if inline_js
        else f'<script src="{_VIS_CDN}"></script>'
    )
    # The vis-network tag is substituted *after* str.format: the minified
    # bundle is full of braces that .format would misparse as fields. The
    # template places __VIS_SCRIPT__ before any user-controlled text (the
    # <title> comes after it), so count=1 always hits the template's own
    # placeholder — a node or title literally named "__VIS_SCRIPT__" survives
    # as data. The lambda replacement keeps re.sub from interpreting
    # backslash escapes inside the bundle.
    page = re.sub(r"__VIS_SCRIPT__", lambda _m: vis_script, page, count=1)
    output_path.write_text(page, encoding="utf-8")


def _js_safe(obj: Any) -> str:
    """JSON-encode and escape ``</script>`` sequences so the payload cannot break out."""
    return json.dumps(obj).replace("</", "<\\/")


def _build_vis_payload(
    graph: nx.DiGraph,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build (nodes, edges, legend) ready for JSON serialization into the HTML."""
    vis_nodes: list[dict[str, Any]] = []
    category_counts: dict[str, dict[str, str | int]] = {}

    for node_id, data in graph.nodes(data=True):
        node_type = data.get("type")
        color, shape, category = _style_for(node_type)
        label = _display_label(node_id, data.get("name"))
        scope = _node_scope(node_id, data)

        # vis-network renders the `title` string as HTML in its tooltip
        # (innerHTML under the hood). Every user-controlled fragment must
        # be html-escaped before being concatenated into the tooltip body.
        tooltip_lines = [html.escape(label)]
        if node_type:
            tooltip_lines.append(f"type: {html.escape(str(node_type))}")
        if scope:
            tooltip_lines.append(f"scope: {html.escape(scope)}")
        tooltip_lines.append(f"id: {html.escape(node_id)}")

        vis_nodes.append(
            {
                "id": node_id,
                "label": label,
                "title": "\n".join(tooltip_lines),
                "shape": shape,
                "color": {
                    "background": color,
                    "border": color,
                    "highlight": {"background": "#ffffff", "border": color},
                },
                "font": {"color": "#e0e0e0", "size": 13, "face": "monospace"},
                "_category": category,
                "_type": node_type or "unknown",
                "_scope": scope or "",
            }
        )

        cc = category_counts.setdefault(category, {"color": color, "count": 0})
        cc["count"] = int(cc["count"]) + 1

    vis_edges: list[dict[str, Any]] = []
    for u, v, data in graph.edges(data=True):
        relation = str(data.get("type", "")) or ""
        description = str(data.get("description", "") or "")
        # Same XSS concern as node tooltips — escape before concatenation.
        if relation and description:
            tooltip = f"{html.escape(relation)}\n{html.escape(description)}"
        else:
            tooltip = html.escape(relation or description)

        vis_edges.append(
            {
                "from": u,
                "to": v,
                "label": relation,
                "title": tooltip,
                "arrows": "to",
                "color": {"color": "#7a7a8c", "opacity": 0.75},
                "font": {"size": 10, "color": "#a0a0b0", "strokeWidth": 0, "align": "middle"},
            }
        )

    legend = [
        {"category": cat, "color": str(info["color"]), "count": int(info["count"])}
        for cat, info in sorted(category_counts.items())
    ]
    return vis_nodes, vis_edges, legend


_HTML_STYLES = """<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f0f1a; color: #e0e0e0;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         display: flex; height: 100vh; overflow: hidden; }
  #graph { flex: 1; }
  #sidebar { width: 280px; background: #1a1a2e; border-left: 1px solid #2a2a4e;
             display: flex; flex-direction: column; overflow: hidden; }
  #header { padding: 12px 14px; border-bottom: 1px solid #2a2a4e; }
  #header h1 { font-size: 14px; color: #e0e0e0; font-weight: 600; }
  #header .stats { font-size: 11px; color: #777; margin-top: 4px; }
  #search-wrap { padding: 12px; border-bottom: 1px solid #2a2a4e; }
  #search { width: 100%; background: #0f0f1a; border: 1px solid #3a3a5e;
            color: #e0e0e0; padding: 7px 10px; border-radius: 6px;
            font-size: 13px; outline: none; }
  #search:focus { border-color: #4E79A7; }
  #search-results { max-height: 140px; overflow-y: auto; padding: 4px 12px;
                    border-bottom: 1px solid #2a2a4e; display: none; }
  .search-item { padding: 4px 6px; cursor: pointer; border-radius: 4px;
                 font-size: 12px; white-space: nowrap; overflow: hidden;
                 text-overflow: ellipsis; }
  .search-item:hover { background: #2a2a4e; }
  #info-panel { padding: 14px; border-bottom: 1px solid #2a2a4e; min-height: 120px; }
  #info-panel h3 { font-size: 12px; color: #888; margin-bottom: 8px;
                   text-transform: uppercase; letter-spacing: 0.05em; }
  #info-content { font-size: 13px; color: #ccc; line-height: 1.6; }
  #info-content .field { margin-bottom: 4px; }
  #info-content .field b { color: #e0e0e0; }
  #info-content .empty { color: #555; font-style: italic; }
  .neighbor-link { display: block; padding: 2px 6px; margin: 2px 0;
                   border-radius: 3px; cursor: pointer; font-size: 12px;
                   white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
                   border-left: 3px solid #333; }
  .neighbor-link:hover { background: #2a2a4e; }
  #neighbors-list { max-height: 180px; overflow-y: auto; margin-top: 4px; }
  #legend-wrap { flex: 1; overflow-y: auto; padding: 12px; }
  #legend-wrap h3 { font-size: 12px; color: #888; margin-bottom: 10px;
                    text-transform: uppercase; letter-spacing: 0.05em; }
  .legend-item { display: flex; align-items: center; gap: 8px;
                 padding: 4px 0; cursor: pointer; border-radius: 4px;
                 font-size: 12px; user-select: none; }
  .legend-item:hover { background: #2a2a4e; padding-left: 4px; }
  .legend-item.dimmed { opacity: 0.35; }
  .legend-dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
  .legend-label { flex: 1; overflow: hidden; text-overflow: ellipsis;
                  white-space: nowrap; }
  .legend-count { color: #666; font-size: 11px; }
</style>"""


# All DOM construction uses createElement + textContent. No innerHTML is set
# from user-supplied data, so node names and descriptions cannot inject markup
# even before the vis-network DataSet escapes them again on render.
_HTML_SCRIPT = """<script>
const RAW_NODES = __NODES__;
const RAW_EDGES = __EDGES__;
const LEGEND    = __LEGEND__;

const nodesDS = new vis.DataSet(RAW_NODES.map(n => ({
  id: n.id, label: n.label, title: n.title, shape: n.shape,
  color: n.color, font: n.font,
  _category: n._category, _type: n._type, _scope: n._scope,
})));

const edgesDS = new vis.DataSet(RAW_EDGES.map((e, i) => ({
  id: i, from: e.from, to: e.to, label: e.label, title: e.title,
  arrows: e.arrows, color: e.color, font: e.font,
})));

const container = document.getElementById('graph');
const network = new vis.Network(container, { nodes: nodesDS, edges: edgesDS }, {
  physics: {
    enabled: true, solver: 'forceAtlas2Based',
    forceAtlas2Based: {
      gravitationalConstant: -80, centralGravity: 0.01,
      springLength: 140, springConstant: 0.08,
      damping: 0.5, avoidOverlap: 0.9,
    },
    stabilization: { iterations: 250, fit: true },
  },
  interaction: { hover: true, tooltipDelay: 150, hideEdgesOnDrag: true },
  nodes: { borderWidth: 1.5, size: 18 },
  edges: { smooth: { type: 'continuous', roundness: 0.2 }, selectionWidth: 3 },
});

network.once('stabilizationIterationsDone', () =>
  network.setOptions({ physics: { enabled: false } }));

function clearChildren(el) { while (el.firstChild) el.removeChild(el.firstChild); }

function field(label, value) {
  const div = document.createElement('div');
  div.className = 'field';
  if (label) {
    const b = document.createElement('b');
    b.textContent = label;
    div.appendChild(b);
    div.appendChild(document.createTextNode(' '));
  }
  div.appendChild(document.createTextNode(value));
  return div;
}

function showInfo(nodeId) {
  const n = nodesDS.get(nodeId);
  const content = document.getElementById('info-content');
  clearChildren(content);
  if (!n) return;

  const nameDiv = document.createElement('div');
  nameDiv.className = 'field';
  const b = document.createElement('b');
  b.textContent = n.label;
  nameDiv.appendChild(b);
  content.appendChild(nameDiv);

  content.appendChild(field('Type:', n._type));
  if (n._scope) content.appendChild(field('Scope:', n._scope));

  const idDiv = document.createElement('div');
  idDiv.className = 'field';
  idDiv.style.cssText = 'color:#777;font-size:11px;margin-top:4px';
  idDiv.textContent = n.id;
  content.appendChild(idDiv);

  const neighborIds = network.getConnectedNodes(nodeId);
  if (neighborIds.length) {
    const header = document.createElement('div');
    header.className = 'field';
    header.style.cssText = 'margin-top:8px;color:#888;font-size:11px';
    header.textContent = 'Neighbors (' + neighborIds.length + ')';
    content.appendChild(header);

    const list = document.createElement('div');
    list.id = 'neighbors-list';
    neighborIds.forEach(nid => {
      const nb = nodesDS.get(nid);
      const color = nb ? nb.color.background : '#555';
      const link = document.createElement('span');
      link.className = 'neighbor-link';
      link.style.borderLeftColor = color;
      link.textContent = nb ? nb.label : nid;
      link.addEventListener('click', () => focusNode(nid));
      list.appendChild(link);
    });
    content.appendChild(list);
  }
}

function focusNode(nodeId) {
  network.focus(nodeId, { scale: 1.4, animation: true });
  network.selectNodes([nodeId]);
  showInfo(nodeId);
}

function emptyInfo() {
  const content = document.getElementById('info-content');
  clearChildren(content);
  const span = document.createElement('span');
  span.className = 'empty';
  span.textContent = 'Click a node to inspect it';
  content.appendChild(span);
}

let hoveredNodeId = null;
network.on('hoverNode', p => { hoveredNodeId = p.node; container.style.cursor = 'pointer'; });
network.on('blurNode',  () => { hoveredNodeId = null; container.style.cursor = 'default'; });
network.on('click', params => {
  if (params.nodes.length > 0) showInfo(params.nodes[0]);
  else if (hoveredNodeId === null) emptyInfo();
});

const searchInput = document.getElementById('search');
const searchResults = document.getElementById('search-results');
searchInput.addEventListener('input', () => {
  const q = searchInput.value.toLowerCase().trim();
  clearChildren(searchResults);
  if (!q) { searchResults.style.display = 'none'; return; }
  const matches = RAW_NODES.filter(n =>
    n.label.toLowerCase().includes(q) || n._type.toLowerCase().includes(q)
  ).slice(0, 25);
  if (!matches.length) { searchResults.style.display = 'none'; return; }
  searchResults.style.display = 'block';
  matches.forEach(n => {
    const el = document.createElement('div');
    el.className = 'search-item';
    el.textContent = n.label + '  (' + n._type + ')';
    el.style.borderLeft = '3px solid ' + n.color.background;
    el.style.paddingLeft = '8px';
    el.addEventListener('click', () => {
      focusNode(n.id);
      searchResults.style.display = 'none';
      searchInput.value = '';
    });
    searchResults.appendChild(el);
  });
});
document.addEventListener('click', e => {
  if (!searchResults.contains(e.target) && e.target !== searchInput)
    searchResults.style.display = 'none';
});

const hiddenCategories = new Set();
function toggleCategory(cat) {
  if (hiddenCategories.has(cat)) hiddenCategories.delete(cat);
  else hiddenCategories.add(cat);
  document.querySelectorAll('.legend-item').forEach(el => {
    if (el.dataset.cat === cat) el.classList.toggle('dimmed');
  });
  const hide = hiddenCategories.has(cat);
  const updates = RAW_NODES
    .filter(n => n._category === cat)
    .map(n => ({ id: n.id, hidden: hide }));
  nodesDS.update(updates);
}

const legendEl = document.getElementById('legend');
LEGEND.forEach(c => {
  const item = document.createElement('div');
  item.className = 'legend-item';
  item.dataset.cat = c.category;
  item.addEventListener('click', () => toggleCategory(c.category));

  const dot = document.createElement('span');
  dot.className = 'legend-dot';
  dot.style.background = c.color;
  item.appendChild(dot);

  const label = document.createElement('span');
  label.className = 'legend-label';
  label.textContent = c.category;
  item.appendChild(label);

  const count = document.createElement('span');
  count.className = 'legend-count';
  count.textContent = String(c.count);
  item.appendChild(count);

  legendEl.appendChild(item);
});
</script>"""


# __VIS_SCRIPT__ must stay *above* the <title> line: render_html substitutes
# the first occurrence, and nothing user-controlled may precede it.
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
__VIS_SCRIPT__
<title>infracontext — {title}</title>
{styles}
</head>
<body>
<div id="graph"></div>
<div id="sidebar">
  <div id="header">
    <h1>{title}</h1>
    <div class="stats">{stats}</div>
  </div>
  <div id="search-wrap">
    <input id="search" type="text" placeholder="Search nodes…" autocomplete="off">
  </div>
  <div id="search-results"></div>
  <div id="info-panel">
    <h3>Node info</h3>
    <div id="info-content"><span class="empty">Click a node to inspect it</span></div>
  </div>
  <div id="legend-wrap">
    <h3>Categories (click to toggle)</h3>
    <div id="legend"></div>
  </div>
</div>
{script_block}
</body>
</html>"""


# ─── SVG (matplotlib) ─────────────────────────────────────────────


def render_svg(graph: nx.DiGraph, output_path: Path, title: str = "Infrastructure") -> None:
    """Write a static SVG of the graph to ``output_path``.

    Uses matplotlib with a spring layout. Requires the ``viz`` extra:
    ``pip install 'infracontext[viz]'``. Lightweight and embeddable in
    markdown (READMEs, runbooks) — no JavaScript needed.
    """
    import networkx as nx

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.patches as mpatches
        import matplotlib.pyplot as plt
    except ImportError as e:  # pragma: no cover - exercised in CI without viz extra
        raise ImportError(
            "matplotlib is required for SVG rendering. "
            "Install with: pip install 'infracontext[viz]'"
        ) from e

    output_path = Path(output_path)
    n_nodes = graph.number_of_nodes()

    if n_nodes == 0:
        # matplotlib chokes on empty graphs; write a minimal placeholder SVG.
        output_path.write_text(
            _EMPTY_SVG.format(title=html.escape(title)), encoding="utf-8"
        )
        return

    limit = _svg_node_limit()
    if limit > 0 and n_nodes > limit:
        raise ValueError(
            f"Graph has {n_nodes} nodes — too large for SVG rendering "
            f"(limit: {limit}). Use --format html for interactive viewing, "
            f"--format graphml to open in Gephi/yEd, or set IC_SVG_MAX_NODES "
            f"to override."
        )

    pos = nx.spring_layout(graph, seed=42, k=2.0 / (n_nodes ** 0.5 + 1))

    degree = dict(graph.degree())
    max_deg = max(degree.values(), default=1) or 1

    node_colors: list[str] = []
    node_sizes: list[float] = []
    category_legend: dict[str, str] = {}
    for node_id in graph.nodes():
        node_type = graph.nodes[node_id].get("type")
        color, _shape, category = _style_for(node_type)
        node_colors.append(color)
        node_sizes.append(300 + 1500 * (degree.get(node_id, 1) / max_deg))
        category_legend.setdefault(category, color)

    fig, ax = plt.subplots(figsize=(16, 11), facecolor="#ffffff")
    ax.set_facecolor("#ffffff")
    ax.axis("off")
    ax.set_title(title, fontsize=14, pad=14)

    nx.draw_networkx_edges(
        graph, pos, ax=ax, edge_color="#888888", width=1.0, alpha=0.6,
        arrows=True, arrowsize=12, connectionstyle="arc3,rad=0.05",
    )

    edge_labels = {
        (u, v): str(d.get("type", ""))
        for u, v, d in graph.edges(data=True)
        if d.get("type")
    }
    if edge_labels:
        nx.draw_networkx_edge_labels(
            graph, pos, edge_labels=edge_labels, ax=ax,
            font_size=7, font_color="#555555",
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.7, "pad": 0.5},
        )

    nx.draw_networkx_nodes(
        graph, pos, ax=ax, node_color=node_colors, node_size=node_sizes,
        edgecolors="#333333", linewidths=0.8, alpha=0.95,
    )

    labels = {n: _display_label(n, d.get("name")) for n, d in graph.nodes(data=True)}
    nx.draw_networkx_labels(graph, pos, labels=labels, ax=ax, font_size=8, font_color="#1a1a2e")

    patches = [
        mpatches.Patch(color=color, label=category)
        for category, color in sorted(category_legend.items())
    ]
    if patches:
        ax.legend(handles=patches, loc="upper left", framealpha=0.85, fontsize=9)

    plt.tight_layout()
    fig.savefig(output_path, format="svg", bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


_EMPTY_SVG = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="80">'
    '<text x="20" y="40" font-family="sans-serif" font-size="14" fill="#666">'
    "{title}: empty graph (no nodes)"
    "</text></svg>\n"
)


# ─── GraphML ──────────────────────────────────────────────────────


def render_graphml(graph: nx.DiGraph, output_path: Path) -> None:
    """Write a GraphML file to ``output_path``.

    Strips non-primitive node/edge attributes (such as the Pydantic Node and
    Relationship objects the loader attaches) so the file is GraphML-compliant.
    Opens in Gephi, yEd, Cytoscape, and most graph tools.
    """
    import networkx as nx

    output_path = Path(output_path)
    sanitized = _sanitize_for_graphml(graph)
    nx.write_graphml(sanitized, output_path)


def _sanitize_for_graphml(graph: nx.DiGraph) -> nx.DiGraph:
    """Return a copy of ``graph`` with non-primitive attributes removed."""
    import networkx as nx

    out = nx.DiGraph()
    for node_id, data in graph.nodes(data=True):
        out.add_node(node_id, **_primitive_attrs(data))
    for u, v, data in graph.edges(data=True):
        out.add_edge(u, v, **_primitive_attrs(data))
    return out


def _primitive_attrs(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce GraphML-serializable values to their base primitive type.

    NetworkX's GraphML writer keys on ``type(v)`` rather than ``isinstance``,
    so StrEnum values (like ``NodeType.VM``) and other primitive subclasses
    must be collapsed to plain ``str`` / ``int`` / ``float`` / ``bool``.
    Non-primitive values (Pydantic models, lists, dicts, ``None``) are dropped.
    """
    out: dict[str, Any] = {}
    for k, v in data.items():
        if v is None:
            continue
        # Check bool *before* int because bool is a subclass of int.
        if isinstance(v, bool):
            out[k] = bool(v)
        elif isinstance(v, int):
            out[k] = int(v)
        elif isinstance(v, float):
            out[k] = float(v)
        elif isinstance(v, str):
            out[k] = str(v)
    return out


# ─── Mermaid ──────────────────────────────────────────────────────

# Arrow style per RelationshipType value: placement/containment edges render
# dashed, identity/replication edges as plain links, dependency/dataflow
# edges as solid arrows. Unknown types fall back to _MERMAID_FALLBACK_ARROW.
# A test asserts this map covers every RelationshipType member, so adding an
# enum value without choosing an arrow fails the suite.
MERMAID_EDGE_ARROWS: dict[str, str] = {
    # placement / containment
    "runs_on": "-.->",
    "hosted_by": "-.->",
    "member_of": "-.->",
    "contains": "-.->",
    "mounts": "-.->",
    # identity / replication
    "resolves_to": "---",
    "replicates_to": "---",
    # dependency / dataflow
    "depends_on": "-->",
    "uses": "-->",
    "connects_to": "-->",
    "fronted_by": "-->",
    "routes_to": "-->",
    "uses_storage": "-->",
    "reads_from": "-->",
    "writes_to": "-->",
}

_MERMAID_FALLBACK_ARROW = "-->"

# Mermaid renderers (mermaid-js in browsers, GitHub markdown) get sluggish
# well before vis-network does. Mirrors the IC_SVG_MAX_NODES pattern, but the
# text format has no hard failure mode — so this only warns, never refuses.
_DEFAULT_MERMAID_MAX_NODES = 300


def _mermaid_node_limit() -> int:
    """Return the mermaid warn threshold, honoring IC_MERMAID_MAX_NODES.

    Set to 0 to disable the warning. Falls back to the default on parse errors.
    """
    raw = os.environ.get("IC_MERMAID_MAX_NODES")
    if raw is None or not raw.strip():
        return _DEFAULT_MERMAID_MAX_NODES
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_MERMAID_MAX_NODES


def mermaid_size_warning(graph: nx.DiGraph) -> str | None:
    """Return a warning string for graphs above the mermaid node cap, else None."""
    limit = _mermaid_node_limit()
    n_nodes = graph.number_of_nodes()
    if 0 < limit < n_nodes:
        return (
            f"Graph has {n_nodes} nodes — mermaid renderers get slow above "
            f"{limit}. Rendering anyway; consider --format html, or set "
            f"IC_MERMAID_MAX_NODES to raise or disable (0) this warning."
        )
    return None


def _mermaid_escape(text: str) -> str:
    """Escape characters that break mermaid quoted labels, as HTML entities."""
    return (
        text.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
    )


def _mermaid_ids(graph: nx.DiGraph) -> dict[str, str]:
    """Map graph node IDs to safe, unique mermaid identifiers.

    ic IDs like ``vm:web-01`` or ``@fleet:prod/vm:pve-01`` contain characters
    mermaid identifiers can't carry; every non-``[A-Za-z0-9_]`` character
    becomes ``_``. Distinct IDs can collide after sanitizing (``vm:web-01``
    vs ``vm:web_01``) — collisions get a numeric suffix in node insertion
    order, which is deterministic for a given graph.
    """
    ids: dict[str, str] = {}
    used: set[str] = set()
    for node_id in graph.nodes():
        base = re.sub(r"[^A-Za-z0-9_]", "_", str(node_id)) or "n"
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        ids[node_id] = candidate
    return ids


def graph_to_mermaid(graph: nx.DiGraph) -> str:
    """Serialize the graph as mermaid ``flowchart TD`` text. Pure — no I/O.

    Node labels are ``name (type)``. Merged/federated graphs — where nodes
    carry a scope per :func:`_node_scope` — group nodes into one ``subgraph``
    per scope; single-project graphs stay flat.
    """
    ids = _mermaid_ids(graph)

    def node_line(node_id: str, indent: str) -> str:
        data = graph.nodes[node_id]
        label = _display_label(node_id, data.get("name"))
        node_type = data.get("type")
        if node_type:
            label = f"{label} ({node_type})"
        return f'{indent}{ids[node_id]}["{_mermaid_escape(label)}"]'

    by_scope: dict[str | None, list[str]] = {}
    for node_id, data in graph.nodes(data=True):
        by_scope.setdefault(_node_scope(node_id, data), []).append(node_id)

    lines = ["flowchart TD"]

    # Unscoped nodes stay flat at the top level.
    lines.extend(node_line(node_id, "    ") for node_id in by_scope.pop(None, []))

    # One subgraph per scope. Subgraph IDs share the node namespace in
    # mermaid, so they are uniquified against node IDs too.
    used = set(ids.values())
    for scope, node_ids in by_scope.items():
        base = "scope_" + (re.sub(r"[^A-Za-z0-9_]", "_", scope) or "unnamed")
        sub_id = base
        suffix = 2
        while sub_id in used:
            sub_id = f"{base}_{suffix}"
            suffix += 1
        used.add(sub_id)
        lines.append(f'    subgraph {sub_id}["{_mermaid_escape(scope)}"]')
        lines.extend(node_line(node_id, "        ") for node_id in node_ids)
        lines.append("    end")

    for u, v, data in graph.edges(data=True):
        relation = str(data.get("type", "") or "")
        arrow = MERMAID_EDGE_ARROWS.get(relation, _MERMAID_FALLBACK_ARROW)
        label = f"|{_mermaid_escape(relation)}|" if relation else ""
        lines.append(f"    {ids[u]} {arrow}{label} {ids[v]}")

    return "\n".join(lines) + "\n"


def render_mermaid(graph: nx.DiGraph, output_path: Path) -> None:
    """Write a mermaid ``flowchart TD`` document to ``output_path``."""
    Path(output_path).write_text(graph_to_mermaid(graph), encoding="utf-8")
