"""Controlled vocabularies for the harness DB (per ``harness-schema-slice1.md``).

Every enum here is a ``str, Enum`` subclass so values serialize cleanly to JSON,
NetBox custom fields, and DB columns. ``SQLAlchemy`` stores these as native
``Enum`` types via ``mapped_column(SAEnum(<Enum>))`` in the model files.

The 16 vocabularies (in spec order) are organized into thematic groups below
for readability; the public API is just "import the enum you need."
"""

from __future__ import annotations

from enum import StrEnum

# ---------------------------------------------------------------------------
# Hardware identity & host shape
# ---------------------------------------------------------------------------


class PartKind(StrEnum):
    """Top-level classification of a physical part."""

    DIMM = "dimm"
    SSD = "ssd"
    HDD = "hdd"
    NVME = "nvme"
    NIC = "nic"
    PSU = "psu"
    GPU = "gpu"
    CPU = "cpu"
    OTHER = "other"


class Architecture(StrEnum):
    """CPU architecture, mirrored on Host and (later) VirtualMachine."""

    AMD64 = "amd64"
    ARM64 = "arm64"
    ARM = "arm"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Power policy & state (the bmax4/5 "expected-off" pattern, generalized)
# ---------------------------------------------------------------------------


class PowerPolicy(StrEnum):
    """How a host is expected to behave power-wise."""

    ALWAYS_ON = "always-on"
    WOL_ON_DEMAND = "wol-on-demand"
    MANUAL = "manual"


class PowerState(StrEnum):
    """The expected power state derived from the policy + current intent."""

    ON = "on"
    OFF = "off"
    EITHER = "either"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Discovery / observation provenance
# ---------------------------------------------------------------------------


class DiscoverySource(StrEnum):
    """Where an observation or inventory fact came from.

    ``NETBOX`` is reserved for facts the harness imported from existing NetBox
    state (e.g. on first run) before any probe has touched the target.
    """

    MANUAL = "manual"
    UNIFI = "unifi"
    PROXMOX = "proxmox"
    K8S = "k8s"
    KERNEL_PROBE = "kernel-probe"
    CLOUDFLARE = "cloudflare"
    NETBOX = "netbox"


class Confidence(StrEnum):
    """Trust level for an observation or placement.

    Kernel probes write ``VERIFIED``; the reconciler may write ``INFERRED``
    for things derived from other observations; operator-supplied history is
    ``ASSERTED``; anything past its freshness window degrades to ``STALE``.
    """

    VERIFIED = "verified"
    INFERRED = "inferred"
    ASSERTED = "asserted"
    STALE = "stale"


class PrivilegeLevel(StrEnum):
    """Credential level a probe needs to run.

    ``NONE`` is for cold network probes that don't authenticate. ``API``
    covers service-API probes (Proxmox, K8s, UniFi, Cloudflare) where the
    "privilege" is a scoped API token rather than a Unix account.
    """

    NONE = "none"
    USER = "user"
    SUDO = "sudo"
    ROOT = "root"
    API = "api"


# ---------------------------------------------------------------------------
# Operational intent (the "ask once, remember forever" answers)
# ---------------------------------------------------------------------------


class IntentTargetType(StrEnum):
    """What kind of resource an OperationalIntent points at."""

    HOST = "host"
    VM = "vm"
    LXC = "lxc"
    SERVICE = "service"
    CIRCUIT = "circuit"


class IntentState(StrEnum):
    """Operator-declared state for the target.

    ``STRAY`` is the WAN2 case — "this was never real, ignore it forever."
    ``REALIZED`` is for circuits/endpoints that actually exist on the wire.
    """

    RUNNING = "running"
    STOPPED_BY_DESIGN = "stopped-by-design"
    DECOMMISSIONING = "decommissioning"
    REALIZED = "realized"
    STRAY = "stray"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Configuration assertions (the things we know should be true)
# ---------------------------------------------------------------------------


class AssertionScope(StrEnum):
    """Where the assertion applies."""

    HOST = "host"
    CLUSTER = "cluster"
    SITE = "site"
    GLOBAL = "global"


class AssertionKind(StrEnum):
    """How the verifier checks the assertion.

    Each kind has a corresponding ``verifier_spec`` JSON shape — see the
    AssertionEngine's verifier dispatch for the per-kind contracts.
    """

    SSH_COMMAND = "ssh-command"
    HTTP_CHECK = "http-check"
    API_QUERY = "api-query"
    OBSERVATION_PREDICATE = "observation-predicate"
    FILE_HASH = "file-hash"


class AssertionStatus(StrEnum):
    """Outcome of a single AssertionRun."""

    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"  # verifier itself crashed; result is unknown
    SKIP = "skip"  # preconditions not met (target unreachable, intent=off, etc.)


# ---------------------------------------------------------------------------
# Reconciliation findings (the audit output)
# ---------------------------------------------------------------------------


class FindingKind(StrEnum):
    """Classification of what's wrong (or merely interesting).

    The starter corpus comes from the dorktool day-one audit; new kinds
    accumulate as the reconciler grows.
    """

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


class FindingSeverity(StrEnum):
    """Severity ladder — drives default sort order in the audit dashboard."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingStatus(StrEnum):
    """Lifecycle state of a finding."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"  # "stop showing me this" (with optional until-date)


# ---------------------------------------------------------------------------
# Proposal log (L1's accountability record)
# ---------------------------------------------------------------------------


class ProposalOutcome(StrEnum):
    """Final disposition of a proposal.

    ``SUPERSEDED`` means a later proposal addresses the same root cause.
    ``EXPIRED`` is for time-bounded proposals that aged out without action.
    """

    PENDING = "pending"
    USER_ACCEPTED = "user-accepted"
    USER_REJECTED = "user-rejected"
    USER_DEFERRED = "user-deferred"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


__all__ = [
    "Architecture",
    "AssertionKind",
    "AssertionScope",
    "AssertionStatus",
    "Confidence",
    "DiscoverySource",
    "FindingKind",
    "FindingSeverity",
    "FindingStatus",
    "IntentState",
    "IntentTargetType",
    "PartKind",
    "PowerPolicy",
    "PowerState",
    "PrivilegeLevel",
    "ProposalOutcome",
]
