"""Two API processes, one SQLite file: daily-result concurrency tests.

These tests reuse the real subprocess server harness so that the SQLite write
lock, the partial unique index and the same-transaction expiration are all
exercised across separate OS processes.
"""

from __future__ import annotations

import random
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.db import DAY_US, HOUR_US, connection
from tests.test_concurrency import Server, _free_port  # reuse the harness

DAY = "2026-10-01"
UTC = timezone.utc


def _slot(hour: int) -> str:
    base = datetime.fromisoformat(DAY).replace(tzinfo=UTC)
    return (base + timedelta(hours=hour)).isoformat().replace("+00:00", "Z")


def _sid(hour: int) -> str:
    return f"M1-{DAY}-h{hour:02d}"


def _hourly_reading(hour: int, value: int | None = None) -> dict:
    return {
        "meter_id": "M1",
        "source_record_id": _sid(hour),
        "read_at": _slot(hour),
        "reading": 1000 + hour * 10 if value is None else value,
        "meter_replaced": False,
    }


def _seed_complete_day(client: httpx.Client) -> None:
    resp = client.post(
        "/api/v1/readings",
        json={"readings": [_hourly_reading(h) for h in range(25)]},
    )
    assert resp.status_code == 200, resp.text


def _client(server: Server) -> httpx.Client:
    return httpx.Client(base_url=server.base_url, timeout=30.0)


def _day_start_us(db_path: str) -> int:
    raw = sqlite3.connect(db_path)
    try:
        return raw.execute(
            "SELECT day_start_us FROM daily_results LIMIT 1"
        ).fetchone()[0]
    finally:
        raw.close()


def assert_valid_results_match_readings(db_path: str) -> None:
    """Independently recompute every valid row from the stored readings.

    This is the core cross-process guarantee: a ``valid`` daily result may
    only exist while its window has all 25 whole-hour readings, no break and
    the exact telescoping usage sum.
    """
    with connection(_settings(db_path)) as conn:
        rows = conn.execute(
            "SELECT meter_id, day_start_us, day_end_us, usage_total, "
            "interval_count, reading_count FROM daily_results "
            "WHERE status = 'valid'"
        ).fetchall()
        for row in rows:
            readings = conn.execute(
                "SELECT read_at_us, reading, meter_replaced FROM readings "
                "WHERE meter_id = ? AND read_at_us >= ? AND read_at_us <= ? "
                "ORDER BY read_at_us ASC",
                (row["meter_id"], row["day_start_us"], row["day_end_us"]),
            ).fetchall()
            present = {r["read_at_us"] for r in readings}
            for hour in range(25):
                assert row["day_start_us"] + hour * HOUR_US in present, (
                    "valid result with a missing whole-hour slot"
                )
            breaks = [
                readings[i]
                for i in range(1, len(readings))
                if bool(readings[i]["meter_replaced"])
                or readings[i]["reading"] < readings[i - 1]["reading"]
            ]
            assert not breaks, "valid result covers a break"
            # Usage telescopes over every adjacent pair in the window.
            usage = sum(
                readings[i]["reading"] - readings[i - 1]["reading"]
                for i in range(1, len(readings))
                if not (
                    bool(readings[i]["meter_replaced"])
                    or readings[i]["reading"] < readings[i - 1]["reading"]
                )
            )
            assert row["usage_total"] == usage
            assert row["interval_count"] == 24
            assert row["reading_count"] == len(readings)


def _settings(db_path: str):
    from app.config import Settings

    return Settings(db_path=db_path, busy_timeout_ms=30_000)


@pytest.fixture()
def two_servers(db_path):
    servers = [Server(db_path, _free_port()).start() for _ in range(2)]
    yield servers
    for server in servers:
        server.stop()


