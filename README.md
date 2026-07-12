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

## Running it locally

> Pre-alpha, run-from-source. There's no packaged release or server to deploy
> yet (the HTTP API and agent layers are later phases). "Running it locally"
> means installing from source, initializing a local SQLite database, and
> driving the `helper` CLI. Everything is **read-only (L1) — it proposes, never
> applies.**

**1. Install** (see [Development](#development) for the full toolchain):

```bash
uv sync --all-extras --group dev
```

**2. Initialize the database** — a SQLite file `./homelab.db` by default
(override with `HOMELAB_HELPER_DATABASE_URL`; a `postgres` extra is available
for Postgres):

```bash
uv run helper db init      # alembic upgrade + register entry-point probes
uv run helper db status
# uv run helper db reset --yes   # DESTRUCTIVE — dev only
```

**3. Configure source credentials.** Credentials are read from the process
environment — there is no `.env` auto-loading, so `export` them or pass
`uv run --env-file .env …`. Each source only needs its variables when you run
that `discover` verb (all are prefixed `HOMELAB_HELPER_`):

| Source | Variables |
|---|---|
| Database | `DATABASE_URL` (default local SQLite) |
| UniFi | `UNIFI_URL`, `UNIFI_API_KEY`, `UNIFI_SITE`, `UNIFI_VERIFY_SSL` |
| Cloudflare | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE` (or `CLOUDFLARE_ZONE_ID`) |
| Argo CD | `ARGOCD_URL`, `ARGOCD_API_TOKEN`, `ARGOCD_VERIFY_SSL` |
| Proxmox | `PROXMOX_URL`, `PROXMOX_TOKEN_ID`, `PROXMOX_TOKEN_SECRET`, `PROXMOX_VERIFY_SSL` |
| Kubernetes | `KUBECONFIG`, `KUBE_CONTEXT` |
| OpenMediaVault | `OMV_URL`, `OMV_USERNAME`, `OMV_PASSWORD`, `OMV_VERIFY_SSL` |
| NetBox | `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_VERIFY_SSL` |

Print the effective configuration (secrets shown only as set/unset, never
printed):

```bash
uv run helper config
```

**4. Run.** Discovery is read-only; add `--persist` to write to the DB and
`--dry-run` to preview:

```bash
uv run helper --help

uv run helper discover host <name> --ssh-user <u> --ssh-key <path>
uv run helper discover unifi --persist
uv run helper discover cloudflare --persist
uv run helper discover argocd
uv run helper discover proxmox --persist
uv run helper discover omv             # OpenMediaVault NAS: filesystems, disks, shares, services

uv run helper view service <name>      # internal/external endpoints + DNS split-brain
uv run helper view host <name>         # guests, endpoints, findings
uv run helper diff git-vs-cluster --persist   # Argo CD drift → DRIFT_CANDIDATE findings
uv run helper audit
uv run helper findings list
```

Keep tokens and keys in the environment — never commit them.

### Using with Claude / MCP

There is no native MCP server yet — it's a **Phase 4** deliverable (see
[`roadmap.md`](./docs/roadmap.md)), which will expose the engine and adapter
capabilities as MCP tools for Claude Desktop / Claude Code / Cursor. Until then,
the practical path is to run **Claude Code inside this repo**: it drives the
`helper` CLI directly ("discover the host at 10.0.6.27", "show me service X"),
which stays within the read-only L1 stance.

## Repo layout

```
.
├── docs/architecture.md            # System architecture (locked decisions)
├── docs/roadmap.md                 # Phased delivery plan
├── docs/backlog.md                 # Concrete task list (Phase 1 gaps + Phase 6)
├── docs/harness-schema-slice1.md   # Slice 1 DB schema spec (11 tables) + trust-gradient forward spec
├── docs/netbox-modeling-walkthrough.md  # NetBox modeling decisions for the example lab
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
