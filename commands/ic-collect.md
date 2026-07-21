---
description: Collect node information via SSH and create or enrich an infracontext node YAML file.
allowed-tools: Bash, Read, Write, AskUserQuestion
---

# Infracontext Node Collector

You are collecting information about a server to create or enrich a node in infracontext. The node may already exist (e.g. from a Proxmox sync or SSH import) — in that case, you enrich it with SSH-discovered data. Your goal is to gather **hard facts** via SSH, then have a **conversation** with the user about context that can't be discovered automatically.

## Input

The user provides:
- **SSH alias**: An alias from `~/.ssh/config` (required)
- **Project**: Target project name (optional, will ask if not provided)

Examples:
- `/ic-collect web-prod`
- `/ic-collect s.myserver --project prod`
- `/ic-collect jump-host`

---

## Phase 1: SSH Discovery (Automatic)

Connect to the host and gather hard facts. Run these commands via SSH:

```bash
SSH_ALIAS="<user-provided-alias>"

# Test connectivity first
ssh -o ConnectTimeout=10 -o BatchMode=yes "$SSH_ALIAS" 'echo OK' 2>&1
```

If connection fails, stop and inform the user to check their SSH config.

### Gather System Info

Run a single SSH command to collect all info efficiently:

```bash
ssh -o ConnectTimeout=30 "$SSH_ALIAS" '
echo "=== HOSTNAME ==="
hostname -f 2>/dev/null || hostname

echo "=== OS ==="
cat /etc/os-release 2>/dev/null | grep -E "^(PRETTY_NAME|ID|VERSION_ID)=" || uname -a

echo "=== IPS ==="
ip -4 addr show scope global 2>/dev/null | grep inet | awk "{print \$2}" | cut -d/ -f1 || ifconfig 2>/dev/null | grep "inet " | awk "{print \$2}"

echo "=== CPU ==="
nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null
lscpu 2>/dev/null | grep -E "^(Model name|Architecture):" || true

echo "=== MEMORY ==="
free -h 2>/dev/null | head -2 || vm_stat 2>/dev/null

echo "=== DISK ==="
df -h / /home /var 2>/dev/null | tail -n +2

echo "=== SERVICES ==="
systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null | head -30 || service --status-all 2>/dev/null | grep "+" | head -30

echo "=== LISTENING_PORTS ==="
ss -tlnp 2>/dev/null | tail -n +2 | head -20 || netstat -tlnp 2>/dev/null | tail -n +3 | head -20

echo "=== DOCKER ==="
docker ps --format "{{.Names}}: {{.Image}}" 2>/dev/null || podman ps --format "{{.Names}}: {{.Image}}" 2>/dev/null || echo "none"

echo "=== MONITORING_AGENTS ==="
pgrep -la node_exporter 2>/dev/null || echo ""
pgrep -la promtail 2>/dev/null || echo ""
pgrep -la alloy 2>/dev/null || echo ""
systemctl is-active monit 2>/dev/null || echo ""

echo "=== VIRTUALIZATION ==="
systemd-detect-virt 2>/dev/null || echo "unknown"
'
```

### Parse the Output

From the SSH output, extract:

| Field | Source |
|-------|--------|
| `hostname` | HOSTNAME section |
| `os_name` | PRETTY_NAME from OS |
| `os_id` | ID from OS (debian, ubuntu, rhel, etc.) |
| `ip_addresses` | IPS section |
| `cpu_cores` | CPU section (nproc) |
| `memory_mb` | MEMORY section (convert to MB) |
| `services` | SERVICES section (service names only) |
| `listening_ports` | LISTENING_PORTS section |
| `containers` | DOCKER section |
| `monitoring_agents` | MONITORING_AGENTS section |
| `virtualization` | VIRTUALIZATION (vm, lxc, docker, none, etc.) |

### Residue Rule

Every listener observed in the LISTENING_PORTS output (`ss -tlnp`) must be
accounted for in the final node YAML: either attributed to a service in
`triage.services`, or listed under an "Unclaimed Listeners" subsection in the
node's notes (see Phase 4). Nothing observed may be silently dropped — keep
the full listener list until the YAML is written.

