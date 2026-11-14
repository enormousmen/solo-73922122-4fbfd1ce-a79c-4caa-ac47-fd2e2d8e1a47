"""Functional tests for daily quality-result issuance and query semantics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db import connection

DAY = "2026-10-01"
NEXT_DAY = "2026-10-02"
UTC = timezone.utc


def slot(hour: int, day: str = DAY) -> str:
    base = datetime.fromisoformat(day).replace(tzinfo=UTC)
    return (base + timedelta(hours=hour)).isoformat().replace("+00:00", "Z")


def sid(meter: str, hour: int, day: str = DAY) -> str:
    return f"{meter}-{day}-h{hour:02d}"


def hourly(meter: str, day: str = DAY, start: int = 1000, step: int = 10):
    """25 whole-hour readings, hours 0..24 (24:00 == next day 00:00)."""
    return [
        {
            "meter_id": meter,
            "source_record_id": f"{meter}-{day}-h{hour:02d}",
            "read_at": slot(hour, day),
            "reading": start + hour * step,
            "meter_replaced": False,
        }
        for hour in range(25)
    ]


def issue(client, meter: str, day: str = DAY):
    return client.post(
        "/api/v1/daily-results/issue", json={"meter_id": meter, "date": day}
    )


def get_day(client, meter: str, day: str = DAY):
    return client.get(f"/api/v1/daily-results/{meter}/{day}")


def post(client, readings):
    return client.post("/api/v1/readings", json={"readings": readings})


def undo(client, ids):
    return client.post(
        "/api/v1/readings/undo", json={"source_record_ids": ids}
    )


# ---------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------


def test_issue_without_any_reading_lists_all_25_slots(client):
    resp = issue(client, "M1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "not_issued"
    assert body["usage_total"] is None
    assert [s["hour"] for s in body["missing_slots"]] == list(range(25))
    assert body["breaks"] == []
    assert body["window"] == {
        "start": f"{DAY}T00:00:00Z",
        "end": f"{NEXT_DAY}T00:00:00Z",
        "inclusive": True,
    }
    # A failed issue must leave nothing behind.
    assert get_day(client, "M1").status_code == 404
    assert client.get("/api/v1/daily-results").json() == []


def test_missing_whole_hours_blocks_issuance(client):
    rows = [r for r in hourly("M1") if r["source_record_id"] not in (
        sid("M1", 7), sid("M1", 24),
    )]
    post(client, rows)
    body = issue(client, "M1").json()
    assert body["status"] == "not_issued"
    assert [s["hour"] for s in body["missing_slots"]] == [7, 24]
    assert get_day(client, "M1").status_code == 404


def test_complete_hours_issue_valid_usage(client):
    post(client, hourly("M1", start=1000, step=10))
    body = issue(client, "M1").json()
    assert body["status"] == "valid"
    assert body["usage_total"] == 240  # 1240 - 1000
    assert body["interval_count"] == 24
    assert body["missing_slots"] == []
    assert body["breaks"] == []
    assert body["idempotent_retry"] is False
    assert body["issued_commit_seq"] == body["commit_seq"]

    stored = get_day(client, "M1").json()
    assert stored["status"] == "valid"
    assert stored["usage_total"] == 240
    assert stored["interval_count"] == 24
    assert stored["reading_count"] == 25
    assert stored["expired_commit_seq"] is None
    assert stored["issued_commit_seq"] == body["issued_commit_seq"]


def test_off_hour_readings_are_included_in_the_usage_sum(client):
    rows = hourly("M1", start=1000, step=10)
    # Insert an extra 12:30 reading between h12=1120 and h13=1130.
    rows.append(
        {
            "meter_id": "M1",
            "source_record_id": "M1-off",
            "read_at": f"{DAY}T12:30:00Z",
            "reading": 1125,
            "meter_replaced": False,
        }
    )
    post(client, rows)
    body = issue(client, "M1").json()
    assert body["status"] == "valid"
    assert body["interval_count"] == 25
    assert body["usage_total"] == 240  # telescoping sum unchanged


def test_meter_replacement_break_blocks_issuance(client):
    rows = hourly("M1")
    rows[10]["meter_replaced"] = True
    post(client, rows)
    body = issue(client, "M1").json()
    assert body["status"] == "not_issued"
    assert body["missing_slots"] == []
    assert len(body["breaks"]) == 1
    brk = body["breaks"][0]
    assert brk["type"] == "meter_replaced"
    assert brk["to_read_at"] == slot(10)
    assert brk["from_source_record_id"] == sid("M1", 9)
    assert brk["to_source_record_id"] == sid("M1", 10)
    assert get_day(client, "M1").status_code == 404


def test_counter_rollback_break_blocks_issuance(client):
    rows = hourly("M1", start=1000)
    rows[15]["reading"] = 50  # h14=1140 -> h15 drops to 50
    post(client, rows)
    body = issue(client, "M1").json()
    assert body["status"] == "not_issued"
    assert len(body["breaks"]) == 1
    assert body["breaks"][0]["type"] == "counter_rollback"
    assert body["breaks"][0]["from_reading"] == 1140
    assert body["breaks"][0]["to_reading"] == 50


def test_break_and_missing_slots_are_both_reported(client):
    rows = [r for r in hourly("M1") if r["source_record_id"] != sid("M1", 5)]
    rows[10]["meter_replaced"] = True  # index shifted after removal is fine
    post(client, rows)
    body = issue(client, "M1").json()
    assert body["status"] == "not_issued"
    assert [s["hour"] for s in body["missing_slots"]] == [5]
    assert [b["type"] for b in body["breaks"]] == ["meter_replaced"]


def test_failed_issue_never_writes_a_row(client):
    post(client, [r for r in hourly("M1") if r["source_record_id"] != sid("M1", 3)])
    issue(client, "M1")
    issue(client, "M1")
    with connection(client.app.state.settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_results").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Idempotency and re-issuance
# ---------------------------------------------------------------------------


def test_identical_retry_does_not_change_state(client):
    post(client, hourly("M1"))
    first = issue(client, "M1").json()
    second = issue(client, "M1").json()
    assert second["status"] == "valid"
    assert second["idempotent_retry"] is True
    assert second["issued_commit_seq"] == first["issued_commit_seq"]
    assert second["commit_seq"] == first["commit_seq"]
    assert second["usage_total"] == first["usage_total"]

    with connection(client.app.state.settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_results").fetchone()[0] == 1
        row = conn.execute("SELECT * FROM daily_results").fetchone()
        assert row["status"] == "valid"
        assert row["issued_commit_seq"] == first["issued_commit_seq"]


def test_reissue_after_expiry_creates_new_valid_row(client):
    post(client, hourly("M1"))
    first = issue(client, "M1").json()

    # Mutate a reading: old result expires in the same mutation transaction.
    undo(client, [sid("M1", 12)])
    post(
        client,
        [
            {
                "meter_id": "M1",
                "source_record_id": "M1-h12b",
                "read_at": slot(12),
                "reading": 1120,
                "meter_replaced": False,
            }
        ],
    )
    expired = get_day(client, "M1").json()
    assert expired["status"] == "expired"

    reissued = issue(client, "M1").json()
    assert reissued["status"] == "valid"
    assert reissued["idempotent_retry"] is False
    assert reissued["issued_commit_seq"] > first["issued_commit_seq"]

    latest = get_day(client, "M1").json()
    assert latest["status"] == "valid"
    assert latest["issued_commit_seq"] == reissued["issued_commit_seq"]
    # Query only returns the latest row; full history remains in storage.
    with connection(client.app.state.settings) as conn:
        rows = conn.execute(
            "SELECT status FROM daily_results ORDER BY id"
        ).fetchall()
        assert [r["status"] for r in rows] == ["expired", "valid"]
    assert (
        client.get("/api/v1/daily-results", params={"meter_id": "M1"}).json()[0][
            "status"
        ]
        == "valid"
    )


def test_reissue_after_break_introduced_stays_not_issued_and_keeps_expired(client):
    post(client, hourly("M1"))
    issue(client, "M1")
    undo(client, [sid("M1", 20)])
    post(
        client,
        [
            {
                "meter_id": "M1",
                "source_record_id": "M1-h20b",
                "read_at": slot(20),
                "reading": 5,  # rollback
                "meter_replaced": False,
            }
        ],
    )
    assert get_day(client, "M1").json()["status"] == "expired"
    failed = issue(client, "M1").json()
    assert failed["status"] == "not_issued"
    # The failed re-issue must not replace or resurrect the expired row.
    stored = get_day(client, "M1").json()
    assert stored["status"] == "expired"
    with connection(client.app.state.settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_results").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Expiration on reading mutations
# ---------------------------------------------------------------------------


def test_ingest_expires_result_in_the_same_commit(client):
    post(client, hourly("M1"))
    issued = issue(client, "M1").json()

    # The undo itself changes the reading set, so expiration lands in that
    # same transaction (commit), not in the later re-insert.
    undone = undo(client, [sid("M1", 10)]).json()
    post(
        client,
        [
            {
                "meter_id": "M1",
                "source_record_id": "M1-h10b",
                "read_at": slot(10),
                "reading": 1100,
                "meter_replaced": False,
            }
        ],
    )
    row = get_day(client, "M1").json()
    assert row["status"] == "expired"
    assert row["expired_commit_seq"] == undone["commit_seq"]
    assert row["issued_commit_seq"] == issued["issued_commit_seq"]
    assert row["commit_seq"] == undone["commit_seq"]
    # The historical usage value is retained on the expired row.
    assert row["usage_total"] == 240


def test_idempotent_reingest_does_not_expire(client):
    post(client, hourly("M1"))
    issue(client, "M1")
    # Re-sending the identical batch is an idempotent no-op (no new commit).
    retry = post(client, hourly("M1")).json()
    assert retry["changed"] is False
    assert get_day(client, "M1").json()["status"] == "valid"


def test_undo_expires_result_including_when_window_empties(client):
    post(client, hourly("M1"))
    issue(client, "M1")
    ids = [sid("M1", h) for h in range(25)]
    undo(client, ids)
    assert get_day(client, "M1").json()["status"] == "expired"
    # The meter no longer has readings; a fresh issue reports all slots missing.
    body = issue(client, "M1").json()
    assert body["status"] == "not_issued"
    assert len(body["missing_slots"]) == 25


def test_unrelated_meter_mutation_does_not_expire(client):
    post(client, hourly("M1"))
    post(client, hourly("M2"))
    issue(client, "M1")
    issue(client, "M2")
    undo(client, [sid("M2", 5)])
    assert get_day(client, "M1").json()["status"] == "valid"
    assert get_day(client, "M2").json()["status"] == "expired"


def test_midnight_reading_touches_both_adjacent_days(client):
    # A reading at next-day 00:00 belongs to DAY's inclusive end and to
    # NEXT_DAY's inclusive start.
    post(client, hourly("M1"))
    issue(client, "M1", DAY)
    # NEXT_DAY reuses DAY's 24:00 reading as its 00:00 endpoint; add its
    # remaining hours 1..24 with values continuing monotonically.
    next_rows = [
        row
        for row in hourly("M1", day=NEXT_DAY, start=1240, step=10)
        if row["source_record_id"] != sid("M1", 0, NEXT_DAY)
    ]
    post(client, next_rows)
    second = issue(client, "M1", NEXT_DAY)
    assert second.status_code == 200 and second.json()["status"] == "valid"

    # Changing the shared midnight reading expires both daily results.
    undo(client, [sid("M1", 24)])  # DAY's 24:00 == NEXT_DAY's 00:00
    assert get_day(client, "M1", DAY).json()["status"] == "expired"
    assert get_day(client, "M1", NEXT_DAY).json()["status"] == "expired"


# ---------------------------------------------------------------------------
# Queries and validation
# ---------------------------------------------------------------------------


def test_query_unknown_day_is_404(client):
    resp = get_day(client, "NOPE", DAY)
    assert resp.status_code == 404
    assert resp.json()["code"] == "daily_result_not_found"


def test_listing_filters_and_orders_results(client):
    post(client, hourly("M1"))
    post(client, hourly("M2"))
    issue(client, "M1")
    issue(client, "M2")
    listing = client.get("/api/v1/daily-results").json()
    assert [row["meter_id"] for row in listing] == ["M1", "M2"]
    assert {row["date"] for row in listing} == {DAY}
    only_m2 = client.get(
        "/api/v1/daily-results", params={"meter_id": "M2"}
    ).json()
    assert [row["meter_id"] for row in only_m2] == ["M2"]
    by_day = client.get(
        "/api/v1/daily-results", params={"date": DAY}
    ).json()
    assert len(by_day) == 2
    assert client.get(
        "/api/v1/daily-results", params={"date": "2026-09-30"}
    ).json() == []


def test_invalid_dates_are_422(client):
    assert issue(client, "M1", "01/10/2026").status_code == 422
    assert issue(client, "M1", "2026-13-01").status_code == 422
    assert get_day(client, "M1", "not-a-date").status_code == 422
    assert client.get(
        "/api/v1/daily-results", params={"date": "nope"}
    ).status_code == 422


def test_blank_meter_id_is_422(client):
    resp = client.post(
        "/api/v1/daily-results/issue", json={"meter_id": "  ", "date": DAY}
    )
    assert resp.status_code == 422


def test_usage_matches_interval_sum_from_meter_snapshot(client):
    rows = hourly("M1", start=300, step=7)
    post(client, rows)
    issued = issue(client, "M1").json()
    snapshot = client.get("/api/v1/meters/M1").json()
    expected = sum(i["usage"] for i in snapshot["intervals"] if not i["is_break"])
    assert issued["usage_total"] == expected == 24 * 7


def test_database_invariants_after_issue_cycle(client, settings):
    post(client, hourly("M1"))
    issue(client, "M1")
    undo(client, [sid("M1", 12)])
    issue(client, "M1")  # not issued: missing slot, expired row stays
    post(
        client,
        [
            {
                "meter_id": "M1",
                "source_record_id": "M1-h12b",
                "read_at": slot(12),
                "reading": 1120,
                "meter_replaced": False,
            }
        ],
    )
    issue(client, "M1")  # valid again
    with connection(settings) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # At most one valid row per meter/day, enforced inside the database.
        bad = conn.execute(
            "SELECT meter_id, day, COUNT(*) FROM daily_results "
            "WHERE status = 'valid' GROUP BY meter_id, day HAVING COUNT(*) > 1"
        ).fetchall()
        assert bad == []
        statuses = [
            r[0]
            for r in conn.execute(
                "SELECT status FROM daily_results ORDER BY id"
            ).fetchall()
        ]
        assert statuses == ["expired", "valid"]
