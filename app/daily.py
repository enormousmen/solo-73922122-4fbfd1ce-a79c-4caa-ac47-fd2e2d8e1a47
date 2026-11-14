"""Daily quality-result domain service.

A daily quality result certifies one meter's energy usage for one UTC day.

Quality rules (all checked inside one write transaction)
--------------------------------------------------------
* The window is ``[day 00:00, next day 00:00]`` — both endpoints included —
  so 25 whole-hour readings (00:00 ... 24:00) must exist.
* Any adjacent pair inside the window with ``meter_replaced`` on the later
  reading, or with a decreasing cumulative value, is a break
  (``meter_replaced`` / ``counter_rollback``).
* With every whole hour present and no break, the usage of every adjacent
  pair inside the window is summed and a ``valid`` result is stored.
* Otherwise nothing is stored for the attempt: the response lists the
  missing whole-hour instants and the breaks, with status ``not_issued``.

Lifecycle
---------
* Reading mutations (ingest / undo) expire every still-valid result of every
  UTC day whose ``[00:00, next 00:00]`` window contains a changed reading,
  inside the *same* transaction that changes the readings.
* Re-issuing identical content is an idempotent no-op.  Re-issuing after a
  mutation recomputes from the latest committed state: the previous result is
  already ``expired`` and a fresh ``valid`` row is created on success.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date as date_cls
from datetime import datetime
from typing import Iterable, Sequence

from .config import Settings
from .db import (
    DAY_US,
    HOUR_US,
    bump_commit_seq,
    connection,
    current_commit_seq,
    read_transaction,
    write_transaction,
)
from .models import (
    DailyBreak,
    DailyIssueResponse,
    DailyResultOut,
    DailyWindow,
    MissingSlot,
)
from .timeutil import (
    microseconds_to_iso,
    to_microseconds,
)

BREAK_METER_REPLACED = "meter_replaced"
BREAK_COUNTER_ROLLBACK = "counter_rollback"
VALID = "valid"
EXPIRED = "expired"
NOT_ISSUED = "not_issued"


# ---------------------------------------------------------------------------
# Day helpers
# ---------------------------------------------------------------------------


def day_bounds(day: date_cls) -> tuple[int, int]:
    """Return ``(start_us, end_us)`` for a UTC calendar day, end inclusive."""
    start = datetime(day.year, day.month, day.day)
    start_us = to_microseconds(start)
    return start_us, start_us + DAY_US


def days_touched_by_instant(read_at_us: int) -> tuple[int, int]:
    """UTC days whose inclusive window contains an instant.

    A whole-hour instant strictly inside a day belongs to that day only.
    Midnight belongs to the day it starts and, as the inclusive end point, to
    the previous day as well.
    """
    start_day_us = (read_at_us // DAY_US) * DAY_US
    if read_at_us == start_day_us:
        return start_day_us - DAY_US, start_day_us
    return (start_day_us,)


def day_label(day_start_us: int) -> str:
    return microseconds_to_iso(day_start_us)[:10]


# ---------------------------------------------------------------------------
# Expiration, invoked from the reading mutation transactions
# ---------------------------------------------------------------------------


def expire_daily_results(
    conn: sqlite3.Connection,
    meters: Iterable[str],
    commit_seq: int,
    extra_instants: dict[str, list[int]] | None = None,
) -> int:
    """Mark valid daily results stale after readings of ``meters`` changed.

    Must run inside the same ``BEGIN IMMEDIATE`` transaction as the reading
    mutation.  Every UTC day touched by any reading of an affected meter is
    considered: a result can only stay valid while its window's readings are
    unchanged, so over-expiring is safe while under-expiring is not.
    ``extra_instants`` covers readings that have already been deleted from the
    table by an undo (their days must be expired as well).
    Returns the number of rows expired.
    """
    extra_instants = extra_instants or {}
    total = 0
    for meter_id in sorted(set(meters)):
        rows = conn.execute(
            "SELECT DISTINCT read_at_us FROM readings WHERE meter_id = ?",
            (meter_id,),
        ).fetchall()
        instants = [int(row["read_at_us"]) for row in rows]
        instants.extend(extra_instants.get(meter_id, ()))
        day_starts = {
            day_start
            for instant in instants
            for day_start in days_touched_by_instant(instant)
        }
        for day_start_us in sorted(day_starts):
            cursor = conn.execute(
                "UPDATE daily_results SET status = 'expired', "
                "expired_commit_seq = ? "
                "WHERE meter_id = ? AND day_start_us = ? AND status = 'valid'",
                (commit_seq, meter_id, day_start_us),
            )
            total += cursor.rowcount
    return total


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------


def _window_readings(
    conn: sqlite3.Connection, meter_id: str, start_us: int, end_us: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT source_record_id, read_at_us, reading, meter_replaced "
        "FROM readings WHERE meter_id = ? AND read_at_us >= ? "
        "AND read_at_us <= ? ORDER BY read_at_us ASC, id ASC",
        (meter_id, start_us, end_us),
    ).fetchall()


def _content_hash(rows: Sequence[sqlite3.Row]) -> str:
    payload = json.dumps(
        [
            [
                row["source_record_id"],
                int(row["read_at_us"]),
                int(row["reading"]),
                bool(row["meter_replaced"]),
            ]
            for row in rows
        ],
        sort_keys=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _missing_slots(
    rows: Sequence[sqlite3.Row], start_us: int
) -> list[MissingSlot]:
    present = {int(row["read_at_us"]) for row in rows}
    missing: list[MissingSlot] = []
    for hour in range(25):
        slot_us = start_us + hour * HOUR_US
        if slot_us not in present:
            # The 25th slot (next day 00:00) is reported as hour 24 of the
            # checked window; hour 0-23 cover the calendar day.
            missing.append(
                MissingSlot(
                    read_at=microseconds_to_iso(slot_us),
                    hour=hour,
                )
            )
    return missing


def _breaks_in_window(
    rows: Sequence[sqlite3.Row],
) -> list[DailyBreak]:
    breaks: list[DailyBreak] = []
    previous: sqlite3.Row | None = None
    for row in rows:
        if previous is None:
            previous = row
            continue
        reason: str | None = None
        if bool(row["meter_replaced"]):
            reason = BREAK_METER_REPLACED
        elif int(row["reading"]) < int(previous["reading"]):
            reason = BREAK_COUNTER_ROLLBACK
        if reason is not None:
            breaks.append(
                DailyBreak(
                    type=reason,
                    from_read_at=microseconds_to_iso(previous["read_at_us"]),
                    to_read_at=microseconds_to_iso(row["read_at_us"]),
                    from_reading=int(previous["reading"]),
                    to_reading=int(row["reading"]),
                    from_source_record_id=previous["source_record_id"],
                    to_source_record_id=row["source_record_id"],
                )
            )
        previous = row
    return breaks


def _window(start_us: int, end_us: int) -> DailyWindow:
    return DailyWindow(
        start=microseconds_to_iso(start_us),
        end=microseconds_to_iso(end_us),
    )


def issue_daily_result(
    settings: Settings, meter_id: str, day: date_cls
) -> DailyIssueResponse:
    """Issue (or idempotently re-issue) one meter's daily quality result."""
    start_us, end_us = day_bounds(day)
    day_text = day.isoformat()
    with connection(settings) as conn:
        with write_transaction(conn):
            rows = _window_readings(conn, meter_id, start_us, end_us)
            missing = _missing_slots(rows, start_us)
            breaks = _breaks_in_window(rows)
            observed_seq = current_commit_seq(conn)

            base = DailyIssueResponse(
                meter_id=meter_id,
                date=day_text,
                window=_window(start_us, end_us),
                status=NOT_ISSUED,
                missing_slots=missing,
                breaks=breaks,
                commit_seq=observed_seq,
            )

            if missing or breaks:
                # Quality failure: never write or change a stored result.
                return base

            interval_count = len(rows) - 1
            usage_total = sum(
                int(row["reading"]) for row in rows[1:]
            ) - sum(int(row["reading"]) for row in rows[:-1])
            content_hash = _content_hash(rows)

            stored = conn.execute(
                "SELECT id, status, usage_total, interval_count, "
                "content_hash, issued_commit_seq FROM daily_results "
                "WHERE meter_id = ? AND day_start_us = ? "
                "ORDER BY id DESC LIMIT 1",
                (meter_id, start_us),
            ).fetchone()

            if stored is not None and stored["status"] == VALID:
                if stored["content_hash"] == content_hash:
                    # Identical content retry: state must not change.
                    return base.model_copy(
                        update={
                            "status": VALID,
                            "usage_total": int(stored["usage_total"]),
                            "interval_count": int(stored["interval_count"]),
                            "issued_commit_seq": int(
                                stored["issued_commit_seq"]
                            ),
                            "idempotent_retry": True,
                        }
                    )
                # A valid row whose readings changed should already have been
                # expired by the mutation transaction.  Defensive guard:
                # expire it before issuing the replacement.
                conn.execute(
                    "UPDATE daily_results SET status = 'expired', "
                    "expired_commit_seq = ? WHERE id = ?",
                    (observed_seq, stored["id"]),
                )

            issue_seq = bump_commit_seq(conn)
            conn.execute(
                "INSERT INTO daily_results (meter_id, day, day_start_us, "
                "day_end_us, status, usage_total, interval_count, "
                "reading_count, missing_slot_count, break_count, "
                "content_hash, issued_commit_seq, expired_commit_seq) "
                "VALUES (?, ?, ?, ?, 'valid', ?, ?, ?, 0, 0, ?, ?, NULL)",
                (
                    meter_id,
                    day_text,
                    start_us,
                    end_us,
                    usage_total,
                    interval_count,
                    len(rows),
                    content_hash,
                    issue_seq,
                ),
            )
            return base.model_copy(
                update={
                    "status": VALID,
                    "usage_total": usage_total,
                    "interval_count": interval_count,
                    "issued_commit_seq": issue_seq,
                    "commit_seq": issue_seq,
                }
            )


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _result_out(row: sqlite3.Row) -> DailyResultOut:
    status = row["status"]
    issued_seq = int(row["issued_commit_seq"])
    expired_seq = row["expired_commit_seq"]
    return DailyResultOut(
        meter_id=row["meter_id"],
        date=row["day"],
        window=_window(int(row["day_start_us"]), int(row["day_end_us"])),
        status=status,
        usage_total=(
            None if row["usage_total"] is None else int(row["usage_total"])
        ),
        interval_count=(
            None
            if row["interval_count"] is None
            else int(row["interval_count"])
        ),
        reading_count=int(row["reading_count"]),
        missing_slot_count=int(row["missing_slot_count"]),
        break_count=int(row["break_count"]),
        issued_commit_seq=issued_seq,
        expired_commit_seq=(
            None if expired_seq is None else int(expired_seq)
        ),
        commit_seq=issued_seq if status == VALID else int(expired_seq),
    )


