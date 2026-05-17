"""CLI layer — Typer-based ``helper`` command.

The CLI is the primary interaction surface for Phase 1. Verbs (per
``architecture.md``):

    helper discover network <cidr>
    helper discover host <name>
    helper audit
    helper findings [show|ack|resolve|suppress]
    helper inventory
    helper host show <name>
    helper assert run <name>
    helper agent enroll <hostname>
    helper netbox bootstrap
    helper db init|reset|status
    helper probes register
    helper config get|set
    helper version

Subcommands are organized into Typer sub-apps in :mod:`homelab_helper.cli.*`
and wired into the top-level app in :mod:`homelab_helper.cli.main`.
"""
