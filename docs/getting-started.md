# Getting started

Two parts. The first gets you to a finding table in a few minutes with nothing
but the tool: a synthetic lab ships inside it. The second points the harness at
your own lab, one source at a time. Nothing in either part changes anything on
your network — discovery is read-only, and execution stays off until you turn a
specific kind of change on yourself.

## Part 1 — a finding table, with no lab at all

**Requirements:** Python 3.12 or newer on Linux or macOS, and
[uv](https://docs.astral.sh/uv/) or pipx.

### 1. Install

```bash
uv tool install --prerelease allow homelab-helper   # from PyPI
# or: pipx install homelab-helper==0.1.0b3
helper --install-completion                          # bash / zsh / fish, optional
```

The project is in beta, so installers skip it unless told to allow
pre-releases; drop the flag once a non-beta release is on PyPI. To work from a
checkout instead, `uv sync --all-extras --group dev` and prefix every command
below with `uv run`.

### 2. Initialize

State lives in a per-user directory, never in the working directory: the
database under `~/.local/share/homelab-helper/` and your credentials under
`~/.config/homelab-helper/.env` (XDG variables are honoured; set
`HOMELAB_HELPER_HOME` to put both somewhere else).

```bash
helper db init
```

```text
running alembic upgrade head...
...
syncing probe entry points...
  inserted: 12, updated: 0
seeding trust domains...
  created: 8 (idempotent)
done
```

### 3. Replay the bundled lab

The harness ships a three-host synthetic lab — hosts and the raw observations a
probe would have returned from them — and a small library of assertions about
what those hosts *should* look like. `discover replay` loads it, reconciles
each host exactly as live discovery would, and runs the assertions.

```bash
helper discover replay
```

```text
replayed: 3 host(s), 22 observation(s), 8 assertion(s) loaded / 8 run
Run helper audit to see the resulting findings.
```

### 4. Look at what it found

```bash
helper findings list
```

```text
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ fingerprint      ┃ sev    ┃ kind                     ┃ status ┃ title                                                   ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ a44de43ef8c38f89 │ medium │ config-drift             │ open   │ Assertion failed: lab-c.memory_at_least_4gb             │
│ 6d1307ebb3baf6da │ low    │ config-drift             │ open   │ Assertion failed: lab-b.cpu_supports_avx2               │
│ 008d1e49f4aee444 │ medium │ config-drift             │ open   │ Assertion failed: lab-a.cpu_cores_at_least_2            │
│ 574bb9fbe593dc80 │ low    │ inventory-gap            │ open   │ Storage device /dev/sdb has no stable identity          │
│ 19314d5fb2cb3653 │ medium │ storage-provenance-delta │ open   │ Storage device /dev/sda shares forged WWN 5000000000000099 │
│ 97d8fbc42e4c1126 │ low    │ inventory-gap            │ open   │ DIMM in DIMM_A1 has no serial                           │
│ e02424bd3eb39acc │ low    │ inventory-gap            │ open   │ Network interface eth0 has no MAC                       │
│ ...              │        │                          │        │                                                         │
└──────────────────┴────────┴──────────────────────────┴────────┴─────────────────────────────────────────────────────────┘
13 finding(s)
```

Thirteen findings, three kinds, and each one is a real thing the harness does:

- **`config-drift`** — an assertion about a host failed. `lab-a` was declared
  to need at least 2 cores and 4 GiB; it has 1 and 2. The one amd64-only
  assertion on the arm64 host was *skipped*, not failed — assertions carry an
  architecture scope.
- **`inventory-gap`** — a part the probe saw but cannot track: a disk with no
  WWN or serial, a NIC with no MAC, a DIMM with no serial. The harness tracks
  physical parts by identity so it can tell you a drive moved hosts; a part
  with no identity is a gap it tells you about rather than guesses at.
- **`storage-provenance-delta`** — three distinct USB drives on three hosts
  all report the same WWN, because cheap enclosures forge one. The harness
  refuses to merge them and says why.

`helper audit` rolls the same findings up by severity and status. Run
`helper discover replay` again: nothing is duplicated, and a finding whose
condition has gone away resolves itself. That lifecycle — stable fingerprint,
reopen on recurrence, resolve only when the category was actually observed —
is what makes the finding table something you can act on rather than re-read.

The lab is a plain YAML file. `helper discover replay asymmetric` loads a
second bundled one, a three-node cluster with one slow uplink, for
`helper bottlenecks` to find; pass a path to replay a fixture of your own.

## Part 2 — your own lab

### 1. Write the config file

```bash
helper config init      # writes a commented .env template to ~/.config/homelab-helper/
helper config           # what the harness will actually talk to, and from where
```

Uncomment what you use. Every variable is prefixed `HOMELAB_HELPER_`, and each
source only needs its variables when you run that source's `discover` verb. A
secret-valued variable may hold a reference instead of a literal —
`file:~/secrets.yaml#proxmox`, `file:~/secrets.sops.yaml#netbox` (decrypted
with sops), an age-encrypted file, or `keyring:homelab-helper/hass` — and
`helper config` reports *set via file* / *keyring* / *env* without ever
printing the value.

| Source | Variables (after `HOMELAB_HELPER_`) |
|---|---|
| Proxmox | `PROXMOX_URL`, `PROXMOX_TOKEN_ID`, `PROXMOX_TOKEN_SECRET`, `PROXMOX_VERIFY_SSL` |
| Kubernetes | `KUBECONFIG`, `KUBE_CONTEXT` (falls back to the ambient kubeconfig) |
| UniFi | `UNIFI_URL`, `UNIFI_API_KEY`, `UNIFI_SITE`, `UNIFI_VERIFY_SSL`; `UNIFI_CONTROLLERS=main,remote` for more than one gateway |
| MikroTik | `MIKROTIK_URL`, `MIKROTIK_USERNAME`, `MIKROTIK_PASSWORD` (a read-only user with the `rest-api` policy), `MIKROTIK_NAME` |
| Cloudflare | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE` or `CLOUDFLARE_ZONE_ID` |
| Argo CD | `ARGOCD_URL`, `ARGOCD_API_TOKEN`, `ARGOCD_VERIFY_SSL` |
| OpenMediaVault | `OMV_URL`, `OMV_USERNAME`, `OMV_PASSWORD`, `OMV_VERIFY_SSL` |
| Home Assistant | `HASS_URL`, `HASS_TOKEN` (a long-lived token; a non-admin user is enough) |
| NetBox | `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_VERIFY_SSL` — the inventory you own; see [NetBox custom fields](netbox-custom-fields.md) |
| Chat | `LLM_PRIVACY` (`strict-local` / `prefer-local` / `open`), `OLLAMA_URL`, `OLLAMA_MODEL`; BYOK: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` |

### 2. Probe a host over SSH

Hosts you deep-probe need SSH with key auth. The SMART and DIMM probes run
`smartctl` and `dmidecode` under `sudo -n`, so give the probe user passwordless
sudo for those two commands — or accept that disks and DIMMs report without
identity, which the finding table will tell you about.

```bash
helper discover host <hostname> --ssh-user <user> --ssh-key ~/.ssh/id_ed25519
helper host show <hostname>
```

Twelve probes run: identity, CPU, memory and DIMMs, network interfaces,
storage, SMART, PCI, GPUs, services, and a Talos variant for nodes that have
no SSH. `helper probes list` shows them; a probe you write yourself
([how](writing-a-probe.md)) joins the list through a Python entry point.

### 3. Read the management planes

```bash
helper discover proxmox --persist   # clusters, nodes, guests
helper discover k8s --persist       # nodes, workloads
helper discover unifi --persist     # static DNS, clients
helper discover omv --persist       # filesystems, disks, shares; stray exports
helper audit
```

Each verb is a read; `--persist` writes what it read into the harness
database, and `--dry-run` previews that. From here the planners have something
to reason about:

```bash
helper plan placement immich        # where would this workload go, and why not elsewhere
helper plan rebalance               # three candidate plans with their trade-offs
helper bottlenecks                  # known patterns, with mitigations derived from your facts
helper plan surplus                 # capacity nothing is using, and what it could do
helper chat "what's wrong with my lab?"
```

### 4. Keep it running

```bash
helper daemon run --once    # discovery → playbooks → listener, one pass; cron-friendly
helper daemon run           # the same on cadences, until Ctrl-C
```

With no `--sources`, the daemon discovers from every source `helper config`
reports configured. Nothing it does executes anything: playbooks turn findings
into *proposals*, and the listener asks you about them only for the kinds of
change you have granted — which is the subject of
[the trust gradient](trust-gradient.md).

### 5. Before trusting it with a real change

Run the [live validation runbook](live-validation.md). The test suite is all
mocks; the runbook is where the acceptance criteria meet your hardware, one
read-only criterion at a time, and then — against a guest created to be
destroyed — the first real execution.
