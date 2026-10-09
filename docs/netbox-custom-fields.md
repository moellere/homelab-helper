# NetBox custom fields

NetBox is the inventory you own. The harness treats it as canonical for the
facts an operator decides — site, role, primary IP, status — and never creates
or deletes a Device. What it adds is a set of custom fields on `dcim.device`,
prefixed `cf_`, for the facts it discovers or derives.

```bash
helper netbox health                   # version banner from /api/status/
helper netbox bootstrap --dry-run      # which fields would be created
helper netbox bootstrap                # create the missing ones; idempotent
helper netbox sync-host <hostname>     # push one Host's cf_* values and part placements
helper netbox sync-cluster <cluster>   # sync a cluster's guests; write NetBox ids back
```

`sync-host` on a hostname with no Device is a clear "create the Device, then
re-run", not a creation. Inventory items it syncs are only the ones it
discovered; anything you entered by hand is invisible to its diff.

## The fields

The bootstrap creates these on `dcim.device`. **Written by** says who sets the
value: the harness on every `sync-host`, or you, by hand, with the harness
only ever reading it.

| Field | Type | Values | Written by |
|---|---|---|---|
| `cf_power_policy` | select | `always-on` · `wol-on-demand` · `manual` | harness, from the Host's power policy |
| `cf_expected_power_state` | select | `on` · `off` · `either` | harness, derived from the policy |
| `cf_discovery_source` | text | `manual` · `unifi` · `proxmox` · `k8s` · `kernel-probe` · … | harness |
| `cf_discovery_last_run` | datetime | UTC | harness, each discovery |
| `cf_last_verified` | date | | harness, from the date you last hand-confirmed the host |
| `cf_capabilities` | json | `{"mem_total_bytes": …, "cpu_model": …, "avx2": true, …}` | harness; the capability bag the planners read |
| `cf_arch` | select | `amd64` · `arm64` · `arm` · `other` | harness |
| `cf_hypervisor_type` | select | `proxmox` · `esxi` · `kvm-host` · `docker-host` · `bare-metal` · `talos` · `none` | **you** |
| `cf_power_draw_idle_watts` | decimal | watts at the wall | **you** |
| `cf_power_draw_max_watts` | decimal | watts at the wall | **you** |

The three you own are the three a probe cannot measure. The harness reads them
when they are set and says nothing when they are not.

## What the harness does not touch

Site, role, tenant, status, primary IP, device type, serial, asset tag: yours.
A `sync-host` sends a `custom_fields` patch and inventory-item changes for
discovered parts, and nothing else. If NetBox and the harness disagree about a
fact NetBox owns, NetBox wins and the harness shows you the difference as a
finding rather than overwriting it.

The [NetBox modelling walkthrough](netbox-modeling-walkthrough.md) is the
longer account of how a homelab maps onto NetBox's objects, and where each
fact's canonical home is.
