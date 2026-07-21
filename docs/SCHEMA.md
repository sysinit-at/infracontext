# Schema Reference

This document describes all fields allowed in node and project YAML files.

---

## Project YAML Schema

Project configuration is stored in `project.yaml` at the project root.

### Example

```yaml
version: "2.0"
name: "ACME Corp Production"
slug: acme-corp/production
description: |
  Production infrastructure for ACME Corp's main web application.

access:
  default_tier: 2       # unprivileged
  max_tier: 3           # privileged (hard ceiling)
  collector_script: /usr/local/bin/ic-collect.sh
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `version` | string | Schema version, always `"2.0"` |
| `name` | string | Human-readable project name |
| `slug` | string | URL-safe identifier (matches directory path) |
| `description` | string | Optional description |
| `access` | ProjectAccessConfig | Access tier configuration (optional) |
| `links` | ProjectLinks | Project-level links (optional) |

### Links

| Field | Type | Description |
|-------|------|-------------|
| `issue_tracker` | string | Issue tracker URL (e.g., Jira, GitHub Issues) |
| `communication_channel` | string | Team communication channel (e.g., Slack, Teams) |

### Access Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `default_tier` | int (0-4) | 2 | Default tier for nodes without explicit override |
| `max_tier` | int (0-4) | 3 | Maximum allowed tier (hard ceiling) |
| `collector_script` | string | `/usr/local/bin/ic-collect.sh` | Path to collector script |

### Access Tiers

| Tier | Value | Capabilities |
|------|-------|--------------|
| `local_only` | 0 | Local data + observability APIs (Prometheus, Loki, CheckMK) |
| `collector` | 1 | + Execute pre-deployed collector script |
| `unprivileged` | 2 | + Arbitrary read-only SSH commands (no sudo) |
| `privileged` | 3 | + SSH with sudo/root |
| `remediate` | 4 | + Autonomous fixes after diagnosis |

**Configuration Hierarchy:**
```
Project default → Node override → CLI restriction
      ↓                ↓               ↓
   baseline      can raise/lower    can only lower
```

The project's `max_tier` acts as a hard ceiling.

---

# Node YAML Schema Reference

This section describes all fields allowed in a node YAML file.

## Complete Example

```yaml
version: "2.0"
id: "vm:web-server"
slug: web-server
type: vm
name: "Production Web Server"

# Source tracking (auto-populated by sync, or null for manual nodes)
source_id: "proxmox:cluster1:qemu:100"
source: "proxmox-prod"
managed_by: "proxmox-prod"  # null = user-defined

# SSH connection - CRITICAL for triage
# This is the SSH alias from ~/.ssh/config that Claude should use
ssh_alias: "web"

# Network identity
ip_addresses:
  - "192.168.1.10"
  - "10.0.0.10"
domains:
  - "web.example.com"
  - "www.example.com"

# Documentation
description: "Main production web server running nginx + PHP-FPM"
notes: |
  Markdown supported here.

  ## Deployment
  Uses ansible playbook `deploy-web.yml`

  ## Known Issues
  - Requires manual cache clear after deploy

# Local source code paths
source_paths:
  - "/home/user/projects/webapp"
  - "/home/user/projects/deploy-scripts"

# Endpoints (ports this node exposes or consumes)
endpoints:
  - name: public-https
    protocol: https
    port: 443
    direction: input
    domains:
      - "web.example.com"
  - name: admin-http
    protocol: http
    port: 8080
    direction: input
  - name: db-client
    protocol: postgres
    port: 5432
    direction: output

# Functions (what this node does)
functions:
  - name: web-server
    endpoints:
      - public-https
    applications:
      - main-website
  - name: reverse-proxy
    endpoints:
      - public-https
    backend_groups:
      app-servers:
        nodes:
          - vm:app-01
          - vm:app-02
        health_check: "/health"

# Observability endpoints
observability:
  - type: prometheus
    instance: web-server:9100
  - type: loki
    selector: '{service_name="web"}'
  - type: checkmk
    host_name: web-server.example.com
  - type: dashboard
    name: "Grafana"
    url: "https://grafana.example.com/d/web-server"

