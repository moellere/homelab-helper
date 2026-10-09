# homelab-helper

A framework that keeps a homelab's inventory honest, audits it against what you
said it should be, recommends what to change — and, only when you have said so
per kind of change, carries the change out through one deterministic gate.

Its stance in one line: **propose, never apply, until you raise a trust cell
yourself.** Every discovery is a read. Every write to the lab goes through a
pure policy function that an LLM is never part of, and leaves a receipt.

## Start here

[Getting started](getting-started.md) takes you from install to a finding
table in a few minutes, with **no hardware, no credentials and no SSH** — a
synthetic lab is bundled so you can see what the harness does before you point
it at anything real. Then it shows you how to point it at your own lab.

## What it does

| | |
|---|---|
| **Inventory** | SSH probes read hosts (CPU, memory, DIMMs, disks by WWN, NICs by MAC, SMART, PCI, GPUs); management-plane adapters read Proxmox, Kubernetes, Talos, UniFi, MikroTik, Cloudflare, Argo CD, OpenMediaVault and Home Assistant. Physical parts keep their identity when they move between hosts. |
| **Audit** | A reconciler turns observations into findings with stable fingerprints: inventory gaps, forged drive identities, DNS split-brain, git-vs-cluster drift, stray config, version currency, backup posture. Findings reopen when the condition recurs and resolve only when the category was actually observed. |
| **Recommendations** | Placement for a new workload, rebalancing across cluster nodes, known bottleneck patterns with generated mitigations, surplus capacity, network-path verdicts, rightsizing from usage history, a weekly digest. All deterministic; an LLM may narrate them, never decide them. |
| **Execution** | Opt-in and per cell: guest power and migration, Kubernetes rollouts, Argo CD syncs, DNS records, a rolling node update. Each runs through the [trust gradient](trust-gradient.md) with a verified rollback where one exists, a receipt always, and a phone tap when the policy says *confirm*. |
| **Interfaces** | A CLI (`helper`), an MCP server for Claude Code / Claude Desktop / Cursor, chat with a local or bring-your-own-key model, and NetBox as the canonical inventory you own. |

## Where to go next

- **Operating it:** the [CLI reference](cli.md), the
  [trust gradient for operators](trust-gradient.md), the
  [status endpoint](status-endpoint.md) for your dashboard, and the
  [live validation runbook](live-validation.md) you run before trusting any of
  it with your own fleet.
- **Extending it:** [writing a probe](writing-a-probe.md) and
  [writing an adapter](writing-an-adapter.md).
- **Understanding it:** the [architecture](architecture.md) records the locked
  decisions; the [roadmap](roadmap.md) and [backlog](backlog.md) say what is
  built, what is validated against real hardware, and what is still owed.

## Status

Beta. Phases 1 to 8 are built and the suite is all mocks; Phases 4 to 7 have
also met one real lab (the runbook's sign-off table says exactly which criteria
have, and which have only ever met a fixture). Phase 9 — this site among them —
is about making it real for someone other than its author.