### Infer Node Type

Based on virtualization detection:
- `none` or `bare-metal` → `physical_host`
- `kvm`, `qemu`, `vmware`, `microsoft`, `xen` → `vm`
- `lxc`, `lxc-libvirt` → `lxc_container`
- `docker`, `podman` → `oci_container`

---

## Phase 2: Present Discovered Facts

Show the user what was discovered:

```
Discovered from <SSH_ALIAS>:

System:
  Hostname: web-prod.example.com
  OS: Ubuntu 22.04 LTS
  Type: vm (detected: kvm)
  CPU: 4 cores
  Memory: 8192 MB

Network:
  IPs: 10.0.1.50, 192.168.1.50

Services (running):
  - nginx
  - php8.2-fpm
  - postgresql@15-main
  - ssh
  - node_exporter

Containers: none

Listening Ports:
  - 22 (ssh)
  - 80 (nginx)
  - 443 (nginx)
  - 5432 (postgres)
  - 9100 (node_exporter)

Monitoring:
  - node_exporter: running
```

---

## Phase 3: User Conversation

Now ask the user questions to fill in what can't be auto-discovered. Use `AskUserQuestion` for each topic.

### Question 1: Project

Check existing projects first:

```bash
ic describe project list
```

Then ask:

```
Which project should this node belong to?

Options:
1. <existing-project-1>
2. <existing-project-2>
3. Create new project
```

### Question 2: Confirm Node Type and Slug

First, check if a node already exists for this host. Search by hostname, IP, or SSH alias:

```bash
ic describe node find "<hostname>" 2>/dev/null
```

**If a matching node is found** (enrichment mode):

```
Found existing node: vm:web-prod (synced from proxmox-prod)

  Enriching this node with SSH-discovered data.
  Confirm? [Y/n/pick different node]
```

Skip type/slug selection — use the existing node's identity. Proceed to Question 3.

**If no matching node** (creation mode):

Based on detected type and hostname, propose a slug:

```
Proposed node identity:

  Type: vm
  Slug: web-prod (from hostname)
  ID: vm:web-prod

Accept this, or provide alternatives?

Options:
1. Accept as proposed
2. Change type (if detection was wrong)
3. Change slug (provide custom)
```

### Question 3: Triage Services

From the running services, filter to the meaningful ones (exclude sshd, cron, dbus, etc.):

```
Which services should be monitored during triage?

Detected services that look important:
  [x] nginx
  [x] php8.2-fpm
  [x] postgresql@15-main
  [ ] node_exporter

Add/remove? (Enter to accept, or list services)
```

Deselecting a service does not drop its listeners: any listener not owned by a
selected triage service must appear under "Unclaimed Listeners" in the node's
notes (Phase 4).

### Question 4: Triage Context

```
Any troubleshooting hints for this server?

This is free-form context that helps during triage. Examples:
- "High memory is normal for this Redis server (caching)"
- "Check logs at /opt/app/logs/ not /var/log"
- "PHP-FPM slow log useful for CPU issues"

(Enter to skip, or type hints)
```

### Question 5: Description

```
Brief description of this node's purpose?

Example: "Primary web server for customer portal"
```

### Question 6: Relationships (Optional)

```
Does this node have dependencies on other nodes?

Common patterns:
- Web server → Database
- App → Cache
- All VMs → Hypervisor

Enter node IDs this depends on (comma-separated), or skip.
Example: vm:db-master, vm:cache-01
```

---

## Phase 4: Generate or Enrich Node YAML

First check if the node already exists (e.g. created by a source sync like Proxmox):

```bash
ic describe node show <type>:<slug> 2>/dev/null
```

### If node already exists (enrichment mode)

The node was likely created by `ic describe source sync`. In this case:
- **Do NOT call `ic describe node create`** — it will fail.
- Read the existing YAML file directly and **merge** your discovered data into it.
- Preserve all existing fields (especially `source_id`, `source`, `managed_by`, `ip_addresses`, `attributes`).
- **Add/update only the enrichment fields**: `ssh_alias`, `triage`, `observability`, `notes`, `description`, and `attributes.collected_at`/`attributes.os_id`/`attributes.virtualization`/`attributes.cpu_cores`/`attributes.memory_mb`.
- Tell the user: "Node <id> already exists (synced from <source>). Enriching with SSH-discovered data."

