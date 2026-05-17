# NetBox modeling walkthrough — dorktool.com homelab

## Approach

The exercise: take the dorktool.com lab as you've described it and figure out what gets modeled where. The output is three lists — *what NetBox handles cleanly, what NetBox handles only with custom fields, what doesn't go in NetBox at all* — and from those three lists, the data-model surface of the harness falls out.

Architectural premise (revisited from earlier in the conversation): NetBox is **the source of truth for physical and L2/L3 inventory**. It is **not** the source of truth for K8s state, Ceph state, ArgoCD-managed config, UniFi runtime config, or anything below the filesystem. The harness *coordinates* across those sources via adapters. Every time we hit something that doesn't fit in NetBox below, we're not finding a bug in NetBox — we're finding an adapter the harness needs.

---

## What NetBox models cleanly

### Sites and tenancy

```
Site: Covington
  description: Primary site, all compute, NAS, UniFi gear
  status: active
  facility: home

Site: Wyola
  description: Remote site reached via site-to-site VPN
  status: active
  facility: remote-home

Tenant: dorktool.com   # optional, only one tenant — skip if it adds noise
```

### VLAN Group, VLANs, Prefixes

NetBox wants a `VLANGroup` to scope the VLAN namespace so VID collisions are detected. One group for the controller's L2 domain at Covington.

```
VLANGroup: dorktool-covington
  scope: Site=Covington

VLANs (all in dorktool-covington):
  vid=1   name=Primary    role=mgmt
  vid=4   name=DorkIOT    role=iot
  vid=6   name=Kubernet   role=compute
  vid=8   name=CovGuest   role=guest
  vid=10  name=CovVMs     role=vm-network

Prefixes:
  10.250.0.0/23  VLAN=Primary   role=mgmt           site=Covington
  10.250.4.0/23  VLAN=DorkIOT   role=iot            site=Covington
  10.250.6.0/23  VLAN=Kubernet  role=compute        site=Covington
  10.250.8.0/24  VLAN=CovGuest  role=guest          site=Covington
  10.250.10.0/24 VLAN=CovVMs    role=vm-network     site=Covington
```

VLAN 6 as a contiguous /23 just works — NetBox doesn't care that it spans two `/24`-shaped halves.

### Inter-site VPN (NetBox 4.x Tunnels)

```
TunnelGroup: dorktool-vpn

Tunnel: wyola-site-vpn
  group: dorktool-vpn
  encapsulation: ipsec-transport   # or whatever UniFi actually uses
  status: active
  terminations:
    - Termination(role=hub, device=Covington-UDM, interface=wan1)
    - Termination(role=spoke, site=Wyola, ip=<wyola-public>)
```

WAN2 is a separate `Circuit` whose status reflects the long-running outage — see "where the model bends" for why this matters beyond just a status field.

### Manufacturers and DeviceTypes

```
Manufacturers:
  Beelink, RaspberryPiFoundation, Ubiquiti, MikroTik, Generic-MiniPC

DeviceTypes:
  Beelink_MiniPC_Gen-A    (placeholder — bmax1/2/3 specifics TBD)
  Beelink_S12_i5-8260U    (bmax0)
  Generic_MiniPC_i5-6500T (bmax4/5/6 — likely a specific Beelink/Minisforum SKU)
  Beelink_NAS_Variant     (covomv)
  RaspberryPi_4B          (pi-cp1/2/3, garageupspi)
  UDM_Pro_(UDMA6A8)       (Covington gateway)
  USW_Pro_Max_16_PoE      (dorkcore)
  USW_24_PoE_2.5G         (usw25office)
  USW_Flex_Mini           (uswjordan, uswlivingroom)
  U7_Pro                  (the APs — model varies, may need multiple types)
  Mikrotik_PTP_Radio      (the dark-infrastructure radios)
```

### Device Roles

