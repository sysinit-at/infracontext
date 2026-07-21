# Version Log

Release history. Full commit-level detail lives in git; entries here record what shipped and why.

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
