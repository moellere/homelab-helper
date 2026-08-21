# CLAUDE.md — instructions for Claude Code sessions on this repo

A pre-alpha framework for homelab planning, inventory, audit, and (eventually)
recommendations. Async Python 3.12 + SQLAlchemy 2.0 + httpx + Typer. **L1
stance: propose, never apply.** L2 (execution) is the planned final phase,
gated by a deterministic trust gradient — see `docs/architecture.md`.

## Read first

Before any non-trivial change, skim:

- `docs/architecture.md` — locked decisions, including the trust gradient
- `docs/roadmap.md` — the six phases + acceptance criteria
- `docs/backlog.md` — the actual punch list (what's done vs. what's left)
- `docs/harness-schema-slice1.md` — DB schema + forward-spec for L2 trust tables

`README.md` is the operator-facing intro; the docs above are what to consult
when making implementation decisions.

## Repo layout

```
src/homelab_helper/
├── adapters/       NetBox, KernelSSH, Proxmox, K8s, UniFi, Cloudflare,
│                   Argo CD, OpenMediaVault — all read-only at L1
├── cli/            Typer apps; entry point in main.py (18 verbs)
├── db/             Models, enums, async session
├── engine/         Reconciler, AssertionEngine, ProbeRunner, fingerprint,
│                   placement/rebalance/bottlenecks/network_path planners
├── llm/            LLMRouter + backends, chat context, Narrator/Planner/
│                   Discovery agents (LLM never in any authorization path)
└── probes/         host.* + network.* plugins; register via entry points
mcp_server.py       MCP tools over stdio (helper mcp serve)
tests/              pytest, asyncio_mode = "auto"
fixtures/           operator-editable YAML (assertions, workload library,
                    network topology example)
alembic/            migrations
```

## Toolchain — every command goes through `uv`

```bash
# One-time / after pulling
uv sync --all-extras --group dev

# Before every commit (CI runs all four; if any fails, fix it)
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest -q

# Quick auto-fix during dev
uv run ruff check --fix src tests
uv run ruff format src tests

# Run the CLI
uv run helper <verb>

# DB lifecycle
uv run helper db init      # alembic upgrade + register entry-point probes
uv run helper db status
uv run helper db reset --yes  # DESTRUCTIVE; dev only
```

CI (`.github/workflows/ci.yml`) runs lint → format → mypy → pytest on push +
PR against `main` under Python 3.12 (matches `.python-version`). Don't push
red commits.

## Workflow

- **Branches:** one unit of work per branch, cut fresh from `main` with a
  descriptive `claude/*` name. Squash-merge means a merged branch holds only
  merged history — restart it from `main` for the next unit.
- **Commits:** never amend an existing commit unless explicitly asked. Pre-
  commit hooks live in `.pre-commit-config.yaml`; install with
  `uv run pre-commit install` if you want them local.
- **PRs are drafts until the user says merge.** Open the draft, report CI,
  wait for the word; the user merges (or says "merge when green").
- **Never commit secrets.** No `.env` files, no NetBox tokens, no SSH keys.
  (`.env` at the repo root is auto-loaded at runtime and gitignored.)

## Load-bearing architecture invariants

These exist for safety reasons; violating them is the kind of thing that
silently corrupts state. Read carefully.

### 1. Reconciler — "absence of an observation must never auto-resolve"

When the reconciler's `_auto_resolve_findings` pass runs, it ONLY closes
findings whose category was observed this run. The ledger tracks
`categories_observed` and `active_fingerprints[category]` separately. An
absent probe observation must not silently look like "everything in that
category is now resolved." Test: `test_absent_observation_does_not_auto_resolve`.

### 2. Reconciler — close-on-this-host loop must filter by `PartKind`

Each lineage method's close-loop joins through `PhysicalPart` and filters by
the kind(s) it handles. Without the filter, reconciling DIMMs would close
storage/NIC placements. There's a regression test:
`test_dimm_reconcile_does_not_touch_storage_placements`. Same pattern for
storage and NIC.

### 3. Findings — deterministic fingerprint + reopen-on-recurrence

Both the reconciler (`INVENTORY_GAP`) and the AssertionEngine (`CONFIG_DRIFT`)
use `make_fingerprint(kind, target_type, target_id, root_cause_token)` so the
same logical issue always hits the same row. A RESOLVED finding whose
fingerprint recurs flips back to OPEN with `first_seen` reset.
`AssertionEngine` keys by `(kind, scope_type, scope_target, "assertion:<name>")`;
the reconciler keys by `(kind, "host", <host_id>, "<category>:<slot>:<reason>")`.

### 4. NetBox sync — operator owns Devices