```
proxmox-node, k8s-control-plane, k8s-worker, k8s-worker-wol,
nas, usb-ip-server, gateway, switch-core, switch-access, ap, ptp-radio
```

`k8s-worker-wol` is its own role specifically to make WoL-aware queries trivial.

### Devices

Eighteen-ish named devices. Listed with the fields that matter for downstream reasoning, not exhaustively:

```
Compute (Site=Covington):
  bmax0     role=proxmox-node           primary_ip=10.250.6.20/23
  bmax1     role=proxmox-node           primary_ip=10.250.6.21/23
  bmax2     role=proxmox-node           primary_ip=10.250.6.22/23
  bmax3     role=proxmox-node           primary_ip=10.250.6.23/23
  bmax4     role=k8s-worker-wol         primary_ip=10.250.6.24/23
  bmax5     role=k8s-worker-wol         primary_ip=10.250.6.25/23
  bmax6     role=k8s-worker             primary_ip=10.250.6.26/23
  pi-cp1    role=k8s-control-plane      primary_ip=10.250.6.17/23
  pi-cp2    role=k8s-control-plane      primary_ip=10.250.6.18/23
  pi-cp3    role=k8s-control-plane      primary_ip=10.250.6.19/23
  garageupspi role=usb-ip-server        primary_ip=10.250.0.53/23
  covomv    role=nas                    primary_ip=10.250.0.29/23   # see interface note

Network (Site=Covington):
  Covington   role=gateway              (UDM-class)
  dorkcore    role=switch-core
  usw25office role=switch-access
  uswjordan   role=switch-access
  uswlivingroom role=switch-access
  u7playroom  role=ap     ← also functional uplink for dorkcore
  u7officehall role=ap
  garage      role=ap
  dorkmesh    role=ap
  upstairs    role=ap

Network (Site=Wyola):
  <mikrotik-ptp-1>  role=ptp-radio    # exact device names tbd
  <mikrotik-ptp-2>  role=ptp-radio
```

### Interfaces on key devices

The bmax/Pi interface set is minimal. covomv is the tricky one:

```
covomv interfaces:
  lan0   type=1000base-t       mac=<realtek-mac>    mode=access(vlan=1)
         ip=10.250.0.29/23
  lan6   type=10gbase-t        mac=<aquantia-mac>   mode=tagged(vlans=[6])
  br6    type=bridge           mac=<aquantia-mac>   # MUST equal lan6.mac
         bridge_members=[lan6]
         mode=access(vlan=6)
         ip=10.250.6.29/23
```

NetBox can express the bridge relationship and the MAC values, but the *invariant* (`br6.mac == lan6.mac`) lives elsewhere — see config assertions.

### Clusters and VMs

```
ClusterType: proxmox-ve
ClusterType: kubernetes

Cluster: dorkprox
  type: proxmox-ve
  site: Covington
  members: bmax0, bmax1, bmax2, bmax3

Cluster: dorkk8s
  type: kubernetes
  site: Covington
  members: pi-cp1, pi-cp2, pi-cp3, bmax4, bmax5, bmax6

VirtualMachines (cluster=dorkprox):
  pbs              # raw-passthrough to sda1 — see bend #5
  <other proxmox VMs you have>

VirtualMachines (cluster=dorkk8s):
  Not modeled per-pod in NetBox. K8s API is SoT.
  Optionally model "logical workloads" as VMs if you want a unified Services view —
  see "what lives outside NetBox" below.
```

### Services

Top-of-mind services attached to whichever Device or VM serves them publicly:

