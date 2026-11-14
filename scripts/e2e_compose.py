"""End-to-end smoke test against a running Compose deployment.

Usage:
    python scripts/e2e_compose.py http://localhost:8000
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

DAILY_DAY = "2026-02-01"
UTC = timezone.utc


def _slot(hour: int) -> str:
    base = datetime.fromisoformat(DAILY_DAY).replace(tzinfo=UTC)
    return (base + timedelta(hours=hour)).isoformat().replace("+00:00", "Z")


def _hourly(meter: str, hour: int) -> dict:
    return {
        "meter_id": meter,
        "source_record_id": f"e2e-day-{meter}-h{hour:02d}",
        "read_at": _slot(hour),
        "reading": 2000 + hour * 8,
        "meter_replaced": False,
    }


def main(base_url: str, second_url: str | None = None) -> int:
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        health = client.get("/health")
        assert health.status_code == 200, health.text
        print("health:", health.json())

        batch = {
            "readings": [
                {
                    "meter_id": "E2E-M1",
                    "source_record_id": "e2e-00",
                    "read_at": "2026-02-01T00:00:00Z",
                    "reading": 1000,
                },
                {
                    "meter_id": "E2E-M1",
                    "source_record_id": "e2e-02",
                    "read_at": "2026-02-01T02:00:00Z",
                    "reading": 1300,
                },
                {
                    "meter_id": "E2E-M1",
                    "source_record_id": "e2e-03",
                    "read_at": "2026-02-01T03:00:00Z",
                    "reading": 20,
                    "meter_replaced": True,
                },
                {
                    "meter_id": "E2E-M1",
                    "source_record_id": "e2e-04",
                    "read_at": "2026-02-01T04:00:00Z",
                    "reading": 75,
                },
            ]
        }
        # First submit with a gap (02:00 missing), then backfill it.
        initial = [r for r in batch["readings"] if r["source_record_id"] != "e2e-02"]
        backfill = [r for r in batch["readings"] if r["source_record_id"] == "e2e-02"]
        assert client.post("/api/v1/readings", json={"readings": initial}).status_code == 200
        assert client.post("/api/v1/readings", json={"readings": backfill}).status_code == 200

        retry = client.post("/api/v1/readings", json=batch)
        assert retry.status_code == 200 and retry.json()["changed"] is False
        print("idempotent retry:", retry.json()["counts"])

        conflict = client.post(
            "/api/v1/readings",
            json={
                "readings": [
                    {
                        "meter_id": "E2E-M1",
                        "source_record_id": "e2e-00",
                        "read_at": "2026-02-01T00:00:00Z",
                        "reading": 1001,
                    }
                ]
            },
        )
        assert conflict.status_code == 409, conflict.text
        print("conflict rejected:", conflict.json()["conflicts"][0]["type"])

        undo = client.post(
            "/api/v1/readings/undo", json={"source_record_ids": ["e2e-02"]}
        )
        assert undo.status_code == 200
        print("undo:", undo.json()["counts"])

        state = client.get("/api/v1/meters/E2E-M1").json()
        print("final state:")
        print(json.dumps(state, ensure_ascii=False, indent=2))
        # e2e-02 is gone: 1000 -> 20 is a meter-replacement break (no
        # usage), 20 -> 75 is 55.
        assert state["usage_total"] == 55
        assert state["breaks"][0]["break_reason"] == "meter_replaced"

        listing = client.get("/api/v1/meters").json()
        assert any(m["meter_id"] == "E2E-M1" for m in listing)

        # ---- Daily quality results ------------------------------------
        daily_meter = "E2E-DAY"
        # Missing whole-hour readings -> not issued, nothing stored.
        missing = client.post(
            "/api/v1/daily-results/issue",
            json={"meter_id": daily_meter, "date": DAILY_DAY},
        ).json()
        assert missing["status"] == "not_issued"
        assert len(missing["missing_slots"]) == 25
        assert (
            client.get(f"/api/v1/daily-results/{daily_meter}/{DAILY_DAY}").status_code
            == 404
        )

        # Complete day (25 whole-hour readings, both midnights inclusive).
        assert (
            client.post(
                "/api/v1/readings",
                json={"readings": [_hourly(daily_meter, h) for h in range(25)]},
            ).status_code
            == 200
        )
        issued = client.post(
            "/api/v1/daily-results/issue",
            json={"meter_id": daily_meter, "date": DAILY_DAY},
        ).json()
        assert issued["status"] == "valid"
        assert issued["usage_total"] == 24 * 8
        assert issued["interval_count"] == 24
        first_issue_seq = issued["issued_commit_seq"]

        # Identical retry must not change the stored state.
        retry = client.post(
            "/api/v1/daily-results/issue",
            json={"meter_id": daily_meter, "date": DAILY_DAY},
        ).json()
        assert retry["idempotent_retry"] is True
        assert retry["issued_commit_seq"] == first_issue_seq

        # A reading mutation expires the result in the same transaction.
        victim = f"e2e-day-{daily_meter}-h10"
        client.post(
            "/api/v1/readings/undo", json={"source_record_ids": [victim]}
        )
        expired = client.get(
            f"/api/v1/daily-results/{daily_meter}/{DAILY_DAY}"
        ).json()
        assert expired["status"] == "expired"
        assert expired["usage_total"] == 24 * 8  # historical value retained

        # Re-issuing while the slot is missing fails and keeps it expired.
        failed = client.post(
            "/api/v1/daily-results/issue",
            json={"meter_id": daily_meter, "date": DAILY_DAY},
        ).json()
        assert failed["status"] == "not_issued"
        assert [s["hour"] for s in failed["missing_slots"]] == [10]
        assert (
            client.get(f"/api/v1/daily-results/{daily_meter}/{DAILY_DAY}").json()[
                "status"
            ]
            == "expired"
        )

        # Restore the reading; re-issue computes from the latest committed state.
        client.post(
            "/api/v1/readings", json={"readings": [_hourly(daily_meter, 10)]}
        )
        reissued = client.post(
            "/api/v1/daily-results/issue",
            json={"meter_id": daily_meter, "date": DAILY_DAY},
        ).json()
        assert reissued["status"] == "valid"
        assert reissued["issued_commit_seq"] > first_issue_seq
        assert reissued["usage_total"] == 24 * 8

        listing_daily = client.get(
            "/api/v1/daily-results", params={"meter_id": daily_meter}
        ).json()
        assert len(listing_daily) == 1 and listing_daily[0]["status"] == "valid"
        print("daily results: issue -> expire -> re-issue ok")

    # Optional: prove both processes serve the same data.
    if second_url:
        with httpx.Client(base_url=second_url, timeout=30.0) as other:
            other_state = other.get("/api/v1/meters/E2E-M1").json()
            other_daily = other.get(
                f"/api/v1/daily-results/E2E-DAY/{DAILY_DAY}"
            ).json()
        # Re-read from the first process: the daily-result flow above has
        # advanced the shared commit sequence after `state` was captured.
        with httpx.Client(base_url=base_url, timeout=30.0) as first:
            latest_state = first.get("/api/v1/meters/E2E-M1").json()
            latest_daily = first.get(
                f"/api/v1/daily-results/E2E-DAY/{DAILY_DAY}"
            ).json()
        assert other_state["commit_seq"] == latest_state["commit_seq"]
        assert other_state["intervals"] == latest_state["intervals"]
        assert other_daily == latest_daily
        assert other_daily["status"] == "valid"
        print(
            "second process sees the same snapshot:",
            latest_state["commit_seq"],
        )

    # /docs must be usable.
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        docs = client.get("/docs")
        assert docs.status_code == 200
        spec = client.get("/openapi.json").json()
        paths = spec["paths"]
        assert "/api/v1/readings" in paths
        assert "/api/v1/readings/undo" in paths
        assert "/api/v1/meters/{meter_id}" in paths
        assert "/api/v1/daily-results/issue" in paths
        assert "/api/v1/daily-results/{meter_id}/{date}" in paths
    print("openapi paths ok")
    return 0


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "BASE_URL", "http://localhost:8000"
    )
    second = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("SECOND_URL")
    raise SystemExit(main(base, second))
