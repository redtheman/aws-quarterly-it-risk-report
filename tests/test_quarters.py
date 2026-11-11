from datetime import UTC, datetime

import pytest

from report.quarters import FiscalQuarter, fixed_offset, last_completed_quarter, quarter_containing

EAT = fixed_offset(3)


def test_july_fiscal_year_q1_bounds():
    q = FiscalQuarter(2026, 1, 7, EAT)
    assert q.start == datetime(2026, 7, 1, tzinfo=EAT)
    assert q.end == datetime(2026, 10, 1, tzinfo=EAT)
    assert q.label == "FY2026/27 Q1"
    assert q.slug == "FY2026-27-Q1"


def test_q3_crosses_calendar_year():
    q = FiscalQuarter(2026, 3, 7)
    assert (q.start.year, q.start.month) == (2027, 1)
    assert (q.end.year, q.end.month) == (2027, 4)


def test_calendar_fiscal_year_label():
    assert FiscalQuarter(2026, 4, 1).label == "FY2026 Q4"


@pytest.mark.parametrize(
    "moment, expected",
    [
        (datetime(2026, 7, 1, tzinfo=EAT), (2026, 1)),
        (datetime(2026, 11, 11, tzinfo=EAT), (2026, 2)),
        (datetime(2027, 2, 14, tzinfo=EAT), (2026, 3)),
        (datetime(2027, 6, 30, tzinfo=EAT), (2026, 4)),
    ],
)
def test_quarter_containing(moment, expected):
    q = quarter_containing(moment, 7, EAT)
    assert (q.fy_start_year, q.quarter) == expected


def test_timezone_boundary_uses_local_time():
    # 30 Sep 22:30 UTC is already 1 Oct 01:30 in Nairobi -> Q2
    q = quarter_containing(datetime(2026, 9, 30, 22, 30, tzinfo=UTC), 7, EAT)
    assert q.quarter == 2


def test_last_completed_wraps_fiscal_year():
    q = last_completed_quarter(datetime(2026, 7, 5, tzinfo=EAT), 7, EAT)
    assert (q.fy_start_year, q.quarter) == (2025, 4)


def test_invalid_quarter():
    with pytest.raises(ValueError):
        FiscalQuarter(2026, 5)