# Source-specific attributes (flexible key-value store)
attributes:
  proxmox_vmid: 100
  proxmox_node: "pve01"
  memory_mb: 4096
  cpu_cores: 2

# Triage hints (minimal - agent discovers the rest)
triage:
  services:
    - nginx
    - php-fpm
  context: |
    This server handles 10k req/s peak.
    If CPU high, check PHP-FPM pool status first.

# Learnings discovered during triage (accumulated over time)
learnings:
  - date: "2024-01-15"
    context: "high CPU investigation"
    finding: "PHP-FPM pool was set to static with too many workers"
    source: agent
  - date: "2024-01-20"
    context: "disk space issue"
    finding: "Laravel log rotation not working, check /etc/logrotate.d/laravel"
    source: human

# Machine-specific overrides go in .infracontext.local.yaml (gitignored)
# See "Local Overrides" section below
```

---

## Field Reference

**Field lifecycle**: the schema `version` stays `"2.0"`. Fields introduced
after that baseline are annotated *added in ic `<version>`* in their table
entry — the annotation, not a separate ledger, is the field's history. Older
ic versions strip unknown fields with a warning on read and preserve their
values on rewrite, so a repo shared by mixed versions loses nothing (see
[Validation](#validation)).

### Core Fields (Required)

| Field | Type | Description |
|-------|------|-------------|
| `version` | string | Schema version, always `"2.0"` |
| `id` | string | Stable ID in format `type:slug`; must equal `<type>:<slug>` (enforced) |
| `slug` | string | URL-safe identifier matching `^[a-z0-9][a-z0-9-]*$` (enforced) |
| `type` | NodeType | Node type (see Node Types below) |
| `name` | string | Human-readable display name |

**Slug and ID enforcement**: `slug` must match `^[a-z0-9][a-z0-9-]*$` —
lowercase alphanumerics and internal hyphens, starting with an alphanumeric.
This is the shape `slugify()` emits and it forbids the scope separator (`:`)
that would otherwise let a slug corrupt an ID. Separately, `id` must equal
`<type>:<slug>`; a mismatch is a validation error (also flagged by
`ic doctor`, which additionally checks the id against the node's on-disk
`type/slug` file location).

### Source Tracking (Optional)

| Field | Type | Description |
|-------|------|-------------|
| `source_id` | string | External source reference (e.g., `proxmox:cluster1:qemu:100`) |
| `source` | string | Source name that created this node |
| `managed_by` | string | Source that manages this node; `null` = user-defined |
| `first_seen` | string | ISO date the node was first created by an importer/sync. Write-once: never updated by later syncs; absent on older nodes stays absent. Added in ic 0.3.0. Mixed-version caveat: a teammate syncing with ic < 0.3.0 rewrites that source's nodes *without* this field (pre-0.3.0 syncs drop unknown fields on every write), and the value is not backfilled — treat it as best-effort provenance until the whole team is on ≥ 0.3.0. |

Freshness is otherwise *derived*, never stored on nodes: each source sync
appends a small run record under `.infracontext/runs/` (file format under
[Run Records](#run-records-infracontextruns)). `ic doctor` classifies
source-managed nodes against the successful, non-empty runs and warns when a
node hasn't been seen recently (`possibly-missing` within a 3-sync grace
window, `missing` beyond it). Syncs never auto-delete nodes, and a failed,
partial, or empty sync never rewrites node files.

### SSH Connection (Critical for Triage)

| Field | Type | Description |
|-------|------|-------------|
| `ssh_alias` | string | SSH alias from `~/.ssh/config` - Claude uses this for all SSH commands |

**Important**: The SSH alias should be defined in your `~/.ssh/config` file. It handles hostnames, ports, jump hosts, keys, and users. Claude simply runs `ssh <alias>`.

### Network Identity (Optional)

| Field | Type | Description |
|-------|------|-------------|
| `ip_addresses` | list[string] | IP addresses (fallback if no ssh_alias) |
| `domains` | list[string] | DNS names (fallback if no ssh_alias or IP) |

### Documentation (Optional)

| Field | Type | Description |
|-------|------|-------------|
| `description` | string | Short description |
| `notes` | string | Free-form notes (Markdown supported) |
| `source_paths` | list[string] | Local paths to related source code |

### Attributes (Optional)

| Field | Type | Description |
|-------|------|-------------|
| `attributes` | dict | Flexible key-value store for source-specific data |

---

## Node Types

### Compute

| Type | Description | Supports Triage |
|------|-------------|-----------------|
| `physical_host` | Bare metal server | Yes |
| `vm` | Virtual machine | Yes |
| `lxc_container` | LXC container | Yes |
| `oci_container` | OCI/Docker container | Yes |
| `docker_compose` | Docker Compose project | Yes |
| `podman_compose` | Podman Compose project | Yes |
| `podman_quadlet` | Podman Quadlet project | No |
| `hypervisor_cluster` | Cluster of hypervisors | No |

### Kubernetes

| Type | Description |
|------|-------------|
| `k8s_cluster` | Kubernetes cluster |
| `k8s_node` | Kubernetes node |
| `k8s_namespace` | Kubernetes namespace |
| `k8s_pod` | Kubernetes pod |
| `k8s_service` | Kubernetes service |
| `k8s_deployment` | Kubernetes deployment |

### Storage

| Type | Description |
|------|-------------|
| `storage` | Generic storage |
| `filesystem` | Filesystem |
| `nfs_share` | NFS share |
| `ceph_cluster` | Ceph cluster |
| `block_storage` | Block storage volume |
| `object_storage` | Object storage (S3-compatible) |

### Network

| Type | Description |
|------|-------------|
| `network` | Network/VLAN |
| `subnet` | Subnet |
| `network_device` | Physical network gear: switch, router, firewall, BMC/iLO (added in ic 0.3.1) |

### Physical / Datacenter

Facility and power assets (added in ic 0.4.0). These carry no SSH triage — they
are deliberately absent from the compute set, so `ic doctor` never nags them for
`ssh_alias` or observability config. See [Physical Layer](#physical-layer) for
placement/power semantics and the hardware/cabling conventions.

| Type | Description | Supports Triage |
|------|-------------|-----------------|
| `site` | Datacenter, building, or colocation facility | No |
| `rack` | Equipment rack within a site | No |
| `pdu` | Power distribution unit | No |
| `ups` | Uninterruptible power supply | No |

### DNS

| Type | Description |
|------|-------------|
| `domain` | Domain name |
| `dns_zone` | DNS zone |

### Services

| Type | Description |
|------|-------------|
| `application` | Business application |
| `service` | Generic service |
| `service_cluster` | Clustered service |
| `external_service` | External/third-party service |
| `cdn_endpoint` | CDN endpoint |

---

## Endpoints

Endpoints describe ports/protocols a node exposes or consumes.

```yaml
endpoints:
  - name: public-https        # Required: unique name
    protocol: https           # See protocols below
    port: 443                 # Required: 1-65535
    direction: input          # input (accepts) or output (initiates)
    domains:                  # Optional: domains served on this endpoint
      - "example.com"
    attributes: {}            # Optional: extra config
