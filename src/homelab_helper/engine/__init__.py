"""Engine layer — orchestration that sits between adapters and the API.

Components landing here as Phase 1 progresses:

- ``ProbeRunner`` — dispatches probes to the right adapter, handles timeouts.
- ``Reconciler`` — observation → inventory, NetBox sync, finding generation.
- ``AssertionEngine`` — runs verifiers, emits AssertionRun rows and Findings.
- ``Scheduler`` — APScheduler-backed cron + event triggers (P2-leaning).
- ``ProposalManager`` — accepts proposals, writes ProposalLog rows.
- ``FingerprintGenerator`` — deterministic finding fingerprints (in
  :mod:`.fingerprint`).
"""
