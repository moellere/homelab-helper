# Security

`homelab-helper` holds credentials for your infrastructure and, once you opt
in, can act on it. Please report anything that could leak a credential, bypass
the trust gradient, or make the harness act without an operator's grant
privately rather than in a public issue.

## Reporting

Use GitHub's private vulnerability reporting on this repository (Security →
Report a vulnerability). Include the `helper version` output, the steps, and
what you observed. You'll get an acknowledgement within a few days; fixes ship
as a patch release and are noted in the release.

## What's in scope

- Any path by which an LLM output, an MCP client, or a probed host's content
  can change trust state, execute an action, or reach a host it was not
  scoped to.
- Secret values appearing in logs, tool results, error messages, or the
  harness database.
- Adapter write methods reachable from anywhere but the executor.

## Design notes for reviewers

The authorization gate (`engine/trust.py::decide`) is a pure function and is
never given an LLM; mechanical tests assert the trust and executor modules
cannot import the LLM package, that the MCP server has no authority-changing
tools, and that Proxmox write methods are named only by the executor and the
rollback orchestrator. See `docs/architecture.md`, "Security & trust".