```

### Protocols

`tcp`, `udp`, `http`, `https`, `nfs`, `grpc`, `ws`, `ssh`, `mysql`, `postgres`, `redis`, `mongodb`

---

## Functions

Functions describe what a node does.

```yaml
functions:
  - name: web-server          # Required: function type
    endpoints:                # Which endpoints this function uses
      - public-https
    applications:             # Application tags
      - main-website
    backend_groups:           # For load balancers/proxies
      app-servers:
        nodes:
          - vm:app-01
          - vm:app-02
        health_check: "/health"
    attributes: {}
```

### Function Types

**Networking:** `reverse-proxy`, `load-balancer`, `firewall`, `gateway`, `vpn`

**Web:** `web-server`, `app-server`, `api-server`

**Data:** `database`, `cache`, `search`, `message-queue`

**Storage:** `nfs-server`, `storage`, `backup`

**Ops:** `monitoring`, `logging`, `scheduler`

**Other:** `custom`

---

## Observability

Links to monitoring/observability endpoints. Supports both generic types and specific monitoring system integrations for `ic query`.

### Generic Observability

```yaml
observability:
  - type: metrics             # Required: see types below
    name: "Node Exporter"     # Display name
    url: "http://host:9100/metrics"  # URL
    credential_hint: "vault:node-exporter"  # Optional
    notes: "Prometheus format"              # Optional