```
Service(name=home-assistant,     device=<ha-vm>,            port=8123/tcp)
Service(name=authentik,          device=<authentik-pod>,    port=9000/tcp)
Service(name=frigate,            device=covomv,             port=5000/tcp)
Service(name=n8n,                device=<n8n-pod>,          port=5678/tcp)
Service(name=grafana,            device=<grafana-pod>,      port=3000/tcp)
Service(name=prometheus,         device=<prom-pod>,         port=9090/tcp)
Service(name=loki,               device=<loki-pod>,         port=3100/tcp)
Service(name=uptime-kuma,        device=<uk-pod>,           port=3001/tcp)
Service(name=minio,              device=covomv,             port=9000/tcp)
Service(name=guacamole,          device=<guac-pod>,         port=8080/tcp)
Service(name=nuclio,             device=<nuclio-pod>,       port=8070/tcp)
Service(name=pbs,                device=pbs-vm,             port=8007/tcp)
Service(name=nfs,                device=covomv,             port=2049/tcp)
Service(name=smb,                device=covomv,             port=445/tcp)
Service(name=tftp-pxe,           device=covomv,             port=69/udp)
```

The K8s-hosted services don't have stable "host" — they live on whatever pod the scheduler picked. Pointing them at a logical `VirtualMachine(name=<service>-workload)` and letting the K8s adapter keep `local_context_data` current is the cleanest pattern. Trying to keep `Service.device` pointing at the actual pod-host is a losing battle.

### Cables — selectively

The one cable that earns its keep:

```
Cable(
  termination_a=dorkcore.uplink,
  termination_b=u7playroom.lan_port,
  label="homelab-chokepoint"
)
```

That edge is load-bearing for the harness's failure-domain reasoning. Everything else (which Pi is in which dorkcore port, which AP is connected to what) is documentation-only — model it if you enjoy that, skip it otherwise. Cable inventory is the single best example of "NetBox accuracy decays to 30% within 60 days" that the surveys keep finding; only model cables you'd actually keep current.

---

## Where the model bends

Ten places, in rough order of architectural significance.

### 1. Parts with history (DIMMs, SSDs that migrated)

NetBox represents sub-components as `InventoryItem` records owned by a `Device`. There is no first-class identity that survives a move. When bmax4's 2×16 GB DIMMs went into bmax1, the NetBox-native operation is *delete from bmax4, create on bmax1*. Lineage gone. Same with the Pi SSDs → md0.

The fix is not a NetBox plugin. It's its own data model in the harness:

```python
class PhysicalPart:
    id: UUID
    kind: Literal["dimm", "ssd", "hdd", "nvme", "nic", "psu", "gpu", ...]
    manufacturer: str
    model: str
    serial: str | None       # canonical identity when present
    capacity: int | None     # bytes
    speed: int | None        # MT/s for DIMM, MB/s for storage
    attributes: dict         # ECC, rank, NVMe gen, etc.
    notes: str

class Placement:
    part: PhysicalPart
    host: Device             # NetBox device ID
    slot: str                # "DIMM_A1", "/dev/nvme0n1", "USB-port-3"
    from_date: datetime
    to_date: datetime | None # null = currently placed
    confidence: Literal["verified", "inferred", "asserted"]
    source: str              # "dmidecode", "user", "import"
```

NetBox holds *current* placements only — as `InventoryItem`s — and the harness keeps the full history. Reconciler updates NetBox when a part appears/disappears.

This model handles your lineage knot natively: the three SSDs in `covomv.md0` have `Placement` rows whose previous entries point at pi-cp1/2/3.

### 2. Expected-power-off (bmax4 and bmax5)

NetBox's `Device.status` has values like `active`, `planned`, `staged`, `decommissioning`, `offline`. None of them mean "intentionally asleep, will wake on demand." Adding a custom status is possible but conflates intent with state.

Cleaner shape:

```
Device.status               → intent: "is this device in service at all"
Device.cf_power_policy      → "always-on" | "wol-on-demand" | "manual"
Device.cf_expected_power_state → derived: "on" | "off" | "either"
Observation                 → actual: probed power state per discovery run
```

The discovery scheduler reads `power_policy` before pinging. The reconciler treats `expected=off, observed=off` as *confirmation*, not absence. The K8s adapter reads node labels and knows that `homelab/always-on=false` corresponds to `power_policy=wol-on-demand`.