### If node does not exist (creation mode)

Create the node:

```bash
ic describe node create --type <type> --name "<name>"
```

Then edit the created file to add all discovered and user-provided information.

### File Location

Nodes are stored at:
```
.infracontext/projects/<project>/nodes/<type>/<slug>.yaml
```

**Federation note:** Newly collected nodes always go into the *local* root
under its active project. External (federated) roots are read-only by
default; if `/ic-collect` is invoked while the discovered host actually
belongs in an external root (e.g. a hypervisor that lives in the fleet
repo), prefer one of:

1. Tell the user the host belongs in `<external-root>` and to run
   `/ic-collect` from that repo instead.
2. Or, if that root is explicitly `mode: read-write` in `external_roots`,
   `cd` to the external root and run the collection there so the node is
   created in its real home.

Do **not** silently create a duplicate of an external-root node in the
local root — `ic doctor` will flag it.

### Enrichment / Creation Fields

Write these fields into the YAML (new node) or merge them in (existing node):

```yaml
# SSH connection — always write
ssh_alias: "<user-provided-ssh-alias>"

# Documentation — write if empty, append if existing
description: "<user-provided-description>"
notes: |
  ## System Info (collected <date>)
  - OS: <os_name>
  - CPU: <cpu_cores> cores
  - Memory: <memory_mb> MB

  ## Running Services
  <list of running services>

  ## Service Evidence
  <one line per triage.services entry, e.g. "nginx — systemd unit running, owns :80/:443">

  ## Listening Ports
  <list of ports>

  ## Unclaimed Listeners
  <listeners from ss -tlnp not owned by any triage.services entry, e.g. ":111 — rpcbind, not selected for triage">

# Observability — merge with existing entries (don't duplicate)
observability:
  - type: metrics
    name: "Node Exporter"
    url: "http://<ip>:9100/metrics"

# Discovered attributes — merge into existing attributes dict
attributes:
  collected_at: "<iso-date>"
  os_id: "<os_id>"
  os_version: "<os_version>"
  cpu_cores: <cpu_cores>
  memory_mb: <memory_mb>
  virtualization: "<virt_type>"

# Triage hints — write if empty
triage:
  services:  # plain list of strings — evidence lines live in notes, not here
    - <service1>
    - <service2>
  context: |
    <user-provided-context>
```

### Listener Accounting and Evidence Lines

Before writing the file, cross-check the notes against the Phase 1 `ss -tlnp`
output:

- **Evidence lines**: for every entry in `triage.services`, write one line
  under "Service Evidence" in notes stating why it made the list and which
  ports it owns, e.g. `nginx — systemd unit running, owns :80/:443`.
  `triage.services` itself stays a plain list of strings — no schema change.
- **Residue rule**: every observed listener is either owned by a
  `triage.services` entry (named in its evidence line) or listed under
  "Unclaimed Listeners" with whatever the process column showed, e.g.
  `:111 — rpcbind, not selected for triage`. Nothing observed may be silently
  dropped. Omit the "Unclaimed Listeners" subsection only when every listener
  is claimed.

For **new** nodes, also write the full identity block:

```yaml
version: "2.0"
id: "<type>:<slug>"
slug: <slug>
type: <type>
name: "<Name>"
first_seen: "<today, YYYY-MM-DD>"  # write-once: set at creation, never update it
ip_addresses:
  - "<ip1>"
  - "<ip2>"
```

`first_seen` is write-once: set it only when creating a node. When enriching
an existing node, never add, change, or remove `first_seen`.

### Record the Run

After writing the node YAML, append a run record documenting this collection
in the shared run history (`.infracontext/runs/`). This is informational
provenance only: `ic doctor` presence checks group nodes by their `managed_by`
source and consult only that source's records, and ic-collect never sets
`managed_by` — so these records are never used for presence classification.
Write a new file
`.infracontext/runs/<UTC-timestamp>-ic-collect.yaml` (timestamp format
`YYYYMMDDTHHMMSSZ`, e.g. `20260716T101530Z`) containing:

