"""Generate a sample evidence pack from synthetic data (no AWS needed).

    python scripts/generate_sample.py  ->  docs/sample/
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from report.collectors import summarise_backups, summarise_series
from report.quarters import FiscalQuarter, fixed_offset
from report.render import Thresholds, build_pack

random.seed(7)
q = FiscalQuarter(2026, 1, 7, fixed_offset(3))

backups = []
for i in range(540):
    rtype = random.choice(["EC2", "EBS", "RDS", "RDS", "EFS"])
    state = "COMPLETED" if random.random() > 0.012 else random.choice(["FAILED", "EXPIRED"])
    backups.append({
        "region": "eu-west-1", "backup_job_id": f"job-{i:04d}", "vault": "daily-vault",
        "resource_type": rtype, "resource_arn": f"arn:aws:{rtype.lower()}:eu-west-1:111122223333:res/{i % 40}",
        "state": state, "status_message": "" if state == "COMPLETED" else "Snapshot quota exceeded",
        "created": f"2026-0{7 + i % 3}-{1 + i % 28:02d}T01:00:00+00:00", "completed": None, "backup_size_bytes": 0,
    })
restores = [{"region": "eu-west-1", "restore_job_id": "r-1", "resource_type": "RDS", "recovery_point_arn": "rp",
             "status": "COMPLETED", "status_message": "", "created": "2026-08-15", "completed": "2026-08-15"}]

ec2 = []
profiles = [("web-01", 35, 15), ("web-02", 33, 15), ("api-01", 62, 20), ("batch-01", 70, 25),
            ("bastion", 2, 1), ("reporting", 4, 2), ("wazuh-manager", 55, 15), ("ci-runner", 20, 30)]
for n, (name, mean, spread) in enumerate(profiles):
    vals = [max(0.0, min(100.0, random.gauss(mean, spread))) for _ in range(2208)]
    s = summarise_series(vals)
    ec2.append({"region": "eu-west-1", "instance_id": f"i-0abc{n:04d}", "name": name, "instance_type": "t3.large",
                "platform": "Linux/UNIX", "state": "running", "cpu_hours_observed": s["datapoints"],
                "cpu_avg_pct": s["avg"], "cpu_p95_pct": s["p95"], "cpu_max_pct": s["max"]})

rds = [
    {"region": "eu-west-1", "db_instance": "orders-prod", "engine": "postgres 16.4", "instance_class": "db.r6g.large",
     "multi_az": True, "status": "available", "cpu_avg_pct": 41.2, "cpu_p95_pct": 77.9, "cpu_max_pct": 96.0,
     "connections_avg": 84.0, "connections_max": 212.0, "min_freeable_memory_mib": 1840.0,
     "allocated_storage_gib": 500, "peak_storage_used_pct": 83.4, "backup_retention_days": 14,
     "storage_encrypted": True},
    {"region": "eu-west-1", "db_instance": "cms-mariadb", "engine": "mariadb 10.11", "instance_class": "db.t4g.medium",
     "multi_az": False, "status": "available", "cpu_avg_pct": 12.5, "cpu_p95_pct": 30.1, "cpu_max_pct": 64.0,
     "connections_avg": 9.0, "connections_max": 41.0, "min_freeable_memory_mib": 610.0,
     "allocated_storage_gib": 100, "peak_storage_used_pct": 38.0, "backup_retention_days": 3,
     "storage_encrypted": True},
]

meta = {"quarter_label": q.label, "account_id": "111122223333", "regions": ["eu-west-1"],
        "period_start": q.start.isoformat(), "period_end": q.end.isoformat(),
        "generated_at": "2026-10-05T03:00:12+00:00"}
files, _ = build_pack(meta, backups, restores, ec2, rds, summarise_backups(backups), Thresholds())

out = Path(__file__).resolve().parents[1] / "docs" / "sample"
out.mkdir(parents=True, exist_ok=True)
for name, body in files.items():
    (out / name).write_bytes(body)
print(f"wrote {len(files)} files to {out}")
