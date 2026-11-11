"""Fiscal-quarter arithmetic.

A fiscal year is identified by the calendar year in which it *starts*.
With the default ``fy_start_month=7`` (July, as used by Kenya and many
other jurisdictions), FY2026/27 runs 1 July 2026 to 30 June 2027 and its
Q1 is July-September 2026.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


@dataclass(frozen=True)
class FiscalQuarter:
    fy_start_year: int
    quarter: int
    fy_start_month: int = 7
    tz: tzinfo = UTC

    def __post_init__(self) -> None:
        if self.quarter not in (1, 2, 3, 4):
            raise ValueError(f"quarter must be 1-4, got {self.quarter}")
        if not 1 <= self.fy_start_month <= 12:
            raise ValueError(f"fy_start_month must be 1-12, got {self.fy_start_month}")

    @property
    def start(self) -> datetime:
        """Inclusive start of the quarter in the report timezone."""
        y, m = _add_months(self.fy_start_year, self.fy_start_month, 3 * (self.quarter - 1))
        return datetime(y, m, 1, tzinfo=self.tz)

    @property
    def end(self) -> datetime:
        """Exclusive end of the quarter in the report timezone."""
        y, m = _add_months(self.fy_start_year, self.fy_start_month, 3 * self.quarter)
        return datetime(y, m, 1, tzinfo=self.tz)

    @property
    def label(self) -> str:
        if self.fy_start_month == 1:
            return f"FY{self.fy_start_year} Q{self.quarter}"
        return f"FY{self.fy_start_year}/{(self.fy_start_year + 1) % 100:02d} Q{self.quarter}"

    @property
    def slug(self) -> str:
        """Filesystem / S3-safe identifier, e.g. ``FY2026-27-Q1``."""
        return self.label.replace("/", "-").replace(" ", "-")

    def previous(self) -> FiscalQuarter:
        if self.quarter > 1:
            return FiscalQuarter(self.fy_start_year, self.quarter - 1, self.fy_start_month, self.tz)
        return FiscalQuarter(self.fy_start_year - 1, 4, self.fy_start_month, self.tz)


def quarter_containing(moment: datetime, fy_start_month: int = 7, tz: tzinfo = UTC) -> FiscalQuarter:
    local = moment.astimezone(tz)
    fy_start_year = local.year if local.month >= fy_start_month else local.year - 1
    offset = (local.month - fy_start_month) % 12
    return FiscalQuarter(fy_start_year, offset // 3 + 1, fy_start_month, tz)


def last_completed_quarter(now: datetime, fy_start_month: int = 7, tz: tzinfo = UTC) -> FiscalQuarter:
    return quarter_containing(now, fy_start_month, tz).previous()


def fixed_offset(hours: float) -> tzinfo:
    """Fixed UTC offset. Adequate for zones without DST (e.g. Africa/Nairobi = +3)."""
    return timezone(timedelta(hours=hours))
