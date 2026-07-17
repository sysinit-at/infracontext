# Version Log

Release history. Full commit-level detail lives in git; entries here record what shipped and why.

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