`sync_host` never creates Devices. NetBox owns canonical inventory facts
(site, role, primary IP, status); the harness owns the `cf_*` custom field
values. Missing Device → `SyncHostResult(found=False)` with a clear "create
the Device, then re-run" message. Same for `sync_inventory_items`: only
touches items with `discovered=True`, so operator-entered InventoryItems are
invisible to the diff.

### 5. L2 / trust gradient — deterministic, never an LLM in the gate

There is no `decide()` function in code yet, but the principle holds for any
future write path: action authorization is deterministic Python, never an LLM.
LLMs may *draft* manifests; policy decides whether they run. See
`docs/architecture.md` "Trust gradient (L2 authorization model)".

## Test patterns

- **Unit tests** use `make_engine("sqlite+aiosqlite:///:memory:")` plus
  `Base.metadata.create_all` — see `tests/test_reconciler.py` for the canonical
  fixture pattern (`engine`, `sessionmaker` fixtures, `session_scope` for the
  test body).
- **CLI tests** must use a **file-backed** SQLite via `tmp_path` + `monkeypatch`
  on `HOMELAB_HELPER_DATABASE_URL`. Each CLI invocation builds its own engine,
  so `:memory:` would be empty. See `tests/test_cli_assert.py` for the
  pattern.
- **Adapter tests** use `httpx.MockTransport` against an `httpx.AsyncClient`
  injected into the adapter. See `tests/test_netbox_adapter.py`. **Never** hit
  a live NetBox in tests.
- **Probe tests** use asyncio loopback servers (`asyncio.start_server`) on
  127.0.0.1 ephemeral ports. See `tests/test_network_probes.py`. Don't mock
  `asyncio.open_connection`.
- `pyproject.toml` sets `asyncio_mode = "auto"` — any `async def test_*` is
  picked up automatically, no `@pytest.mark.asyncio` needed.
- Rich tables wrap aggressively under CliRunner's narrow virtual terminal.
  Assert on **fingerprint columns** (no-wrap) or **footer counts** (`"2
  finding(s)"`), not on long title substrings — see `tests/test_cli_assert.py`
  for examples of resilient assertions.

## Common pitfalls

- **`scalar_one_or_none()` returns Any** when the session isn't typed.
  Annotate the session as `AsyncSession` (in TYPE_CHECKING block) and either
  cast the result or annotate the receiving variable. See
  `cli/discover.py::_resolve_host` for the clean pattern.
- **`httpx.Response.json()` returns Any.** Methods that pass it through a
  typed return need `cast("dict[str, Any]", ...)`. See `adapters/netbox.py`.
- **SQLAlchemy JSON columns require reassignment.** In-place mutation
  (`host.capabilities["k"] = v`) won't mark the row dirty. Always do
  `new = dict(host.capabilities or {}); new["k"] = v; host.capabilities = new`.
- **Owner-of-client flag** on adapters that accept an injected client. The
  adapter must not close a client it didn't create (tests would lose their
  MockTransport). See `NetBoxAdapter._owns_client`.
- **CLI commands that call `asyncio.run`** must wrap their `_go()` in a
  `try/finally` that `await engine.dispose()`. Otherwise the test suite leaks
  connections across runs.

## Don't do

- Don't push to `main` directly; everything goes through a PR.
- Don't open a PR without the user asking.
- Don't bypass pre-commit / `--no-verify`. If a hook fails, fix the issue.
- Don't add destructive operations (`rm -rf`, `git reset --hard`, force-push)
  without explicit confirmation in the conversation.
- Don't introduce features beyond what the task requires. The repo is
  deliberately L1-only; new adapter write paths must route through gated
  code (or wait for Phase 6).
- Don't write multi-line docstrings or aggressive comments. The codebase
  conventions are: one-line attribute docstrings where helpful, methodical
  doc-string-as-spec at the top of new modules, no comments that just restate
  the code.

## Status snapshot

Build state by phase (see `docs/backlog.md` for the authoritative punch list):

| Phase | State |
|---|---|
| 1 — Inventory & discovery | Core complete; a few probes open (`host.raid`, dmidecode DIMM depth, example replay fixture) |
| 2 — Continuous agent / time-series | **Deliberately deferred** (several planners note "lands with Phase-2 time-series") |
| 3 — Management-plane adapters | Complete (6 adapters, split-brain, drift, stray-config) |
| 4 — Conversational + MCP | Complete (router, chat, narrator, onboard, skills, MCP server); Web UI deferred to 4.5 |
| 5 — Planning & recommendations | Build complete (all six ACs implemented) |
| 6 — L2 execution & trust gradient | In design; PR sequence agreed (schema+`decide()` first) |

Live-fleet validation of Phases 4–5 ACs is the outstanding sign-off gate, and
required before any Phase-6 execution path runs against real infrastructure.

When in doubt, search `docs/backlog.md` — every committed-but-incomplete item
has a sub-list of what's left.
