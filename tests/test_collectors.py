from datetime import UTC, datetime

import boto3
from botocore.stub import ANY, Stubber

from report import collectors

START = datetime(2026, 7, 1, tzinfo=UTC)
END = datetime(2026, 10, 1, tzinfo=UTC)


def _client(service):
    return boto3.client(service, region_name="eu-west-1", aws_access_key_id="x", aws_secret_access_key="x")


def test_summarise_series():
    s = collectors.summarise_series([float(v) for v in range(1, 101)])
    assert s["avg"] == 50.5
    assert s["p95"] == 95.0
    assert s["max"] == 100.0
    assert collectors.summarise_series([])["avg"] is None


def test_summarise_backups_ignores_running_jobs():
    jobs = [
        {"state": "COMPLETED", "resource_type": "EC2"},
        {"state": "COMPLETED", "resource_type": "EC2"},
        {"state": "FAILED", "resource_type": "EC2"},
        {"state": "COMPLETED", "resource_type": "RDS"},
        {"state": "RUNNING", "resource_type": "RDS"},
    ]
    s = collectors.summarise_backups(jobs)
    assert s["finished_jobs"] == 4
    assert s["success_rate_pct"] == 75.0
    assert s["by_resource_type"]["EC2"]["success_rate_pct"] == 66.67
    assert s["by_resource_type"]["RDS"]["total"] == 1


def test_collect_backup_jobs_paginates():
    backup = _client("backup")
    with Stubber(backup) as stub:
        params = {"ByCreatedAfter": START, "ByCreatedBefore": END}
        stub.add_response(
            "list_backup_jobs",
            {"BackupJobs": [{"BackupJobId": "a", "State": "COMPLETED", "ResourceType": "EBS"}], "NextToken": "t"},
            params,
        )
        stub.add_response(
            "list_backup_jobs",
            {"BackupJobs": [{"BackupJobId": "b", "State": "FAILED", "ResourceType": "RDS"}]},
            {**params, "NextToken": "t"},
        )
        jobs = collectors.collect_backup_jobs(backup, "eu-west-1", START, END)
    assert [j["backup_job_id"] for j in jobs] == ["a", "b"]
    assert jobs[1]["state"] == "FAILED"


def test_collect_ec2_maps_metrics_to_instances():
    ec2, cw = _client("ec2"), _client("cloudwatch")
    with Stubber(ec2) as s_ec2, Stubber(cw) as s_cw:
        s_ec2.add_response(
            "describe_instances",
            {"Reservations": [{"Instances": [
                {"InstanceId": "i-1", "InstanceType": "t3.small", "State": {"Name": "running"},
                 "Tags": [{"Key": "Name", "Value": "web-1"}]},
                {"InstanceId": "i-2", "InstanceType": "m5.large", "State": {"Name": "running"}},
            ]}]},
            {"Filters": ANY},
        )
        s_cw.add_response(
            "get_metric_data",
            {"MetricDataResults": [
                {"Id": "q0", "Values": [10.0, 20.0, 30.0]},
                {"Id": "q1", "Values": [90.0, 95.0]},
            ]},
            {"MetricDataQueries": ANY, "StartTime": START, "EndTime": END, "ScanBy": "TimestampAscending"},
        )
        rows = collectors.collect_ec2(ec2, cw, "eu-west-1", START, END)
    assert rows[0]["name"] == "web-1" and rows[0]["cpu_avg_pct"] == 20.0
    assert rows[1]["cpu_max_pct"] == 95.0


def test_collect_rds_storage_percentage():
    rds, cw = _client("rds"), _client("cloudwatch")
    gib = 1024**3
    with Stubber(rds) as s_rds, Stubber(cw) as s_cw:
        s_rds.add_response("describe_db_instances", {"DBInstances": [{
            "DBInstanceIdentifier": "orders-db", "Engine": "postgres", "EngineVersion": "16.3",
            "DBInstanceClass": "db.t4g.medium", "AllocatedStorage": 100, "BackupRetentionPeriod": 7,
            "StorageEncrypted": True,
        }]}, {})
        s_cw.add_response("get_metric_data", {"MetricDataResults": [
            {"Id": "q0", "Values": [30.0, 50.0]},
            {"Id": "q1", "Values": [12.0, 40.0]},
            {"Id": "q2", "Values": [512 * 1024**2]},
            {"Id": "q3", "Values": [40 * gib, 25 * gib]},
        ]}, {"MetricDataQueries": ANY, "StartTime": START, "EndTime": END, "ScanBy": "TimestampAscending"})
        rows = collectors.collect_rds(rds, cw, "eu-west-1", START, END)
    row = rows[0]
    assert row["peak_storage_used_pct"] == 75.0
    assert row["connections_max"] == 40.0
    assert row["min_freeable_memory_mib"] == 512.0
