# Infracontext User Guide

Infracontext is a CLI tool for documenting infrastructure and troubleshooting issues. It combines two workflows:

1. **System Description** - Document your infrastructure during "peace times" so you're prepared for incidents
2. **Troubleshooting** - Claude performs diagnostics using the USE method with context from your documentation

## Key Concept: Context for Humans and Agents

Unlike traditional monitoring tools, infracontext provides **structured context** to Claude Code, which then performs the actual troubleshooting. Your node documentation is a "living document" that:

- Guides Claude during triage
- Accumulates learnings over time (from both humans and Claude)
- Evolves as you discover new things about your infrastructure

You don't need to document every detail - Claude fills gaps with its knowledge and assumptions. Focus on what's unique or non-obvious about your setup.

## Getting Started

### Installation

Requires Python 3.14+.

```bash
git clone <repo>
cd infracontext

# Recommended: install the `ic` command onto your PATH
# ('[mcp]' bundles the MCP server for agents; drop it if you don't need that)
uv tool install '.[mcp]'

# Alternative: run from the checkout without installing
uv sync
alias ic='uv run --directory /path/to/infracontext ic'   # add to .zshrc/.bashrc

# Verify installation
ic --help

# Enable shell completion for node IDs and project names
ic --install-completion   # then restart your shell
```

By default `ic` finds its data by walking up from the current directory until
it sees a `.infracontext/` directory, so it works inside your infra repo with
no further setup. To reach an environment from any directory, register it once
with `ic config env` — see [Running `ic` From Anywhere](#running-ic-from-anywhere).

### Running `ic` From Anywhere

`ic` resolves its environment in this order:

1. **`IC_ROOT`** environment variable, if it points at a directory containing
   `.infracontext/`. A one-off override for scripts and cron:
   `IC_ROOT=~/work/infra ic doctor`.
2. **Walk up from the current directory** looking for `.infracontext/` (the
   original behavior — works anywhere inside your infra repo).
3. **The default environment** registered with `ic config env`.

Register your environments once so `ic` reaches them from any directory:

```bash
# Register the current repo and make it the default
ic config env add home . --default

# Register another and switch the default later
ic config env add work ~/work/infra
ic config env default work

# List registered environments (marks the default and whether each still exists)
ic config env list

# Remove one (does not touch its data)
ic config env remove work
```

The registry lives at `$XDG_CONFIG_HOME/infracontext/environments.yaml`
(falling back to `~/.config`). A missing or malformed registry degrades to "no
default" with a one-line warning rather than crashing a command.

### Initialize and Create a Project

Infracontext supports multiple projects with optional hierarchy (customer/project).

```bash
# Initialize (also adds .infracontext.local.yaml to .gitignore for you)
ic init

# Create a simple project
ic describe project create homelab

# Or use hierarchical organization (customer/project)
ic describe project create acme/production
ic describe project create acme/staging
ic describe project create bigcorp/prod

# List projects (shows hierarchy when present)
ic describe project list
#              Projects
# ┏━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━┓
# ┃ Customer ┃ Environment  ┃ Active ┃
# ┡━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━┩
# │ acme     │ production   │ *      │
# │ acme     │ staging      │        │
# │ bigcorp  │ prod         │        │
# └──────────┴──────────────┴────────┘

# Switch with full path
ic describe project switch acme/staging
```

## Documenting Infrastructure

### Adding Nodes

Nodes represent infrastructure components: VMs, containers, services, etc.

```bash
# Fastest path: add a node from an SSH alias in one step.
# Derives the slug (s.myserver -> s-myserver), sets ssh_alias so `ic ssh`
# works immediately, and defaults the type to vm.
ic describe node add web-prod
ic describe node add db.internal --type vm --name "Primary DB"

# Full control: create with explicit fields
ic describe node create --type vm --name "Web Server"
ic describe node create --type physical_host --name "Hypervisor 01"

# List nodes
ic describe node list
ic describe node list --type vm  # Filter by type
ic describe node list --json     # Machine-readable

# Find nodes by domain, IP, name, SSH alias, or ID
ic describe node find example.com
ic describe node find 192.168.1.100

# View node details
ic describe node show vm:web-server
ic describe node show web-server    # fuzzy: bare slug works when unambiguous

# Edit in your editor
ic describe node edit vm:web-server

# Delete a node
ic describe node delete vm:web-server
```

### Referring to Nodes: Exact vs. Fuzzy

Every node-taking command (`show`, `context`/`ctx`, `edit`, `delete`,
`learning`/`learn`, `ssh`, all `query *`, and `graph analyze`/`impact`) accepts
either form:

- **Exact** — `type:slug` (e.g. `vm:web-01`). Contains a `:`, so it takes the
  fast path with no directory scan. This is the precise, scriptable form.
- **Fuzzy** — a bare query (e.g. `web-01`, `example.com`, an SSH alias). Matched
  against the active project's nodes. One hit resolves; several print a
  candidates table so you can pick the exact ID; none prints a "did you mean"
  suggestion.

Fuzzy resolution keeps the incident hot path short (`ic ssh web`) without
forcing you to type the full ID. Qualified `@alias:type:slug` IDs work too, for
nodes in external roots.

### Consolidating Duplicate Nodes

Importers (ssh-config, Proxmox, SOS, kubectl) can discover the same box under
different identifiers, leaving two node files for one machine. Merge the
duplicate into the node you want to keep instead of hand-editing YAML:

```bash
# Preview the merge plan without changing anything
ic describe node consolidate vm:web-prod vm:web-prod-2 --dry-run

# Merge SRC into DEST, rewrite every reference, delete the SRC file
ic describe node consolidate vm:web-prod vm:web-prod-2
```

Merge semantics: scalar fields are fill-only (DEST wins), lists are unioned or
appended with dedupe, and `first_seen` keeps the earlier date. Every
relationship edge, chain member, and local override key pointing at SRC is
rewritten to DEST — including inbound `@project:type:slug` references from
other local projects, which would otherwise dangle. Chains untouched by the
rewrite are never altered, and within touched chains only duplicate hops the
rewrite itself created are collapsed. Local overrides transfer by *effective
entry*: at lookup a project-scoped key wins wholly over the global one, so
exactly one SRC entry (scoped if present, else global) transfers to DEST —
shadowed global-only fields never activate. Because the global key form
applies to every project, a transferring global entry is copied instead of
moved when another project still has a node with SRC's ID, and written under
the project-scoped `project/type:slug` key when another project has its own
node with DEST's ID (SRC's `ssh_alias` must not leak onto that unrelated
node). Consolidation refuses to cross projects or roots, and
refuses when the merged node's source binding would misbehave on the next
sync: either the two nodes are owned by *different* sources (two syncs would
fight over the merged node), or SRC is source-managed and DEST is not (DEST
would adopt SRC's binding, and the next sync would rename or re-create the
merged node — swap the arguments to keep the source-managed node instead).
Pass `--force` to proceed anyway.

