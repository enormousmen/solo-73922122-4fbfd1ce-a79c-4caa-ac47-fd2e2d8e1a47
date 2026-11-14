"""Durability and schema-migration tests for daily quality results."""

from __future__ import annotations

import sqlite3

import pytest

from app.db import SCHEMA_VERSION, connection, initialize
from tests.test_daily import DAY, hourly, issue, post

# The schema shipped by the previous (v1) revision, without daily_results.
_V1_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_record_id TEXT NOT NULL,
    meter_id TEXT NOT NULL,
    read_at_us INTEGER NOT NULL,
    reading INTEGER NOT NULL CHECK (reading >= 0),
    meter_replaced INTEGER NOT NULL CHECK (meter_replaced IN (0, 1)),
    content_hash TEXT NOT NULL,
    created_commit_seq INTEGER NOT NULL,
    UNIQUE (source_record_id),
    UNIQUE (meter_id, read_at_us)
);
CREATE TABLE intervals (
    id INTEGER PRIMARY KEY,
    meter_id TEXT NOT NULL,
    from_reading_id INTEGER NOT NULL REFERENCES readings(id) ON DELETE CASCADE,
    to_reading_id INTEGER NOT NULL REFERENCES readings(id) ON DELETE CASCADE,
    from_at_us INTEGER NOT NULL,
    to_at_us INTEGER NOT NULL,
    from_value INTEGER NOT NULL,
    to_value INTEGER NOT NULL,
    usage INTEGER,
    is_break INTEGER NOT NULL CHECK (is_break IN (0, 1)),
    break_reason TEXT,
    commit_seq INTEGER NOT NULL,
    UNIQUE (meter_id, to_reading_id),
    CHECK (
        (is_break = 0 AND usage IS NOT NULL AND break_reason IS NULL)
        OR (is_break = 1 AND usage IS NULL
            AND break_reason IN ('meter_replaced', 'counter_rollback'))
    )
);
CREATE INDEX idx_intervals_meter_to ON intervals (meter_id, to_at_us);
CREATE INDEX idx_intervals_from ON intervals (from_reading_id);
CREATE INDEX idx_intervals_to ON intervals (to_reading_id);
"""


def _create_v1_database(db_path: str) -> None:
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("PRAGMA journal_mode = WAL")
        raw.executescript(_V1_SCHEMA)
        raw.execute(
            "INSERT INTO meta (key, value) VALUES ('commit_seq', '3')"
        )
        raw.execute("PRAGMA user_version = 1")
        raw.commit()
    finally:
        raw.close()


def test_v1_database_is_upgraded_and_keeps_its_data(db_path, settings):
    # Build the legacy database before any current-version client opens it.
    _create_v1_database(db_path)
    raw = sqlite3.connect(db_path)
    raw.execute(
        "INSERT INTO readings (source_record_id, meter_id, read_at_us, "
        "reading, meter_replaced, content_hash, created_commit_seq) "
        "VALUES ('old-1', 'M1', 0, 42, 0, 'h', 3)"
    )
    raw.commit()
    raw.close()

    initialize(settings)
    with connection(settings) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        # Existing data survives.
        row = conn.execute(
            "SELECT meter_id, reading FROM readings WHERE source_record_id = 'old-1'"
        ).fetchone()
        assert tuple(row) == ("M1", 42)
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'commit_seq'"
        ).fetchone()[0] == "3"
        # The new table and its unique index exist.
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            )
        }
        assert "daily_results" in names
        assert "idx_daily_one_valid" in names

    # The upgraded database serves the new endpoints.
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings)) as client:
        resp = client.post(
            "/api/v1/daily-results/issue",
            json={"meter_id": "M1", "date": DAY},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_issued"


def test_failed_issue_rolls_back_everything(db_path, settings, client, monkeypatch):
    post(client, hourly("M1"))
    # Force the issuance insert to fail after the checks have run: no row and
    # no commit-sequence bump may remain.
    import app.daily as daily

    def explode(*args, **kwargs):
        raise RuntimeError("boom during insert")

    monkeypatch.setattr(daily, "bump_commit_seq", explode)
    with pytest.raises(RuntimeError):
        issue(client, "M1", DAY)

    with connection(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_results").fetchone()[0] == 0
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'commit_seq'"
        ).fetchone()[0] == "1"  # only the ingest commit exists


def test_expiration_rolls_back_with_a_failed_interval_rebuild(
    db_path, settings, client, monkeypatch
):
    post(client, hourly("M1"))
    issue(client, "M1")
    import app.service as service

    def explode(*args, **kwargs):
        raise RuntimeError("boom during recompute")

    monkeypatch.setattr(service, "_recompute_intervals", explode)
    undo_payload = {"source_record_ids": ["M1-2026-10-01-h12"]}
    with pytest.raises(RuntimeError):
        client.post("/api/v1/readings/undo", json=undo_payload)

    with connection(settings) as conn:
        # The reading is still present, the daily result is still valid...
        assert conn.execute(
            "SELECT COUNT(*) FROM readings WHERE source_record_id = ?",
            ("M1-2026-10-01-h12",),
        ).fetchone()[0] == 1
        row = conn.execute(
            "SELECT status, expired_commit_seq FROM daily_results"
        ).fetchone()
        assert row["status"] == "valid"
        assert row["expired_commit_seq"] is None


def test_daily_results_check_constraints(db_path, settings):
    initialize(settings)
    with connection(settings) as conn:
        # A valid row must carry usage and zero quality defects.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO daily_results (meter_id, day, day_start_us, "
                "day_end_us, status, usage_total, interval_count, reading_count,"
                " missing_slot_count, break_count, content_hash, "
                "issued_commit_seq, expired_commit_seq) "
                "VALUES ('M1', ?, 0, 1, 'valid', NULL, 24, 25, 0, 0, 'h', 1, NULL)",
                (DAY,),
            )
        # An expired row must record the expiring commit.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO daily_results (meter_id, day, day_start_us, "
                "day_end_us, status, usage_total, interval_count, reading_count,"
                " missing_slot_count, break_count, content_hash, "
                "issued_commit_seq, expired_commit_seq) "
                "VALUES ('M2', ?, 0, 1, 'expired', 5, 24, 25, 0, 0, 'h', 1, NULL)",
                (DAY,),
            )
