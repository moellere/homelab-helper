# Handoff — Phase 9.5 onward (2026-10-09)

Transient. Written by the cloud session that built 9.1–9.4 so a session on a
local machine can pick up 9.5 cold. Everything durable lives in `CLAUDE.md`,
`docs/roadmap.md` and `docs/backlog.md`; this file only adds what those do not
say yet. **Delete it in the PR that closes 9.5.**

## Where the repo stands

- `main` carries Phase 9.1 (#72), 9.2 (#73), 9.3 (#74). 9.4 is PR #75 —
  merged or awaiting the merge word when you read this; check.
- Every PR followed the same rhythm: draft, report CI, wait for "merge". Keep
  it. The gate is five checks (`CLAUDE.md` → Toolchain) and CI runs it on
  3.12 and 3.13.
- Tests: 1161 passing, 2 skipped (SSH integration, env-gated). All mocks.

## Things only the operator, or a machine on the LAN, can do

Being local is the point of the move. These are open and need the lab:

| Item | Where it is written up | What it needs |
|---|---|---|
| Enable GitHub Pages once | backlog 9.3 | Settings → Pages → Source "GitHub Actions". `docs.yml` fails harmlessly on `main` until then. |
| P4-AC2 narration half | `docs/live-validation.md` P4-AC2 | `helper discover replay asymmetric` on a scratch DB, then `helper bottlenecks --narrate` against your own router. One command; closes the row. |
| P4-AC3 onboarding | runbook Part 1 | A host the harness has never seen. Interactive. |
| P6 steps 5–6 live | runbook Part 2 | A dispatch failure demoting a cell; the kill switch mid-flight. Needs a write that fails after a read succeeds — hard to stage honestly. |
| P8 first `node-update` | runbook **Part 4** | One drained node. No inverse: the cell can never reach AUTONOMOUS, and should not. |
| Postgres in CI | backlog 9.4 | A `postgres` service in `ci.yml` and the suite against it. The stability page says SQLite-only until then. |

Closing the four runbook rows closes Phase 9 AC #1 ("no `⏳` rows").

## 9.5, corrected: two probes, not three

The roadmap row names three items. One is already done and its backlog rows
were stale — verified this session, not assumed:

**dmidecode DIMM depth — done.** `host.memory` runs `sudo -n dmidecode -t
memory` and emits `host.memory.dimms` with slot, size, serial, vendor and part
(`probes/host/memory.py::parse_dmidecode_memory`); the reconciler's
`_reconcile_dimm_lineage` keys a `PhysicalPart` by serial and opens a
`Placement`. Replaying the bundled `asymmetric` lab, whose three DIMMs carry
serials, yields three `PhysicalPart(kind=DIMM)` rows, three open placements
and zero gap findings. Phase 9 AC #5's DIMM clause is met today. The stale
rows are corrected in the backlog alongside this file.

What remains:

### `host.raid` / `host.shares`

Two SSH probes, same shape as `host.smart` (parsers as pure functions, `sudo
-n` prefix when the user is not root, `success=False` with a reason when the
tool is absent). Register both in `pyproject.toml`'s
`[project.entry-points."homelab_helper.probes"]`; `helper probes register`
syncs them; `discover host` runs every host probe by default (`--probe` to
restrict).

- **`host.raid`** — sources: `/proc/mdstat` (no root) and `mdadm --detail
  /dev/mdN` (root) for each array. Emit `host.raid.arrays`: a list of `{name,
  level, state, size_bytes, uuid, degraded, members: [{device, state, slot}]}`.
  `host.storage` already sees `md*` nodes in the lsblk tree
  (`BlockDevice.type == "raid"`) and the reconciler deliberately filters `md`
  out of part lineage (`reconciler.py` ~line 139: logical devices carry no
  WWN). The roadmap wants "an array as a volume over its member parts". Two
  honest levels, decide with the operator before building:
    1. *Observation + capability + finding.* Project a summary into
       `Host.capabilities["raid"]`, and raise a finding for a degraded or
       rebuilding array (a new category under `engine/category_findings.py`,
       resolved only when the category was observed — invariant 1). No
       schema change. This is what a day-one user with an mdraid box needs.
    2. *A volume model.* A table linking an array to its member
       `PhysicalPart`s by WWN. That is a migration, and migrations are
       forward-only by the stability promise, so it should be a deliberate
       design, not a side effect of a probe. Write it up as an issue first.
  AC #5 says "a host with an mdraid array reports its composition" — level 1
  satisfies it.
- **`host.shares`** — sources: `/etc/exports` + `exportfs -v` for NFS,
  `testparm -s` (or `/etc/samba/smb.conf`) for SMB. Emit
  `host.shares.exports`: `[{protocol: nfs|smb, name, path, options|clients}]`.
  `engine/stray_export.py` already detects "an export with nothing behind it"
  for OpenMediaVault; a host-level share list lets the same check run on any
  Linux NAS. Reuse it rather than writing a second detector.

### `talos.host` CPU / DIMM depth

Probe-side only. `adapters/talos.py::get_resources(node, resource)` is
generic — `talosctl get <resource> -o json` — so the probe can pull
`memorymodules` and `processors` (Talos COSI hardware resources) with no
adapter change. Project `memorymodules` onto the canonical
`host.memory.dimms` shape (slot ← `deviceLocator`, serial ← `serialNumber`,
size ← `size`, vendor ← `manufacturer`, part ← `productName`) and the DIMM
lineage works for Talos nodes with **no reconciler change** — that is the
whole design of `talos.host` (its module docstring says why). `processors`
gives vendor/model/core and thread counts; keep `/proc/cpuinfo` as the source
for flags. Expect empty serials on VMs and some consumer boards: that becomes
a DIMM gap finding, which is the correct answer. The test fake is
`tests/test_talos_probe.py` (a `get_resources` stub returning documents).

## Gotchas this session paid for

- **Stale backlog rows.** Twice now (8.7's GPU/PCI probes, 9.5's DIMM depth)
  the backlog said a thing was missing that existed. Verify against the code
  or a replayed lab before planning around a `[ ]`.
- **`uv run` rebuilds the env from `.python-version`.** To run anything on
  3.13, export `UV_PYTHON=3.13` (and `UV_PROJECT_ENVIRONMENT=<dir>` to keep
  the main venv). `uv run --python` alone silently gives you 3.12.
- **Rich wraps under CliRunner.** Assert on tokens that cannot wrap
  (fingerprints, counts, a short phrase), never on a long message.
- **CLI tests are sync.** `runner.invoke` calls `asyncio.run`; an
  `async def test_` around it fails. Async fixtures are fine.
- **`uv sync --all-extras --group dev --group docs`** — without `--all-extras`
  mypy fails on the `keyring` extra; without `--group docs` the strict build
  is missing.
- **Before every commit:** `git diff --diff-filter=U --name-only` and a grep
  for `<<<<<<<`. A conflict marker reached a commit once in this project.
- **Membership is one rule.** Anything that treats a host as a place guests
  can go reads `engine/cluster_nodes.py`. Do not reinvent it in a new planner.
- **`discover replay`** takes a bundled name (`example`, `asymmetric`) or a
  path; the labs live in `src/homelab_helper/data/labs/`, not `fixtures/`.

## Cheat sheet

```bash
uv sync --all-extras --group dev --group docs
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src && uv run pytest -q && uv run mkdocs build --strict
uv run mkdocs serve                                  # the site, locally

export HOMELAB_HELPER_DATABASE_URL="sqlite+aiosqlite:///$HOME/.homelab-scratch.db"
uv run helper db init && uv run helper discover replay asymmetric
uv run helper bottlenecks --narrate                  # P4-AC2's open half
```