```yaml
timestamp: "<UTC ISO timestamp, e.g. 2026-07-16T10:15:30Z>"
ic_version: ""
source: ic-collect
project: <target project>
status: success
created:              # node IDs this run created (empty list if none)
  - <type>:<slug>
updated:              # node IDs this run enriched (empty list if none)
confirmed_unchanged: []
```

The skill name (`ic-collect`) is the source; list only the node IDs you
actually touched. Never modify or delete existing run records.

---

## Phase 5: Hardware Collection (physical_host only)

This phase captures the *physical* substrate — chassis identity, out-of-band
controller, and switch cabling — that only exists on bare metal. It runs **only
when the confirmed node type is `physical_host`**.

**Skip it entirely for VMs and containers.** A guest has no chassis, no BMC, and
no LLDP peers of its own, so `dmidecode`/`ipmitool`/`lldpctl` return virtualized
noise or nothing. Gate on virtualization: the Phase 1 `VIRTUALIZATION` line
(`systemd-detect-virt`) must be `none`. If you are unsure, re-check it —
`ssh "$SSH_ALIAS" 'systemd-detect-virt'` — and if it reports anything other than
`none` (`kvm`, `qemu`, `vmware`, `lxc`, `docker`, …), skip straight to Phase 6.

Everything here is **enrichment** of the host node written in Phase 4 plus a few
small `network_device` companions — never a rewrite. Follow the same fill-only
discipline: write a discovered value only where the human left the slot empty.

### Gather hardware facts (SSH)

Every command below is **optional and degrades gracefully**: a missing tool
(`command not found`) or a denied privilege (`permission denied`) means "not
collected", never a failed phase. Most of these need root, so the block probes
passwordless sudo the same way the connectivity checker does (`sudo -n true`)
and prefixes privileged tools with `sudo -n`; if sudo would prompt for a
password, `BatchMode` can't answer it and the command simply degrades to
"unavailable".

```bash
ssh -o ConnectTimeout=30 "$SSH_ALIAS" '
# Bare-metal guard — bail out on any virtualized guest.
VIRT=$(systemd-detect-virt 2>/dev/null || echo unknown)
echo "=== VIRT ==="
echo "$VIRT"
[ "$VIRT" = "none" ] || { echo "SKIP: $VIRT (no physical hardware to collect)"; exit 0; }

# Privilege: prefer passwordless sudo, otherwise run bare and let it degrade.
if [ "$(id -u)" = "0" ]; then SUDO=""; elif sudo -n true 2>/dev/null; then SUDO="sudo -n"; else SUDO=""; fi

echo "=== DMI_SYSTEM_CHASSIS ==="
$SUDO dmidecode -t system -t chassis 2>/dev/null || echo "unavailable (needs root/sudo, or dmidecode missing)"

echo "=== BMC_LAN ==="
$SUDO ipmitool lan print 2>/dev/null || echo "unavailable (no BMC/ipmitool, or permission denied)"

echo "=== BMC_FRU ==="
$SUDO ipmitool fru print 2>/dev/null || echo "unavailable"

echo "=== LLDP ==="
lldpctl -f keyvalue 2>/dev/null || lldpctl 2>/dev/null || echo "unavailable (lldpd not running)"

echo "=== NIC_PERM_MACS ==="
for nic in $(ls /sys/class/net 2>/dev/null | grep -vE "^(lo|docker|veth|br-|virbr|tap|tun)"); do
  mac=$($SUDO ethtool -P "$nic" 2>/dev/null | awk "{print \$NF}")
  [ -n "$mac" ] && echo "$nic $mac"
done
'
```

### Parse into hardware facts