```

### Monitoring Query Integration

For `ic query` integration, use type-specific fields:

```yaml
observability:
  - type: prometheus
    instance: web-server:9100           # Prometheus instance label
    source: prometheus-prod             # Optional: specific source config
  - type: loki
    selector: '{service_name="web"}'    # LogQL selector
  - type: checkmk
    host_name: web-server.example.com   # CheckMK host name
  - type: monit
    monit_url: http://web-server:2812   # Direct HTTP (optional, defaults to SSH mode)
    monit_port: 2812                    # Monit port for SSH mode (default: 2812)
    credential_hint: monit:web-server   # Optional basic auth
    tls_skip_verify: true               # Optional: self-signed https monit_url
```

**Entry ownership** (added in ic 0.4.0): the `source` field doubles as an
ownership marker for sync sources that attach their own query endpoint (SNMP,
Redfish). An entry whose `source` names the sync source is *owned* by it — a
re-sync updates that entry when the configured endpoint changes (new BMC URL,
new SNMP target host). Entries without `source`, or naming a different source,
are manual configuration and are never modified by any sync. Set `source` on
hand-written entries only when you want that sync source to manage them.

### Source Configuration Fields

Prometheus and Loki source configs support `credential_key` for keychain-based authentication:

```yaml
# sources/prometheus.yaml
type: prometheus
addr: https://prometheus:9090
credential_key: prometheus:prod  # Preferred: keychain account for bearer token
# bearer_token: "..."           # Fallback: plaintext (avoid in version control)
verify_ssl: true                 # Default; verify TLS certificates
# tls_skip_verify: true          # Override to disable verification (self-signed)
```

If `credential_key` is set, the token is retrieved from the system keychain. Falls back to `bearer_token` if the keychain lookup fails or is not configured.

HTTPS source configs (prometheus, loki, checkmk, redfish, netbox) verify
certificates by default (`verify_ssl: true`). Set `tls_skip_verify: true` for a
self-signed endpoint.

**SNMP** (added in ic 0.4.0). Discovers network devices via LLDP/ENTITY-MIB/IF-MIB
and answers `ic query snmp`. Credentials live in the keychain keyed by source name
(never in YAML): v2c reads `snmp:<source>:community`; v3 reads `snmp:<source>:auth`
and, optionally, `snmp:<source>:priv`.

```yaml
# sources/snmp.yaml
type: snmp
snmp_version: "2c"           # 2c | 3
targets:                     # explicit host list (no CIDR expansion)
  - host: 10.0.0.1
    name: core-sw-01         # optional; else sysName, else host
  - 10.0.0.2                 # a bare host string is also accepted
port: 161
timeout: 5
retries: 1
max_interfaces: 64           # cap on interfaces stored in attributes.snmp
default_node_type: network_device
# v3 only:
v3_user: monitor
v3_auth_protocol: sha        # md5 | sha
v3_priv_protocol: aes        # des | aes
```

**Redfish** (added in ic 0.4.0). Imports BMC/host inventory and answers
`ic query redfish`. `credential` names a keychain account whose secret is stored
as `user:password`.

```yaml
# sources/redfish.yaml
type: redfish
endpoints:                   # one BMC per entry
  - url: https://bmc-web-01.example.com
    name: web-01-bmc         # optional; else system HostName, else URL host
  - url: https://10.0.0.51
