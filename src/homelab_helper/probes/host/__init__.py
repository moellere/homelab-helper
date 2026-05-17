"""First-party host probes (run over SSH against a Linux target).

Each probe is its own module + class. Phase-1 set:

- ``identity`` — hostname, kernel, OS, machine-id, boot time.
- (CPU, memory, storage, network, PCI, GPU, services land here as Task #10+
  progresses.)
"""
