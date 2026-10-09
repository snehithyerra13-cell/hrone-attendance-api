from datetime import datetime, timezone
import pytest

def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

def test_create_employee_success(client):
    payload = {
        "emp_code": "EMP9999",
        "name": "Kiran Kumar",
        "email": "kiran@example.com",
        "department": "Engineering",
        "shift_start": "09:00",
        "shift_end": "18:00",
        "joined_on": "2026-07-01",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP9999"
    assert data["name"] == "Kiran Kumar"
    assert isinstance(data["created_at"], int)
    assert "_id" not in data

def test_create_employee_duplicate_conflict(client):
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao Duplicate",
        "email": "asha.dup@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 409

def test_create_employee_validation_failure(client):
    # Invalid emp_code format
    payload = {
        "emp_code": "INVALID",
        "name": "Invalid Code",
        "email": "invalid@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }
    res = client.post("/employees", json=payload)
    assert res.status_code == 422

    # Equal shift_start and shift_end
    payload["emp_code"] = "EMP1234"
    payload["shift_start"] = "09:30"
    payload["shift_end"] = "09:30"
    res = client.post("/employees", json=payload)
    assert res.status_code == 422

def test_list_employees(client):
    res = client.get("/employees")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 6
    assert len(data["items"]) == 6
    assert data["page"] == 1
    assert data["page_size"] == 20

    # Filter by department
    res_eng = client.get("/employees?department=Engineering")
    assert res_eng.status_code == 200
    data_eng = res_eng.json()
    assert data_eng["total"] == 2
    assert len(data_eng["items"]) == 2
    assert all(item["department"] == "Engineering" for item in data_eng["items"])

def test_punch_in_success(client):
    # Punch in for EMP0001 on 2026-07-20 at 09:28 IST (03:58 UTC)
    # 2026-07-20 03:58:00 UTC = 1784519880000 ms
    dt = datetime(2026, 7, 20, 3, 58, 0, tzinfo=timezone.utc)
    ts_ms = int(dt.timestamp() * 1000)

    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": ts_ms,
        "status": "PRESENT"
    })
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["date"] == "2026-07-20"
    assert data["late_minutes"] == 0
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["history"] == []
    assert "_id" not in data
    assert "id" not in data

def test_punch_in_late_rule_r2(client):
    # Shift start is 09:30 IST.
    # 09:40:00 IST -> late_minutes = 0 (10 min grace)
    dt_grace = datetime(2026, 7, 21, 4, 10, 0, tzinfo=timezone.utc)  # 09:40:00 IST
    res_grace = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(dt_grace.timestamp() * 1000),
    })
    assert res_grace.status_code == 201
    assert res_grace.json()["late_minutes"] == 0

    # 10:05:00 IST (35 min after shift start) -> late_minutes = 35
    dt_late = datetime(2026, 7, 22, 4, 35, 0, tzinfo=timezone.utc)  # 10:05:00 IST
    res_late = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(dt_late.timestamp() * 1000),
    })
    assert res_late.status_code == 201
    assert res_late.json()["late_minutes"] == 35

def test_punch_in_overnight_shift_r1(client):
    # EMP0005 has shift 22:00 - 06:00
    # Punch-in at 2026-07-21 05:45:00 IST (before shift_end 06:00) belongs to 2026-07-20
    dt = datetime(2026, 7, 21, 0, 15, 0, tzinfo=timezone.utc)  # 05:45 IST on July 21
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005",
        "punched_at": int(dt.timestamp() * 1000),
    })
    assert res.status_code == 201
    assert res.json()["date"] == "2026-07-20"

def test_punch_in_duplicate_conflict(client):
    # Already has log for EMP0001 on 2026-07-06
    dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(dt.timestamp() * 1000),
    })
    assert res.status_code == 409

def test_punch_in_unknown_employee(client):
    dt = datetime(2026, 7, 20, 4, 0, 0, tzinfo=timezone.utc)
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP9999",
        "punched_at": int(dt.timestamp() * 1000),
    })
    assert res.status_code == 404

def test_punch_out_success(client):
    # EMP0006 on 2026-07-07 has open punch_in at 04:05:00 UTC (09:35 IST)
    # Punch out at 13:35 UTC (19:05 IST) -> 9.5 hours work, 35 min overtime (shift_end 18:30)
    dt_out = datetime(2026, 7, 7, 13, 35, 0, tzinfo=timezone.utc)
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0006",
        "punched_at": int(dt_out.timestamp() * 1000),
    })
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 9.5
    assert data["overtime_minutes"] == 35
    assert data["half_day"] is False
    assert data["punch_out"] == int(dt_out.timestamp() * 1000)

def test_punch_out_half_day_rule_r5(client):
    # First punch in on a new date
    dt_in = datetime(2026, 7, 23, 4, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": int(dt_in.timestamp() * 1000),
    })
    # Punch out 3.5 hours later
    dt_out = datetime(2026, 7, 23, 7, 30, 0, tzinfo=timezone.utc)
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(dt_out.timestamp() * 1000),
    })
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 3.5
    assert data["half_day"] is True

def test_punch_out_already_punched_out(client):
    # EMP0001 on 2026-07-06 is already punched out
    dt_out = datetime(2026, 7, 6, 14, 0, 0, tzinfo=timezone.utc)
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": int(dt_out.timestamp() * 1000),
    })
    assert res.status_code == 409

def test_list_attendance(client):
    res = client.get("/attendance")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 9
    assert len(data["items"]) == 9

    # Test filtering by date range
    res_range = client.get("/attendance?date_from=2026-07-06&date_to=2026-07-07")
    assert res_range.status_code == 200
    data_range = res_range.json()
    assert data_range["total"] == 6
    assert all("2026-07-06" <= item["date"] <= "2026-07-07" for item in data_range["items"])

    # Test invalid date range
    res_inv = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert res_inv.status_code == 422

def test_regularize_attendance_success(client):
    # Regularize EMP0006 on 2026-07-07 (open record) to punched out
    dt_out = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
    res = client.patch("/attendance/EMP0006/2026-07-07", json={
        "punch_out": int(dt_out.timestamp() * 1000),
        "reason": "Forgot to punch out on kiosk",
        "regularized_by": "manager.sam"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] is not None
    assert len(data["history"]) == 1
    assert data["history"][0]["by"] == "manager.sam"
    assert data["history"][0]["reason"] == "Forgot to punch out on kiosk"
    assert "punch_out" in data["history"][0]["changes"]
    assert "work_hours" in data["history"][0]["changes"]

def test_regularize_attendance_to_absent(client):
    # Change status to ABSENT
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "ABSENT",
        "reason": "Marked present by mistake",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ABSENT"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert len(data["history"]) >= 1

def test_regularize_absent_with_punch_times_fails(client):
    # Sending punch_in with ABSENT must return 422
    dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "ABSENT",
        "punch_in": int(dt.timestamp() * 1000),
        "reason": "Invalid regularization attempt",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 422

def test_regularize_no_changes_fails(client):
    # Sending request with exact same values should yield 422
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "PRESENT",
        "reason": "Trying no-op change",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 422
