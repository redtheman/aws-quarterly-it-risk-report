"""Turn collected evidence into findings, CSVs, an HTML summary and a hash manifest."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape


@dataclass(frozen=True)
class Thresholds:
    backup_success_target_pct: float = 99.0
    cpu_high_p95_pct: float = 80.0
    cpu_low_avg_pct: float = 10.0
    storage_high_pct: float = 80.0
    min_backup_retention_days: int = 7


def build_findings(backup_summary: dict, restores: list[dict], ec2: list[dict], rds: list[dict],
                   t: Thresholds) -> list[dict]:
    f: list[dict] = []

    def add(severity: str, area: str, resource: str, detail: str) -> None:
        f.append({"severity": severity, "area": area, "resource": resource, "detail": detail})

    rate = backup_summary["success_rate_pct"]
    if rate is None:
        add("High", "Backup", "All", "No finished AWS Backup jobs in the period.")
    elif rate < t.backup_success_target_pct:
        add("High", "Backup", "All",
            f"Backup success rate {rate}% is below the {t.backup_success_target_pct}% target "
            f"({backup_summary['unsuccessful_jobs']} unsuccessful jobs).")
    if not any(r.get("status") == "COMPLETED" for r in restores):
        add("Medium", "Backup", "All", "No completed restore test in the period: recoverability is unevidenced.")

    for i in ec2:
        rid = f"{i['instance_id']} ({i['name']})" if i["name"] else i["instance_id"]
        if i["state"] == "running" and i["cpu_hours_observed"] == 0:
            add("Medium", "EC2", rid, "Running instance with no CPU metrics: monitoring gap.")
        if i["cpu_p95_pct"] is not None and i["cpu_p95_pct"] >= t.cpu_high_p95_pct:
            add("Medium", "EC2", rid, f"CPU p95 {i['cpu_p95_pct']}% (capacity risk).")
        elif i["state"] == "running" and i["cpu_avg_pct"] is not None and i["cpu_avg_pct"] < t.cpu_low_avg_pct:
            add("Low", "EC2", rid, f"CPU average {i['cpu_avg_pct']}%: candidate for rightsizing.")

    for d in rds:
        rid = d["db_instance"]
        if d["cpu_p95_pct"] is not None and d["cpu_p95_pct"] >= t.cpu_high_p95_pct:
            add("Medium", "RDS", rid, f"CPU p95 {d['cpu_p95_pct']}% (capacity risk).")
        if d["peak_storage_used_pct"] is not None and d["peak_storage_used_pct"] >= t.storage_high_pct:
            add("High", "RDS", rid, f"Peak storage use {d['peak_storage_used_pct']}% of allocated.")
        if (d["backup_retention_days"] or 0) < t.min_backup_retention_days:
            add("High", "RDS", rid,
                f"Automated backup retention {d['backup_retention_days']} days "
                f"(< {t.min_backup_retention_days}).")
        if not d["storage_encrypted"]:
            add("Medium", "RDS", rid, "Storage is not encrypted at rest.")

    order = {"High": 0, "Medium": 1, "Low": 2}
    return sorted(f, key=lambda x: (order[x["severity"]], x["area"], x["resource"]))


def to_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _table(rows: list[dict], columns: list[tuple[str, str]], empty: str) -> str:
    if not rows:
        return f"<p class='muted'>{escape(empty)}</p>"
    head = "".join(f"<th>{escape(label)}</th>" for _, label in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape('' if r.get(k) is None else str(r.get(k)))}</td>" for k, _ in columns) + "</tr>"
        for r in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(meta: dict, backup_summary: dict, findings: list[dict], backups: list[dict],
                restores: list[dict], ec2: list[dict], rds: list[dict]) -> bytes:
    failed = [b for b in backups if b["state"] in {"FAILED", "ABORTED", "EXPIRED", "PARTIAL"}][:25]
    by_type = [{"resource_type": k, **v} for k, v in backup_summary["by_resource_type"].items()]
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("High", "Medium", "Low")}
    rate = backup_summary["success_rate_pct"]

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>IT Risk Evidence Pack: {escape(meta['quarter_label'])}</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#1f2937;max-width:1100px;margin:32px auto;padding:0 16px}}
 h1{{margin:0 0 4px}} h2{{margin-top:32px;border-bottom:2px solid #e5e7eb;padding-bottom:4px}}
 .muted{{color:#6b7280}} .tiles{{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}}
 .tile{{border:1px solid #e5e7eb;border-radius:8px;padding:12px 16px;min-width:150px}}
 .tile b{{display:block;font-size:24px}}
 table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}}
 th,td{{border:1px solid #e5e7eb;padding:6px 8px;text-align:left}} th{{background:#f9fafb}}
 .High{{color:#b91c1c;font-weight:600}} .Medium{{color:#b45309;font-weight:600}} .Low{{color:#1d4ed8}}
</style></head><body>
<h1>IT Risk Evidence Pack: {escape(meta['quarter_label'])}</h1>
<p class="muted">Account {escape(meta['account_id'])} · Regions {escape(', '.join(meta['regions']))} ·
Period {escape(meta['period_start'])} to {escape(meta['period_end'])} (end exclusive) ·
Generated {escape(meta['generated_at'])}</p>

<div class="tiles">
 <div class="tile">Backup success<b>{'n/a' if rate is None else f'{rate}%'}</b>{backup_summary['completed_jobs']}/{backup_summary['finished_jobs']} jobs</div>
 <div class="tile">Restore tests<b>{sum(1 for r in restores if r.get('status') == 'COMPLETED')}</b>completed</div>
 <div class="tile">EC2 instances<b>{len(ec2)}</b>in scope</div>
 <div class="tile">RDS instances<b>{len(rds)}</b>in scope</div>
 <div class="tile">Findings<b>{len(findings)}</b>{counts['High']} high · {counts['Medium']} med · {counts['Low']} low</div>
</div>

<h2>Findings</h2>
{''.join(f"<p><span class='{f['severity']}'>{f['severity']}</span> · {escape(f['area'])} · <code>{escape(f['resource'])}</code>: {escape(f['detail'])}</p>" for f in findings) or "<p class='muted'>No findings against the configured thresholds.</p>"}

<h2>1. Backup success</h2>
{_table(by_type, [("resource_type", "Resource type"), ("total", "Finished jobs"), ("completed", "Completed"), ("success_rate_pct", "Success %")], "No backup jobs.")}
<h3>Unsuccessful jobs (first 25)</h3>
{_table(failed, [("created", "Created"), ("resource_type", "Type"), ("resource_arn", "Resource"), ("state", "State"), ("status_message", "Message")], "None.")}

<h2>2. EC2 CPU utilisation (hourly averages)</h2>
{_table(ec2, [("instance_id", "Instance"), ("name", "Name"), ("instance_type", "Type"), ("state", "State"), ("cpu_avg_pct", "Avg %"), ("cpu_p95_pct", "p95 %"), ("cpu_max_pct", "Max %")], "No EC2 instances.")}

<h2>3. RDS utilisation</h2>
{_table(rds, [("db_instance", "DB instance"), ("engine", "Engine"), ("instance_class", "Class"), ("cpu_avg_pct", "CPU avg %"), ("cpu_p95_pct", "CPU p95 %"), ("connections_max", "Max conns"), ("peak_storage_used_pct", "Peak storage %"), ("backup_retention_days", "Backup days"), ("storage_encrypted", "Encrypted")], "No RDS instances.")}

<p class="muted">Raw evidence is attached as CSV. Integrity: see <code>manifest.sha256</code>.
Utilisation is computed from CloudWatch 1-hour average datapoints. Instances terminated before the
report ran are not listed.</p>
</body></html>"""
    return html.encode("utf-8")


def build_manifest(files: dict[str, bytes]) -> bytes:
    lines = [f"{hashlib.sha256(data).hexdigest()}  {name}" for name, data in sorted(files.items())]
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_pack(meta: dict, backups: list[dict], restores: list[dict], ec2: list[dict], rds: list[dict],
               backup_summary: dict, thresholds: Thresholds) -> tuple[dict[str, bytes], list[dict]]:
    findings = build_findings(backup_summary, restores, ec2, rds, thresholds)
    files = {
        "summary.html": render_html(meta, backup_summary, findings, backups, restores, ec2, rds),
        "summary.json": json.dumps(
            {"meta": meta, "backup_summary": backup_summary, "findings": findings}, indent=2, default=str
        ).encode("utf-8"),
        "findings.csv": to_csv(findings),
        "backup_jobs.csv": to_csv(backups),
        "restore_jobs.csv": to_csv(restores),
        "ec2_utilisation.csv": to_csv(ec2),
        "rds_utilisation.csv": to_csv(rds),
    }
    files["manifest.sha256"] = build_manifest(files)
    return files, findings


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