def test_concurrent_issuance_creates_exactly_one_valid_row(two_servers):
    with _client(two_servers[0]) as client:
        _seed_complete_day(client)

    def issue(server):
        with _client(server) as client:
            return client.post(
                "/api/v1/daily-results/issue",
                json={"meter_id": "M1", "date": DAY},
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(issue, [two_servers[0], two_servers[1]] * 3)
        )
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["status"] == "valid" for r in responses)
    # Serialised writers: exactly one insert, every later call is a retry.
    flags = [r.json()["idempotent_retry"] for r in responses]
    assert flags.count(False) == 1
    assert flags.count(True) == 5
    db_path = two_servers[0].db_path
    with connection(_settings(db_path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_results"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM daily_results WHERE status = 'valid'"
        ).fetchone()[0] == 1


def test_database_forbids_two_valid_rows_for_one_day(two_servers):
    db_path = two_servers[0].db_path
    with _client(two_servers[0]) as client:
        _seed_complete_day(client)
        assert (
            client.post(
                "/api/v1/daily-results/issue",
                json={"meter_id": "M1", "date": DAY},
            ).status_code
            == 200
        )
    start_us = _day_start_us(db_path)
    raw = sqlite3.connect(db_path)
    raw.isolation_level = None
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute(
            "INSERT INTO daily_results (meter_id, day, day_start_us, day_end_us,"
            " status, usage_total, interval_count, reading_count, "
            "missing_slot_count, break_count, content_hash, issued_commit_seq) "
            "VALUES ('M1', ?, ?, ?, 'valid', 1, 24, 25, 0, 0, 'x', 999)",
            (DAY, start_us, start_us + DAY_US),
        )
    raw.close()


def test_concurrent_mutation_and_issue_never_contradicts_readings(two_servers):
    db_path = two_servers[0].db_path
    with _client(two_servers[0]) as client:
        _seed_complete_day(client)
        client.post(
            "/api/v1/daily-results/issue",
            json={"meter_id": "M1", "date": DAY},
        )

    stop = time.monotonic() + 8
    rng = random.Random(11)

    def issuer():
        with _client(two_servers[rng.randrange(2)]) as client:
            while time.monotonic() < stop:
                client.post(
                    "/api/v1/daily-results/issue",
                    json={"meter_id": "M1", "date": DAY},
                )

    def mutator():
        # Alternate undo / re-insert of one whole-hour reading.  Between the
        # two calls the slot is missing, so an issued (valid) result can only
        # exist while the full day is present.
        with _client(two_servers[rng.randrange(2)]) as client:
            while time.monotonic() < stop:
                hour = rng.randrange(25)
                client.post(
                    "/api/v1/readings/undo",
                    json={"source_record_ids": [_sid(hour)]},
                )
                client.post(
                    "/api/v1/readings",
                    json={"readings": [_hourly_reading(hour)]},
                )

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(issuer) for _ in range(2)]
        futures += [pool.submit(mutator) for _ in range(3)]
        for future in futures:
            future.result()

    # Restore the pristine complete day and re-issue for a clean end state.
    with _client(two_servers[1]) as client:
        client.post(
            "/api/v1/readings",
            json={"readings": [_hourly_reading(h) for h in range(25)]},
        )
        final = client.post(
            "/api/v1/daily-results/issue",
            json={"meter_id": "M1", "date": DAY},
        ).json()
        assert final["status"] == "valid"
        assert final["usage_total"] == 240
    assert_valid_results_match_readings(db_path)


def test_kill_mid_flight_leaves_no_contradicting_valid_result(db_path, settings):
    initialize = __import__("app.db", fromlist=["initialize"]).initialize
    initialize(settings)
    server = Server(db_path, _free_port()).start()
    try:
        with _client(server) as client:
            _seed_complete_day(client)
            client.post(
                "/api/v1/daily-results/issue",
                json={"meter_id": "M1", "date": DAY},
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                hour = random.Random().randrange(25)
                client.post(
                    "/api/v1/readings/undo",
                    json={"source_record_ids": [_sid(hour)]},
                )
                client.post(
                    "/api/v1/readings/undo",
                    json={"source_record_ids": ["never-seen"]},
                )
                client.post(
                    "/api/v1/daily-results/issue",
                    json={"meter_id": "M1", "date": DAY},
                )
                client.post(
                    "/api/v1/readings",
                    json={"readings": [_hourly_reading(hour)]},
                )
            # Hard crash: no shutdown handlers, open transactions roll back.
            server.kill()
    finally:
        server.stop()

    # No valid row may describe readings that no longer exist.
    assert_valid_results_match_readings(db_path)
    with connection(settings) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_restart_preserves_daily_result_and_expiration(db_path, settings):
    initialize = __import__("app.db", fromlist=["initialize"]).initialize
    initialize(settings)
    server = Server(db_path, _free_port()).start()
    try:
        with _client(server) as client:
            _seed_complete_day(client)
            issued = client.post(
                "/api/v1/daily-results/issue",
                json={"meter_id": "M1", "date": DAY},
            ).json()
            client.post(
                "/api/v1/readings/undo",
                json={"source_record_ids": [_sid(7)]},
            )
    finally:
        server.stop()

    restarted = Server(db_path, _free_port()).start()
    try:
        with _client(restarted) as client:
            row = client.get(f"/api/v1/daily-results/M1/{DAY}").json()
            assert row["status"] == "expired"
            assert row["issued_commit_seq"] == issued["issued_commit_seq"]
            assert row["usage_total"] == 240
            # Restore and re-issue after restart.
            client.post(
                "/api/v1/readings",
                json={"readings": [_hourly_reading(7)]},
            )
            again = client.post(
                "/api/v1/daily-results/issue",
                json={"meter_id": "M1", "date": DAY},
            ).json()
            assert again["status"] == "valid"
            assert again["usage_total"] == 240
    finally:
        restarted.stop()
    assert_valid_results_match_readings(db_path)
