"""Read-only collectors for AWS Backup, EC2 and RDS evidence.

Every function takes an already-built boto3 client so it can be unit-tested
with ``botocore.stub.Stubber``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

# CloudWatch keeps 1-hour datapoints for 455 days but 5-minute datapoints for
# only 63 days, so hourly resolution is the finest that is reliably available
# for a whole past quarter.
HOURLY = 3600
MAX_QUERIES_PER_CALL = 500

FINISHED_BACKUP_STATES = {"COMPLETED", "FAILED", "ABORTED", "EXPIRED", "PARTIAL"}


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def _name_tag(tags: list[dict] | None) -> str:
    for tag in tags or []:
        if tag.get("Key") == "Name":
            return tag.get("Value", "")
    return ""


def _chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# --------------------------------------------------------------------------- #
# AWS Backup
# --------------------------------------------------------------------------- #
def collect_backup_jobs(backup, region: str, start: datetime, end: datetime) -> list[dict]:
    jobs = []
    for page in backup.get_paginator("list_backup_jobs").paginate(
        ByCreatedAfter=start, ByCreatedBefore=end
    ):
        for j in page.get("BackupJobs", []):
            jobs.append(
                {
                    "region": region,
                    "backup_job_id": j.get("BackupJobId"),
                    "vault": j.get("BackupVaultName"),
                    "resource_type": j.get("ResourceType"),
                    "resource_arn": j.get("ResourceArn"),
                    "state": j.get("State"),
                    "status_message": j.get("StatusMessage", ""),
                    "created": _iso(j.get("CreationDate")),
                    "completed": _iso(j.get("CompletionDate")),
                    "backup_size_bytes": j.get("BackupSizeInBytes", 0),
                }
            )
    return jobs


def collect_restore_jobs(backup, region: str, start: datetime, end: datetime) -> list[dict]:
    """Restore jobs are evidence that backups are actually recoverable."""
    jobs = []
    for page in backup.get_paginator("list_restore_jobs").paginate(
        ByCreatedAfter=start, ByCreatedBefore=end
    ):
        for j in page.get("RestoreJobs", []):
            jobs.append(
                {
                    "region": region,
                    "restore_job_id": j.get("RestoreJobId"),
                    "resource_type": j.get("ResourceType"),
                    "recovery_point_arn": j.get("RecoveryPointArn"),
                    "status": j.get("Status"),
                    "status_message": j.get("StatusMessage", ""),
                    "created": _iso(j.get("CreationDate")),
                    "completed": _iso(j.get("CompletionDate")),
                }
            )
    return jobs


def summarise_backups(jobs: Iterable[dict]) -> dict:
    """Success rate overall and per resource type, over *finished* jobs only."""
    per_type: dict[str, dict[str, int]] = {}
    for job in jobs:
        state = job["state"]
        if state not in FINISHED_BACKUP_STATES:
            continue  # still RUNNING/CREATED/PENDING at report time
        bucket = per_type.setdefault(job["resource_type"] or "Unknown", {"total": 0, "completed": 0})
        bucket["total"] += 1
        bucket["completed"] += state == "COMPLETED"

    def rate(c: int, t: int) -> float | None:
        return round(100 * c / t, 2) if t else None

    total = sum(b["total"] for b in per_type.values())
    completed = sum(b["completed"] for b in per_type.values())
    return {
        "finished_jobs": total,
        "completed_jobs": completed,
        "unsuccessful_jobs": total - completed,
        "success_rate_pct": rate(completed, total),
        "by_resource_type": {
            k: {**v, "success_rate_pct": rate(v["completed"], v["total"])}
            for k, v in sorted(per_type.items())
        },
    }


# --------------------------------------------------------------------------- #
# CloudWatch helpers
# --------------------------------------------------------------------------- #
def summarise_series(values: list[float]) -> dict:
    if not values:
        return {"datapoints": 0, "avg": None, "p95": None, "max": None, "min": None}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
    return {
        "datapoints": len(values),
        "avg": round(sum(values) / len(values), 2),
        "p95": round(p95, 2),
        "max": round(ordered[-1], 2),
        "min": round(ordered[0], 2),
    }


def fetch_metric_series(cloudwatch, specs: list[dict], start: datetime, end: datetime,
                        period: int = HOURLY) -> dict[str, list[float]]:
    """Fetch many metric series with as few GetMetricData calls as possible.

    ``specs`` items: ``{"key", "namespace", "metric", "dimensions", "stat"}``.
    Returns ``{key: [values...]}``.
    """
    out: dict[str, list[float]] = {s["key"]: [] for s in specs}
    for chunk in _chunks(specs, MAX_QUERIES_PER_CALL):
        id_to_key = {}
        queries = []
        for i, spec in enumerate(chunk):
            qid = f"q{i}"
            id_to_key[qid] = spec["key"]
            queries.append(
                {
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": spec["namespace"],
                            "MetricName": spec["metric"],
                            "Dimensions": spec["dimensions"],
                        },
                        "Period": period,
                        "Stat": spec.get("stat", "Average"),
                    },
                    "ReturnData": True,
                }
            )
        for page in cloudwatch.get_paginator("get_metric_data").paginate(
            MetricDataQueries=queries, StartTime=start, EndTime=end, ScanBy="TimestampAscending"
        ):
            for result in page.get("MetricDataResults", []):
                out[id_to_key[result["Id"]]].extend(result.get("Values", []))
    return out


# --------------------------------------------------------------------------- #
# EC2
# --------------------------------------------------------------------------- #
def collect_ec2(ec2, cloudwatch, region: str, start: datetime, end: datetime) -> list[dict]:
    instances = []
    for page in ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]
    ):
        for reservation in page.get("Reservations", []):
            instances.extend(reservation.get("Instances", []))

    specs = [
        {
            "key": inst["InstanceId"],
            "namespace": "AWS/EC2",
            "metric": "CPUUtilization",
            "dimensions": [{"Name": "InstanceId", "Value": inst["InstanceId"]}],
            "stat": "Average",
        }
        for inst in instances
    ]
    series = fetch_metric_series(cloudwatch, specs, start, end) if specs else {}

    rows = []
    for inst in instances:
        cpu = summarise_series(series.get(inst["InstanceId"], []))
        rows.append(
            {
                "region": region,
                "instance_id": inst["InstanceId"],
                "name": _name_tag(inst.get("Tags")),
                "instance_type": inst.get("InstanceType"),
                "platform": inst.get("PlatformDetails", ""),
                "state": inst.get("State", {}).get("Name"),
                "cpu_hours_observed": cpu["datapoints"],
                "cpu_avg_pct": cpu["avg"],
                "cpu_p95_pct": cpu["p95"],
                "cpu_max_pct": cpu["max"],
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# RDS
# --------------------------------------------------------------------------- #
def collect_rds(rds, cloudwatch, region: str, start: datetime, end: datetime) -> list[dict]:
    dbs = []
    for page in rds.get_paginator("describe_db_instances").paginate():
        dbs.extend(page.get("DBInstances", []))

    specs = []
    for db in dbs:
        dim = [{"Name": "DBInstanceIdentifier", "Value": db["DBInstanceIdentifier"]}]
        ident = db["DBInstanceIdentifier"]
        specs += [
            {"key": f"{ident}|cpu", "namespace": "AWS/RDS", "metric": "CPUUtilization", "dimensions": dim, "stat": "Average"},
            {"key": f"{ident}|conn", "namespace": "AWS/RDS", "metric": "DatabaseConnections", "dimensions": dim, "stat": "Average"},
            {"key": f"{ident}|mem", "namespace": "AWS/RDS", "metric": "FreeableMemory", "dimensions": dim, "stat": "Minimum"},
            {"key": f"{ident}|disk", "namespace": "AWS/RDS", "metric": "FreeStorageSpace", "dimensions": dim, "stat": "Minimum"},
        ]
    series = fetch_metric_series(cloudwatch, specs, start, end) if specs else {}

    rows = []
    for db in dbs:
        ident = db["DBInstanceIdentifier"]
        cpu = summarise_series(series.get(f"{ident}|cpu", []))
        conn = summarise_series(series.get(f"{ident}|conn", []))
        mem = series.get(f"{ident}|mem", [])
        disk = series.get(f"{ident}|disk", [])

        allocated_gib = db.get("AllocatedStorage") or 0
        is_aurora = str(db.get("Engine", "")).startswith("aurora")
        peak_storage_used_pct = None
        if disk and allocated_gib and not is_aurora:
            min_free_gib = min(disk) / 1024**3
            peak_storage_used_pct = round(100 * (1 - min_free_gib / allocated_gib), 2)

        rows.append(
            {
                "region": region,
                "db_instance": ident,
                "engine": f'{db.get("Engine")} {db.get("EngineVersion", "")}'.strip(),
                "instance_class": db.get("DBInstanceClass"),
                "multi_az": db.get("MultiAZ", False),
                "status": db.get("DBInstanceStatus"),
                "cpu_avg_pct": cpu["avg"],
                "cpu_p95_pct": cpu["p95"],
                "cpu_max_pct": cpu["max"],
                "connections_avg": conn["avg"],
                "connections_max": conn["max"],
                "min_freeable_memory_mib": round(min(mem) / 1024**2, 1) if mem else None,
                "allocated_storage_gib": allocated_gib if not is_aurora else None,
                "peak_storage_used_pct": peak_storage_used_pct,
                "backup_retention_days": db.get("BackupRetentionPeriod"),
                "storage_encrypted": db.get("StorageEncrypted", False),
            }
        )
    return rows
