# homelab-helper — Slice 1 schema

## Scope

This is the harness's own database for the discovery/inventory MVP. NetBox is in-the-loop and owns inventory facts (Devices, Interfaces, IPs, VLANs, Prefixes, Clusters, VMs, Services). This DB owns everything NetBox doesn't model well:

- Part-level identity that survives moves (DIMMs, SSDs, NICs)
- Discovery state (when ran, what was observed, by which probe at which privilege)
- Operational intent (the "expected stopped is healthy" pattern, generalized)
- Configuration assertions and their verification history
- Reconciliation findings (the audit output)
- Proposal log (what the framework suggested, what the user did)

Deferred to later slices, deliberately not here: workload profiles, service endpoints, storage volumes, network paths, action manifests, audit log for L2 actions, trust gradient, LLM router state, skill profile, project memory.

L1 stance: the framework reads, proposes, never applies. Manifest schema is L2-compatible (so we don't repaint later) but no executor wires through it in Slice 1. L2 (execution) is a committed phase — Phase 6, gated by the trust gradient; its tables are forward-specified at the end of this doc so the L1 `ProposalLog` shape stays compatible with what `decide()` will consume.

## Cross-cutting conventions

- **ID strategy**: UUIDv7 primary keys everywhere. Sortable, no PG sequences, agents can mint IDs offline. Helper: `def uuid7() -> uuid.UUID`.
- **Timestamps**: every persisted time is timezone-aware UTC. Helper: `def now() -> datetime` returns `datetime.now(UTC)`.
- **Soft delete**: not used. Append-only history tables where lineage matters (Placement, Observation, AssertionRun, ProposalLog); status fields elsewhere.
- **JSON columns**: used for capability bags and observation values where the schema is genuinely open. Indexed via GIN in Postgres, untyped in SQLite for dev.
- **NetBox links**: foreign keys to NetBox are integer columns (`netbox_*_id`) — no cross-database FK, just IDs. The NetBox adapter mediates.
- **Polymorphic targets**: where a row points to "some target in NetBox or harness" (OperationalIntent, ReconciliationFinding evidence), use `(target_type, target_id)` pairs with a discriminator enum. No SQLAlchemy polymorphic inheritance — the discriminator is queried, not joined.

```python
from __future__ import annotations
import uuid
from datetime import datetime, UTC
from enum import Enum
from typing import Any, Optional
from sqlalchemy import (
    String, DateTime, JSON, ForeignKey, Index, Text,
    Integer, Boolean, BigInteger, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass

def uuid7() -> uuid.UUID:
    """Placeholder; use uuid6-py or roll your own per RFC 9562."""
    ...

def now() -> datetime:
    return datetime.now(UTC)
```

## Controlled vocabularies (enums)

```python
class PartKind(str, Enum):
    DIMM = "dimm"
    SSD = "ssd"
    HDD = "hdd"
    NVME = "nvme"
    NIC = "nic"
    PSU = "psu"
    GPU = "gpu"
    CPU = "cpu"
    OTHER = "other"

class Architecture(str, Enum):
    AMD64 = "amd64"
    ARM64 = "arm64"
    ARM = "arm"
    OTHER = "other"

class PowerPolicy(str, Enum):
    ALWAYS_ON = "always-on"
    WOL_ON_DEMAND = "wol-on-demand"
    MANUAL = "manual"

class PowerState(str, Enum):
    ON = "on"
    OFF = "off"
    EITHER = "either"
    UNKNOWN = "unknown"

class DiscoverySource(str, Enum):
    MANUAL = "manual"
    UNIFI = "unifi"
    PROXMOX = "proxmox"
    K8S = "k8s"
    KERNEL_PROBE = "kernel-probe"
    CLOUDFLARE = "cloudflare"
    NETBOX = "netbox"          # imported from existing NetBox state

class Confidence(str, Enum):
    VERIFIED = "verified"      # authoritative source, fresh
    INFERRED = "inferred"      # derived from other facts
    ASSERTED = "asserted"      # human-claimed, not verified
    STALE = "stale"            # was verified, past freshness window

class PrivilegeLevel(str, Enum):
    NONE = "none"              # network probes only, no creds
    USER = "user"              # SSH as unprivileged user
    SUDO = "sudo"              # SSH with sudo
    ROOT = "root"              # SSH as root
    API = "api"                # service API (proxmox, k8s, unifi, cloudflare)

class IntentTargetType(str, Enum):
    HOST = "host"
    VM = "vm"
    LXC = "lxc"
    SERVICE = "service"
    CIRCUIT = "circuit"

class IntentState(str, Enum):
    RUNNING = "running"
    STOPPED_BY_DESIGN = "stopped-by-design"
    DECOMMISSIONING = "decommissioning"
    REALIZED = "realized"          # for circuits / endpoints — "actually exists"
    STRAY = "stray"                # configured but never realized (the WAN2 case)
    UNKNOWN = "unknown"

class AssertionScope(str, Enum):
    HOST = "host"
    CLUSTER = "cluster"
    SITE = "site"
    GLOBAL = "global"

class AssertionKind(str, Enum):
    SSH_COMMAND = "ssh-command"
    HTTP_CHECK = "http-check"
    API_QUERY = "api-query"
    OBSERVATION_PREDICATE = "observation-predicate"
    FILE_HASH = "file-hash"

class AssertionStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"

class FindingKind(str, Enum):
    CONFIG_DRIFT = "config-drift"
    CONFIG_DRIFT_UNKNOWN = "config-drift-unknown"
    OUTAGE_STALE = "outage-stale"
    STRAY_CONFIG = "stray-config"
    CHOKEPOINT = "chokepoint"
    INVENTORY_GAP = "inventory-gap"
    CEPH_BOTTLENECK = "ceph-bottleneck"
    LEGACY_WORKLOAD = "legacy-workload"
    UNKNOWN_WORKLOAD = "unknown-workload"
    STORAGE_PROVENANCE_DELTA = "storage-provenance-delta"
    DISCOVERY_AGENTLESS_NEEDED = "discovery-agentless-needed"
    DRIFT_CANDIDATE = "drift-candidate"
    PROVENANCE_PARTIAL = "provenance-partial"
    INTRA_SITE_LINK_MANUAL = "intra-site-link-manual"
    OTHER = "other"

class FindingSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

class FindingStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"      # user said "stop showing me this"

class ProposalOutcome(str, Enum):
    PENDING = "pending"
    USER_ACCEPTED = "user-accepted"
    USER_REJECTED = "user-rejected"
    USER_DEFERRED = "user-deferred"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
```

## Models

### Host

The harness-side projection of a NetBox Device, holding harness-only fields that don't belong in NetBox's data model. NetBox is canonical for inventory facts (model, location, primary_ip, status); this table is authoritative for capabilities, discovery state, and power policy.

```python
class Host(Base):
    __tablename__ = "host"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    netbox_device_id: Mapped[Optional[int]] = mapped_column(unique=True, index=True)

    hostname: Mapped[str] = mapped_column(String(255), index=True)
    primary_ip: Mapped[Optional[str]] = mapped_column(String(45))
    arch: Mapped[Architecture] = mapped_column(default=Architecture.OTHER)

    power_policy: Mapped[PowerPolicy] = mapped_column(default=PowerPolicy.ALWAYS_ON)
    expected_power_state: Mapped[PowerState] = mapped_column(default=PowerState.ON)

    discovery_source: Mapped[DiscoverySource] = mapped_column(default=DiscoverySource.MANUAL)
    discovery_last_run: Mapped[Optional[datetime]] = mapped_column(index=True)
    last_verified: Mapped[Optional[datetime]]

    # Capability bag: cpu_flags, iommu, ecc, nvenc codecs, max_supported_ram, etc.
    # Schema is open during MVP; promote frequently-queried keys to typed columns later.
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # Opaque reference into the secrets store; never store credentials here.
    credentials_ref: Mapped[Optional[str]] = mapped_column(String(255))

    notes: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    placements: Mapped[list["Placement"]] = relationship(back_populates="host")
    discovery_runs: Mapped[list["DiscoveryRun"]] = relationship(back_populates="host")
```

Design notes:
- `netbox_device_id` is optional because a Host can exist in the harness DB *before* it's been pushed to NetBox (during first-contact discovery), then linked once NetBox is updated.
- `capabilities` JSON keys we expect to see early: `cpu_model`, `cpu_flags`, `cpu_cores`, `cpu_threads`, `mem_total_bytes`, `mem_max_supported_bytes`, `iommu`, `virtualization`, `nvenc_codecs`, `pcie_slots_free`, `nic_speeds_mbps`.
- `last_verified` is operator-facing — "when did a human last confirm this is accurate." `discovery_last_run` is system-facing — "when did a probe last touch this."

### PhysicalPart

First-class identity for parts that survive moves. Serial-keyed where possible; ID-only where not.

```python
class PhysicalPart(Base):
    __tablename__ = "physical_part"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    kind: Mapped[PartKind] = mapped_column(index=True)

    manufacturer: Mapped[Optional[str]] = mapped_column(String(255))
    model: Mapped[Optional[str]] = mapped_column(String(255))
    serial: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    wwid: Mapped[Optional[str]] = mapped_column(String(255), index=True)  # storage

    capacity_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    speed_mts: Mapped[Optional[int]] = mapped_column(Integer)  # DIMM MT/s
    speed_mbps: Mapped[Optional[int]] = mapped_column(Integer)  # storage / NIC

    # Open-ended part attributes: ECC, rank, NVMe gen, encoder generation, etc.
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    notes: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    placements: Mapped[list["Placement"]] = relationship(back_populates="part")

    __table_args__ = (
        # When serial is present, it's globally unique within a kind.
        # Nullable serials mean we can't enforce DB-side; reconciler handles dupe-merge.
        Index("ix_part_kind_serial", "kind", "serial"),
        Index("ix_part_kind_wwid", "kind", "wwid"),
    )
```

Design notes:
- Generic USB SSDs (your cp1/2/3 case) often have garbage serials and reused WWIDs. The reconciler needs a "best effort identity merge" pass that prefers serial → wwid → model+capacity+first-seen-host. Accepting some ambiguity is correct.
- `attributes` JSON keys we expect early: `ecc`, `rank`, `dimm_form_factor`, `nvme_generation`, `tdp_watts`, `pcie_lanes`.

### Placement

Append-only history of where each part has lived. Current placement = row with `to_date IS NULL`.

```python
class Placement(Base):
    __tablename__ = "placement"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    part_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("physical_part.id"), index=True)
    host_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("host.id"), index=True)

    slot: Mapped[str] = mapped_column(String(128))
    # Slot examples: "DIMM_A1", "/dev/nvme0n1", "USB-port-3", "md0:member-2"

    from_date: Mapped[datetime] = mapped_column(default=now)
    to_date: Mapped[Optional[datetime]] = mapped_column(index=True)

    confidence: Mapped[Confidence] = mapped_column(default=Confidence.VERIFIED)
    source: Mapped[DiscoverySource] = mapped_column(default=DiscoverySource.KERNEL_PROBE)
    discovery_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("discovery_run.id"), index=True
    )
    notes: Mapped[Optional[str]] = mapped_column(Text)

    part: Mapped["PhysicalPart"] = relationship(back_populates="placements")
    host: Mapped["Host"] = relationship(back_populates="placements")

    __table_args__ = (
        Index("ix_placement_current", "part_id", "to_date"),  # to_date IS NULL filter
        Index("ix_placement_host_current", "host_id", "to_date"),
    )
```

Design notes:
- "Closing out" a placement is a write to `to_date`, never a row delete. Lineage chains are reconstructible by walking `part_id` rows ordered by `from_date`.
- The reconciler opens a new placement only when the *slot* changes or the *host* changes. Re-discovering the same DIMM in the same slot is a `last_verified` bump on the part, not a new placement.
- `confidence=ASSERTED` for operator-supplied historical placements (your node4→node1 DIMM moves). `confidence=VERIFIED` for placements written by a fresh kernel probe.

### OperationalIntent

Generic across hosts, VMs, LXCs, services, and circuits. The "ask once, remember forever" answers live here.

```python
class OperationalIntent(Base):
    __tablename__ = "operational_intent"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)

    target_type: Mapped[IntentTargetType] = mapped_column(index=True)
    # For HOST: target_id = Host.id (UUID as string)
    # For VM/LXC: target_id = "<host_hostname>:<vmid>" or "<host_id>:<vmid>"
    # For SERVICE: target_id = NetBox Service ID as string
    # For CIRCUIT: target_id = NetBox Circuit ID as string
    target_id: Mapped[str] = mapped_column(String(255), index=True)

    intent: Mapped[IntentState] = mapped_column(default=IntentState.UNKNOWN)
    declared_at: Mapped[datetime] = mapped_column(default=now)
    declared_by: Mapped[str] = mapped_column(String(255))  # user identifier
    rationale: Mapped[Optional[str]] = mapped_column(Text)

    # When intent should be re-asked. Null = never auto-expire.
    review_at: Mapped[Optional[datetime]]

    __table_args__ = (
        UniqueConstraint("target_type", "target_id", name="uq_intent_per_target"),
        Index("ix_intent_target", "target_type", "target_id"),
    )
```

Design notes:
- One current intent per target. Updating intent overwrites; if you want history, query `ProposalLog` for "intent change" proposals.
- The reconciler uses this to suppress findings: if `target_type=vm, target_id=node2:104, intent=stopped-by-design`, then "VM 104 is stopped" produces no finding.
- The `STRAY` state is the WAN2 case — "this was never real, ignore it forever."
- `review_at` lets you decay intent for things that shouldn't be permanent ("ask me again in 90 days if openclaw is still stopped").

### Probe

Plugin registry. Each probe declares what observation keys it produces.

```python
class Probe(Base):
    __tablename__ = "probe"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    # "kernel.cpu", "kernel.memory.dmidecode", "network.subnet-scan",
    # "proxmox.cluster", "unifi.controller"

    version: Mapped[str] = mapped_column(String(32))
    module_path: Mapped[str] = mapped_column(String(512))
    # e.g. "homelab_helper.probes.kernel.cpu:CPUProbe"

    schema_version: Mapped[int] = mapped_column(default=1)
    # Bumps when output structure changes; reconciler dispatches by version

    required_privilege: Mapped[PrivilegeLevel] = mapped_column(default=PrivilegeLevel.USER)

    # Observation keys this probe can produce, for capability matching.
    # e.g. ["host.cpu.model", "host.cpu.flags", "host.cpu.cores"]
    produces_keys: Mapped[list[str]] = mapped_column(JSON, default=list)

    enabled: Mapped[bool] = mapped_column(default=True)
    description: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)
```

Design notes:
- Probes register themselves on framework startup. The DB row is the source of "what's available right now"; the module on disk is the source of behavior.
- A probe's `name` is its stable identifier across versions. `version` is the implementation version (semver). `schema_version` bumps only when the *output shape* changes.
- The plugin SDK design (a separate artifact) defines the `Probe` base class, the registration mechanism, and the output contract.

### DiscoveryRun

Every probe execution.

```python
class DiscoveryRun(Base):
    __tablename__ = "discovery_run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)

    host_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("host.id"), index=True)
    # Null for network-wide scans (cold discovery against a CIDR).
    target_spec: Mapped[Optional[str]] = mapped_column(String(255))
    # For network-wide: "10.0.6.0/23". For host-scoped: same as host.primary_ip.

    probe_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("probe.id"), index=True)
    probe_name: Mapped[str] = mapped_column(String(255), index=True)
    probe_version: Mapped[str] = mapped_column(String(32))

    privilege_level: Mapped[PrivilegeLevel]

    started_at: Mapped[datetime] = mapped_column(default=now, index=True)
    ended_at: Mapped[Optional[datetime]]
    success: Mapped[Optional[bool]]
    error: Mapped[Optional[str]] = mapped_column(Text)

    # How many observations this run produced.
    observation_count: Mapped[int] = mapped_column(default=0)

    triggered_by: Mapped[str] = mapped_column(String(64), default="scheduled")
    # "scheduled" | "manual" | "agent" | "first-contact" | "reconciler"

    host: Mapped[Optional["Host"]] = relationship(back_populates="discovery_runs")
    observations: Mapped[list["Observation"]] = relationship(back_populates="run")
```

### Observation

Atomic facts produced by probes. Append-only.

```python
class Observation(Base):
    __tablename__ = "observation"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("discovery_run.id"), index=True)

    # Dotted key path: "host.cpu.model", "host.memory.dimms",
    # "host.storage.devices[0].smart.healthy", "network.subnet.live_hosts"
    key: Mapped[str] = mapped_column(String(255), index=True)

    # Value as JSON to accept anything: scalar, list, dict.
    value: Mapped[Any] = mapped_column(JSON)

    confidence: Mapped[Confidence] = mapped_column(default=Confidence.VERIFIED)
    recorded_at: Mapped[datetime] = mapped_column(default=now, index=True)

    # Target this observation describes (may differ from run.host_id for network scans).
    target_type: Mapped[Optional[IntentTargetType]] = mapped_column(index=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(255), index=True)

    run: Mapped["DiscoveryRun"] = relationship(back_populates="observations")

    __table_args__ = (
        # Time-series query: "latest observations of key K for target T"
        Index("ix_obs_target_key_time", "target_type", "target_id", "key", "recorded_at"),
        Index("ix_obs_key_time", "key", "recorded_at"),
    )
```

Design notes:
- Observations are not the inventory itself — they're evidence. The reconciler reads observations and updates Host capabilities, Placements, and NetBox.
- Time-series storage of every observation enables "what changed between Tuesday and now" queries cheaply.
- Retention: in MVP, keep all observations. Add a configurable retention policy later (e.g. "raw observations 90 days, hourly rollups 1 year"). Cheap to defer.

### ConfigurationAssertion

The things we know should be true. Your 16 supplied assertions are the seed corpus.

```python
class ConfigurationAssertion(Base):
    __tablename__ = "configuration_assertion"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    # "nas0.br6.mac_pinned", "node-fleet.e1000e-offload-disabled"

    scope: Mapped[AssertionScope]
    scope_target: Mapped[Optional[str]] = mapped_column(String(255))
    # For host scope: hostname. For cluster: cluster name. Null for global.

    description: Mapped[str] = mapped_column(Text)
    rationale: Mapped[Optional[str]] = mapped_column(Text)

    kind: Mapped[AssertionKind]
    # Verifier specification. Structure depends on kind.
    # ssh-command: {"command": "...", "expected_exit": 0, "expected_stdout_regex": "..."}
    # http-check: {"url": "...", "expected_status": 200, "expected_body_regex": "..."}
    # api-query: {"adapter": "proxmox", "query": {...}, "expected": {...}}
    # observation-predicate: {"key": "host.network.br6.mac", "predicate": "eq",
    #                        "compare_to_key": "host.network.lan6.mac"}
    # file-hash: {"path": "/etc/systemd/system/e1000e-offload-fix.service",
    #             "expected_sha256": "..."}
    verifier_spec: Mapped[dict[str, Any]] = mapped_column(JSON)

    severity_on_fail: Mapped[FindingSeverity] = mapped_column(default=FindingSeverity.MEDIUM)

    # When to re-check. Cron expression or interval.
    schedule: Mapped[Optional[str]] = mapped_column(String(64))

    artifact_link: Mapped[Optional[str]] = mapped_column(String(512))
    # Pointer to Ansible role, git path, runbook URL

    enabled: Mapped[bool] = mapped_column(default=True)

    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    runs: Mapped[list["AssertionRun"]] = relationship(back_populates="assertion")
```

### AssertionRun

History of verifier executions. Append-only.

```python
class AssertionRun(Base):
    __tablename__ = "assertion_run"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    assertion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("configuration_assertion.id"), index=True
    )

    ran_at: Mapped[datetime] = mapped_column(default=now, index=True)
    status: Mapped[AssertionStatus]
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Verifier output: stdout, exit code, observed value, etc.
    error: Mapped[Optional[str]] = mapped_column(Text)

    # If this run produced a finding, link it.
    finding_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("reconciliation_finding.id"), index=True
    )

    assertion: Mapped["ConfigurationAssertion"] = relationship(back_populates="runs")

    __table_args__ = (
        Index("ix_assertion_run_latest", "assertion_id", "ran_at"),
    )
```

### ReconciliationFinding

The audit output. Findings persist across runs; the same finding doesn't re-create on every reconciliation pass.

```python
class ReconciliationFinding(Base):
    __tablename__ = "reconciliation_finding"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)

    kind: Mapped[FindingKind] = mapped_column(index=True)
    severity: Mapped[FindingSeverity] = mapped_column(index=True)

    # Stable identity: same kind + same target + same root cause = same finding.
    # Used to dedupe "the e1000e fix is still missing on node0" across reconciler runs.
    fingerprint: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(Text)

    # Pointers to the evidence that produced this finding.
    # List of refs: [{"type": "observation", "id": "..."}, {"type": "assertion_run", "id": "..."}]
    evidence_refs: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)

    # Affected resources for blast-radius display.
    # [{"target_type": "host", "target_id": "node0"}, ...]
    affected: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)

    # Suggested fixes. Structured so the UI can render them; L1 means we show but don't run.
    proposed_actions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    status: Mapped[FindingStatus] = mapped_column(default=FindingStatus.OPEN, index=True)
    first_seen: Mapped[datetime] = mapped_column(default=now, index=True)
    last_seen: Mapped[datetime] = mapped_column(default=now, index=True)

    acknowledged_at: Mapped[Optional[datetime]]
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(255))
    resolved_at: Mapped[Optional[datetime]]
    suppressed_until: Mapped[Optional[datetime]]
    notes: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        Index("ix_finding_open", "status", "severity", "last_seen"),
    )
```

Design notes:
- `fingerprint` is the dedup key. For "node0 e1000e missing," the fingerprint is something like `"config-drift:node0:e1000e-offload-disabled"`. The reconciler computes it deterministically.
- Re-running reconciliation updates `last_seen`, refreshes `evidence_refs`, and bumps `description` if it changed. Doesn't create a new row.
- `resolved` is automatic: if the underlying condition stops being true (assertion now passes, drift now matches), the reconciler sets `status=RESOLVED`.
- `suppressed` is operator-set: "stop showing me this until $date" or "stop showing me this ever." Suppression respects fingerprint.

### ProposalLog

L1's accountability record: every proposed change, what the user did with it. Becomes the audit log if/when L2 lands.

```python
class ProposalLog(Base):
    __tablename__ = "proposal_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)

    proposed_at: Mapped[datetime] = mapped_column(default=now, index=True)
    proposed_by: Mapped[str] = mapped_column(String(64), default="reconciler")
    # "reconciler" | "agent:planner" | "agent:incident-triage" | etc.

    # What this proposal is about.
    finding_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("reconciliation_finding.id"), index=True
    )
    assertion_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("configuration_assertion.id"), index=True
    )

    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[Optional[str]] = mapped_column(Text)

    # The artifact being proposed. L2-ready shape; today nothing executes it.
    # {"kind": "ansible-playbook", "content": "...", "rollback": "..."}
    # {"kind": "shell-command", "command": "...", "rollback_command": "..."}
    # {"kind": "netbox-change", "object_type": "device", "patch": {...}}
    artifact: Mapped[dict[str, Any]] = mapped_column(JSON)

    affected: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    blast_radius: Mapped[str] = mapped_column(String(64), default="unknown")
    # "single-host" | "cluster" | "site" | "everything" | "metadata-only"

    outcome: Mapped[ProposalOutcome] = mapped_column(default=ProposalOutcome.PENDING)
    outcome_at: Mapped[Optional[datetime]]
    outcome_by: Mapped[Optional[str]] = mapped_column(String(255))
    outcome_notes: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        Index("ix_proposal_pending", "outcome", "proposed_at"),
    )
```

Design notes:
- This is the table that grows the fastest in operation. Plan for retention (probably "keep forever" while it's small; archive after a year once it's not).
- The `artifact` JSON is shaped exactly like a future L2 action manifest would be — kind, content, rollback strategy, blast radius. L2 = wiring up an executor that reads pending proposals and applies them after policy check. Today the executor doesn't exist; the structure is identical regardless.
- Tracks both "agent suggested it" (proposed_by=agent:planner) and "reconciler surfaced it" (proposed_by=reconciler). Same shape.

## Indexes worth calling out explicitly

Beyond the per-table indexes above:

- **Current placements**: filter `placement.to_date IS NULL` is hot. Postgres partial index on `(part_id) WHERE to_date IS NULL` cuts current-state queries to near-zero cost.
- **Latest observation per key per target**: `observation(target_type, target_id, key, recorded_at DESC)` supports "what's the most recent value of X for Y." Used constantly.
- **Latest assertion run per assertion**: `assertion_run(assertion_id, ran_at DESC)`. Used by every "is this assertion currently passing" query.
- **Open findings**: `reconciliation_finding(status, severity, last_seen DESC) WHERE status = 'open'`. Drives the audit dashboard.

## NetBox sync invariants

The reconciler is the only thing that writes to NetBox. Rules:

- **Devices**: every `Host` row has a NetBox Device. Reconciler creates the Device on first observation; never deletes (decommissioned hosts get `status=offline` and stay).
- **Custom fields on Device** are written from `Host`:
  - `cf_power_policy` ← `host.power_policy`
  - `cf_expected_power_state` ← `host.expected_power_state`
  - `cf_discovery_source` ← `host.discovery_source`
  - `cf_discovery_last_run` ← `host.discovery_last_run`
  - `cf_last_verified` ← `host.last_verified`
  - `cf_capabilities` ← `host.capabilities` (JSON dump)
  - `cf_arch` ← `host.arch`
- **InventoryItems** mirror *current* Placements (those with `to_date IS NULL`). When a placement closes out, the corresponding InventoryItem is deleted from NetBox; when one opens, a new InventoryItem is created. NetBox never sees placement history; the harness DB owns lineage.
- **VirtualMachines** sync from Proxmox/K8s adapters (Slice 2+); MVP discovery does not create VMs in NetBox.
- **Services** are not written by Slice 1 — that's a later slice when the workload model is fleshed out.
- **Conflicts**: if NetBox is hand-edited and a sync would overwrite, the reconciler logs a finding (`kind=netbox-divergence`) and *skips the write*. Operator resolves manually. No silent overwrites.

## What's deliberately NOT in this schema

For visibility, the things you'll see references to in design docs but not here:

- `WorkloadIntent`, `WorkloadProfile` — planner slice
- `ServiceEndpoint` — DNS adapter slice
- `StorageVolume`, `StorageBacking` — storage adapter slice
- `NetworkPath` — planner slice
- `ActionManifest`, `AuditLog` — L2 (Phase 6); the `ProposalLog.artifact` already carries the manifest shape
- Trust gradient tables (`Domain`, `CellTrust`, `TrustBoundary`, `ElevationWindow`, `TrustHistory`) — L2 (Phase 6); forward-specified below under "Trust gradient tables"
- LLM router state (`LLMRequest`, `ModelInvocation`) — agent slice
- Skill profile, project memory — agent slice

Each of these has a placeholder in design but no MVP code path. Worth noting because the day-one reconciler will produce *some* findings that the full system would handle automatically; in MVP they're text in `finding.description` and links in `finding.proposed_actions`.

## Trust gradient tables (L2 slice — forward spec)

Not in Slice 1. Specified here because the design is settled and the shapes constrain the L1 `ProposalLog`: `blast_radius`, `affected`, and `artifact.rollback` are exactly the inputs the gradient consumes. The gradient is the deterministic authorization model that gates execution when L2 (Phase 6) lands — see `architecture.md`, "Trust gradient (L2 authorization model)". The decision function `decide(action, context) → AutonomyLevel` is pure Python and never calls an LLM.

```python
class AutonomyLevel(str, Enum):
    BLOCK = "block"            # forbidden by policy
    PROPOSE = "propose"        # L1 default — log it, a human applies by hand
    CONFIRM = "confirm"        # execute after explicit per-action approval
    AUTONOMOUS = "autonomous"  # auto-execute, emit receipt, can auto-rollback

class TrustDomain(str, Enum):
    INVENTORY_METADATA = "inventory-metadata"
    CONTAINERS = "containers"
    DNS = "dns"
    NETWORK_FABRIC = "network-fabric"
    STORAGE = "storage"
    HYPERVISOR = "hypervisor"        # vm / lxc lifecycle
    HOST_OS = "host-os"
    SECRETS = "secrets"              # absolute by default — see Domain.is_absolute


# Domain — taxonomy + policy defaults. Seeded; edited at config level, rarely.
class Domain(Base):
    __tablename__ = "trust_domain"

    name: Mapped[TrustDomain] = mapped_column(primary_key=True)
    default_level: Mapped[AutonomyLevel] = mapped_column(default=AutonomyLevel.PROPOSE)
    max_level: Mapped[AutonomyLevel] = mapped_column(default=AutonomyLevel.AUTONOMOUS)
    is_absolute: Mapped[bool] = mapped_column(default=False)
    # is_absolute=True ⇒ no override and no elevation window can cross this
    # domain's floors; only a policy-config edit changes it. SECRETS ships True.


# CellTrust — the moving floor, per (domain × action-kind × blast-radius).
class CellTrust(Base):
    __tablename__ = "cell_trust"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    domain: Mapped[TrustDomain] = mapped_column(index=True)
    action_kind: Mapped[str] = mapped_column(String(64))    # "restart" | "recreate" | "reweight" | ...
    blast_radius: Mapped[str] = mapped_column(String(64))   # mirrors ProposalLog.blast_radius
    level: Mapped[AutonomyLevel] = mapped_column(default=AutonomyLevel.PROPOSE)
    granted_by: Mapped[Optional[str]] = mapped_column(String(255))  # None ⇒ reached by auto-escalation
    clean_streak: Mapped[int] = mapped_column(default=0)
    on_probation: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(default=now)

    __table_args__ = (
        UniqueConstraint("domain", "action_kind", "blast_radius", name="uq_cell"),
    )
    # Auto-escalation raises level by one step ONLY for reversible + low-blast
    # cells (blast_radius in {"metadata-only", "single-host"}) after N clean
    # approvals. One bad outcome resets: level→PROPOSE, clean_streak→0,
    # on_probation→True. Every other cell needs an explicit grant (granted_by set)
    # and never auto-promotes.


# TrustBoundary — per-host ceiling and window-proof marking.
class TrustBoundary(Base):
    __tablename__ = "trust_boundary"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    host_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("host.id"), index=True)
    netbox_device_id: Mapped[Optional[int]] = mapped_column(index=True)
    max_agent_authority: Mapped[AutonomyLevel] = mapped_column(default=AutonomyLevel.CONFIRM)
    absolute: Mapped[bool] = mapped_column(default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # decide() clamps a cell's computed level to max_agent_authority for actions
    # touching this host. absolute=True ⇒ window-proof: no override or window
    # crosses the ceiling ("my production NAS, never, under any circumstances").


# ElevationWindow — time-boxed lift of soft-hard floors, scoped and killable.
class ElevationWindow(Base):
    __tablename__ = "elevation_window"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    opened_by: Mapped[str] = mapped_column(String(255))     # owner identity; never an agent
    reason: Mapped[str] = mapped_column(Text)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON)     # {"domains": [...], "hosts": [...], "cells": [...]}
    opened_at: Mapped[datetime] = mapped_column(default=now, index=True)
    expires_at: Mapped[datetime] = mapped_column(index=True)  # hard expiry; no auto-renew
    revoked_at: Mapped[Optional[datetime]]                    # the kill switch
    revoked_by: Mapped[Optional[str]] = mapped_column(String(255))
    # Within an open, unexpired, unrevoked window whose scope matches an action's
    # cell, the executor may cross the verified-rollback floor and a NON-absolute
    # host ceiling without per-action prompts — but still captures a best-effort
    # snapshot first and tags the receipt with this window id. Never crosses an
    # absolute floor. Outside any window, autonomous-meets-floor degrades to CONFIRM.


# TrustHistory — append-only audit spine. Every authority change of any kind.
class TrustHistory(Base):
    __tablename__ = "trust_history"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    at: Mapped[datetime] = mapped_column(default=now, index=True)
    actor: Mapped[str] = mapped_column(String(255))   # owner identity, or "system:auto-escalation"
    event: Mapped[str] = mapped_column(String(64))
    # "grant" | "auto-promote" | "demote" | "override" | "window-open" | "window-revoke"
    domain: Mapped[Optional[TrustDomain]]
    cell_trust_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("cell_trust.id"))
    window_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("elevation_window.id"))
    proposal_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("proposal_log.id"))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (Index("ix_trust_history_at", "at"),)
```

Design notes:
- **`decide()` is the only gate.** Every adapter `write()` at L2 routes through it; it reads the action's `(domain, action_kind, blast_radius)` cell, the matching `CellTrust.level`, the host's `TrustBoundary`, any open `ElevationWindow`, and the manifest's `artifact.rollback`. No LLM is ever in this path.
- **Three tiers of floor hardness.** The cell default (`CellTrust.level`, moves) sits under the soft-hard floors (verified-rollback requirement + non-absolute `TrustBoundary` ceiling, crossable by override or window) which sit under the absolute floors (`Domain.is_absolute` or `TrustBoundary.absolute`, crossable only by editing policy config).
- **Override is an event, not a table.** A per-action owner override is recorded as a `TrustHistory` row with `event="override"`; the action then proceeds and emits a receipt. Like the elevation window, it is owner-only and interactive — never available to an agent and never self-invoked by autonomous execution.
- **Receipts.** Executed actions write back to `ProposalLog` (outcome, outcome_by, outcome_notes) and, where a snapshot was taken, store its restore handle in the proposal's outcome metadata. `AuditLog`/receipt detail beyond `ProposalLog` + `TrustHistory` is a Phase 6 design call.

## Migration strategy

Alembic. First migration creates everything in this doc. Subsequent migrations are normal Alembic autogenerated from model changes — schema is small enough that hand-review is easy.

Seeding for development: a `fixtures/example.yaml` that loads your real lab as the test fixture. Lets the day-one audit run as an integration test in CI.

## Open questions for the next pass

1. **JSON-column query patterns**: Postgres GIN indexes give us fast `jsonb` query. SQLite gives us nothing. If we want feature parity, we constrain ourselves to "fetch row, parse JSON in app code" rather than DB-side JSON filtering. Probably fine for MVP scale; worth confirming.

2. **Probe output schema validation**: Pydantic models per probe output, or open JSON? Recommend Pydantic — strong typing at the probe→observation boundary catches bugs early. Each probe declares a Pydantic schema in code, the framework validates output, observation rows store the validated dict.

3. **`fingerprint` generation**: the algorithm matters for dedup quality. Recommend `sha256(kind || target_type || target_id || root_cause_token)[:16]` — deterministic, short enough to debug. The `root_cause_token` is per-finding-kind logic.

4. **NetBox custom-field schema migration**: the `cf_*` fields need to exist in NetBox before the reconciler writes them. Either we ship a "first-run NetBox bootstrap" routine that creates them via NetBox's API, or we document them as a setup prerequisite. The bootstrap is friendlier and worth doing.

5. **Bootstrap of `Probe` rows**: registration on first import vs. explicit `helper probes register --all` command. Probably both — auto on first run, manual command for re-registration after changes.

Each is a small decision, none block the schema itself. Worth deciding before writing the first probe.