credential: redfish:prod     # keychain account holding "user:password"
verify_ssl: true             # default; tls_skip_verify forces off
```

**NetBox** (added in ic 0.4.0). Pulls DCIM sites/racks/devices from the NetBox
REST API. `credential` names a keychain account holding the raw API token.

```yaml
# sources/netbox.yaml
type: netbox
url: https://netbox.example.com
credential: netbox:prod      # keychain account holding the API token
verify_ssl: true             # default; tls_skip_verify forces off
site: dc1                    # optional; restrict the sync to one site slug
max_devices: 500             # optional; per-sync device cap (default 500)
role_map:                    # optional; NetBox role slug -> ic node type
  core-router: network_device
```

### Observability Fields

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Type (see below) |
| `name` | string | Display name (default: empty) |
| `url` | string | URL (default: empty) |
| `credential_hint` | string | Credential reference (optional) |
| `notes` | string | Free-form notes (optional) |
| `source` | string | Source config name for multiple sources of same type (optional) |
| `instance` | string | Prometheus instance label; also the SNMP device host and Redfish BMC base URL (optional) |
| `selector` | string | Loki LogQL selector (optional) |
| `host_name` | string | CheckMK host name (optional) |
| `monit_port` | int | Monit HTTP port for SSH mode, default 2812 (optional) |
| `monit_url` | string | Direct Monit HTTP URL (optional) |
| `tls_skip_verify` | bool | Disable TLS verification for a https `monit_url`, default false (optional) |

### Observability Types

**Generic:** `metrics`, `logs`, `events`, `traces`, `dashboard`, `health`

**Monitoring systems (for `ic query`):** `prometheus`, `loki`, `checkmk`,
`snmp`, `redfish` (the last two added in ic 0.4.0; both use `instance` for the
device host / BMC URL, and `ic query status` includes them only when the node
declares the entry — there is no slug fallback for a device that may be
unreachable)

---

## Relationships

Relationships are stored in `relationships.yaml` within each project directory.

```yaml
version: "2.0"
relationships:
  - source: "vm:web-01"
    target: "vm:db-01"
    type: depends_on
    description: "PostgreSQL database connection"
