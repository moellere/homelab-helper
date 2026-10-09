# Stability and deprecation

What you can build on, what you cannot, and how you will find out when
something changes. This page is the contract; a change to any surface it
names updates it in the same pull request, and a test holds the CLI and MCP
tables below to the code.

homelab-helper is **pre-1.0** (`helper version` tells you which pre-release).
The rules under [Deprecation policy](#deprecation-policy) say what that means
in practice; the short version is that the surfaces marked *held now* are
already treated as promises, and 1.0 is the point where the rest join them.

## The surfaces

| Surface | Status | The promise |
|---|---|---|
| [The invariants](#the-invariants) | **held now** | No release puts a model in the authorization path, lets an agent grant or elevate, or runs an unverifiable action unattended. |
| [CLI verbs](#cli-verbs) | **held now** | A verb's name, position and required arguments stay. Options are added, not removed. A renamed verb keeps its old name as a warning alias for a release. Human-readable output is not a contract. |
| [MCP tools](#mcp-tools) | **held now** | Tool names and argument names stay. Result objects only gain keys. Misses return `{"error": ...}`, never raise. |
| [Probe plugin contract](#probe-plugin-contract) | **held now** | The entry-point group, the `Probe` attributes and the four transport types only gain fields. |
| [Action manifests](#action-manifests) | **held now** | The envelope and every shipped action kind's target fields stay. New kinds are added; the executor rejects what it does not know. |
| [Database](#database) | **held now** | `helper db init` upgrades any earlier release's database. Migrations are forward-only. The schema itself is not an API. |
| [Configuration](#configuration) | **held now** | `HOMELAB_HELPER_*` names, secret references, file locations and precedence stay. |
| [Operator YAML files](#operator-yaml-files) | held now where versioned | Versioned formats change only with a new `version`. Unversioned ones only gain keys until they are versioned. |
| [NetBox custom fields](#netbox-custom-fields) | **held now** | A `cf_*` field's name and type never change; new ones are added by `helper netbox bootstrap`. |
| [Python versions](#python-versions) | **held now** | Every version CI runs is supported; one is dropped only in a minor release, after notice. |
| Python API (`import homelab_helper`) | **not stable** | Everything outside the probe contract is internal and may change in any release without notice. |
| Exit codes | partly | `0` is success and non-zero is failure; specific non-zero values are not promised. |

## The invariants

These are the promises that matter most, and the only ones enforced by tests
that would fail a release rather than by this page:

1. **An LLM never authorizes.** `decide()` in `engine/trust.py` is pure
   Python; two subprocess tests assert that neither it nor the executor
   transitively imports `homelab_helper.llm`. A model may draft and may
   trigger; it never decides.
2. **Every write goes through the executor.** An adapter's mutating methods
   are reachable only from `engine/executor.py` (and the rollback it drives);
   a test greps the whole package for every write method's name.
3. **The MCP surface cannot grant, elevate, override, roll back or open a
   window.** Mechanical tests assert the absence of any such tool, and that
   `execute_proposal` can never carry an override.
4. **Reversibility is verified, never claimed.** An action whose rollback
   cannot be verified against the live target is degraded to `CONFIRM`; a kind
   with no inverse can never reach `AUTONOMOUS`.
5. **The gate runs first, pessimistically.** A refused action never touches
   the target, even to probe it; rollback state is captured only after
   authorization.

A release that weakened any of these would be a different product. None is
scheduled to change at 1.0 or after.

## CLI verbs

`helper <group> [<verb>]`. The groups and verbs below are the stable surface;
the [CLI reference](cli.md) is generated from the same definitions and carries
every option.

| Group | Verbs |
|---|---|
| `approvals` | `show` |
| `assert` | `list` `load` `run` `show` |
| `audit` | — |
| `bottlenecks` | — |
| `chat` | — |
| `config` | `init` |
| `daemon` | `run` |
| `db` | `init` `migrate` `reset` `status` |
| `diff` | `git-vs-cluster` |
| `digest` | `history` `send` `show` |
| `discover` | `argocd` `backups` `cloudflare` `hass` `host` `import` `k8s` `mikrotik` `network` `omv` `proxmox` `replay` `show` `storage` `suggestions` `talos` `unifi` `usage` `versions` |
| `exec` | `accept` `list` `receipts` `reject` `rollback` `run` |
| `findings` | `ack` `list` `narrate` `resolve` `show` `suppress` |
| `host` | `retire` `show` |
| `mcp` | `serve` `tools` |
| `netbox` | `bootstrap` `health` `sync-cluster` `sync-host` |
| `onboard` | — |
| `part` | `merge` `show` |
| `plan` | `add-workload` `path` `rebalance` `rightsize` `surplus` `workloads` |
| `probes` | `list` `register` |
| `service` | `aliases` `resolvers` `retire-resolver` |
| `skills` | `set` |
| `trust` | `boundary` `grant` `history` `show` |
| `usage` | — |
| `version` | — |
| `view` | `host` `service` |
| `window` | `kill` `list` `open` `revoke` |

What is promised: the group and verb names, the order and meaning of
positional arguments, and that an option, once shipped, keeps its name and
meaning. What is not: the wording, layout or colour of what a verb prints.
Tables wrap to your terminal and titles get reworded. A script that needs
structured output uses the MCP tools, which exist for exactly that.

## MCP tools

`helper mcp serve` exposes these over stdio. Names and argument names are
stable; results are JSON objects that only ever gain keys.

| Area | Tools |
|---|---|
| Inventory | `list_hosts` `get_host` `list_services` `get_service` `audit_summary` `config_status` |
| Findings | `list_findings` `get_finding` `ack_finding` `resolve_finding` `suppress_finding` |
| Discovery | `run_discovery` `probe_host` `probe_talos` `retire_host` |
| Planning | `list_workloads` `recommend_placement` `plan_rebalance` `analyze_bottlenecks` `analyze_surplus` `network_path` `rightsizing` `usage_summary` |
| Proposals | `propose_action` `propose_workload_action` `propose_argocd_sync` `propose_dns_record` `draft_remediations` `list_proposals` `get_proposal` `pending_actions` `execute_proposal` |
| Trust, read-only | `trust_status` `list_receipts` |

Two conventions are part of the contract. A lookup that misses returns
`{"error": "..."}` with a message a model can act on, rather than raising a
protocol error. And the trust surface stays read-only: there is no tool that
grants, elevates, overrides, rolls back or opens a window, and a test asserts
there never will be. `execute_proposal` is a trigger, not an authority — it
calls the executor with no override, and at `CONFIRM` the executor still asks
a human.

## Probe plugin contract

A third-party probe depends on exactly this, all of it in
`homelab_helper.probes.base`:

- the entry-point group **`homelab_helper.probes`**, mapping a probe name to
  a `Probe` subclass;
- the `Probe` class attributes: `name`, `version`, `schema_version`,
  `required_privilege`, `produces_keys`, `target_kinds`, `output_schema`,
  `description`, and the abstract `async run(ctx) -> ProbeResult`;
- the four transport types — `ProbeTarget`, `ProbeContext`, `ProbeResult`,
  `ObservationData` — and their current fields;
- the `AdapterRegistry` with the adapter names `kernel-ssh` and `talos`;
- the rule that a probe never writes to the database: the runner persists.

These only gain fields. A field is never renamed or removed, and a new
required field is never added to something a probe constructs. Observation
*keys* the reconciler reads (`host.storage.devices`, `host.network.interfaces`,
`host.memory.dimms`, `host.memory.mem_total_bytes`, …) are likewise stable:
a probe that emits them today is read the same way tomorrow. The
[probe guide](writing-a-probe.md) has the worked example.

## Action manifests

`ProposalLog.artifact` — what anything that drafts an action writes, and what
the executor validates before it runs — has a fixed envelope:

```json
{"kind": "action",
 "action": {"domain": "...", "action_kind": "...", "target": {...}, "hostnames": [...]},
 "rollback": {"verified": false, "strategy": null}}
```

The shipped action kinds and their target fields are stable: `start` `stop`
`shutdown` `restart` `migrate` `cpu-type` `resize` (a Proxmox guest),
`workload-restart` `workload-scale` (a Kubernetes workload), `argocd-sync`,
`dns-record`, `node-update`. New kinds are added; the authoring schema in
`engine/manifest.py` and the executor's `parse_manifest` are held in agreement
by a test. Two things are deliberately **not** promised: that a manifest
written by a newer release is accepted by an older executor (it is rejected,
by design), and that `rollback.verified` in the manifest means anything —
reversibility is a finding the executor makes against the live target, never
a claim the manifest carries.

## Database

The database is reachable, not readable:

- `helper db init` (and `helper db migrate`) bring **any earlier release's
  database** to the current schema. The Alembic chain ships in the wheel.
- Migrations are **forward-only**. No release ships a downgrade; before
  upgrading, back up the SQLite file (or your Postgres database) if you may
  want to go back.
- A migration never requires a manual step that the release notes did not
  name.
- The tables and columns are **not an API**. Read the harness through the
  CLI and the MCP tools; a query against the schema may break in any release.

`HOMELAB_HELPER_DATABASE_URL` accepts SQLite (the default, a per-user file)
and Postgres (the `postgres` extra). CI runs the suite against SQLite only;
Postgres is supported but not yet exercised in CI, and this page will say so
until it is.

## Configuration

- Every variable is prefixed `HOMELAB_HELPER_`, and a variable's name and
  meaning never change. A renamed variable keeps the old name working, with
  a warning in `helper config`, for a release.
- A secret-valued variable accepts a literal or a reference —
  `env:NAME`, `file:/path#key` (plain, sops- or age-encrypted),
  `keyring:service/user` — and the reference syntax is stable.
- Locations: the database under the XDG data dir, the config file under the
  XDG config dir, both overridable together by `HOMELAB_HELPER_HOME`.
  Precedence, lowest to highest: `~/.env`, the per-user config file, a
  project `.env` in the working directory, the process environment.
  `HOMELAB_HELPER_NO_DOTENV=1` disables file loading entirely.
- `helper config` reports every source and every variable's origin without
  printing a secret, and that report's shape (the MCP `config_status` tool)
  only gains keys.

## Operator YAML files

| File | Pointed at by | Versioned | Promise |
|---|---|---|---|
| Lab replay fixture | `helper discover replay <path>` | `version: 1` | A `version: 1` file loads in every release that supports it; a breaking change is `version: 2`, and the loader says which it expected. |
| Assertion library | `helper assert load` | `version: 1` | Same. |
| Network topology | `HOMELAB_HELPER_NETWORK_TOPOLOGY` | not yet | Keys are only added until the format gains a `version`. |
| Service aliases | `HOMELAB_HELPER_SERVICE_ALIASES` | not yet | Same. |
| Workload library | `HOMELAB_HELPER_WORKLOAD_LIBRARY` | not yet | Same; the bundled library is data, and entries may be added, corrected or removed in any release. |

## NetBox custom fields

A `cf_*` field created by `helper netbox bootstrap` keeps its name and type
for good — NetBox holds the data, and a rename would strand it. New fields
are added by a later bootstrap, which is idempotent. The harness never
creates, renames or deletes a Device, and never writes a field NetBox owns.
The [field reference](netbox-custom-fields.md) says who writes each one.

## Python versions

**3.12 and 3.13.** CI runs the full gate — lint, format, types, tests, the
docs build — on each, and the `pyproject.toml` classifiers list exactly those
versions. A version is removed only in a
minor release, after its removal was announced in the previous one, and
never while it is the newest release of its line.

## Deprecation policy

**Before 1.0.** A surface marked *held now* above is already a promise:
changing it needs a deprecation, not just a release note. Everything else may
change between pre-releases, and each release's notes carry a *Breaking*
section when it does. Between `0.1.0b` builds the same rule applies; a beta is
not a licence to break what is marked held.

**From 1.0.** Versions follow semantic versioning on top of PEP 440.

- **Deprecate first.** A CLI verb or option prints a warning to stderr; an
  MCP tool result carries a `deprecated` key naming its replacement; a
  configuration variable is reported as deprecated by `helper config`; a YAML
  format gains a new `version` while the old one still loads. The warning
  names the replacement and the release that will remove the old form.
- **Remove later.** Not before the next **minor** release after the
  deprecation shipped, and never in a patch release.
- **Major releases** are the only place a held surface is removed without a
  deprecated interim, and the release notes list every such removal.
- **The database** never needs a downgrade and never needs an unannounced
  manual step, at any version.
- **The invariants** are not subject to this policy. They do not deprecate.

**How you find out.** The GitHub release notes, generated from merged pull
requests and edited for a *Breaking* and a *Deprecated* section when either
applies; `helper version`; and this page, which changes in the same pull
request as the surface it describes — the contributing guide makes that a
review rule.