### 3. Multi-source labels (K8s)

`homelab/always-on=true` and `homelab/big-pods=true` live in the K8s API. They affect placement (and therefore the planner's reasoning), but they're not NetBox's concern. NetBox should be able to *display* them via the K8s adapter populating `Device.local_context_data`, but the SoT remains the K8s API.

Pattern: any field whose canonical home is somewhere else gets stamped with `cf_source = "k8s" | "proxmox" | "unifi" | "kernel-probe" | "user"`. NetBox is read-mostly for those fields; writes happen at the source.

### 4. MAC-pinned bridge invariant (covomv.br6)

NetBox stores `br6.mac_address` and `lan6.mac_address` as independent fields. Nothing enforces that they match, and the lab's correct operation requires it.

This is a `ConfigurationAssertion`:

```python
ConfigurationAssertion(
  id=...,
  host="covomv",
  description="br6 bridge MAC must equal lan6 NIC MAC",
  rationale="DHCP reservation matches against bridge MAC; mismatch returns wrong IP",
  verifier_kind="ssh",
  verifier_command="ip link show br6 | awk '/link\\/ether/{print $2}' | "
                   "cmp -s - <(ip link show lan6 | awk '/link\\/ether/{print $2}')",
  expected_exit_code=0,
  artifact_link="ansible/roles/covomv-network/templates/br6.j2",
  last_verified=...,
  severity="high"
)
```

The harness runs the verifier on a schedule. If it fails, that's drift, and it goes into the reconciliation findings.

### 5. Raw-passthrough partition (covomv.sda1 → PBS VM)

NetBox can model:
- `covomv.sda` as an `InventoryItem`
- `pbs-vm` as a `VirtualMachine`
- A `VirtualDisk` on `pbs-vm`

What it can't model is "the storage backing that VirtualDisk is *this specific partition on this specific physical disk on this specific host.*"

Two reasonable options:

- *Cheap:* custom field `VirtualDisk.cf_backing_block_device = "covomv:/dev/disk/by-partlabel/proxmox-bs"`. Free-text, the planner parses it.
- *Better:* a harness object `StorageBacking(virtual_disk, host, block_device, mode=raw|qcow2|nfs|...)` that the harness owns and updates from Proxmox API.

The planner needs the second one if it's going to reason about "if I migrate pbs-vm, where does its storage have to go" — which is exactly the kind of question this lab's planner should answer.

### 6. RAID-of-harvested-disks (covomv.md0)

NetBox doesn't model software RAID arrays as relationships between sub-components of a single device. You can shove md0 into an `InventoryItem` with a description and lose the structure, or you can let the harness own it:

```python
StorageVolume(
  host="covomv",
  name="md0",
  kind="mdadm-raid5",
  capacity_bytes=894 * GiB,
  members=[PhysicalPart-id-x3],   # the three 447 GB SSDs
  attributes={"chunk_size": "512K", "layout": "left-symmetric", ...}
)
```

Combined with `PhysicalPart` + `Placement` history, you get the lineage knot for free: query `members[*].placement_history` and you see they used to live in pi-cp1/2/3.

### 7. DNS split-brain

Same hostname can resolve to different IPs depending on whether the resolver is internal (UniFi gateway) or external (Cloudflare → ingress). NetBox `FHRPGroup` isn't this; `IPAddress.dns_name` is single-valued. The Services model attaches to a device but doesn't capture "this service has internal-via-x and external-via-y resolution paths."

Harness owns it:

```python
ServiceEndpoint(
  service: Service,
  resolution_scope: "internal" | "external",
  hostname: str,
  ip: str,
  resolver: "unifi" | "cloudflare" | "consul" | ...,
  tls_provider: "cert-manager-dns01" | "self-signed" | None,
)
```

Each Service has 1..N endpoints. The harness's DNS adapter (UniFi for internal, Cloudflare for external) keeps them current. NetBox's `Service` becomes a thin pointer.

### 8. WAN2 down since March 2026 (no failover)

The static fact is modelable: WAN2 = `Circuit(status=offline)`. The interesting parts aren't:
- *No failover configured* — this is a configuration assertion that *failed silently* (or was never configured). Whether it's a bug or a design choice depends on intent. The harness needs both an assertion of intent ("dual WAN should fail over") *and* a verifier that checks the current UniFi config.
- *Outage has persisted for months* — this is a finding worth surfacing, not just a status field. "WAN2 has been down ≥ 60 days; if this is permanent, decommission the circuit; if not, why hasn't it been fixed."

Generalizes: a static inventory store can't distinguish "this is the current state" from "this is the current state and that's been true for an unusually long time." The harness needs **temporal awareness** of inventory state to surface stale degradation as audit findings.

### 9. Dark infrastructure (Mikrotik PTP)

These devices are real, but no live source confirms them. NetBox holds them. The harness should mark them clearly:

```
Device.cf_discovery_source = "manual"
Device.cf_last_verified    = <date user last touched them>
Device.cf_verification_method = "user-asserted"
```

Audit rule: any device with `discovery_source=manual` and `last_verified > 180 days` gets flagged. Doesn't mean it's broken; means the harness can't vouch for it.

### 10. Topology chokepoint

The dorkcore → u7playroom uplink is a NetBox cable, as noted. The *implication* — every node in VLAN 6 depends on u7playroom for any traffic leaving the homelab subnet — is derived. The harness needs a topology-aware function: "for service S running on device D, enumerate the failure domains it depends on for (a) internal traffic, (b) internet egress, (c) cross-VLAN traffic." That function reads NetBox cables, traces paths, returns dependency sets.

That's downstream of NetBox modeling, but it's worth flagging now because it shapes which cables you model. Model the cables that affect failure-domain queries; skip the rest.

---

## Custom fields to add on day one

Concrete list, scoped per object type. All can be added via NetBox admin without writing a plugin.

**On `Device`:**

```
cf_power_policy            choice: always-on / wol-on-demand / manual
cf_expected_power_state    choice: on / off / either           (derived)
cf_discovery_source        text: manual / unifi / proxmox / k8s / kernel-probe
cf_discovery_last_run      datetime
cf_last_verified           date
cf_capabilities            json: {"avx2":true,"iommu":true,"nvenc":["h264","hevc"],...}
cf_arch                    choice: amd64 / arm64 / arm / other
cf_hypervisor_type         choice: proxmox / esxi / kvm-host / docker-host / bare-metal / talos / none
cf_power_draw_idle_watts   decimal
cf_power_draw_max_watts    decimal
```

**On `VirtualMachine`:**

```
cf_arch                    choice: amd64 / arm64 / arm / other
cf_workload_intent_id      uuid → harness-owned WorkloadIntent
cf_deployment_artifact     text: pointer to ansible role / helm chart / git path
```

**On `Service`:**

```
cf_workload_profile_id     uuid → harness-owned WorkloadProfile
cf_endpoint_internal       text: internal hostname (resolves via UniFi)
cf_endpoint_external       text: external hostname (resolves via Cloudflare)
cf_sso_provider            choice: authentik-forward-auth / oauth / none / unknown
```

**On `Interface`:**

```
cf_pinned_mac_to           text: name of interface whose MAC this must match (for invariants)
cf_pxe_boot_target         boolean: is this interface a PXE-boot target
```

**On `Circuit`:**

```
cf_failover_partner        ref → other Circuit (for dual-WAN pairs)
cf_failover_configured     boolean
cf_outage_since            date
```

**On `Cable`:**

```
cf_chokepoint              boolean: cuts a failure domain — keep accurate
```

JSON custom fields are escape hatches. Use them for capability bags where the schema is still settling; promote frequently-queried fields to discrete typed fields once you know what you actually filter on.

---

## Harness objects (not in NetBox)

These are the ones I'd ship as part of the harness's own data layer, with NetBox as one of several data sources behind them.

```
PhysicalPart           kind, mfr, model, serial, capacity, speed, attributes
Placement              part_id, host_id (→ NetBox), slot, from/to, confidence, source
StorageVolume          host, name, kind, capacity, members[part_id], attributes
StorageBacking         virtual_disk (→ NetBox VM disk), host, block_device, mode
ConfigurationAssertion host_or_scope, description, rationale, verifier, expected, severity
ServiceEndpoint        service (→ NetBox), scope, hostname, ip, resolver, tls
WorkloadIntent         user_request, target_arch_constraints, resource_estimate, status
WorkloadProfile        name, baseline_resources, scaling_shape, arch_support
DiscoveryRun           host, started_at, ended_at, probes[], privilege_level
Observation            run_id, probe, key, value, confidence, raw_payload_ref
ReconciliationFinding  kind, severity, description, evidence[], proposed_action
TrustBoundary          host, max_agent_authority, requires_user_approval[]
```

Most of these we've discussed in prior turns; bundling them here for completeness.

---

## External sources of truth (not in NetBox at all)

| Source | Owns | Adapter responsibility |
| --- | --- | --- |
| K8s API | live pod/node state, labels, taints, NotReady reasons | populate Device.local_context_data; resolve service→host |
| Proxmox API | VM state, Ceph OSD weights, replication health, snapshots | populate VM status; surface Ceph health to harness |
| ArgoCD + Git | declared application state (the 46 apps) | feed planner with "what should be running"; diff vs K8s API for drift |
| UniFi controller | DNS records, DHCP leases, switch port config, VLAN assignments | populate internal DNS endpoints; verify cable terminations |
| Cloudflare API | external DNS, ACME state | populate external endpoints; check cert expiry |
| Kernel probes (SSH) | actual storage layout, RAID state, NIC offloads, SMART | ground truth that beats every management-plane DB (see covomv lesson) |
| ZFS/mdadm/LVM commands | array health, scrub state, resync progress | flowed in via kernel probes |
| `dmidecode` | DIMM slot topology, board, BIOS | the only reliable source for memory layout |

The harness coordinates across these. None of them are individually sufficient. None of them have to be NetBox.

---

## Day-one reconciliation report

What the harness produces from the inventory above on its first full run, without being told anything specific to look for. Each item is a `ReconciliationFinding`:

```
[HIGH] CEPH-BOTTLENECK
  bmax0 has the largest RAM allocation (64 GB) in the Proxmox cluster but
  the lowest NIC speed (1 GbE). All other cluster members have ≥2.5 GbE.
  Ceph replication is bounded by the slowest peer.
  Options:
    (a) Reweight CRUSH map to reduce bmax0's primary-OSD share.
    (b) Add USB 2.5GbE NIC (~$25) dedicated to Ceph backend.
    (c) Move OSD off bmax0; use as compute-only.
    (d) Accept the cap, document it.

[HIGH] CONFIG-DRIFT-UNKNOWN
  e1000e TX-hang workaround deployed on bmax1, bmax2, bmax3 (Intel I219-V).
  bmax0 has the same NIC; workaround status unknown. Verify.

[HIGH] OUTAGE-STALE
  Circuit "WAN2" has been status=offline for ≥60 days (since March 2026).
  No failover from WAN1 to WAN2 is configured. Either:
    (a) Repair/replace WAN2 and configure failover.
    (b) Decommission the Circuit and dual-WAN expectation.
  Current state is "broken in a way nothing alerts on."

[MEDIUM] CHOKEPOINT
  dorkcore (homelab core switch) uplinks via u7playroom (AP). Single edge
  between VLAN 6 and the rest of the network. If u7playroom fails, the
  entire homelab is partitioned. Consider direct dorkcore→gateway uplink.

[MEDIUM] INVENTORY-GAP
  CPU model unknown for bmax1, bmax2, bmax3. Will be filled by next
  credentialed discovery run; flagged for visibility.

[MEDIUM] DISCOVERY-AGENTLESS-NEEDED
  Devices marked discovery_source=manual (Mikrotik PTP radios x2) have
  no live verification path. Recommend periodic operator review or SNMP
  read-only credentials if available.

[LOW] DRIFT-CANDIDATE
  GitOps caveat noted: node labels, CoreDNS/authentik-outpost patches,
  and e1000e systemd units are deliberately not in ArgoCD repo.
  Recommend: capture as ConfigurationAssertions so they're verified
  even though they're not Git-managed.

[LOW] PROVENANCE-PARTIAL
  PhysicalPart placements reconstructed from operator notes:
    - bmax1.dimm_a/b: from bmax4 (verified by operator)
    - bmax2.dimm_a:   from bmax5 (verified by operator)
    - covomv.md0.member[0..2]: from pi-cp1/2/3 (verified by operator)
  Earlier history unknown. Treat pre-current placement as low-confidence.
```

That's eight findings without an LLM in the loop. Add the LLM and you get readable explanations and "do you want me to file these as tickets / open PRs / write a runbook entry" follow-ups.

---

## Known unknowns to resolve before completing the model

Things the user-supplied data didn't pin down. Each gets filled by either discovery or a quick operator answer:

1. **bmax1/2/3 CPU models** — first SSH probe resolves.
2. **bmax0 e1000e workaround status** — first SSH probe resolves; this is a HIGH finding until verified.
3. **WAN2 outage status** — operator-only: is it being fixed, or should the harness recommend decommissioning?
4. **pi-cp1/2/3 replacement USB SSD models/capacities** — discovery fills.
5. **Specific AP models** (U6 Pro / U7 Pro / mesh variants) — UniFi adapter fills.
6. **Mikrotik PTP radio model + firmware** — operator entry; no live source.
7. **Are pi-cp1/2/3 Pi 4B 4GB or 8GB?** — listed as 4GB but worth confirming via probe.
8. **Proxmox VM list beyond PBS** — Proxmox API fills.
9. **Other ConfigurationAssertions in your head** — the 11 in §5 of the inventory are an excellent start, but there are almost certainly more captured only in shell history. A "tell me about this host" conversational extraction agent is one of the highest-value pieces of the eventual UI.

---

## Decision points this walkthrough surfaces

Worth deciding before going further:

1. **Use NetBox in-the-loop, or just use NetBox's model?**
   The walkthrough assumes you actually run NetBox and the harness reads/writes via API. The alternative — model your own SQLAlchemy schema closely inspired by NetBox's, skip the install — is defensible if you'd rather not maintain another Postgres/Redis/worker stack. The custom fields and bend-cases are roughly the same either way; what changes is who owns the canonical store.

2. **Cables: which ones?**
   "Just the chokepoint" is one extreme. "Every cable" is the other. Middle ground: model cables that participate in failure-domain queries (uplinks between switches and to gateways/APs, NICs to switches for hosts whose workloads have HA requirements). Skip patch cables to APs and end-user devices.

3. **K8s pod-level modeling: yes or no?**
   The walkthrough says no — pods are too ephemeral, K8s API is SoT. But a *logical service* layer in NetBox that the K8s adapter keeps in sync gives you a unified Services view. The question is whether you want NetBox to be that surface, or whether the harness's UI is the right place.

4. **Plugin objects: as NetBox plugins or as harness DB?**
   `PhysicalPart`/`Placement` could be a NetBox plugin (writes via NetBox API, follows NetBox's permission model). Cleaner. But it ties you to NetBox for those objects. Harness DB keeps optionality.

Each of those is a real fork in the road. Worth picking before any code gets written.
