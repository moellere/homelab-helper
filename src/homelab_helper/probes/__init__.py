"""Probe plugin layer — plugin-from-day-one per ``architecture.md``.

Probes register at framework startup via Python entry points
(``homelab_helper.probes``). The framework's own probes use the same path —
no internal/external distinction.

Probe categories shipping with Phase 1:

- ``network.*`` — cold scans (subnet-scan, fingerprint).
- ``host.*`` — warm probes over SSH (identity, cpu, memory.dmidecode,
  storage, network, pci, gpu, services).

The probe base class and registration mechanism live in :mod:`.base`.
"""