Imports point you here: when a sync is about to create a node whose IPs,
domains, or `ssh_alias` already belong to exactly one existing node, it warns
and suggests the consolidate command. Detection only — loopback IPs and
identifiers shared by several nodes are ignored, and importers never merge
automatically.

### Essential Node Fields

The minimum viable node for triage:

```yaml
version: "2.0"
id: "vm:web-server"
slug: web-server
type: vm
name: "Web Server"

# SSH connection - CRITICAL for triage
ssh_alias: "web"  # Must match an alias in ~/.ssh/config
```

**Important**: `ssh_alias` is at the top level, not nested. SSH aliases are defined in your `~/.ssh/config` and handle hostnames, ports, jump hosts, keys, and usernames. Claude uses the alias directly: `ssh web`.

### Recommended Node Fields

```yaml
version: "2.0"
id: "vm:web-server"
slug: web-server
type: vm
name: "Production Web Server"

# SSH connection
ssh_alias: "web-prod"

# Network identity
ip_addresses: ["192.168.1.10"]
domains: ["web.example.com"]

# What you'd tell an on-call engineer
description: "Main web application server running nginx + PHP-FPM"
notes: |
  Peak traffic: 5-7pm weekdays
  Known issue: Memory climbs until weekly restart (Sunday 3am)
```

### Triage Configuration

Minimal hints for Claude - the agent discovers logs and commands itself:

```yaml
triage:
  # Services that matter on this node
  services:
    - nginx
    - php-fpm

  # Free-form context for Claude
  context: |
    This server handles 10k req/s at peak.
    If CPU high, check PHP-FPM pool status first.
    Redis cache runs on the same host.

  # Optional: override project default tier (0-4)
  tier: 3  # privileged

  # Optional: override collector script path
  collector_script: /opt/custom/collect.sh
```

## Access Tiers

Access tiers control what diagnostic methods Claude can use during triage. Tiers prevent over-privileged access to sensitive production systems.

### Tier Levels

| Tier | Value | Capabilities |
|------|-------|--------------|
| `local_only` | 0 | Local data + observability APIs only (Prometheus, Loki, CheckMK) |
| `collector` | 1 | + Execute pre-deployed `/usr/local/bin/ic-collect.sh` |
| `unprivileged` | 2 | + Arbitrary read-only SSH commands (no sudo) |
| `privileged` | 3 | + SSH with sudo/root access |
| `remediate` | 4 | + Autonomous fixes after diagnosis |

### Configuration Hierarchy

```
Project default → Node override → CLI restriction
     ↓                ↓               ↓
  baseline      can raise/lower    can only lower
```

The project's `max_tier` acts as a hard ceiling that cannot be exceeded.

### Project Configuration

Configure default tier in `project.yaml` at the project root:

```yaml
version: "2.0"
name: "Production"
slug: production

access:
  default_tier: 2       # unprivileged (default for all nodes)
  max_tier: 3           # privileged (hard ceiling)
  collector_script: /usr/local/bin/ic-collect.sh
```

### CLI Override

Restrict tier via CLI flag (can only lower, not elevate):

```bash
# Restrict to observability-only diagnostics
ic --tier local_only describe node context vm:web-server

# Restrict to collector script only
ic --tier collector describe node context vm:web-server

# Or via environment variable
IC_TIER=unprivileged ic describe node context vm:web-server
```

### Node Context Output

The `access` section appears in node context:

```yaml
access:
  tier: unprivileged
  tier_level: 2
  capabilities:
    - local_data
    - observability_api
    - collector_script
    - ssh_readonly
```

Claude checks this section before running diagnostics and adjusts its approach accordingly

### Learnings

Learnings are discovered knowledge that accumulates over time. Claude records them during triage, and you can add them manually.

The `ic learn` shortcut is the quick human path — it resolves the node fuzzily
and defaults the source to `human`:

```bash
# One-liner
ic learn web-01 "PHP-FPM pool was misconfigured" -c "high CPU investigation"

# Omit the finding to compose a longer note in $EDITOR (a commented template;
# saving without a finding aborts without writing)
ic learn web-01
```

The canonical form gives full control over source (`agent` is its default, so
Claude uses it during triage):

```bash
ic describe node learning vm:web-server \
  "PHP slow log is at /var/log/php8.1-fpm/slow.log (non-standard)" \
  --context "slow request investigation" \
  --source human
```

Learnings appear in node context and help future investigations:

```yaml
learnings:
  - date: "2024-01-15"
    context: "high CPU investigation"
    finding: "PHP-FPM pool was set to static with too many workers"
    source: agent
  - date: "2024-01-20"
    context: "disk space issue"
    finding: "Laravel log rotation not working, check /etc/logrotate.d/laravel"
    source: human
```

### Defining Relationships

Relationships document dependencies:

```bash
# Create a relationship
ic describe relationship create \
  --source vm:web-server \
  --target vm:db-server \
  --type depends_on \
  --description "PostgreSQL connection"

# Interactive wizard
ic describe relationship wizard

# List relationships
ic describe relationship list
```

### Request-Path Chains

A chain documents a request path (lb → app → db) as *one ordered entry*
instead of N pairwise relationships. Chains live in `chains.yaml`, a sibling
of `relationships.yaml` that older ic versions simply never read:

```bash
# Members in path order — repeat -m two or more times
ic describe relationship chain add web-request-path \
  -m vm:lb-01 -m vm:app-01 -m vm:db-01 \
  --description "Customer HTTP traffic"

# List chains with their hops
ic describe relationship chain list
```

