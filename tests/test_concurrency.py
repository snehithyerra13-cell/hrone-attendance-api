import concurrent.futures
from datetime import datetime, timezone
import pytest

def test_concurrent_punch_in(client):
    # Two simultaneous punch-ins for EMP0002 on 2026-07-25
    dt = datetime(2026, 7, 25, 4, 0, 0, tzinfo=timezone.utc)
    ts_ms = int(dt.timestamp() * 1000)

    def do_punch():
        return client.post("/attendance/punch-in", json={
            "emp_code": "EMP0002",
            "punched_at": ts_ms,
            "status": "PRESENT"
        })

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch) for _ in range(5)]
        results = [f.result() for f in futures]

    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1
    assert statuses.count(409) == 4

def test_concurrent_punch_out(client):
    # Punch in first
    dt_in = datetime(2026, 7, 26, 4, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0002",
        "punched_at": int(dt_in.timestamp() * 1000),
    })

    # Simultaneous punch out
    dt_out = datetime(2026, 7, 26, 13, 0, 0, tzinfo=timezone.utc)
    def do_punch_out():
        return client.post("/attendance/punch-out", json={
            "emp_code": "EMP0002",
            "punched_at": int(dt_out.timestamp() * 1000),
        })

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_out) for _ in range(5)]
        results = [f.result() for f in futures]

    statuses = [r.status_code for r in results]
    assert statuses.count(200) == 1
    assert statuses.count(409) == 4
