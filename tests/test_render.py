import hashlib

from report.render import Thresholds, build_findings, build_pack

META = {
    "quarter_label": "FY2026/27 Q1", "account_id": "111122223333", "regions": ["eu-west-1"],
    "period_start": "2026-07-01T00:00:00+03:00", "period_end": "2026-10-01T00:00:00+03:00",
    "generated_at": "2026-10-05T03:00:00+00:00",
}
SUMMARY_OK = {"finished_jobs": 100, "completed_jobs": 100, "unsuccessful_jobs": 0,
              "success_rate_pct": 100.0, "by_resource_type": {}}


def _ec2(**kw):
    base = {"instance_id": "i-1", "name": "", "state": "running", "cpu_hours_observed": 2000,
            "cpu_avg_pct": 40.0, "cpu_p95_pct": 60.0, "cpu_max_pct": 70.0}
    return {**base, **kw}


def _rds(**kw):
    base = {"db_instance": "db1", "cpu_p95_pct": 30.0, "peak_storage_used_pct": 50.0,
            "backup_retention_days": 7, "storage_encrypted": True}
    return {**base, **kw}


def test_low_backup_rate_and_missing_restore_test():
    summary = {**SUMMARY_OK, "completed_jobs": 95, "unsuccessful_jobs": 5, "success_rate_pct": 95.0}
    f = build_findings(summary, [], [], [], Thresholds())
    assert [x["severity"] for x in f] == ["High", "Medium"]


def test_ec2_and_rds_findings():
    restores = [{"status": "COMPLETED"}]
    ec2 = [_ec2(instance_id="i-hot", cpu_p95_pct=91.0), _ec2(instance_id="i-idle", cpu_avg_pct=2.0),
           _ec2(instance_id="i-blind", cpu_hours_observed=0, cpu_avg_pct=None, cpu_p95_pct=None)]
    rds = [_rds(peak_storage_used_pct=88.0, backup_retention_days=1, storage_encrypted=False)]
    f = build_findings(SUMMARY_OK, restores, ec2, rds, Thresholds())
    details = " | ".join(x["resource"] + ":" + x["detail"] for x in f)
    assert "i-hot" in details and "capacity" in details
    assert "i-idle" in details and "rightsizing" in details
    assert "i-blind" in details and "monitoring gap" in details
    assert sum(1 for x in f if x["area"] == "RDS") == 3
    assert f[0]["severity"] == "High"


def test_pack_manifest_matches_files():
    files, _ = build_pack(META, [], [{"status": "COMPLETED"}], [_ec2()], [_rds()], SUMMARY_OK, Thresholds())
    manifest = files["manifest.sha256"].decode().splitlines()
    assert len(manifest) == len(files) - 1
    for line in manifest:
        digest, name = line.split("  ")
        assert hashlib.sha256(files[name]).hexdigest() == digest
    assert b"FY2026/27 Q1" in files["summary.html"]


def test_html_escapes_untrusted_values():
    ec2 = [_ec2(name="<script>alert(1)</script>")]
    files, _ = build_pack(META, [], [], ec2, [], SUMMARY_OK, Thresholds())
    assert b"<script>alert" not in files["summary.html"]
