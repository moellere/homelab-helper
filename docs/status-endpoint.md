# Status endpoint and dashboards

`helper status` is the one-screen answer to "is anything wrong, and is
anything waiting on me?" — open findings by severity, action proposals awaiting
approval, when discovery last ran, the trust cells, the last day's receipts.
The same rollup is served over HTTP for a dashboard tile, so the harness shows
up next to everything else on the wall instead of only in a terminal.

It is a read. The endpoint has two `GET` routes and nothing that acknowledges a
finding, approves a proposal or touches the trust gradient; a test holds it to
that. Approving still happens on the phone (`helper approvals`) or at the CLI.

## In the terminal

```bash
helper status show          # traffic light, headline, one table
helper status show --json   # the object the endpoint serves
```

`health` is one of three words:

| `health` | When |
|---|---|
| `critical` | a critical or high finding is open, or an execution failed in the last 24 h |
| `attention` | a medium finding is open, a proposal awaits approval, or discovery has not run in 12 h |
| `ok` | otherwise (low and info findings do not raise it) |

## Serving it

```bash
helper status serve --host 0.0.0.0 --port 8710
curl -s http://host:8710/status | jq .health
curl -s http://host:8710/healthz        # {"ok": true}, never needs a token
```

The process reads the same database and `.env` as every other verb, so run it
where the daemon runs. A systemd user unit is enough:

```ini
# ~/.config/systemd/user/homelab-status.service
[Unit]
Description=homelab-helper status endpoint

[Service]
WorkingDirectory=%h/repos/homelab-helper
ExecStart=%h/.local/bin/helper status serve --host 0.0.0.0 --port 8710
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now homelab-status
loginctl enable-linger   # keep user units running when you are logged out
```

Set `HOMELAB_HELPER_STATUS_TOKEN` (a literal or a `file:`/`keyring:`/`env:`
reference, like every secret-valued setting) and `/status` requires
`Authorization: Bearer <token>` or `X-API-Token: <token>`. Leave it unset on a
LAN that is already the boundary; the body holds counts and finding titles,
never a credential.

## The object

Keys are only ever added. The ones a widget usually wants:

| Key | Meaning |
|---|---|
| `health`, `headline` | the traffic light and a one-line summary |
| `findings.open`, `findings.highest`, `findings.critical_or_high` | open + acknowledged findings |
| `findings.by_severity.<sev>`, `findings.by_kind.<kind>` | counts |
| `proposals.pending`, `proposals.titles`, `proposals.oldest_at` | action proposals awaiting an operator |
| `discovery.last_run_at`, `discovery.age_seconds`, `discovery.failed_24h` | the newest `DiscoveryRun` and the last day |
| `discovery.probes.<name>` | newest run per probe |
| `assertions.last_run_at` | newest `AssertionRun` |
| `trust.cells_by_level.<level>`, `trust.open_windows` | the gradient at a glance |
| `receipts_24h.succeeded`, `receipts_24h.failed` | what actually ran |
| `inventory.hosts` … | row counts |

## Homepage

A [`customapi`](https://gethomepage.dev/widgets/services/customapi/) service
widget; dot paths reach nested keys.

```yaml
- Homelab Helper:
    href: https://moellere.github.io/homelab-helper/
    description: inventory, audit, approvals
    icon: mdi-clipboard-check-outline
    widget:
      type: customapi
      url: http://ubuntu-dev.lan:8710/status
      refreshInterval: 60000
      # headers: { X-API-Token: "{{HOMEPAGE_VAR_HOMELAB_HELPER_TOKEN}}" }
      mappings:
        - field: health
          label: Health
          format: text
        - field: findings.open
          label: Open findings
          format: number
        - field: proposals.pending
          label: Awaiting approval
          format: number
        - field: discovery.last_run_at
          label: Last discovery
          format: relativeDate
```

## Home Assistant

A [REST sensor](https://www.home-assistant.io/integrations/sensor.rest/) turns
`health` into an entity automations can watch — a light that goes amber when a
proposal is waiting, say:

```yaml
sensor:
  - platform: rest
    name: homelab_helper
    resource: http://ubuntu-dev.lan:8710/status
    scan_interval: 120
    value_template: "{{ value_json.health }}"
    json_attributes_path: "$"
    json_attributes: [headline, findings, proposals, discovery, trust]
```