```

### Relationship Fields

| Field | Type | Description |
|-------|------|-------------|
| `source` | string | Source node ID |
| `target` | string | Target node ID |
| `type` | RelationshipType | Relationship type (see below) |
| `description` | string | Optional description |
| `managed_by` | string | Source that manages this relationship (null = user-defined) |
| `attributes` | dict | Flexible key-value store |

### Relationship Types

| Type | Description |
|------|-------------|
| `depends_on` | Source requires target to function |
| `uses` | Source uses services provided by target |
| `runs_on` | Source executes on target infrastructure |
| `hosted_by` | Source is hosted on target |
| `member_of` | Source belongs to target group/cluster |
| `contains` | Source contains/manages target |
| `connects_to` | Source has network connection to target |
| `fronted_by` | Source is fronted/proxied by target |
| `resolves_to` | Source DNS resolves to target |
| `routes_to` | Source routes traffic to target |
| `uses_storage` | Source uses storage from target |
| `mounts` | Source mounts filesystem from target |
| `reads_from` | Source reads data from target |
| `writes_to` | Source writes data to target |
| `replicates_to` | Source replicates data to target |
| `located_in` | Source is physically contained by target: child → container (added in ic 0.4.0) |
| `powered_by` | Source draws power from target: consumer → supplier (added in ic 0.4.0) |
| `manages` | Source is an out-of-band controller for target: controller → host (added in ic 0.4.0) |

Relationships are constrained by node types. Use `ic describe relationship wizard` to see valid combinations interactively.

---

## Physical Layer

The physical/datacenter layer (added in ic 0.4.0) models the facility and power
substrate that compute sits on. It is intentionally documentation-only: node
types, relationship types, and the two attribute conventions below are all
free-form data the model accepts, not behavior the tooling enforces.

**Rack position and power cabling are human-curated — never auto-discovered.**
Importers and syncs populate compute and network topology; where a host sits in
a rack and which PDU/UPS feeds it are facts an operator records by hand (or from
a DCIM export). No `ic` source plugin writes these.

### Node types

| Type | Description |
|------|-------------|
| `site` | Datacenter, building, or colocation facility |
| `rack` | Equipment rack within a site |
| `pdu` | Power distribution unit |
| `ups` | Uninterruptible power supply |

None of these support SSH triage: they are absent from the compute set, so
`ic doctor` never warns about a missing `ssh_alias` or observability config on
them.

### Relationship semantics and direction

All three edges are directed from the dependent to the thing it depends on, so
graph traversal from a host reaches its rack, site, and power sources.

| Type | Direction | Example | Constrained pairs |
|------|-----------|---------|-------------------|
| `located_in` | child → container | `physical_host:h1 → rack:r1` | `{physical_host, network_device, pdu, ups} → rack`; `{physical_host, network_device, pdu, ups, rack} → site` |
| `powered_by` | consumer → supplier | `physical_host:h1 → pdu:pdu1` | `{physical_host, network_device} → pdu`; `pdu → ups`; `pdu → pdu` (daisy chain); `ups → site` (building feed) |
| `manages` | controller → host | `network_device:bmc-h1 → physical_host:h1` | `network_device → physical_host` (a BMC/iLO is modeled as a `network_device`) |

`contains` is accepted as the **reverse direction** of `located_in`
(`rack → {physical_host, network_device, pdu, ups}`, `site → rack`) so
containment reads naturally either way. Both directions are legal and
`ic doctor` treats each independently — record whichever direction your queries
prefer, or both.

A `ups` sitting in a datacenter is both `located_in` and `powered_by` that
`site` (the building feed), so that pair permits both edge types.

The constraint matrix is kept deliberately minimal; if you hit a legitimate
pairing it lacks, `ic doctor`'s constraint warning already points you at
`RELATIONSHIP_CONSTRAINTS` in `models/relationship.py` to extend it.

### Hardware attributes convention (`attributes.hardware`)

Physical asset metadata lives under a `hardware` namespace in a node's free-form
`attributes` dict — source-fillable, all keys optional:

```yaml
attributes:
  hardware:
    manufacturer: "Dell"
    model: "PowerEdge R750"
    serial: "ABC123"
    asset_tag: "DC1-0042"
    u_height: 2                # rack units occupied
    rack_position: 14          # lowest U the device occupies
    rack_face: front           # front | rear
    firmware: "2.10.2"
    is_full_depth: true
```

The namespace is open — any key is accepted. Beyond the keys above, the shipped
importers also fill: `part_number`, `airflow`, `weight`, `weight_unit`,
`subdevice_role` (from [`ic import devicetype`](USAGE.md#device-types-netbox-devicetype-library)),
and `uuid`, `board_serial`, `chassis_type` (from the `/ic-collect` hardware
phase). All are optional; every importer merges fill-only, so a hand-set value
is never overwritten.

### Port cabling convention (`connects_to` attributes)

Port-level cabling is documented on a `connects_to` edge's free-form
`attributes` dict, naming the endpoint on each side:

```yaml
relationships:
  - source: "physical_host:h1"
    target: "network_device:sw1"
    type: connects_to
    attributes:
      local_port: "eno1"       # port on the source
      remote_port: "Gi1/0/14"  # port on the target
```

---

## Chains (`chains.yaml`)

A chain describes a request path as *one ordered entry* instead of N pairwise
relationships (added in ic 0.3.0). Chains are stored in `chains.yaml`, a
sibling of `relationships.yaml` in each project directory:

```yaml
version: "2.0"
chains:
  - name: web-request-path
    description: "Customer HTTP traffic"
    type: routes_to                  # optional, default routes_to
    members:                         # ordered, at least 2
      - vm:lb-01                     # plain string member
      - id: vm:app-01                # or mapping with per-member context
        via: "HTTPS 443, sticky sessions"
      - id: "@fleet:vm:db-01"        # @-qualified refs are allowed
        via: "pgbouncer 6432"