At load time each chain expands into consecutive pairwise edges
(`vm:lb-01 --routes_to--> vm:app-01`, …), so `ic graph`, `ic ctx`, and
`ic graph render` see ordinary relationships; the chain name and hop position
ride along in edge attributes. The edge type defaults to `routes_to`
(override with `--type`). See [SCHEMA.md](SCHEMA.md#chains-chainsyaml) for the
YAML format, including per-member `via` annotations ("HTTPS 443, sticky
sessions").

## Physical Layer: Datacenter and Power

Added in ic 0.4.0, the physical layer models the facility and power substrate
that compute sits on, so the graph can reach from a failing service down to the
rack and the PDU feeding it. It is opt-in — a pure-cloud estate never needs it.

### When to use each type

| Type | Use it for |
|------|------------|
| `site` | A datacenter, building, or colocation facility |
| `rack` | An equipment rack within a site |
| `pdu` | A power distribution unit (rack PDU, or an upstream one in a daisy chain) |
| `ups` | An uninterruptible power supply |

These four carry **no SSH triage surface** — they are absent from the compute
set, so `ic doctor` never nags them for `ssh_alias` or observability config.
Reach for them once you care about "which rack is this in" or "what loses power
if this UPS trips"; skip them otherwise.

### Placement and power edges

Three directed relationship types wire the layer together. All point from the
dependent to the thing it depends on, so a traversal outward from a host reaches
its rack, its site, and its power chain:

| Type | Direction | Example |
|------|-----------|---------|
| `located_in` | child → container | `physical_host:h1 located_in rack:r1`; `rack:r1 located_in site:dc1` |
| `powered_by` | consumer → supplier | `physical_host:h1 powered_by pdu:pdu-a`; `pdu:pdu-a powered_by ups:ups-1` |
| `manages` | controller → host | `network_device:h1-bmc manages physical_host:h1` (a BMC/iLO is a `network_device`) |

```bash
ic describe relationship create --source physical_host:h1 --target rack:r1 --type located_in
ic describe relationship create --source physical_host:h1 --target pdu:pdu-a --type powered_by
ic describe relationship create --source network_device:h1-bmc --target physical_host:h1 --type manages
```

`contains` reads the same containment the other way (`rack:r1 contains
physical_host:h1`); both directions are legal and `ic doctor` treats each
independently. A `pdu` may be `powered_by` another `pdu` (daisy chain) and a
`ups` `powered_by` its `site` (the building feed). Where a host physically sits
and which PDU/UPS feeds it are **human-curated facts** — no source plugin
auto-discovers rack position or power cabling.

Physical asset metadata (manufacturer, model, serial, `u_height`,
`rack_position`, …) lives under `attributes.hardware`, and port-level cabling on
a `connects_to` edge's `attributes` (`local_port`/`remote_port`). See
[SCHEMA.md "Physical Layer"](SCHEMA.md#physical-layer) for the full conventions,
and [`/ic-collect`](#using-ic-collect) / [`ic import devicetype`](#device-types-netbox-devicetype-library)
for populating them.

### Fleet-repo pattern for shared datacenter gear

Sites, racks, PDUs, and UPSes are usually shared across every app that lives in
them, so the natural home is a **fleet repo** that also holds the hypervisors —
kept out of each application's own `.infracontext/`. App repos then reference the
shared physical nodes read-only through [external roots](#federating-multiple-repositories-external-roots):

```yaml
# app repo: .infracontext/projects/prod/relationships.yaml
- source: physical_host:h1
  target: "@fleet:rack:r1"
  type: located_in
- source: physical_host:h1
  target: "@fleet:pdu:pdu-a"
  type: powered_by
```

This keeps one source of truth for the facility while `ic graph spof -A` and
`ic graph impact -A` still traverse the power/placement chain across roots (a
tripped UPS surfaces every downstream app).

## Incident Hot Path

When something is on fire, the short commands are the fast path. Each resolves
the node fuzzily (see [Exact vs. Fuzzy](#referring-to-nodes-exact-vs-fuzzy)), so
a bare name is enough when it is unambiguous:

```bash
ic ssh web              # print a context banner to stderr, then ssh onto the node
ic ssh web uptime       # run a remote command instead of opening a shell
ic ssh web --no-banner  # skip the banner

ic ctx web              # full triage context (services, learnings, dependencies)
ic status web           # every configured monitoring source at once
ic find example.com     # which node serves this domain / IP / alias?
ic learn web "root cause: connection pool exhausted"   # capture the finding
```

### `ic ssh` and the Context Banner

`ic ssh` is the flagship. It resolves the node, prints a short (≤5 line) banner
to **stderr**, then `exec`s `ssh`. Sending the banner to stderr keeps piped
output clean, so `ic ssh web cat /etc/hosts > hosts.txt` captures only the
remote file:

```text
● vm:web-01  Production Web Server
  services: nginx, php-fpm — If CPU high, check PHP-FPM pool first.
  last learning (2024-01-20): Laravel log rotation not working
  3 direct dependents — docs: ic ctx vm:web-01
```

The banner is best-effort: if anything fails to assemble it, `ic ssh` still
connects. The SSH target is the node's `ssh_alias`, then its first domain, then
its first IP. Extra arguments after the query become a remote command.

These short commands are aliases for the canonical, scriptable long forms:
`ic ctx` → `ic describe node context`, `ic find` → `ic describe node find`,
`ic status` → `ic query status`, `ic learn` → `ic describe node learning`
(with `source=human`). Use the long forms in scripts and the short ones at the
keyboard.

## Troubleshooting with Claude

### The /ic-triage Skill

Use the `/ic-triage` skill in Claude Code:

```
/ic-triage vm:web-server "high CPU usage"
/ic-triage db-master "slow queries"
/ic-triage myhost  # General health check
```

Claude will:
1. Get full context from `ic describe node context <node-id>`
2. Extract the SSH alias and test connectivity
3. Run USE method diagnostics
4. Check configured services, logs, and custom commands
5. Consider previous learnings
6. Synthesize findings and recommendations
7. Record new learnings if discoveries are valuable

### Getting Node Context

Claude uses this command to understand the node:

```bash
ic describe node context vm:web-server
ic describe node context vm:web-server --format json
ic describe node context vm:web-server --json   # shorthand for --format json

# Or the shortcut:
ic ctx web-server
ic ctx web-server --json
```

Output includes:
- SSH connection info (alias or fallback to IP/domain)
- Access tier and capabilities
- Triage hints (services, context)
- Previous learnings
- Dependencies (upstream and downstream)

### Graph Analysis

Analyze infrastructure dependencies:

```bash
# What does this node depend on?
ic graph analyze vm:web-server --upstream

# What depends on this node?
ic graph analyze vm:db-master --downstream

# Find all paths between two nodes
ic graph analyze vm:web-server --paths-to vm:db-master

# Impact analysis: what breaks if this node fails?
ic graph impact vm:db-master

# Find single points of failure
ic graph spof

# Detect circular dependencies
ic graph cycles

# Find orphaned nodes
ic graph orphans
```

### Rendering the Graph

Turn the dependency graph into a shareable diagram:

```bash
# Interactive HTML (search, click-to-inspect, category filters) — default
ic graph render --open

# Static SVG for READMEs and runbooks (requires: pip install 'infracontext[viz]')
ic graph render -f svg -o docs/topology.svg

# GraphML for Gephi, yEd, or Cytoscape
ic graph render -f graphml

# Mermaid flowchart text for markdown code fences (default: <name>.mmd)
ic graph render -f mermaid
ic graph render -f mermaid -o - >> runbook.md   # -o - writes to stdout

# Everything across projects and external roots
ic graph render -A --open
```

The HTML page is a single self-contained file — the vis-network library
(vendored, pinned to an exact version) is inlined, so it opens offline. Drop
it in a wiki or send it to a colleague. Pass `--cdn` for a much smaller file
that loads vis-network from the pinned CDN URL instead (needs internet on
first view). `--open` launches the result in your default application.

Mermaid output uses one `subgraph` per project/root when rendering merged
graphs (`-A`); single-project renders stay flat. Very large graphs trigger a
stderr warning (tune with `IC_MERMAID_MAX_NODES`, `0` disables).

## SSH Configuration

### Setting Up SSH Aliases

Configure SSH aliases in `~/.ssh/config`:

```
Host web-prod
    HostName 192.168.1.10
    User admin
    IdentityFile ~/.ssh/infra_key

Host db-prod
    HostName 192.168.1.20
    User admin
    IdentityFile ~/.ssh/infra_key
    # Through a bastion
    ProxyJump bastion

Host bastion
    HostName bastion.example.com
    User admin
    IdentityFile ~/.ssh/bastion_key
```

Then in your node YAML:

```yaml
ssh_alias: "web-prod"
```

Claude will simply run `ssh web-prod` - all connection details are handled by SSH config.

### Why SSH Aliases?

- **Simplicity**: One name instead of user@host:port + options
- **Jump hosts**: ProxyJump handled automatically
- **Key management**: IdentityFile per-host
- **Consistency**: Same alias works everywhere

## Infrastructure Sources

### SSH Config Import

Import nodes from an SSH config file:

```bash
# Auto-discovers path from project hierarchy
# Project acme/production → ~/.ssh/conf.d/acme/production.conf
ic import ssh-config

# Or specify an explicit path
ic import ssh-config --path ~/.ssh/config
```

> The command was renamed from `ic import ssh` to `ic import ssh-config` (to
> avoid confusion with the top-level `ic ssh`, which connects). The old name
> still works but is deprecated and prints a warning.

### Proxmox Integration

Sync nodes from a Proxmox VE cluster:

```bash
# Store API credentials in system keychain
ic config credential set proxmox:prod

# Add the source
ic describe source add prod --type proxmox

# Configure (opens editor)
ic describe source configure prod

# Sync nodes
ic describe source sync prod
```

Synced nodes get `managed_by: proxmox-prod`. You can still add `ssh_alias`, `triage` config, and other fields - they won't be overwritten.

Every sync (ssh-config and Proxmox alike) also appends a run record under
`.infracontext/runs/` listing what the source reported, and stamps a
write-once `first_seen` date on nodes it creates. `ic doctor` derives node
presence from these records and warns when a source-managed node stops
appearing in successful syncs — nodes are never auto-deleted (see
[Data Validation](#data-validation)).

### CheckMK Integration

Import hosts from a CheckMK site via Livestatus over SSH — read-only and
credential-free (no automation user or REST API token needed). Monitoring is
usually the most complete host inventory an environment already has, which
makes this a good *first* sync source when mapping an existing estate:

```bash
# Add the source (writes a config skeleton)
ic describe source add cmk --type checkmk

# Edit the config: set ssh_alias (SSH alias of the CheckMK server)
# and site (OMD site name)
ic describe source configure cmk

# Sync nodes
ic describe source sync cmk
```

Config options in the source YAML:

```yaml
type: checkmk
ssh_alias: monitor            # SSH alias of the CheckMK server
site: mysite                  # OMD site name
exclude_patterns:             # host-name regexes to skip
  - "^[0-9a-f]{12}$"          # default: docker piggyback container IDs
strip_domain_suffixes:        # optional: shorten slugs (names keep the FQDN)
  - ".example.com"
default_node_type: vm
type_patterns:                # optional overrides, first match wins
  network_device: ["^switch-", "^fw-"]
  physical_host: ["^storage"]
```

Node types are inferred in order: `type_patterns` (explicit config), the
CheckMK `cmk/device_type` label (`vm`, `container`, `switch`, `router`,
`firewall`, `appliance`, `bmc`), then `default_node_type`. Every imported
node gets a `checkmk` observability entry, so `ic query checkmk <node>`
works as soon as a CheckMK query source is configured. `ssh_alias` and other
manual fields are never overwritten on re-sync.

### SNMP Discovery

Discover network devices (switches, routers, appliances) that speak SNMP but no
shell — the SSH/kubectl importers can't reach them. Added in ic 0.4.0.

```bash
# Add the source and edit its config
ic describe source add snmp --type snmp
ic describe source configure snmp

# Store the community (v2c) or auth/priv keys (v3) in the keychain — never YAML
ic config credential set snmp:snmp:community      # <-- snmp:<source-name>:community
# v3 instead:
# ic config credential set snmp:snmp:auth
# ic config credential set snmp:snmp:priv

# Sync
ic describe source sync snmp
```

Config in the source YAML (see [SCHEMA.md](SCHEMA.md#source-configuration-fields)
for the full field list):

```yaml
type: snmp
snmp_version: "2c"           # 2c | 3
targets:                     # explicit host list (no CIDR expansion)
  - host: 10.0.0.1
    name: core-sw-01         # optional; else sysName, else host
  - 10.0.0.2                 # a bare host string is also accepted
port: 161
max_interfaces: 64           # cap on interfaces stored in attributes.snmp
default_node_type: network_device
# v3 only: v3_user, v3_auth_protocol (md5|sha), v3_priv_protocol (des|aes)
```

**Credentials** live in the system keychain, keyed by source name (never in the
YAML): v2c reads `snmp:<source>:community`; v3 reads `snmp:<source>:auth` and,
optionally, `snmp:<source>:priv`.

**What it collects** per target, from standard MIBs:

- **Identity** (SNMPv2-MIB system group): sysName → slug, plus sysDescr,
  sysLocation, sysUpTime.
- **Hardware** (ENTITY-MIB): the best physical entity (chassis > stack > module)
  yields manufacturer/model/serial into `attributes.hardware`.
- **Interfaces** (IF-MIB): a port summary (name, admin/oper status, speed, MAC)
  into `attributes.snmp.interfaces`, capped at `max_interfaces` with a
  truncation note so a large chassis can't bloat the file.

Each synced device gets an `snmp` observability entry, so `ic query snmp <node>`
works immediately (see [Querying Monitoring Sources](#querying-monitoring-sources)).
The entry is source-owned (its `source` field names this sync source), so a
changed target host is tracked on the next sync; hand-written entries without
`source` are never touched.

**LLDP edge behavior**: LLDP-MIB neighbor tables give physical topology. A
neighbor whose `lldpRemSysName` matches an *existing* node becomes a
`connects_to` edge; everything else is recorded under
`attributes.snmp.unmatched_neighbors` and surfaced as a sync warning — the sync
**never auto-creates a node from an LLDP string**. Each target is collected
independently: one that fails mid-walk is marked partial and left exactly as it
was, while the other targets still sync.

### Redfish (BMC) Integration

Import bare-metal inventory straight from the BMC (iDRAC, iLO, XClarity,
OpenBMC) over standard HTTPS/JSON — no vendor SDK. Added in ic 0.4.0.

```bash
ic describe source add redfish --type redfish
ic describe source configure redfish

# Credential is a keychain account holding "user:password"
ic config credential set redfish:prod

ic describe source sync redfish
```

```yaml
type: redfish
endpoints:                   # one BMC per entry
  - url: https://bmc-web-01.example.com
    name: web-01-bmc         # optional; else system HostName, else URL host
  - url: https://10.0.0.51
credential: redfish:prod     # keychain account holding "user:password"
verify_ssl: true             # default; set tls_skip_verify: true for self-signed
```

Each endpoint imports one `network_device` node — the BMC — carrying the
ComputerSystem inventory (manufacturer/model/serial/SKU/UUID/BIOS) and a
source-owned `redfish` observability entry for `ic query redfish` (a changed
BMC URL — new scheme or port — is tracked on the next sync; hand-written
entries without `source` are never touched). The BMC's `manages` edge to
the host it controls is inferred by matching the system serial number against
existing nodes' `attributes.hardware.serial` (case-insensitive, exact): a single
match yields the edge; zero or multiple matches only warn (with the candidates)
and never guess.

### NetBox (DCIM) Integration

Pull datacenter inventory from NetBox — the de-facto open-source DCIM source of
truth — so sites, racks, and devices round-trip into the physical layer. Added
in ic 0.4.0.

```bash
ic describe source add netbox --type netbox
ic describe source configure netbox

# Credential is a keychain account holding the NetBox API token
ic config credential set netbox:prod

ic describe source sync netbox
```

```yaml
type: netbox
url: https://netbox.example.com
credential: netbox:prod      # keychain account holding the API token
verify_ssl: true             # default; tls_skip_verify forces off
site: dc1                    # optional; restrict the sync to one site slug
max_devices: 500             # optional; per-sync device cap (default 500)
role_map:                    # optional; NetBox role slug -> ic node type
  core-router: network_device
```

The sync walks three DCIM collections:

- `/api/dcim/sites/` → `site` nodes
- `/api/dcim/racks/` → `rack` nodes, plus a `located_in` edge rack → site
- `/api/dcim/devices/` → `physical_host` / `network_device` / `pdu` / `ups`
  nodes (type inferred from the device role, overridable via `role_map`), plus a
  `located_in` edge device → rack (or → site when unracked). Each device carries
  `attributes.hardware` (manufacturer/model/serial/asset_tag/u_height/
  rack_position/rack_face) and its `primary_ip`.

Sites and racks are uncapped; devices are capped at `max_devices` (default 500)
to keep a sync bounded. NetBox primary keys are stable, so a renamed object is
matched by PK and relocated rather than duplicated — the same ownership,
manual-field preservation, and run-record contract as the other sync sources.

**Relocation safety** (all sync sources — CheckMK, SNMP, Redfish, NetBox): a
relocation changes the node id and deletes the file at the old slug, so every
reference to the old id — manual relationship edges, chain members, and the
sync's own topology edges — is rewritten to the new id in the same run.
Qualified cross-project references can't be edited safely from a sync and are
left for `ic doctor` to flag.

### Device Types (NetBox devicetype-library)

Fill a node's `attributes.hardware` from a community
[devicetype-library](https://github.com/netbox-community/devicetype-library)
YAML file — an offline hardware spec, no NetBox instance needed. Added in ic 0.4.0.

```bash
ic import devicetype dell-poweredge-r750.yaml --node physical_host:h1
ic import devicetype cisco-catalyst-9300-48p.yaml -n sw-01 --force
```

Only the physical-identity subset is mapped (`manufacturer`, `model`,
`part_number`, `u_height`, `is_full_depth`, `airflow`, `weight`/`weight_unit`,
`subdevice_role`); interface, console, and power port template lists in the file
are ignored by design — infracontext models running hosts, not port inventories.
The merge is **fill-only**: an existing hardware value always wins and only
empty/absent fields are filled, so re-importing never clobbers curated data.
Pass `--force` to overwrite. A file lacking both `manufacturer` and `model` is
rejected before merge.

### Managing Sources

```bash
# List configured sources
ic describe source list

# Remove a source (does not delete synced nodes)
ic describe source remove prod
```

### Manual Nodes

For infrastructure not covered by plugins:

```bash
ic describe node create --type external_service --name "AWS S3"
ic describe node edit external_service:aws-s3
```

## Best Practices

### 1. Start Simple

Begin with:
- Node name, type, slug
- SSH alias
- One-line description

Add more detail when you need it.

### 2. Document the Non-Obvious

Claude knows standard things. Document what's unique:
- Non-standard log locations
- Custom health endpoints
- Expected high resource usage
- Known issues and workarounds

### 3. Use SSH Aliases

Don't put connection details in YAML. Use SSH config - it's more flexible and works with other tools too.

### 4. Let Learnings Accumulate

After each triage, Claude records what it discovered. Over time, your documentation becomes richer without manual effort.

## Querying Monitoring Sources

Query your monitoring systems directly from the CLI. Useful for quick status checks before SSH diagnostics.

### Supported Sources

| Source | Backend | Query Method |
|--------|---------|--------------|
| Prometheus | HTTP API | HTTP requests |
| Loki | HTTP API | HTTP requests |
| CheckMK | REST API | HTTP requests |
| SNMP | Network device | SNMP walk (added in ic 0.4.0) |
| Redfish | BMC | HTTPS/JSON (added in ic 0.4.0) |
| Monit | HTTP | SSH tunnel or direct HTTP |

### Setup

**1. Configure source endpoints** (per project):

```bash
# Add sources
ic describe source add prometheus --type prometheus
ic describe source add loki --type loki
ic describe source add checkmk --type checkmk

# Configure each source
ic describe source configure prometheus
```

Source config examples:

```yaml
# sources/prometheus.yaml
type: prometheus
addr: http://prometheus.example.com:9090
credential_key: prometheus:prod  # Keychain account for bearer token (preferred)
# bearer_token: "..."           # Plaintext fallback (avoid in version control)

# sources/loki.yaml
type: loki
addr: http://loki.example.com:3100
credential_key: loki:prod        # Keychain account for bearer token (preferred)

# sources/checkmk.yaml
type: checkmk
api_url: https://monitoring.example.com/mysite/check_mk/api/1.0
credential: checkmk:mysite       # Keychain account (user:secret format)
```

**TLS verification**: HTTPS source configs (prometheus, loki, checkmk) verify
certificates by default (`verify_ssl: true`). For a self-signed monitoring
endpoint, set `tls_skip_verify: true` in the source config to turn verification
off. Monit's direct-HTTP mode verifies by default for `https://` URLs; set
`tls_skip_verify: true` in the node's monit observability entry to disable it
for a self-signed Monit endpoint.

For Prometheus and Loki, use `credential_key` to store bearer tokens in the system keychain rather than plaintext in YAML:

```bash
ic config credential set prometheus:prod -p "your-bearer-token"
ic config credential set loki:prod -p "your-bearer-token"
```

**2. Configure node observability** (how to find each node in monitoring):

```yaml
# In node YAML
observability:
  - type: prometheus
    instance: web-server:9100           # Prometheus instance label
  - type: loki
    selector: '{service_name="web"}'    # LogQL selector
  - type: checkmk
    host_name: web-server.example.com   # CheckMK host name
  - type: monit
    monit_url: http://web-server:2812   # Direct HTTP (optional)
    # OR use SSH mode (default) - queries via node's ssh_alias
  - type: snmp
    instance: 10.0.0.1                  # device host to walk (added in ic 0.4.0)
  - type: redfish
    instance: https://bmc-web-01        # BMC base URL (added in ic 0.4.0)
```

The `snmp` and `redfish` entries are usually written for you by their sync
sources. Unlike the others they carry no slug fallback in `ic query status`,
which probes a device only when the node explicitly declares the entry. (The
standalone `ic query snmp` command is more lenient: when the node lacks an
`snmp` instance it falls back to the node's first IP/domain/slug, since the
operator asked for that specific device by name.)

### Multiple Sources of Same Type

For multiple Prometheus/Loki instances (e.g., prod vs staging):

```yaml
# sources/prometheus-prod.yaml
type: prometheus
addr: http://prometheus-prod:9090

# sources/prometheus-staging.yaml
type: prometheus
addr: http://prometheus-staging:9090
```

Reference specific source in node config:

```yaml
observability:
  - type: prometheus
    source: prometheus-prod    # Uses prometheus-prod.yaml
    instance: web-server:9100
```

### Query Commands

```bash
# Quick status from all configured sources (sources are fetched concurrently,
# so total time is bounded by the slowest source, not the sum)
ic query status vm:web-server
ic query status vm:web-server --json           # one aggregated JSON document
ic status web-server                           # shortcut, fuzzy resolution

# Individual sources
ic query prometheus vm:web-server              # Key metrics (CPU, memory, disk)
ic query prometheus vm:web-server -t cpu       # Specific metric
ic query prometheus vm:web-server --promql 'up{instance="web:9100"}'

ic query loki vm:web-server                    # Recent logs
ic query loki vm:web-server --grep error       # Filter for errors
ic query loki vm:web-server --since 2h         # Time range
ic query loki vm:web-server --labels           # List available labels

ic query checkmk vm:web-server                 # Host status
ic query checkmk vm:web-server -t services     # All services
ic query checkmk vm:web-server -t alerts       # Active problems

ic query snmp network_device:core-sw           # Device health (sysName, uptime, ifs)
ic query snmp core-sw -t interfaces            # Per-interface up/down state

ic query redfish network_device:web-01-bmc     # BMC health rollup + thermal
ic query redfish web-01-bmc -t power           # Live power draw (watts)

ic query monit vm:web-server                   # Monit service summary
ic query monit vm:web-server -s nginx          # Specific service
ic query monit vm:web-server --url http://monit.example.com:2812  # Direct HTTP
```

Every `ic query` command (and `ic query status`) takes `--json` for
machine-readable output. The older `--raw`/`-r` flag is a deprecated alias for
`--json` and still works.

### Monit Modes

Monit runs locally on each server. Two access modes:

**SSH mode** (default): Connects via SSH and queries localhost:2812
```bash
ic query monit vm:web-server  # Uses node's ssh_alias
```

**Direct HTTP mode**: Queries exposed Monit interface
```bash
ic query monit vm:web-server --url http://monit.example.com:2812
```

Or configure in node YAML:
```yaml
observability:
  - type: monit
    monit_url: http://monit.example.com:2812
    credential_hint: monit:web-server  # Optional basic auth (user:pass in keychain)
```

### Storing Credentials

Secrets are stored in the system keychain (macOS Keychain on macOS,
libsecret on Linux, Credential Manager on Windows) via the `keyring`
library — never on argv.

```bash
# Store a credential. Secret is read from interactive prompt or stdin
# (no --password flag — would leak via shell history).
ic config credential set checkmk:mysite
echo "$SECRET" | ic config credential set monit:web-server   # piped

# List accounts known to ic.
ic config credential list

# Check if a credential exists (use --show to reveal)
ic config credential get checkmk:mysite
ic config credential get checkmk:mysite --show

# Delete a credential
ic config credential delete checkmk:mysite
```

**What `list` shows**: account names that `ic` itself has stored. It reads
from a small JSON index at `$XDG_CONFIG_HOME/infracontext/credentials-index.json`
(metadata only — no secrets touch this file). Credentials added to the
keychain *outside* `ic` (e.g. directly via `security` or `secret-tool`)
won't appear in the list; `ic config credential get <name>` still finds
them by name. The split exists because no portable keyring API exposes
enumeration without forcing the backend to decrypt every matching secret.

**Upgrading from a pre-index version of `ic`**: credentials you stored
before the index existed live in the keychain but not in the index, so
`list` will under-report. Backfill once on macOS:

```bash
ic config credential migrate
# -> Added N account(s) to the credential index: ...
```

On Linux there is no metadata-only enumeration path, so migration is not
supported there — re-run `credential set <name>` for each account you
remember (no need to re-enter secrets you haven't lost; you can also
simply ignore the gap if you only use `get <name>` workflows).

## Configuration

View current configuration (environment root, config file, active project):

```bash
ic config show
```

### Shell Completion

Install completion for your current shell, then restart it:

```bash
ic --install-completion
```

Once installed, node IDs and project names tab-complete: `ic ssh <TAB>`,
`ic ctx <TAB>`, and `-p <TAB>` all suggest from the active project on disk.
Completion reads only the `nodes/<type>/<slug>.yaml` directory structure (no
YAML parsing) so it stays fast, and degrades to no suggestions rather than an
error if the environment is missing or half-written.

## Local Overrides

Team members may have different SSH configs or local paths. Use `.infracontext.local.yaml` for machine-specific settings that shouldn't be committed:

```yaml
# .infracontext.local.yaml (add to .gitignore)
nodes:
  "vm:web-server":
    ssh_alias: my-web-alias        # My SSH config uses different alias
    source_paths:
      - /Users/me/projects/webapp  # My local checkout path
```

Overrides are applied automatically when reading nodes. Only these fields can be overridden:
- `ssh_alias` - SSH connection alias
- `source_paths` - Local source code paths (must be absolute)

## Federating Multiple Repositories (External Roots)

A single `.infracontext/` directory works for one team or one project, but an
infrastructure admin often needs a unified view across several repos: a
**fleet repo** holding hypervisors and standalone hosts, plus per-app repos
holding their own VMs and services. External roots compose those without
duplicating node definitions.

### Concept

- Each `.infracontext/` is a *root*: a single source of truth for the nodes
  it defines.
- A root can declare *external roots* in its `.infracontext/config.yaml` to
  pull in other repos read-only (or read-write if you really mean it).
- References across roots use the same `@scope:type:slug` syntax as
  cross-project references. The `scope` is resolved first as an external root
  alias, then as a local project slug. `ic doctor` flags collisions.

### Configuring external roots

```yaml
# .infracontext/config.yaml
active_project: prod
external_roots:
  - alias: fleet
    path: ../infra-fleet          # Relative to env root, or absolute, or with ~
    mode: read-only               # default; use read-write if you edit it here
    description: Shared hypervisors and network gear
```

### Referencing a node in an external root

```yaml
# .infracontext/projects/prod/relationships.yaml
- source: vm:web-01
  target: "@fleet:physical_host:pve-01"
  type: runs_on
```

`@fleet:physical_host:pve-01` resolves to the fleet root's active project.

### Federated CLI commands

Listing and graph commands span roots; per-node commands accept qualified
`@alias:type:slug` IDs so the federated view round-trips cleanly:

```bash
# List nodes across the local root and every external root:
ic describe node list -A

# Filter to one root (use '' for the local root):
ic describe node list -A --root fleet

# Search across the local root + every external root. Matches outside the
# current project come back as qualified IDs ready to paste into other
# commands:
ic describe node find pve-01 -A
# -> @fleet:physical_host:pve-01  (slug contains 'pve-01')

# Inspect a node in another root via its qualified ID:
ic describe node show @fleet:physical_host:pve-01
ic describe node context @fleet:physical_host:pve-01

# Graph traversals follow cross-root edges automatically:
ic graph analyze vm:web-01 --upstream
# -> Web (vm:web-01)
#    runs_on -> PVE-01 (@fleet:default/physical_host:pve-01)
```

### Writing to external roots

External roots default to `mode: read-only`, which is what you want for
typical fleet/app federations — the local working copy can read everything
but only writes into its own home repo. Any write command against a
read-only root errors clearly instead of silently doing the wrong thing:

```bash
ic describe node learning @fleet:physical_host:pve-01 "..." -c "..."
# Root 'fleet' is read-only. Set mode: read-write in external_roots to
# allow writes.

ic describe node edit @fleet:physical_host:pve-01     # same error
ic describe node delete @fleet:physical_host:pve-01   # same error
```

Set `mode: read-write` in `external_roots` if the admin workspace genuinely
owns the external repo and should be able to edit it from here:

```yaml
external_roots:
  - alias: fleet
    path: ../infra-fleet
    mode: read-write
```

If a root is intentionally read-only and you want to record a finding
about one of its nodes, store the learning on a *local* node and reference
the external one in the `--context` text:

```bash
ic describe node learning vm:web-01 \
    "PVE-01 needs firmware update" \
    --context "blocked by @fleet:physical_host:pve-01 reboot window"
```

### Three patterns for cross-repo nodes

1. **Reference only (clean default).** Node lives in one home repo. Other
   repos reference it via `@alias:...`. No duplication.
2. **Overlay** (planned, not yet implemented). Different teams contribute
   non-identity fields to a shared node.
3. **Duplicate definitions** (avoid). `ic doctor` warns when the same
   `type:slug` is defined in multiple roots — promote one to authoritative
   and convert the other to a reference.

### What `ic doctor` checks

- Each external root path exists and contains `.infracontext/`.
- Aliases don't collide with local project names (refs would be ambiguous).
- All `@alias:...` references resolve to an existing node.
- Same node ID defined in multiple roots is reported (warning).

## Data Storage

All data is stored in `.infracontext/` within your project repo (git-tracked):

```
.infracontext/
├── config.yaml                  # Environment config (active project)
├── runs/                        # Per-sync run records (newest 20 per source)
└── projects/
    ├── homelab/                 # Simple project
    │   ├── nodes/
    │   ├── relationships.yaml
    │   └── sources/
    └── acme/                    # Hierarchical: customer
        └── production/          # Hierarchical: project
            ├── nodes/
            │   ├── vm/
            │   │   ├── web-server.yaml
            │   │   └── db-master.yaml
            │   └── physical_host/
            │       └── pve01.yaml
            ├── relationships.yaml
            └── sources/
                ├── prometheus.yaml
                ├── loki.yaml
                └── checkmk.yaml

.infracontext.local.yaml         # Local overrides (gitignored)
```

### Migration from Legacy Location

If you have data in the old `~/.local/share/` location:

```bash
# Check migration status
ic migrate status

# Preview migration
ic migrate legacy --dry-run

# Migrate all projects
ic migrate legacy

# Migrate specific project
ic migrate legacy -p acme/production
```

YAML files are human-editable and preserve comments when modified by the CLI.

## Data Validation

Validate your infrastructure data for syntax errors, schema compliance, and completeness:

```bash
# Full validation
ic doctor

# JSON output (for CI/CD)
ic doctor --json
```

Doctor checks for:
- **Syntax errors**: Invalid YAML (including `.infracontext/config.yaml` and
  `.infracontext.local.yaml`)
- **Config schema violations**: Bad keys in `config.yaml` itself (reported
  cleanly, never a traceback)
- **Schema violations**: Fields that don't match Pydantic models
- **Node id vs. path**: A node whose `id` disagrees with its `type:slug`
  directory location
- **Local override errors**: Invalid fields or relative paths in `.infracontext.local.yaml`
- **Missing info**: Compute nodes without `ssh_alias`, nodes without descriptions
- **Orphaned relationships**: References to non-existent nodes
- **Duplicates**: Redundant relationships
- **Relationship constraints**: Hand-edited `(source, target, type)` triples
  re-checked against the create-time constraint matrix — invalid triples
  silently poison graph traversals (warning, since the matrix may simply lack
  a legitimate pairing)
- **Chains**: Duplicate chain names, dangling member references, unknown edge
  types, and constraint violations on the expanded pairs
- **Duplicate identifiers**: An `ssh_alias` or IP address shared by several
  nodes in one project makes fuzzy resolution ambiguous (warning); the same
  alias reused across projects is often legitimate (info)
- **Ungrouped nodes**: Compute/service nodes not reachable from any
  application node via contains/depends_on/uses edges (info; skipped when the
  project has no application nodes)
- **Blank learnings**: Whitespace-only context or finding (info)
- **Stale source-managed nodes**: Nodes a source stopped reporting. Every sync
  writes a run record to `.infracontext/runs/` (newest 20 per source kept);
  doctor warns when a `managed_by` node is absent from recent *successful*
  syncs (possibly-missing within a 3-sync grace window, missing beyond it).
  Manual nodes never warn, and syncs never auto-delete — a failed, partial,
  or empty sync also never rewrites node files.

Exit code is 1 if errors are found (warnings/info are non-blocking).

## Claude Code Integration

### Installing Skills and Agents

Symlink the skills and agents to your Claude Code configuration:

```bash
# Triage skill - USE method diagnostics
ln -s /path/to/infracontext/commands/ic-triage.md ~/.claude/commands/ic-triage.md

# Node collector skill - auto-discover and create node YAML
ln -s /path/to/infracontext/commands/ic-collect.md ~/.claude/commands/ic-collect.md

# Diagnostic agents (used by triage skill)
ln -s /path/to/infracontext/agents ~/.claude/agents/infracontext
```

### Using /ic-triage

```
/ic-triage vm:web-server "high CPU"
```

Claude reads the skill instructions, gets node context from `ic`, and performs the investigation.

### Using /ic-collect

```
/ic-collect web-prod
/ic-collect s.myserver --project prod
```

Claude SSHes to the server, auto-discovers system info (OS, services, ports, monitoring agents), then walks you through an interactive conversation to fill in project, triage config, and context. Outputs a complete node YAML.

For a bare-metal `physical_host` (gated on `systemd-detect-virt` = `none`, so
guests are skipped), a hardware phase added in ic 0.4.0 also probes the physical
substrate — `dmidecode` chassis identity, `ipmitool` BMC/FRU, `lldpctl` switch
peers, `ethtool -P` permanent MACs. Findings enrich `attributes.hardware`
(fill-only), and with your confirmation spawn a BMC `network_device` (with a
`manages` edge) and `connects_to` cabling edges to discovered switches. Every
probe is optional and degrades gracefully on a missing tool or denied sudo.

### MCP Server

`ic mcp serve` runs infracontext as an [MCP](https://modelcontextprotocol.io)
server over stdio, so any MCP client (Claude Desktop, other agents) can reach
the same context the CLI exposes. It requires the optional `mcp` dependency:

```bash
# installed as a uv tool: include the extra at install time
uv tool install '.[mcp]'

# running from a dev checkout instead: sync the extra
uv sync --extra mcp

ic mcp serve
```

Tools exposed:

| Tool | Purpose |
|------|---------|
| `find_node` | Fuzzy node lookup (domain, IP, name, SSH alias, or ID) |
| `get_context` | Full triage context for a node |
| `query_status` | Aggregated status across configured monitoring sources |
| `add_learning` | Record a finding on a node |
| `parked_schema` | Structure outline of a parked query payload |
| `parked_grep` | Regex search over a parked payload, with context lines |
| `parked_slice` | Numbered line range from a parked payload |
| `parked_get` | Extract a nested value from a parked payload by dotted path |

Point your MCP client at the `ic mcp serve` command; it inherits the same
environment discovery as the CLI (`IC_ROOT`, cwd walk-up, or the registered
default environment), so set `IC_ROOT` in the client's launch config when it
runs outside your infra repo.

#### Oversized-output parking

Observability payloads (Loki logs, CheckMK service lists, SOS findings) can
dwarf an agent's context window. On the MCP path, `query_status` parks any
per-source `data` larger than a byte threshold to a per-user scratch
directory and returns a compact pointer (`"_parked": true`, plus a structure
preview and copy-pasteable tool hints) in its place. The agent then pulls
only the slices it needs through the `parked_*` tools, each bounded by a
per-call size cap. Small sources stay inline; only the offender is parked.

Parking applies **only** to `ic mcp serve` — CLI `--json` output always stays
complete, so scripts piping to `jq` are unaffected.

| Variable | Default | Purpose |
|----------|---------|---------|
| `IC_PARK_THRESHOLD` | `20000` | Bytes of pretty-printed JSON above which a source payload is parked |
| `IC_SCRATCH_DIR` | `~/.cache/infracontext/parked` | Where parked payloads live (honors `XDG_CACHE_HOME`) |

Parked files are content-addressed (re-running the same query reuses the same
file) and pruned after 7 days.
