# homelab-helper

> An OSS framework for homelab planning, inventory, audit, and (eventually) recommendations.

**Status:** Pre-alpha, Phase 1 in progress. Not yet usable.

`homelab-helper` is a framework for people who run their own infrastructure at home.
It discovers what you have, maintains a coherent inventory across the many sources
of truth a real homelab spans (NetBox, Proxmox, Kubernetes, UniFi, Cloudflare,
kernel probes), surfaces drift and gaps as auditable findings, and proposes
changes. Phases 1–5 are strictly "propose, never apply" (L1) — and remain a
complete product for anyone who never opts in. Phase 6 (L2) adds execution,
but only behind the **trust gradient**: a deterministic, operator-controlled
authorization model that the framework can never escalate on its own.

See [`architecture.md`](./docs/architecture.md) for the architecture document,
[`roadmap.md`](./docs/roadmap.md) for the phased delivery plan, and
[`backlog.md`](./docs/backlog.md) for the concrete task list of what's left.

## Phase 1 — Inventory & Discovery

The current MVP wedge. After Phase 1, the framework can:

- Scan a network and fingerprint live hosts
- Deep-probe Linux hosts over SSH (CPU, memory, storage, network, PCI, GPU, services)
- Maintain part-level identity that survives moves (DIMMs, SSDs, NICs)
- Push inventory into NetBox via its API
- Run configuration assertions and produce reconciliation findings
- Produce a day-one audit against a real homelab

## Repo layout

```
.
├── docs/architecture.md            # System architecture (locked decisions)
├── docs/roadmap.md                 # Phased delivery plan
├── docs/backlog.md                 # Concrete task list (Phase 1 gaps + Phase 6)
├── docs/harness-schema-slice1.md   # Slice 1 DB schema spec (11 tables) + trust-gradient forward spec
├── docs/netbox-modeling-walkthrough.md  # NetBox modeling decisions for the dorktool lab
├── pyproject.toml                  # uv-managed project metadata
├── src/homelab_helper/             # The package
│   ├── db/                         # Models, enums, session
│   ├── adapters/                   # NetBox, Kernel-SSH (P1); Proxmox, K8s, UniFi, ... (P3+)
│   ├── probes/                     # Probe plugin SDK + first-party probes
│   ├── engine/                     # Reconciler, assertion engine, scheduler, fingerprint
│   ├── cli/                        # `helper` Typer app
│   ├── api/                        # FastAPI HTTP API (P1+)
│   └── agent/                      # helper-agent daemon (P2)
├── alembic/                        # DB migrations
└── tests/                          # pytest suite
```

## Development

This project uses [uv](https://docs.astral.sh/uv/) for environment and dependency
management. Install uv first if you don't have it.

```bash
# Sync dev environment (creates .venv, installs all deps including dev group)
uv sync --all-extras --group dev

# Run the test suite
uv run pytest

# Run the CLI
uv run helper --help

# Format + lint
uv run ruff format .
uv run ruff check . --fix

# Typecheck
uv run mypy src
```

### Pre-commit

```bash
uv run pre-commit install
```

## License

Apache License 2.0 — see [`LICENSE`](./LICENSE).
