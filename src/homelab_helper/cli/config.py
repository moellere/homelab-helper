"""`helper config` — show the effective harness configuration and its sources.

Read-only inspection: where the DB lives, which ``.env`` is in play, and which
discovery sources have credentials. Secrets are reported as set/unset, never
printed. This is the "what is the harness actually going to do" view — handy
before a discover/sync run, and the first thing to check when a source errors
out with a credentials message.

``helper config init`` scaffolds the per-user ``.env`` (every variable listed,
commented out) so an installed ``helper`` has somewhere to keep credentials
that isn't a repo checkout.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from homelab_helper.config import SOURCES, config_dir, config_status

config_app = typer.Typer(
    name="config",
    help="Show effective configuration and its sources.",
    no_args_is_help=False,
    invoke_without_command=True,
)

console = Console()

_GENERAL_VARS: tuple[tuple[str, str], ...] = (
    ("HOMELAB_HELPER_DATABASE_URL", "sqlite+aiosqlite:///<data dir>/homelab.db by default"),
    ("HOMELAB_HELPER_SSH_KEY", "private key for `discover host` and the MCP probe_host tool"),
    ("HOMELAB_HELPER_OPERATOR", "operator identity recorded on trust grants (OS user by default)"),
    (
        "HOMELAB_HELPER_MCP_PROBE_ALLOW",
        "hostname/IP globs the MCP probes may reach beyond known hosts",
    ),
    (
        "HOMELAB_HELPER_NETWORK_TOPOLOGY",
        "topology YAML (see fixtures/network-topology.example.yaml)",
    ),
    (
        "HOMELAB_HELPER_WORKLOAD_LIBRARY",
        "operator workload profiles layered over the starter library",
    ),
    ("HOMELAB_HELPER_AGE_IDENTITY", "age key for file:<path.age>#<key> secret references"),
)

_LLM_VARS: tuple[tuple[str, str], ...] = (
    ("HOMELAB_HELPER_LLM_PRIVACY", "strict-local | prefer-local (default) | open"),
    ("HOMELAB_HELPER_OLLAMA_URL", "http://localhost:11434"),
    ("HOMELAB_HELPER_OLLAMA_MODEL", "model name served by Ollama"),
    ("HOMELAB_HELPER_OLLAMA_TIER", "tiny | small | mid | frontier"),
    ("HOMELAB_HELPER_ANTHROPIC_API_KEY", "BYOK cloud backend (secret)"),
    ("HOMELAB_HELPER_ANTHROPIC_MODEL", ""),
    ("HOMELAB_HELPER_OPENAI_API_KEY", "BYOK cloud backend (secret)"),
    ("HOMELAB_HELPER_OPENAI_MODEL", ""),
    ("HOMELAB_HELPER_OPENAI_COMPAT_BASE_URL", "any OpenAI-compatible server"),
    ("HOMELAB_HELPER_OPENAI_COMPAT_API_KEY", "(secret)"),
    ("HOMELAB_HELPER_OPENAI_COMPAT_MODEL", ""),
    ("HOMELAB_HELPER_OPENAI_COMPAT_TIER", "tiny | small | mid | frontier"),
)


def render_env_template() -> str:
    """The ``.env`` scaffold: every variable the harness reads, commented out."""
    lines = [
        "# homelab-helper configuration. Uncomment and fill what you use.",
        "# Loaded after a project .env (repo checkout) and before ~/.env;",
        "# an exported variable always wins. Keep this file private.",
        "#",
        "# Any secret may be a reference instead of a literal:",
        "#   file:~/.config/homelab-helper/secrets.yaml#proxmox_token   (plain, .age, or .sops.*)",
        "#   keyring:homelab-helper/proxmox                              (OS keyring; [keyring] extra)",
        "#   env:OTHER_VARIABLE",
        "",
        "# --- general",
    ]
    for var, note in _GENERAL_VARS:
        lines.append(f"# {var}=    # {note}" if note else f"# {var}=")
    for source in SOURCES:
        lines.append("")
        header = f"# --- {source.name}"
        if source.note:
            header += f"  ({source.note})"
        lines.append(header)
        for var in (*source.required, *source.optional):
            tag = "  # secret" if var in source.secret else ""
            need = "" if var in source.required else "  # optional"
            lines.append(f"# {var}={tag}{need}")
    lines.append("")
    lines.append("# --- llm (chat, narration, planning)")
    for var, note in _LLM_VARS:
        lines.append(f"# {var}=    # {note}" if note else f"# {var}=")
    lines.append("")
    return "\n".join(lines)


def _render_general(status: dict) -> Table:
    table = Table(title="homelab-helper configuration")
    table.add_column("setting")
    table.add_column("value", overflow="fold")

    table.add_row("database url", status["database_url"])
    if status["database_exists"] is not None:
        table.add_row(
            "database state",
            "file exists"
            if status["database_exists"]
            else "not initialized (run `helper db init`)",
        )
    table.add_row("data dir", status["data_dir"])
    table.add_row("config dir", status["config_dir"])
    table.add_row(
        "env files",
        ", ".join(status["env_files"]) or "(none found; run `helper config init`)",
    )
    table.add_row("ssh key", status["ssh_key"] or "(unset)")
    table.add_row(
        "mcp probe allow",
        ", ".join(status["mcp_probe_allow"]) or "(unset: known hosts only)",
    )
    return table


_VAR_PREFIX = "HOMELAB_HELPER_"


def _short(var: str) -> str:
    return var.removeprefix(_VAR_PREFIX).lower()


def _detail(row: dict) -> str:
    """Configured -> what it points at; unconfigured -> what's missing.

    Secrets render as ``set`` — their values are never available here.
    """
    if not row["configured"]:
        return "missing: " + ", ".join(_short(v) for v in row["missing"])
    if row.get("controllers"):
        return "controllers: " + ", ".join(row["controllers"])
    bits = [
        f"{_short(v['variable'])}="
        + (
            ("set" + (f" via {v['reference']}" if v.get("reference") else ""))
            if v["secret"]
            else str(v["value"])
        )
        for v in row["variables"]
        if v["set"]
    ]
    if row["note"]:
        bits.append(f"[dim]{row['note']}[/dim]")
    return ", ".join(bits)


def _render_sources(status: dict) -> Table:
    table = Table(title="discovery sources")
    table.add_column("source", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("detail", overflow="fold")

    by_name = {s["source"]: s for s in status["sources"]}
    for source in SOURCES:
        row = by_name[source.name]
        state = "[green]ready[/green]" if row["configured"] else "[yellow]not configured[/yellow]"
        table.add_row(source.name, state, _detail(row))
    return table


def _render_llm(llm: dict) -> Table:
    table = Table(title=f"llm backends (privacy: {llm.get('privacy', '?')})")
    table.add_column("backend", no_wrap=True)
    table.add_column("model", overflow="fold")
    table.add_column("tier", no_wrap=True)
    table.add_column("origin", no_wrap=True)
    for b in llm.get("backends", []):
        table.add_row(b["backend"], b["model"], b["tier"], "local" if b["local"] else "cloud")
    return table


@config_app.command(name="init")
def config_init(
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file."),
    path: Path | None = typer.Option(
        None, "--path", help="Write here instead of the config directory."
    ),
) -> None:
    """Write a commented .env template to the per-user config directory."""
    target = path or (config_dir() / ".env")
    if target.exists() and not force:
        console.print(f"[yellow]already exists[/yellow] (use --force to overwrite): {target}")
        raise typer.Exit(code=1)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_env_template())
    with contextlib.suppress(OSError):  # filesystems without POSIX modes
        target.chmod(0o600)
    console.print(f"[green]wrote[/green] {target}")
    console.print("[dim]Uncomment the sources you use, then `helper config` to check.[/dim]")


@config_app.callback(invoke_without_command=True)
def config(ctx: typer.Context) -> None:
    """Print the effective configuration."""
    if ctx.invoked_subcommand is not None:
        return
    status = config_status()
    console.print(_render_general(status))
    console.print(_render_sources(status))
    llm = status["llm"]
    if "error" in llm:
        console.print(f"[red]llm config error:[/red] {llm['error']}")
    else:
        console.print(_render_llm(llm))

    ready = status["configured_sources"]
    blocked = [s for s in status["unconfigured_sources"] if s != "netbox"]
    console.print(f"[dim]{len(ready)} source(s) ready: {', '.join(ready) or 'none'}[/dim]")
    if blocked:
        console.print(
            f"[dim]Set the missing variables in {status['config_env_file']} "
            "(`helper config init` scaffolds it), a project .env, or the environment.[/dim]"
        )


__all__ = ["config_app", "render_env_template"]