```

### Chain Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Slug-like (lowercase letters, digits, hyphens), unique per project |
| `description` | string | Optional description, copied onto each expanded edge |
| `type` | RelationshipType | Edge type for each consecutive pair (default `routes_to`) |
| `members` | list | Ordered node refs, at least 2. Each entry is a plain string ref or `{id, via}` |

### Member Fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Node ref: `type:slug` or `@scope:type:slug` (cross-project / external root) |
| `via` | string | Optional free text: how traffic reaches *this* hop (port, protocol, path). Lands on the edge *into* the member — the first member has no inbound edge, so a `via` there never appears in the expanded graph (`ic doctor` warns) |

### Expansion

Chains are expanded at load time into consecutive-pair relationships — the
example above becomes `vm:lb-01 --routes_to--> vm:app-01` and
`vm:app-01 --routes_to--> @fleet:vm:db-01`. Every consumer (`ic graph`,
`ic ctx`, `ic doctor`, `ic graph render`) sees these ordinary pairwise edges;
each carries the chain name and 0-based hop position in its `attributes`
(`chain`, `chain_position`) and a human-readable description including `via`.

`ic doctor` warns about duplicate chain names, dangling member refs, and
consecutive pairs that violate the relationship constraint matrix.

CLI:

```bash
ic describe relationship chain add web-request-path \
    --member vm:lb-01 --member vm:app-01 --member vm:db-01 [--type routes_to]
ic describe relationship chain list
```

**Compatibility**: chains live in their own file *on purpose*. Older ic
versions reject unknown fields in `relationships.yaml` and skip the entire
file on validation errors — embedding chains there would make all existing
edges vanish for teammates on older versions. Older versions simply never
read `chains.yaml`.

---

## Run Records (`.infracontext/runs/`)

Every source sync appends one small YAML run record (added in ic 0.3.0); the
newest 20 per (project, source) pair are kept, older ones pruned. Filenames
are `<compact-timestamp>-<source>.yaml` (e.g.
`20260716T141725Z-proxmox-prod.yaml`), so a directory listing sorts
chronologically.

```yaml
timestamp: "2026-07-16T14:17:25Z"    # UTC, ISO 8601
ic_version: "0.3.0"                  # infracontext version that ran the sync
source: proxmox-prod                 # source name
project: prod                        # project the sync ran against
status: success                      # success | partial | failed
created: []                          # node IDs created by this run
updated:                             # node IDs whose YAML changed
  - vm:web-01
confirmed_unchanged:                 # reported by the source, no YAML change
  - vm:db-01
```

Records are informational history: the node lists describe what the source
*reported*, even when the sync guard prevented node writes. Only successful,
non-empty runs advance the presence classification behind `ic doctor`'s
staleness warnings — a failed, partial, or empty sync is recorded but
ignored, so a broken source can never declare the fleet gone (see
[Source Tracking](#source-tracking-optional)).

---

## Environment Config (`.infracontext/config.yaml`)

```yaml
active_project: prod                # Default project for unqualified commands

external_roots:                     # Optional: federate other infracontext repos
  - alias: fleet                    # Required: lowercase identifier, used in @-refs
    path: ../infra-fleet            # Required: path to env root (~ expansion allowed)
    mode: read-only                 # Optional: read-only (default) | read-write
    description: Shared hypervisors # Optional: free-form