| Output section | Extract | Destination |
|----------------|---------|-------------|
| `DMI_SYSTEM_CHASSIS` — system: Manufacturer | manufacturer | `attributes.hardware.manufacturer` |
| `DMI_SYSTEM_CHASSIS` — system: Product Name | model | `attributes.hardware.model` |
| `DMI_SYSTEM_CHASSIS` — system: Serial Number | serial | `attributes.hardware.serial` |
| `DMI_SYSTEM_CHASSIS` — system: UUID | uuid | `attributes.hardware.uuid` |
| `DMI_SYSTEM_CHASSIS` — chassis: Type | chassis_type (optional) | `attributes.hardware.chassis_type` |
| `BMC_FRU` — Board Serial / Product Serial | board_serial | `attributes.hardware.board_serial` |
| `BMC_LAN` — IP Address | BMC IP | → BMC `network_device` (below) |
| `LLDP` — per-NIC chassis.name + port.ifname/descr | switch + port | → `connects_to` edges (below) |
| `NIC_PERM_MACS` — `<nic> <mac>` | permanent MAC per NIC | notes; correlates LLDP local ports |

The permanent MACs are not one of the `hardware` keys — LLDP keys neighbours by
interface *name*, and the permanent MAC survives NIC renames, so keep the
`<nic> → MAC` map to disambiguate `local_port` and record it in notes rather
than inventing a schema field.

### Write `attributes.hardware` (fill-only)

