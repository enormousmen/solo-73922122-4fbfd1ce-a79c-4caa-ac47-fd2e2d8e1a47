"""Optional demo data for the Docker Compose environment.

Seeding is idempotent: source record ids are fixed, so restarting the stack or
running two processes at once leaves exactly one copy of each demo reading.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import Settings
from .models import ReadingIn
from .service import BatchRejected, ingest

_DEMO_DAY = datetime(2026, 10, 1, tzinfo=timezone.utc)

_READINGS = [
    # (meter, source id, utc time, cumulative kWh, replaced flag)
    ("M-1001", "demo-m1001-0000", "2026-10-01T00:00:00Z", 1000, False),
    ("M-1001", "demo-m1001-0100", "2026-10-01T01:00:00Z", 1120, False),
    ("M-1001", "demo-m1001-0300", "2026-10-01T03:00:00Z", 1300, False),
    ("M-1001", "demo-m1001-0200", "2026-10-01T02:00:00Z", 1245, False),
    ("M-1001", "demo-m1001-0400", "2026-10-01T04:00:00Z", 25, True),
    ("M-1001", "demo-m1001-0500", "2026-10-01T05:00:00Z", 120, False),
    ("M-1002", "demo-m1002-0000", "2026-10-01T00:00:00Z", 500, False),
    ("M-1002", "demo-m1002-0100", "2026-10-01T01:00:00Z", 640, False),
    ("M-1002", "demo-m1002-0200", "2026-10-01T02:00:00Z", 610, False),
    ("M-1002", "demo-m1002-0300", "2026-10-01T03:00:00Z", 700, False),
]


def _demo_full_day() -> list[ReadingIn]:
    """M-1003 has all 25 whole-hour readings of 2026-10-01, so its daily
    result can be issued successfully straight away (12 kWh per hour)."""
    rows: list[ReadingIn] = []
    for hour in range(25):
        when = _DEMO_DAY + timedelta(hours=hour)
        rows.append(
            ReadingIn(
                meter_id="M-1003",
                source_record_id=f"demo-m1003-h{hour:02d}",
                read_at=when,
                reading=800 + hour * 12,
                meter_replaced=False,
            )
        )
    return rows


def _reading(meter: str, source: str, at: str, value: int, replaced: bool) -> ReadingIn:
    return ReadingIn(
        meter_id=meter,
        source_record_id=source,
        read_at=datetime.fromisoformat(at.replace("Z", "+00:00")),
        reading=value,
        meter_replaced=replaced,
    )


def seed_demo_data(settings: Settings, attempts: int = 5) -> None:
    readings = [_reading(*row) for row in _READINGS] + _demo_full_day()
    # Split into two batches so the backfilled reading exercises the
    # "recompute after late arrival" path.
    first = [r for r in readings if r.source_record_id != "demo-m1001-0200"]
    second = [r for r in readings if r.source_record_id == "demo-m1001-0200"]
    for batch in (first, second):
        for attempt in range(attempts):
            try:
                ingest(settings, batch)
                break
            except BatchRejected:
                # Another process seeded the same ids with the same content;
                # a retry then reports "unchanged" and succeeds.
                if attempt == attempts - 1:
                    raise
