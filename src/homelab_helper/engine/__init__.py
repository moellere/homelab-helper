"""Engine layer — orchestration that sits between adapters and the API.

Components landing here as Phase 1 progresses:

- ``ProbeRunner`` — dispatches probes to the right adapter, handles timeouts.
- ``Reconciler`` — observation → inventory (this slice: ``host.identity.*``
  onto Host rows; future slices: parts/placements, NetBox sync, findings).
- ``AssertionEngine`` — runs verifiers, emits AssertionRun rows and Findings.
- ``Scheduler`` — APScheduler-backed cron + event triggers (P2-leaning).
- ``ProposalManager`` — accepts proposals, writes ProposalLog rows.
- ``FingerprintGenerator`` — deterministic finding fingerprints (in
  :mod:`.fingerprint`).
"""

from homelab_helper.engine.fingerprint import make_fingerprint
from homelab_helper.engine.reconciler import (
    HostProjectionRule,
    Reconciler,
    ReconcileResult,
)
from homelab_helper.engine.runner import ProbeRunner

__all__ = [
    "HostProjectionRule",
    "ProbeRunner",
    "ReconcileResult",
    "Reconciler",
    "make_fingerprint",
]
