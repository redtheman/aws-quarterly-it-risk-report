"""Lambda entry point: quarterly IT-risk evidence pack.

Event (all optional; defaults to the last *completed* fiscal quarter):
    {"fy_start_year": 2026, "quarter": 1}
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

import boto3

from report import collectors
from report.quarters import FiscalQuarter, fixed_offset, last_completed_quarter
from report.render import Thresholds, build_pack, utc_now_iso

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def _config() -> dict:
    env = os.environ
    return {
        "bucket": env["REPORT_BUCKET"],
        "prefix": env.get("REPORT_PREFIX", "it-risk-reports").strip("/"),
        "regions": [r.strip() for r in env.get("REGIONS", env.get("AWS_REGION", "us-east-1")).split(",") if r.strip()],
        "fy_start_month": int(env.get("FY_START_MONTH", "7")),
        "tz": fixed_offset(float(env.get("REPORT_UTC_OFFSET_HOURS", "3"))),
        "topic_arn": env.get("SNS_TOPIC_ARN") or None,
        "thresholds": Thresholds(
            backup_success_target_pct=float(env.get("BACKUP_SUCCESS_TARGET_PCT", "99")),
            cpu_high_p95_pct=float(env.get("CPU_HIGH_P95_PCT", "80")),
            cpu_low_avg_pct=float(env.get("CPU_LOW_AVG_PCT", "10")),
            storage_high_pct=float(env.get("STORAGE_HIGH_PCT", "80")),
            min_backup_retention_days=int(env.get("MIN_BACKUP_RETENTION_DAYS", "7")),
        ),
    }


def resolve_quarter(event: dict, fy_start_month: int, tz) -> FiscalQuarter:
    if event and "fy_start_year" in event and "quarter" in event:
        return FiscalQuarter(int(event["fy_start_year"]), int(event["quarter"]), fy_start_month, tz)
    return last_completed_quarter(datetime.now(UTC), fy_start_month, tz)


def handler(event, context):
    cfg = _config()
    q = resolve_quarter(event or {}, cfg["fy_start_month"], cfg["tz"])
    start, end = q.start, q.end
    log.info("Building evidence pack for %s (%s to %s)", q.label, start, end)

    session = boto3.session.Session()
    account_id = session.client("sts").get_caller_identity()["Account"]

    backups, restores, ec2_rows, rds_rows = [], [], [], []
    for region in cfg["regions"]:
        backup = session.client("backup", region_name=region)
        cw = session.client("cloudwatch", region_name=region)
        backups += collectors.collect_backup_jobs(backup, region, start, end)
        restores += collectors.collect_restore_jobs(backup, region, start, end)
        ec2_rows += collectors.collect_ec2(session.client("ec2", region_name=region), cw, region, start, end)
        rds_rows += collectors.collect_rds(session.client("rds", region_name=region), cw, region, start, end)

    meta = {
        "quarter_label": q.label,
        "account_id": account_id,
        "regions": cfg["regions"],
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "generated_at": utc_now_iso(),
    }
    backup_summary = collectors.summarise_backups(backups)
    files, findings = build_pack(meta, backups, restores, ec2_rows, rds_rows, backup_summary, cfg["thresholds"])

    s3 = session.client("s3")
    key_prefix = f"{cfg['prefix']}/{account_id}/{q.slug}/{meta['generated_at'].replace(':', '')}"
    content_types = {".html": "text/html", ".json": "application/json", ".csv": "text/csv"}
    for name, body in files.items():
        ctype = next((v for k, v in content_types.items() if name.endswith(k)), "text/plain")
        s3.put_object(Bucket=cfg["bucket"], Key=f"{key_prefix}/{name}", Body=body,
                      ContentType=f"{ctype}; charset=utf-8", ServerSideEncryption="AES256")

    result = {
        "quarter": q.label,
        "s3_prefix": f"s3://{cfg['bucket']}/{key_prefix}/",
        "backup_success_rate_pct": backup_summary["success_rate_pct"],
        "findings": {s: sum(1 for f in findings if f["severity"] == s) for s in ("High", "Medium", "Low")},
        "ec2_instances": len(ec2_rows),
        "rds_instances": len(rds_rows),
    }

    if cfg["topic_arn"]:
        lines = [
            f"IT Risk Evidence Pack ready: {q.label} (account {account_id})",
            "",
            f"Backup success rate: {result['backup_success_rate_pct']}%",
            (
                f"Findings: {result['findings']['High']} high, {result['findings']['Medium']} medium, "
                f"{result['findings']['Low']} low"
            ),
            f"EC2 instances: {len(ec2_rows)} | RDS instances: {len(rds_rows)}",
            "",
            f"Evidence: {result['s3_prefix']}  (open summary.html)",
        ]
        session.client("sns").publish(
            TopicArn=cfg["topic_arn"], Subject=f"IT Risk Evidence Pack: {q.label}"[:100], Message="\n".join(lines)
        )

    log.info(json.dumps(result))
    return result
