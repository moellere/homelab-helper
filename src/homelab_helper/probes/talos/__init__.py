"""Talos probes — inventory for Talos Linux nodes via the machine API.

Talos has no SSH, so these probes use the :class:`~homelab_helper.adapters.
talos.TalosAdapter` instead of ``kernel-ssh``. They deliberately emit the same
dotted observation keys as the ``host.*`` SSH probes (``host.storage.devices``,
``host.network.interfaces``, ``host.cpu.*``, …) so the reconciler, assertion
engine, and audit roll-up consume Talos nodes with zero changes.
"""