def get_daily_result(
    settings: Settings, meter_id: str, day: date_cls
) -> DailyResultOut | None:
    """Return the latest stored result for one meter/day, or None if absent."""
    start_us, _ = day_bounds(day)
    with connection(settings) as conn:
        with read_transaction(conn):
            row = conn.execute(
                "SELECT * FROM daily_results WHERE meter_id = ? "
                "AND day_start_us = ? ORDER BY id DESC LIMIT 1",
                (meter_id, start_us),
            ).fetchone()
            return None if row is None else _result_out(row)


def list_daily_results(
    settings: Settings,
    meter_id: str | None = None,
    day: date_cls | None = None,
) -> list[DailyResultOut]:
    """List the latest stored result per meter/day, newest status first."""
    clauses: list[str] = []
    params: list[object] = []
    if meter_id is not None:
        clauses.append("meter_id = ?")
        params.append(meter_id)
    if day is not None:
        start_us, _ = day_bounds(day)
        clauses.append("day_start_us = ?")
        params.append(start_us)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connection(settings) as conn:
        with read_transaction(conn):
            rows = conn.execute(
                "SELECT r.* FROM daily_results r "
                "JOIN (SELECT meter_id, day_start_us, MAX(id) AS max_id "
                f"FROM daily_results {where} GROUP BY meter_id, day_start_us) "
                "latest ON latest.max_id = r.id "
                "ORDER BY r.meter_id ASC, r.day_start_us DESC",
                params,
            ).fetchall()
            return [_result_out(row) for row in rows]
