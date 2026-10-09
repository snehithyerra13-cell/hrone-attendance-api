import pytest
from datetime import datetime, timezone
from pymongo import MongoClient

def test_employee_monthly_analytics(client):
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 2.0
    assert data["leave_days"] == 0
    assert data["late_count"] == 0
    assert data["total_late_minutes"] == 0
    assert data["total_overtime_minutes"] == 0
    assert data["attendance_pct"] == 8.7  # 2.0 / 23 * 100 = 8.6956... -> 8.7

def test_employee_monthly_zero_logs(client):
    # Create employee with 0 logs
    client.post("/employees", json={
        "emp_code": "EMP8888",
        "name": "Zero Logs Employee",
        "email": "zerologs@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-01",
    })
    res = client.get("/analytics/employees/EMP8888/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 23
    assert data["present_days"] == 0.0
    assert data["late_count"] == 0
    assert data["total_late_minutes"] == 0
    assert data["attendance_pct"] == 0.0

def test_employee_monthly_mid_month_joiner(client):
    # Employee joined on 2026-07-20 (Monday) -> 10 working days in July 2026 (July 20 to July 31)
    client.post("/employees", json={
        "emp_code": "EMP7777",
        "name": "Mid Month Joiner",
        "email": "midjoin@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-20",
    })
    res = client.get("/analytics/employees/EMP7777/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 10
    assert data["present_days"] == 0.0
    assert data["attendance_pct"] == 0.0

def test_employee_monthly_future_joiner(client):
    # Employee joined after the month
    client.post("/employees", json={
        "emp_code": "EMP6666",
        "name": "Future Joiner",
        "email": "futurejoin@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-08-01",
    })
    res = client.get("/analytics/employees/EMP6666/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 0
    assert data["present_days"] == 0.0
    assert data["attendance_pct"] is None

def test_employee_monthly_unknown_employee(client):
    res = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert res.status_code == 404

def test_department_summary_analytics(client):
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    items = data["items"]
    assert len(items) == 3

    # Check Engineering
    eng = next(item for item in items if item["department"] == "Engineering")
    assert eng["headcount"] == 2
    assert eng["present_days"] == 3.0
    assert eng["late_count"] == 0
    assert eng["avg_work_hours"] == 9.04

    # Check Sales
    sales = next(item for item in items if item["department"] == "Sales")
    assert sales["headcount"] == 2
    assert sales["present_days"] == 1.5
    assert sales["late_count"] == 1
    assert sales["total_late_minutes"] == 35
    assert sales["leave_count"] == 1
    assert sales["avg_work_hours"] == 6.29

    # Check Support
    support = next(item for item in items if item["department"] == "Support")
    assert support["headcount"] == 2
    assert support["present_days"] == 2.0
    assert support["avg_work_hours"] == 8.75

def test_department_summary_single_department(client):
    res = client.get("/analytics/departments/summary?month=2026-07&department=Sales")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["department"] == "Sales"

def test_late_leaderboard_competition_ranking(client, reset_database):
    db = reset_database
    # Insert multiple late records with ties to test competition ranking (1, 2, 2, 4)
    # EMP0001: 50 min late -> Rank 1
    # EMP0002: 30 min late -> Rank 2
    # EMP0003: 30 min late -> Rank 2 (tie with EMP0002)
    # EMP0004: 15 min late -> Rank 4 (skips rank 3)
    db.attendance_logs.delete_many({})
    db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 50},
        {"emp_code": "EMP0002", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 30},
        {"emp_code": "EMP0003", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 30},
        {"emp_code": "EMP0004", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 15},
    ])

    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert res.status_code == 200
    items = res.json()["items"]
    # Limit is 2, rank <= 2 should return 3 rows (rank 1, rank 2, rank 2)
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP0001"
    assert items[0]["rank"] == 1
    assert items[1]["emp_code"] == "EMP0002"
    assert items[1]["rank"] == 2
    assert items[2]["emp_code"] == "EMP0003"
    assert items[2]["rank"] == 2

    # Now with limit = 4, rank 4 employee is included
    res4 = client.get("/analytics/leaderboard/late?month=2026-07&limit=4")
    assert res4.status_code == 200
    items4 = res4.json()["items"]
    assert len(items4) == 4
    assert items4[3]["emp_code"] == "EMP0004"
    assert items4[3]["rank"] == 4

def test_department_trend(client):
    res = client.get("/analytics/departments/Sales/trend?from=2026-07-06&to=2026-07-10")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Sales"
    items = data["items"]
    assert len(items) == 5

    # July 6 (Monday): present_count = 1.0, headcount = 2 -> attendance_rate = 0.5
    assert items[0]["date"] == "2026-07-06"
    assert items[0]["is_working_day"] is True
    assert items[0]["headcount"] == 2
    assert items[0]["present_count"] == 1.0
    assert items[0]["attendance_rate"] == 0.5
    assert items[0]["moving_avg_7d"] == 0.5

    # July 7 (Tuesday): present_count = 0.5 (half day), headcount = 2 -> attendance_rate = 0.25
    assert items[1]["date"] == "2026-07-07"
    assert items[1]["present_count"] == 0.5
    assert items[1]["attendance_rate"] == 0.25
    assert items[1]["moving_avg_7d"] == 0.375

def test_department_trend_invalid_range(client):
    # 'to' before 'from'
    res = client.get("/analytics/departments/Sales/trend?from=2026-07-10&to=2026-07-05")
    assert res.status_code == 422

    # range > 92 days
    res_large = client.get("/analytics/departments/Sales/trend?from=2026-01-01&to=2026-06-01")
    assert res_large.status_code == 422

def test_department_trend_unknown_department(client):
    res = client.get("/analytics/departments/NonExistentDept/trend?from=2026-07-01&to=2026-07-05")
    assert res.status_code == 404

@pytest.mark.parametrize("endpoint, params", [
    ("attendance_list", {}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("department_trend", {"department": "Sales", "from": "2026-07-06", "to": "2026-07-10"}),
])
def test_admin_explain(client, endpoint, params):
    res = client.get(f"/admin/explain/{endpoint}", params=params)
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == endpoint
    assert "collection" in data
    assert "explain" in data