Merge the extracted values into the host node's `attributes.hardware` dict — the
`hardware` namespace convention (added in ic 0.4.0, see SCHEMA.md "Physical
Layer"). All keys are optional and free-form.

**Fill-only, never overwrite human values.** Manufacturer, model, serial,
`asset_tag`, and rack position are frequently hand-entered from a DCIM export;
write a key only where it is currently absent or empty. If a discovered value
*conflicts* with an existing human value, keep the human value and record the
discovered one under "Unclaimed neighbors" (below) — do not clobber.

```yaml
attributes:
  hardware:
    manufacturer: "Dell"          # only if not already set
    model: "PowerEdge R750"
    serial: "ABC123"
    uuid: "4c4c4544-0042-..."
    board_serial: "CN7016..."     # from ipmitool fru
```

### LLDP neighbours → `connects_to` edges (user-confirmed)

For each local NIC that LLDP reports a remote switch on, propose a
`connects_to` edge from the host to that switch, carrying the port endpoints in
its `attributes` (the `connects_to` cabling convention, SCHEMA.md "Physical
Layer"). Present them and let the user confirm, exactly like the Phase 3
service selection:

```
LLDP neighbours discovered on <host>:
  eno1 -> switch "sw-core-01"  remote port Gi1/0/14
  eno2 -> switch "sw-core-01"  remote port Gi1/0/15

Create these connections? (deselect any you don't want)
  [x] eno1 -> network_device:sw-core-01  (Gi1/0/14)   [will create node sw-core-01]
  [x] eno2 -> network_device:sw-core-01  (Gi1/0/15)
```

For each confirmed neighbour:

1. **Ensure the switch node exists.** If no `network_device` matches the LLDP
   `sysname` (`ic describe node find "<sysname>"`), create a minimal one, slug
   derived from the sysname:

   ```bash
   ic describe node create --type network_device --name "sw-core-01" --slug sw-core-01
   ```

2. **Create the edge**, then add the port endpoints. `ic describe relationship
   create` does not set edge `attributes`, so create the edge and then edit
   `relationships.yaml` to attach `local_port`/`remote_port`:

   ```bash
   ic describe relationship create \
     --source physical_host:<slug> --target network_device:sw-core-01 --type connects_to
   ```

   ```yaml
   # .infracontext/projects/<project>/relationships.yaml
   - source: "physical_host:<slug>"
     target: "network_device:sw-core-01"
     type: connects_to
     attributes:
       local_port: "eno1"        # the NIC on this host
       remote_port: "Gi1/0/14"   # LLDP port.ifname / port.descr
   ```

### BMC → `network_device` + `manages` edge (user-confirmed)

If `ipmitool lan print` reports a BMC IP address, propose an out-of-band
controller node plus the `manages` edge (controller → host, added in ic 0.4.0).
Confirm with the user first, then:

```bash
ic describe node create --type network_device --name "<host> BMC" --slug <slug>-bmc --ip <bmc-ip>
ic describe relationship create \
  --source network_device:<slug>-bmc --target physical_host:<slug> --type manages
```

Fold any FRU board/product serials into the BMC node's
`attributes.hardware.board_serial` (fill-only), same as the host.

### Unclaimed neighbours (residue discipline)

The Phase 4 listener residue rule has a hardware analogue: **every LLDP
neighbour and BMC endpoint observed must be accounted for.** Anything the user
did *not* confirm into a `connects_to` or `manages` edge — a neighbour they
deselected, a switch they declined to create, a BMC they skipped, or a
discovered value that conflicted with a human-set `attributes.hardware` field —
lands verbatim under an **"Unclaimed neighbours"** subsection in the host node's
`notes`. Nothing observed is silently dropped.

```
## Unclaimed neighbours
- eno3 -> switch "sw-mgmt-02" port Gi0/2 (LLDP; not confirmed as connects_to)
- BMC 10.0.9.5 (ipmitool lan print; not confirmed as network_device)
- dmidecode serial "ABC123" differs from human-set hardware.serial "DC1-0042" (kept human value)
```

Omit the subsection only when every neighbour and endpoint is claimed.

### Record the physical companions

Extend the Phase 4 run record so it reflects this phase: add any
`network_device` nodes you created (switches, BMC) to its `created` list, and
list the host under `updated` — it gained `attributes.hardware`. Write the run
record after hardware collection completes so the provenance is complete.

---

## Phase 6: Confirm and Offer Next Steps

After writing the file:

For **new** nodes:
```
Created node: <type>:<slug>
File: .infracontext/projects/<project>/nodes/<type>/<slug>.yaml

Next steps:
1. Review and edit: ic describe node edit <type>:<slug>
2. Add relationships: ic describe relationship wizard
3. Test triage: /ic-triage <type>:<slug>
```

For **enriched** nodes (already existed from sync):
```
Enriched node: <type>:<slug>
File: .infracontext/projects/<project>/nodes/<type>/<slug>.yaml
Added: ssh_alias, triage config, observability, system notes

Next steps:
1. Review: ic describe node show <type>:<slug>
2. Test triage: /ic-triage <type>:<slug>
3. Enrich next node: /ic-collect <next-ssh-alias>
```

---

## Error Handling

### SSH Connection Failed

```
Could not connect to '<ssh_alias>'.

Check:
1. SSH alias exists in ~/.ssh/config
2. Host is reachable: ssh <alias> 'echo test'
3. Key authentication is configured

Once fixed, try again: /ic-collect <alias>
```

### Node Already Exists (Not an Error)

If the node already exists, this is the **enrichment** path — see Phase 4. This commonly happens when:
- A source sync (Proxmox, SSH import) created the node skeleton
- A previous ic-collect run was interrupted

Proceed with enrichment. The only case to warn the user is if the existing node already has `ssh_alias` and `triage` populated — ask if they want to overwrite.

---

## Example Session

```
User: /ic-collect s.webapp

Claude: Connecting to s.webapp...
[SSH discovery happens]

Discovered from s.webapp:

System:
  Hostname: webapp.prod.example.com
  OS: Debian 12 (Bookworm)
  Type: vm (detected: kvm)
  CPU: 2 cores
  Memory: 4096 MB

Network:
  IPs: 10.8.0.25

Services:
  - nginx
  - gunicorn
  - postgresql@15-main
  - redis-server
  - ssh

Listening Ports:
  - 22, 80, 443, 5432, 6379, 8000

Monitoring:
  - node_exporter: running

---

Which project? [dev / staging / prod / new]
User: prod

Proposed: vm:webapp - Accept? [Y/n/change]
User: Y

Services to monitor: nginx, gunicorn, postgresql@15-main, redis-server
[Accept/modify]
User: accept

Troubleshooting hints?
User: Python app - check gunicorn logs at /var/log/gunicorn/

Description?
User: Main API backend for mobile apps

Dependencies?
User: skip

---

Created: vm:webapp
File: .infracontext/projects/prod/nodes/vm/webapp.yaml

Node is ready for triage: /ic-triage vm:webapp
```

Now execute the collection based on the user's input.
