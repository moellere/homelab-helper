"""Workload usage (Phase 11.4) — metrics-server readings as usage history for baselines.

What is load-bearing: pods are attributed to their Deployment / StatefulSet /
DaemonSet (a ReplicaSet's hash stripped), Jobs and bare pods are left out,
usage and declared resources are summed per workload, each reading becomes one
hourly ``workload`` sample, and the baseline engine judges those samples like a
guest's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from homelab_helper.adapters.kubernetes import (
    K8sAdapter,
    K8sConfig,
    parse_cpu_cores,
    pod_workload,
    workload_usage,
)
from homelab_helper.db.base import Base
from homelab_helper.db.models import UsageSample
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine import baselines
from homelab_helper.engine.usage import HOUR, WORKLOAD, record_workload_usage, workload_bucket


def _pod(
    ns: str,
    name: str,
    owner_kind: str,
    owner_name: str,
    *,
    hash_: str | None = None,
    phase: str = "Running",
    limits: dict[str, str] | None = None,
    requests: dict[str, str] | None = None,
) -> dict[str, Any]:
    labels = {"pod-template-hash": hash_} if hash_ else {}
    resources: dict[str, Any] = {}
    if limits:
        resources["limits"] = limits
    if requests:
        resources["requests"] = requests
    return {
        "metadata": {
            "namespace": ns,
            "name": name,
            "labels": labels,
            "ownerReferences": [{"kind": owner_kind, "name": owner_name}],
        },
        "spec": {"containers": [{"name": "c", "resources": resources}]},
        "status": {"phase": phase},
    }


def _metric(ns: str, name: str, cpu: str, memory: str) -> dict[str, Any]:
    return {
        "metadata": {"namespace": ns, "name": name},
        "containers": [{"name": "c", "usage": {"cpu": cpu, "memory": memory}}],
    }


@pytest.mark.parametrize(
    ("raw", "cores"),
    [("250m", 0.25), ("3456789n", 0.003456789), ("2", 2.0), ("1500u", 0.0015), (None, None)],
)
def test_parse_cpu_cores(raw: str | None, cores: float | None) -> None:
    got = parse_cpu_cores(raw)
    assert got == cores if cores is None else got == pytest.approx(cores)


def test_pods_are_attributed_to_their_workload() -> None:
    assert pod_workload(
        _pod("web", "api-7d9f8b6c4-x1", "ReplicaSet", "api-7d9f8b6c4", hash_="7d9f8b6c4")
    ) == (
        "web",
        "deployment",
        "api",
    )
    assert pod_workload(_pod("db", "pg-0", "StatefulSet", "pg")) == ("db", "statefulset", "pg")
    assert pod_workload(_pod("kube-system", "cni-abc", "DaemonSet", "cni")) == (
        "kube-system",
        "daemonset",
        "cni",
    )
    assert pod_workload(_pod("batch", "backup-1", "Job", "backup-1")) is None
    assert pod_workload({"metadata": {"namespace": "x", "name": "bare"}}) is None


def test_usage_and_resources_are_summed_per_workload() -> None:
    pods = [
        _pod(
            "web",
            "api-h1-a",
            "ReplicaSet",
            "api-h1",
            hash_="h1",
            limits={"cpu": "500m", "memory": "256Mi"},
        ),
        _pod(
            "web",
            "api-h1-b",
            "ReplicaSet",
            "api-h1",
            hash_="h1",
            limits={"cpu": "500m", "memory": "256Mi"},
        ),
        _pod("web", "api-h1-c", "ReplicaSet", "api-h1", hash_="h1", phase="Pending"),
        _pod("db", "pg-0", "StatefulSet", "pg", requests={"cpu": "1"}),
        _pod("batch", "backup-1", "Job", "backup-1"),
    ]
    metrics = [
        _metric("web", "api-h1-a", "100m", "100Mi"),
        _metric("web", "api-h1-b", "50000000n", "50Mi"),
        _metric("db", "pg-0", "2", "1Gi"),
    ]
    rows = {(r["namespace"], r["kind"], r["name"]): r for r in workload_usage(pods, metrics)}
    assert set(rows) == {("web", "deployment", "api"), ("db", "statefulset", "pg")}
    api = rows[("web", "deployment", "api")]
    assert api["pods"] == 2, "the pending pod is not counted"
    assert api["cpu_cores"] == pytest.approx(0.15)
    assert api["mem_bytes"] == 150 * 1024**2
    assert api["cpu_limit"] == pytest.approx(1.0)
    assert api["mem_limit"] == 512 * 1024**2
    assert api["cpu_request"] is None, "no request set anywhere"
    pg = rows[("db", "statefulset", "pg")]
    assert pg["cpu_limit"] is None
    assert pg["cpu_request"] == pytest.approx(1.0)
    assert pg["mem_limit"] is None


def test_the_bucket_is_a_fraction_of_the_declared_cpu() -> None:
    now = datetime(2026, 10, 12, 13, 42, tzinfo=UTC)
    limited = workload_bucket(
        {"cpu_cores": 0.25, "cpu_limit": 1.0, "cpu_request": 0.5, "mem_bytes": 10, "mem_limit": 20},
        now,
    )
    assert limited.ts == datetime(2026, 10, 12, 13, 0)
    assert limited.values["cpu"] == pytest.approx(0.25)
    assert limited.values["cpus"] == 1.0
    assert limited.values["mem_total"] == 20
    requested = workload_bucket({"cpu_cores": 0.25, "cpu_limit": None, "cpu_request": 0.5}, now)
    assert requested.values["cpu"] == pytest.approx(0.5)
    bare = workload_bucket({"cpu_cores": 0.25, "cpu_limit": None, "cpu_request": None}, now)
    assert bare.values["cpu"] == pytest.approx(0.25)
    assert bare.values["cpus"] == 1.0
    assert "mem_total" not in bare.values


async def test_adapter_reads_pods_and_metrics_with_the_exact_argv() -> None:
    seen: list[list[str]] = []

    async def runner(args, timeout_s):  # noqa: ANN001, ARG001
        seen.append(list(args))
        return 0, '{"items": [{"metadata": {"name": "p"}}]}', ""

    adapter = K8sAdapter(K8sConfig(context="lab"), runner=runner)
    assert await adapter.list_pods() == [{"metadata": {"name": "p"}}]
    assert await adapter.pod_metrics() == [{"metadata": {"name": "p"}}]
    assert seen == [
        ["--context", "lab", "get", "pods", "-A", "-o", "json"],
        ["--context", "lab", "get", "--raw", "/apis/metrics.k8s.io/v1beta1/pods"],
    ]


@pytest.fixture
async def sessionmaker():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    await engine.dispose()


async def test_readings_become_hourly_workload_samples_and_baselines_judge_them(
    sessionmaker,
) -> None:
    row = {
        "namespace": "web",
        "kind": "deployment",
        "name": "api",
        "pods": 2,
        "cpu_cores": 0.2,
        "mem_bytes": 100 * 1024**2,
        "cpu_limit": 1.0,
        "cpu_request": None,
        "mem_limit": None,
    }
    end = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    hours = baselines.MIN_HISTORY_HOURS + 6
    async with session_scope(sessionmaker) as s:
        for i in range(hours):
            moment = end - timedelta(hours=hours - i)
            quiet = {**row, "cpu_cores": 0.2 if i < hours - 3 else 0.9}
            await record_workload_usage(s, [quiet], now=moment)
        samples = (
            (await s.execute(select(UsageSample).where(UsageSample.subject_type == WORKLOAD)))
            .scalars()
            .all()
        )
        assert len(samples) == hours
        assert samples[0].subject_key == "web/deployment/api"
        assert samples[0].resolution == HOUR
        issues, _observed, judged = await baselines.evaluate(s, now=end)
        assert ("workload", "web/deployment/api") in judged
        assert any(
            i.category == "cpu-above-baseline" and i.target_type == "workload" for i in issues
        )


async def test_a_second_reading_in_the_same_hour_replaces_the_first(sessionmaker) -> None:
    row = {
        "namespace": "web",
        "kind": "deployment",
        "name": "api",
        "cpu_cores": 0.2,
        "cpu_limit": 1.0,
        "mem_bytes": 1,
    }
    t = datetime(2026, 10, 12, 12, 5, tzinfo=UTC)
    async with session_scope(sessionmaker) as s:
        first = await record_workload_usage(s, [row], now=t)
        second = await record_workload_usage(
            s, [{**row, "cpu_cores": 0.4}], now=t + timedelta(minutes=30)
        )
        assert (first.inserted, second.inserted, second.updated) == (1, 0, 1)
        sample = (await s.execute(select(UsageSample))).scalar_one()
        assert sample.cpu == pytest.approx(0.4)
