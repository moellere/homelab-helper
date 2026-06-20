"""Virtualization models — Cluster + VirtualMachine (Phase 3).

First-class harness rows for what the management-plane adapters (Proxmox today,
K8s later) discover. NetBox owns the canonical virtualization objects; these
rows are the harness-side projection, carrying discovery state and the link back
to the NetBox object (``netbox_*_id``). A VM names the cluster node it runs on
(``node_name``); when that node is a known ``Host`` it's linked via
``node_host_id`` so VM placement can be reasoned about against hardware.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, Index, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from homelab_helper.db.base import Base, now, uuid7
from homelab_helper.db.enums import DiscoverySource

if TYPE_CHECKING:
    from .host import Host


class Cluster(Base):
    """A virtualization cluster (e.g. a Proxmox cluster, a K8s cluster)."""

    __tablename__ = "cluster"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    netbox_cluster_id: Mapped[int | None] = mapped_column(unique=True, index=True)

    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # "proxmox" | "kubernetes" | ...
    discovery_source: Mapped[DiscoverySource] = mapped_column(
        SAEnum(DiscoverySource), default=DiscoverySource.PROXMOX
    )

    quorate: Mapped[bool | None] = mapped_column(Boolean)
    node_count: Mapped[int | None] = mapped_column(Integer)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    discovery_last_run: Mapped[datetime | None] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    vms: Mapped[list[VirtualMachine]] = relationship(
        back_populates="cluster", cascade="all, delete-orphan"
    )


class VirtualMachine(Base):
    """A VM or container discovered on a cluster, keyed by ``(cluster, vmid)``."""

    __tablename__ = "virtual_machine"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    netbox_vm_id: Mapped[int | None] = mapped_column(unique=True, index=True)

    cluster_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cluster.id"), index=True)
    vmid: Mapped[int | None] = mapped_column(Integer, index=True)  # hypervisor-local id
    name: Mapped[str] = mapped_column(String(255), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # "qemu" | "lxc"
    status: Mapped[str | None] = mapped_column(String(32))  # "running" | "stopped" | ...
    template: Mapped[bool] = mapped_column(Boolean, default=False)

    # The cluster node this guest runs on; linked to a Host when that node is known.
    node_name: Mapped[str | None] = mapped_column(String(255), index=True)
    node_host_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("host.id"), index=True)

    vcpus: Mapped[int | None] = mapped_column(Integer)
    memory_bytes: Mapped[int | None] = mapped_column(BigInteger)
    disk_bytes: Mapped[int | None] = mapped_column(BigInteger)

    discovery_source: Mapped[DiscoverySource] = mapped_column(
        SAEnum(DiscoverySource), default=DiscoverySource.PROXMOX
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime] = mapped_column(default=now, onupdate=now)

    cluster: Mapped[Cluster] = relationship(back_populates="vms")
    node_host: Mapped[Host | None] = relationship()

    __table_args__ = (Index("ix_vm_cluster_vmid", "cluster_id", "vmid"),)


__all__ = ["Cluster", "VirtualMachine"]