```

| Field          | Type                  | Required | Description                                                  |
| -------------- | --------------------- | -------- | ------------------------------------------------------------ |
| active_project | string \| null        | no       | Default project slug for `ic` commands when `-p` is omitted. |
| external_roots | list[ExternalRoot]    | no       | Other infracontext repos federated into this view.           |

### ExternalRoot

| Field       | Type            | Required | Description                                                                 |
| ----------- | --------------- | -------- | --------------------------------------------------------------------------- |
| alias       | string          | yes      | Lowercase identifier (`[a-z][a-z0-9_-]*`). Used in `@alias:...` references. |
| path        | string          | yes      | Path to the env root (the dir containing `.infracontext/`). May be `~`-expanded or relative to the local env root. |
| mode        | enum            | no       | `read-only` (default) or `read-write`. Read-only refuses writes.            |
| description | string          | no       | Human-readable note about what the root contains.                           |

Cross-root references use the `@scope:type:slug` syntax shared with
cross-project refs. Scope resolves first as an external root alias, then as a
local project slug. `ic doctor` flags collisions.

## Local Overrides

Machine-specific settings are stored in `.infracontext.local.yaml` (gitignored), not in the node YAML.

```yaml
# .infracontext.local.yaml
nodes:
  "vm:web-server":
    ssh_alias: my-web-alias     # Override SSH alias for this machine
    source_paths:               # Local paths to related code
      - "/home/user/projects/webapp"
```

Only `ssh_alias` and `source_paths` can be overridden. See [USAGE.md](USAGE.md#local-overrides) for details.

---

## Triage Configuration

Minimal hints for Claude during triage. The agent discovers logs, commands, and check methods itself.

```yaml
triage:
  services:           # Services that matter on this node
    - nginx
    - php-fpm
    - postgresql
  context: |          # Free-form hints for troubleshooting
    This server handles 10k req/s at peak.
    If CPU high, check PHP-FPM pool status first.
    Redis cache runs on the same host.
  tier: 3             # Override project default (optional)
  collector_script: /opt/custom/collect.sh  # Override collector path (optional)
```

| Field | Type | Description |
|-------|------|-------------|
| `services` | list[string] | Service names to check (agent figures out how) |
| `context` | string | Free-form troubleshooting hints |
| `tier` | int (0-4) | Override project default tier (optional) |
| `collector_script` | string | Override collector script path (optional) |

---

## Learnings

Discovered knowledge that accumulates over time from triage sessions.

```yaml
learnings:
  - date: "2024-01-15"        # ISO date
    context: "high CPU investigation"  # What was being investigated
    finding: "PHP-FPM pool was set to static with too many workers"
    source: agent             # "agent" or "human"
  - date: "2024-01-20"
    context: "disk space issue"
    finding: "Laravel log rotation not working, check /etc/logrotate.d/laravel"
    source: human
```

Claude can add learnings with:

```bash
ic describe node learning vm:web-server "Finding description" --context "Investigation context"
```

---

## Validation

The schema uses `extra: "forbid"`, so typos are caught rather than silently
ignored. Unknown fields are not fatal, though: the read path strips them with
a warning (recursively — nested models, lists, and dicts included) and
preserves the stripped values, so a read → edit → write cycle never deletes
fields written by a newer infracontext. Unknown node/relationship *type*
values round-trip verbatim the same way. `ic doctor` reports unknown fields
and unknown enum variants as warnings; every other validation failure is an
error.

Use the doctor command to validate all files:

```bash
ic doctor

# JSON output for CI/CD
ic doctor --json
```

Doctor checks:
- YAML syntax errors (including `.infracontext/config.yaml` and `.infracontext.local.yaml`)
- Config schema violations (bad keys in `config.yaml`, reported without a traceback)
- Schema violations (unknown fields, wrong types, invalid slug, id ≠ `type:slug`)
- Node id vs. on-disk `type/slug` path mismatches
- Local override errors (invalid fields, relative paths in `.infracontext.local.yaml`)
- Missing recommended info (compute nodes without `ssh_alias`)
- Orphaned relationships (references to non-existent nodes)
- Duplicate relationships
- Relationship type constraints: the create-time matrix re-validated over
  hand-edited YAML (warning)
- Chains: duplicate names, dangling member refs, unknown edge types, and
  constraint violations on the expanded pairs (warning)
- Duplicate `ssh_alias` / IP addresses within a project (warning); the same
  alias across projects (info)
- Compute/service nodes not grouped under any application (info)
- Blank learnings — whitespace-only context or finding (info)
- Source-managed nodes absent from recent successful syncs, derived from
  [run records](#run-records-infracontextruns) (warning)
