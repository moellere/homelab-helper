# CLI reference

Every verb of `helper`, generated from the command definitions themselves so it
cannot drift from what `--help` says. Verbs are grouped the way the tool groups
them; each group's own `--help` lists its subcommands.

A few conventions hold across all of them:

- **Discovery is a read.** `discover <source>` reads a source and prints what it
  saw; `--persist` writes it into the harness database and `--dry-run` previews
  that write.
- **Planners are reports.** `plan`, `bottlenecks` and friends print a
  deterministic analysis; `--narrate` adds prose from a model, `--persist`
  records hits as findings with the standard lifecycle.
- **Execution is gated.** `exec run` is the only path that changes the lab, and
  it runs through [the trust gradient](trust-gradient.md) every time.

::: mkdocs-click
    :module: homelab_helper.cli.main
    :command: click_app
    :prog_name: helper
    :depth: 1
    :style: table
