"""Adapter layer — uniform interface to external sources of truth.

Per ``architecture.md``: every external source the harness talks to gets an
adapter implementing a common ``Adapter`` protocol with ``health_check``,
``query``, ``write``, and ``supports`` methods.

Phase 1 adapters (implemented in this folder):

- ``NetBoxAdapter`` — read+write Devices, Interfaces, IPs, VLANs, Cables, CFs.
- ``KernelSSHAdapter`` — read-only SSH session manager for host probes.

Phase 3 adapters (Proxmox, K8s, UniFi, Cloudflare, Git/ArgoCD) will land here
alongside the P1 ones once we get there.
"""
