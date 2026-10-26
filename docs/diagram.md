# Diagram of the lab

`helper diagram` draws what runs where from the rows discovery has already
persisted. It contacts nothing, so it is safe to run anywhere the database is
reachable, and it draws an empty page for a lab that has not been discovered.

```bash
helper diagram                          # Mermaid to stdout: paste into a README or a docs page
helper diagram --format html -o lab.html   # a standalone page (mermaid.js from a CDN, pinned)
helper diagram --format json             # the graph: sites, nodes, edges, stats
helper diagram --site wyola              # one site only
helper diagram --no-services --no-guests # hosts and clusters alone
```

The MCP tool `diagram(site=None, format="mermaid")` returns the same text with
the stats, so an agent can embed the current picture in a page or a message.

## What is drawn

| Element | Source | Drawn as |
|---|---|---|
| Site | the operator's [network topology file](cli.md) (`HOMELAB_HELPER_NETWORK_TOPOLOGY`), else the `/24` of the host's primary IP; `unplaced` when there is neither | a subgraph |
| Host | `Host` rows, retired hosts left out | a box: hostname, primary IP, guests, open-finding count |
| Guest | `VirtualMachine` rows with a node host, templates left out | a line inside the node's box (the first 6 names, then `+N more`) |
| Cluster | `Cluster` rows the hosts are nodes of, by the same rule the placement planners use | a cylinder, with a `node of` edge from each member host |
| Service | `ServiceEndpoint` rows whose IP is a host's primary IP, minus records that only restate the host (its own name, or a UniFi DHCP lease name such as `bmax1-b4-be`) | a stadium, with one edge per resolver: `internal via unifi` solid, `external via cloudflare` dashed |
| Findings | open `ReconciliationFinding` rows that affect the host | a count on the host box |

Workloads are not in the picture: the harness reads them live from Kubernetes
rather than persisting them, so they belong to `helper plan workloads` and the
`list_workloads` tool.

## Rendering

Mermaid renders in GitHub, in this site (mkdocs-material), and in the Claude
artifact viewer. The HTML page loads `mermaid@11.4.1` from jsDelivr and keeps
the source in a collapsed block for copying. The JSON form is the stable
contract for anything that wants to draw its own picture: `sites` (site →
host node ids), `nodes` (`id`, `kind`, `label`, `lines`, `site`), `edges`
(`from`, `to`, `label`, `style`) and `stats`.
