"""One place that builds each adapter from the environment.

The CLI, the daemon and the MCP server used to keep their own copies of these
factories, and the copies drifted. Callers reach them through this module
(``factories.proxmox()``), so a test that replaces ``factories.proxmox``
replaces it for every path at once.

The ``*_if_*`` variants answer ``None`` where an action can go ahead without
that adapter (no kubectl on this machine, Argo CD not configured, no UniFi
controller of that name); the plain ones raise the adapter's config error.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homelab_helper.adapters.argocd import ArgoCDAdapter, ArgoCDConfigError
from homelab_helper.adapters.cloudflare import CloudflareAdapter
from homelab_helper.adapters.homeassistant import HomeAssistantAdapter, HomeAssistantConfig
from homelab_helper.adapters.jetkvm import JetKVMAdapter
from homelab_helper.adapters.kernel_ssh import KernelSSHAdapter
from homelab_helper.adapters.kubernetes import K8sAdapter
from homelab_helper.adapters.mikrotik import MikroTikAdapter
from homelab_helper.adapters.netbox import NetBoxAdapter, NetBoxConfig
from homelab_helper.adapters.openmediavault import OpenMediaVaultAdapter
from homelab_helper.adapters.printwarden import PrintWardenAdapter
from homelab_helper.adapters.proxmox import ProxmoxAdapter, ProxmoxConfigError
from homelab_helper.adapters.talos import TalosAdapter
from homelab_helper.adapters.unifi import UniFiAdapter, UniFiConfig, UniFiConfigError

if TYPE_CHECKING:
    from collections.abc import Iterator


def proxmox() -> ProxmoxAdapter:
    return ProxmoxAdapter.from_env()


def netbox() -> NetBoxAdapter:
    return NetBoxAdapter(NetBoxConfig.from_env())


def k8s() -> K8sAdapter:
    return K8sAdapter.from_env()


def k8s_if_available() -> K8sAdapter | None:
    """The Kubernetes adapter, or ``None`` when kubectl is not on this machine."""
    return k8s() if shutil.which("kubectl") else None


def unifi() -> UniFiAdapter:
    return UniFiAdapter.from_env()


def unifi_all() -> list[UniFiAdapter]:
    """Every configured controller; the single-controller factory unless
    ``HOMELAB_HELPER_UNIFI_CONTROLLERS`` names more than one."""
    raw = os.environ.get("HOMELAB_HELPER_UNIFI_CONTROLLERS") or ""
    if not [n for n in raw.split(",") if n.strip()]:
        return [unifi()]
    return [UniFiAdapter(cfg) for cfg in UniFiConfig.all_from_env()]


def unifi_for(controller: str | None) -> UniFiAdapter | None:
    """The controller a DNS action names (or the only one); ``None`` if unconfigured."""
    try:
        adapters = unifi_all()
    except UniFiConfigError:
        return None
    if controller is None:
        return adapters[0] if len(adapters) == 1 else None
    return next((a for a in adapters if a.config.name == controller), None)


def cloudflare() -> CloudflareAdapter:
    return CloudflareAdapter.from_env()


def argocd() -> ArgoCDAdapter:
    return ArgoCDAdapter.from_env()


def argocd_if_configured() -> ArgoCDAdapter | None:
    try:
        return argocd()
    except ArgoCDConfigError:
        return None


def omv() -> OpenMediaVaultAdapter:
    return OpenMediaVaultAdapter.from_env()


def hass() -> HomeAssistantAdapter:
    return HomeAssistantAdapter.from_env()


def hass_all() -> list[HomeAssistantAdapter]:
    """Every configured Home Assistant instance; the single-instance factory unless
    ``HOMELAB_HELPER_HASS_INSTANCES`` names one or more."""
    raw = os.environ.get("HOMELAB_HELPER_HASS_INSTANCES") or ""
    if not [n for n in raw.split(",") if n.strip()]:
        return [hass()]
    return [HomeAssistantAdapter(cfg) for cfg in HomeAssistantConfig.all_from_env()]


def mikrotik() -> MikroTikAdapter:
    return MikroTikAdapter.from_env()


def jetkvm() -> JetKVMAdapter:
    return JetKVMAdapter.from_env()


def printwarden() -> PrintWardenAdapter:
    return PrintWardenAdapter.from_env()


@dataclass(frozen=True)
class ActionAdapters:
    """What the executor needs to run one action; the optional ones only when it does."""

    proxmox: ProxmoxAdapter
    k8s: K8sAdapter | None
    argocd: ArgoCDAdapter | None
    unifi: UniFiAdapter | None
    ssh: KernelSSHAdapter | None = None
    talos: TalosAdapter | None = None

    def __iter__(self) -> Iterator[Any]:
        return iter((self.proxmox, self.k8s, self.argocd, self.unifi, self.ssh, self.talos))


def for_action(manifest: Any) -> tuple[ActionAdapters | None, str | None]:
    """The adapters one manifest needs, or a message naming what is not configured.

    Shared by the MCP ``execute_proposal`` trigger, maintenance runs and the
    daemon's approval listener, so each resolves an action the same way.
    """
    try:
        pve = proxmox()
    except ProxmoxConfigError as exc:
        return None, f"Proxmox adapter: {exc}"
    argo = None
    dns = None
    if manifest.is_argocd:
        try:
            argo = argocd()
        except ArgoCDConfigError as exc:
            return None, f"Argo CD adapter: {exc}"
    if manifest.is_dns:
        dns = unifi_for(manifest.controller)
        if dns is None:
            return None, (
                f"UniFi adapter: no controller matches {manifest.controller!r} "
                "(set HOMELAB_HELPER_UNIFI_CONTROLLERS)"
            )
    ssh = KernelSSHAdapter() if manifest.is_node else None
    talos = TalosAdapter() if manifest.uses_talosctl else None
    return ActionAdapters(pve, k8s_if_available(), argo, dns, ssh, talos), None


__all__ = [
    "ActionAdapters",
    "argocd",
    "argocd_if_configured",
    "cloudflare",
    "for_action",
    "hass",
    "hass_all",
    "jetkvm",
    "k8s",
    "k8s_if_available",
    "mikrotik",
    "netbox",
    "omv",
    "printwarden",
    "proxmox",
    "unifi",
    "unifi_all",
    "unifi_for",
]
