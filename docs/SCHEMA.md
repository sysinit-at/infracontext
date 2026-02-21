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

### Core Fields (Required)

| Field | Type | Description |
|-------|------|-------------|
| `version` | string | Schema version, always `"2.0"` |
| `id` | string | Stable ID in format `type:slug` |
| `slug` | string | URL-safe identifier (lowercase, hyphens) |
| `type` | NodeType | Node type (see Node Types below) |
| `name` | string | Human-readable display name |

### Source Tracking (Optional)

| Field | Type | Description |
|-------|------|-------------|
| `source_id` | string | External source reference (e.g., `proxmox:cluster1:qemu:100`) |
| `source` | string | Source name that created this node |
| `managed_by` | string | Source that manages this node; `null` = user-defined |

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
| `instance` | string | Prometheus instance label (optional) |
| `selector` | string | Loki LogQL selector (optional) |
| `host_name` | string | CheckMK host name (optional) |
| `monit_port` | int | Monit HTTP port for SSH mode, default 2812 (optional) |
| `monit_url` | string | Direct Monit HTTP URL (optional) |

### Observability Types

**Generic:** `metrics`, `logs`, `events`, `traces`, `dashboard`, `health`

**Monitoring systems (for `ic query`):** `prometheus`, `loki`, `checkmk`

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

Relationships are constrained by node types. Use `ic describe relationship wizard` to see valid combinations interactively.

---

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

The schema uses `extra: "forbid"` - unknown fields will cause validation errors. This ensures typos are caught rather than silently ignored.

Use the doctor command to validate all files:

```bash
ic doctor

# JSON output for CI/CD
ic doctor --json
```

Doctor checks:
- YAML syntax errors
- Schema violations (unknown fields, wrong types)
- Missing recommended info (compute nodes without `ssh_alias`)
- Orphaned relationships (references to non-existent nodes)
- Duplicate relationships
