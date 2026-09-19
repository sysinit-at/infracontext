# Version Log

Release history. Full commit-level detail lives in git; entries here record what shipped and why.

## 0.7.0 — 2026-09-19

NetBox device components and richer device fields.

- **Device components** (`components: true` on a netbox source): imports interfaces, inventory items,
  modules, and console/front/rear/power ports into `attributes.netbox_components`, preserving NetBox's
  natural row order. Opt-in because it is verbose (a documented server carries ~50 inventory items, a
  48-port switch ~100 interfaces). Each collection is fetched once fleet-wide and grouped by device PK
  — a handful of requests, not one per device — capped by `max_components` (default 10000) with a
  warning when truncated. A collection the token cannot read (403) is skipped with a warning rather
  than failing the DCIM sync.
- **Unreadable != empty**: only a collection *proven* complete (rows collected == NetBox's `count`)
  may replace what is on disk. A failed request, a cap truncation, a page breaking mid-walk, a
  paginator omitting `count`, or rows that could not be attributed to a device all keep the rows
  already recorded on each node, so one transient error
  cannot blank the fleet's inventory while the sync still reports success. Collections proven complete
  that no longer list a device still drop out, so real deletions propagate.
- **Device fields**: `attributes.netbox` now also carries platform, tenant, site, description, and
  comments. NetBox's `description` stays in the owned namespace — the node's own `description`/`notes`
  remain manual fields that other collectors write.
- **MCP over streamable HTTP**: `ic mcp serve --http [--host] [--port]` serves the same eight tools
  at `http://HOST:PORT/mcp` (stateless) for web MCP clients like Open WebUI. `--allow-host` extends
  the localhost-only DNS-rebinding allowlist for clients arriving by container/DNS name.
- **TLS for the HTTP transport**: `--tls-cert` / `--tls-key` (both or neither) serve the endpoint over
  HTTPS, so a bearer token no longer crosses the network in cleartext; startup warns when a token is
  configured without TLS. Host allowlist entries now cover `http://` and `https://` origins.
- **Bearer auth for the HTTP transport**: `IC_MCP_AUTH_TOKEN` / `--auth-token-file` require
  `Authorization: Bearer <token>` (constant-time compare); no `--auth-token` flag, since argv is
  world-readable via `ps`. `--require-auth` fails startup when no token is configured, so a typo
  cannot silently expose the map.
- **Node context carries `attributes`**: `ic ctx` / `get_context` now include the structured collector
  layer (hardware, netbox/netbox_components, proxmox_*, dmi_*). It was silently absent while notes
  prose got through, making component inventory invisible to agents.
- `netbox_components` joins the owned-namespace set, so `components: false` clears it on the next sync
  instead of leaving a stale snapshot.
- Dependency floors raised to current releases (typer 0.27.2, pydantic 2.13.5, mcp 1.30.0, regex 2026.9.10,
  ruff 0.16.8, mypy 2.3.1, matplotlib 3.11.2, prompt-toolkit 3.0.53); lockfile refreshed. mcp stays capped `<2`.

## 0.6.0 — 2026-07-21

Node attachments and 3D card refinements.

- **Attachments**: `ic describe node attach/detach` stores context-critical files (rack photos, label
  photos, IP lists, diagrams) under `attachments/<type>/<slug>/` and records them on the node
  (`attachments:` field). Doctor validates existence, flags orphans, rejects path escapes. Scope rule
  in docs: incident-relevant context only — documentation links belong in notes/observability URLs.
- **3D node card**: hypervisor view now shows only hypervisors (cluster members, not their guests);
  per-type sections — expandable Guests (hypervisors), Connected (network devices), Housed here
  (racks/sites); clickable endpoint chips (served domains, iLO/admin UIs, dashboards) opening in a new
  window; attachment summary row.
- **Doctor**: the missing-ssh_alias lint now honors machine-local overrides — aliases are personal
  SSH-config names and belong in `.infracontext.local.yaml`, not shared YAML.

## 0.5.2 — 2026-07-21

- **3D split surfaces**: node identity + full details (incl. SSH alias, CheckMK folder, sources — cap
  raised to 18 rows) live in the popover attached to the star; the outage/impact analysis moved to a
  fixed right panel. The popover's clamp reserves the right panel's strip so the two never overlap.

## 0.5.1 — 2026-07-21

- **3D info-box nub fix**: the pointer connecting the info box to its node is now a viewport-fixed sibling
  of the card rather than an absolutely-positioned child, so scrolling a tall card no longer drags the nub
  off the node. Combined with the earlier clamp-tracking fix, the nub points at the star (or hides) under
  every card position and scroll offset.

## 0.5.0 — 2026-07-21

Datacenter/physical layer and a 3D outage explorer.

- **NetBox DCIM source** (`type: netbox`): imports sites, racks and devices over the token-authed REST
  API, typing devices from role or device-type model (PDU/UPS/switch/patch-panel), with `slug_map` to
  adopt existing manual nodes in place (namespace-scoped merge preserves other collectors' enrichment on
  every sync), `role_map`, and `exclude_model_patterns` for rack filler.
- **`ic graph render -f 3d`**: self-contained WebGL outage explorer (vendored 3d-force-graph). Click a
  node to simulate its outage — precomputed blast radius (matches `ic graph impact`), failure particles
  spreading outward, a per-node info box that spawns at the star and tracks it, type-aware property panel,
  five view lenses (Datacenter/Hypervisors/VMs/Services/Everything), cluster constellations, deep links.
  The artifact records the renderer version for reproducible re-rendering.

## 0.4.1 — 2026-07-21

Agent-integration hardening: infracontext works with any coding agent, and now says (and packages) so
accurately.

- **Agent-agnostic banner**: README states support for Claude Code, OpenAI Codex, OpenCode, and pi,
  with a per-agent integration table (verified against each agent's official docs: Codex skills at
  `~/.agents/skills/<name>/SKILL.md` with legacy `/prompts:` invocation deprecated; OpenCode plural
  `commands/` dirs). `ic mcp serve --help` shows registration snippets for all three MCP clients
  (Rich-markup escaping fixed so the TOML header actually renders).
- **Self-contained triage fallback**: the wheel now bundles `agents/` and `commands/` under
  `infracontext/data/`, and the new `ic triage checklist [name]` serves the diagnostic checker
  checklists in every install — even when only the skill file was copied to another agent. The
  `/ic-triage` skill degrades in explicit tiers: subagents → `ic triage checklist` (>= 0.4.1) →
  checklists next to the skill file → plain USE-method checks, with an upgrade hint on version skew.

## 0.4.0 — 2026-07-21

A **physical / datacenter layer**: model infrastructure down to facility and power topology, and
discover it from the network devices and BMCs that have no shell. Everything below is additive —
new enum values and fields older ic versions tolerate, so federated repos are unaffected.

- **Physical model**: new node types `site`, `rack`, `pdu`, `ups` (deliberately outside the compute
  set — no SSH triage surface) and relationship types `located_in` (child → container), `powered_by`
  (consumer → supplier), and `manages` (out-of-band controller → host, e.g. a BMC). The constraint
  matrix, graph render, and doctor lints cover the new layer; `contains` reads containment the other
  way. Conventions: `attributes.hardware` for asset metadata, `connects_to` edge `attributes` for
  port-level cabling.
- **SNMP source + query**: discover switches/routers/appliances from standard MIBs — SNMPv2-MIB
  identity, ENTITY-MIB hardware, IF-MIB interface tables, and LLDP-MIB topology (a neighbor matching
  an existing node becomes a `connects_to` edge; unmatched neighbors warn, never auto-create).
  `ic query snmp <node> [-t status|interfaces]` walks a device live during triage. v2c/v3 credentials
  live in the keychain, keyed by source name.
- **Redfish source + query**: import BMC/host inventory over plain HTTPS/JSON (iDRAC, iLO, XClarity,
  OpenBMC — no vendor SDK). Discovery serial-matches each BMC to its host and emits a `manages` edge;
  `ic query redfish <node> [-t status|power]` returns the health rollup or live power draw.
- **NetBox source**: pull DCIM sites/racks/devices from the NetBox REST API into `site`/`rack`/
  `physical_host`/`network_device`/`pdu`/`ups` nodes with `located_in` edges; PK-stable relocation,
  `role_map` overrides, and a per-sync device cap (`max_devices`, default 500).
- **Device-type import**: `ic import devicetype <file> --node <query>` fills `attributes.hardware`
  from a NetBox community devicetype-library YAML (physical-identity subset only; port templates
  ignored). Fill-only merge, `--force` to overwrite.
- **ic-collect hardware phase**: for a bare-metal `physical_host` (gated on `systemd-detect-virt`),
  `/ic-collect` probes `dmidecode`/`ipmitool`/`lldpctl`/`ethtool -P` to enrich `attributes.hardware`
  fill-only and, on confirmation, spawn a BMC `network_device` plus `connects_to` cabling edges.
  Every probe is optional and degrades gracefully.
- **Sync-safety hardening** (post-review): relocations (renamed devices) rewrite every reference to
  the old node id — manual edges, chain members, and the sync's own topology edges — across all four
  relocating sources (shared helper in `sources/base.py`); the SNMP/Redfish observability entries are
  *source-owned* (`source` field = ownership) and track a changed target host or BMC URL on re-sync,
  while entries without `source` are never modified by any sync.
- **Fleet-repo pattern**: shared datacenter gear (sites/racks/PDUs) lives in a fleet repo and is
  referenced read-only from app repos via `@fleet:...`; `ic graph spof/impact -A` traverse the
  power/placement chain across roots.

## 0.3.0 — 2026-07-17

Borrowed the best ideas from [scanopy](https://github.com/scanopy/scanopy)'s data-hygiene and export
model (ideas only — no code; scanopy is AGPL) and hardened them through four adversarial review rounds.

- **Mermaid export**: `ic graph render -f mermaid` (and `-o -` for stdout) — diagrams that render
  natively on GitHub/GitLab/Obsidian; all relationship types mapped with an exhaustiveness test.
- **Offline HTML render**: vis-network is vendored and inlined, so the default HTML artifact opens
  offline/air-gapped; `--cdn` restores the smaller CDN-loading file.
- **Request-path chains**: one ordered entry in a new per-project `chains.yaml` describes lb → app → db;
  expanded to pairwise edges for graph/doctor/render. `ic describe relationship chain add/list`.
  Kept out of `relationships.yaml` so older ic versions in federated repos are unaffected.
- **Duplicate reconciliation**: `ic describe node consolidate <dest> <src>` merges duplicates fill-only
  and rewrites every reference (relationships, chains — including inbound cross-project refs — and
  local override keys, transferred by effective entry). Importers warn on duplicate candidates but
  never merge automatically.
- **Freshness signals**: syncs write pruned run records under `.infracontext/runs/`
  (created/updated/confirmed-unchanged); presence is derived with a 3-sync grace window; doctor warns
  about source-managed nodes a source stopped reporting. Empty/failed/partial syncs never rewrite node
  files. New write-once `first_seen` node field.
- **Doctor lints**: relationship-constraint re-validation on disk, duplicate ssh_alias/IP detection,
  application-coverage report, blank-learning check.
- **Forward compatibility**: unknown fields in nested models are tolerated and survive edit round-trips;
  unknown enum values (node/relationship types from newer versions) load without mangling; doctor
  reports the drift.
- **ic-collect discipline**: every observed listener is attributed or listed as unclaimed; each
  triage service gets an evidence line.
- **MCP**: oversized query payloads are parked on disk with `parked_*` explore tools; `parked_grep`
  is ReDoS-proof.

## 0.2.0 — 2026-07-07

- Version bump and release hygiene: public GitHub export script hardened, internal tooling stripped
  from the mirror.

## 0.1.0

- Initial release: repo-centric node YAML, relationship graph (SPOF/impact/cycles/orphans), triage
  context for LLM-driven troubleshooting, monitoring source plugins, SSH hot path, federation across
  repos, MCP server.
