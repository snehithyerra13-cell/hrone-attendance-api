"""
Employee Attendance & Analytics API

Implemented in compliance with OpenAPI 3.0.3 contract and business rules R1 - R10.
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import os
import re
from typing import Any, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Database Initialization & Indexes
# --------------------------------------------------------------------------- #
def init_db():
    """Create all required MongoDB indexes idempotently at startup."""
    try:
        # employees collection
        db.employees.create_index([("emp_code", 1)], unique=True)
        db.employees.create_index([("department", 1)])
        db.employees.create_index([("joined_on", 1)])
        db.employees.create_index([("department", 1), ("joined_on", 1)])

        # attendance_logs collection
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
        db.attendance_logs.create_index([("date", 1), ("late_minutes", -1)])
        db.attendance_logs.create_index([("date", 1), ("status", 1)])
        db.attendance_logs.create_index([("status", 1), ("date", -1)])
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helper Functions (Rules R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], places: int) -> Optional[float]:
    """R8: Round half-up to exact decimal places (financial rounding)."""
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal("10") ** -places
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def to_epoch_ms(val: Any) -> Optional[int]:
    """Convert a datetime (naive or aware) to epoch milliseconds."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=UTC)
        return int(val.timestamp() * 1000)
    return None


def to_utc_dt(epoch_ms: Optional[int]) -> Optional[datetime]:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds (R1)."""
    if epoch_ms is None:
        return None
    epoch_sec = epoch_ms // 1000
    return datetime.fromtimestamp(epoch_sec, tz=UTC)


def to_ist(dt: datetime) -> datetime:
    """Convert datetime to IST timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def compute_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Compute attendance date (YYYY-MM-DD) in IST.
    For overnight shifts (shift_end <= shift_start), a punch-in earlier than
    shift_end in IST belongs to the shift that started the previous calendar day.
    """
    punch_ist = to_ist(punch_in_dt)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    is_overnight = (end_h, end_m) <= (start_h, start_m)
    if is_overnight:
        shift_end_time = time(end_h, end_m, 0)
        if punch_ist.time() < shift_end_time:
            return (punch_ist.date() - timedelta(days=1)).isoformat()
    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: Late calculation.
    Grace period is 10 minutes. A punch-in is late only if strictly > 10:00 after shift_start.
    When late, late_minutes = floor((punch_in - shift_start) in minutes). Otherwise 0.
    """
    punch_ist = to_ist(punch_in_dt)
    date_obj = date.fromisoformat(attendance_date)
    start_h, start_m = map(int, shift_start.split(":"))
    shift_start_dt = datetime(date_obj.year, date_obj.month, date_obj.day, start_h, start_m, 0, tzinfo=IST)

    diff_sec = (punch_ist - shift_start_dt).total_seconds()
    if diff_sec > 600:  # strictly more than 10 minutes (600 seconds)
        return int(diff_sec // 60)
    return 0


def compute_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3: Overtime calculation.
    Whole minutes (floor) between shift_end and punch_out, counted only if >= 30; otherwise 0.
    For overnight shifts, shift_end is on attendance_date + 1 day.
    """
    punch_ist = to_ist(punch_out_dt)
    date_obj = date.fromisoformat(attendance_date)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    is_overnight = (end_h, end_m) <= (start_h, start_m)
    end_date = date_obj + timedelta(days=1) if is_overnight else date_obj
    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, end_h, end_m, 0, tzinfo=IST)

    diff_sec = (punch_ist - shift_end_dt).total_seconds()
    if diff_sec >= 1800:  # at least 30 minutes (1800 seconds)
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    return round_half_up(diff_sec / 3600.0, 2)


def is_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day is true when the rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(start_date_str: str, end_date_str: str) -> int:
    """R7: Working days are Mon-Fri (no holiday calendar)."""
    start_d = date.fromisoformat(start_date_str)
    end_d = date.fromisoformat(end_date_str)
    if start_d > end_d:
        return 0
    cur = start_d
    count = 0
    while cur <= end_d:
        if cur.weekday() < 5:  # Mon-Fri
            count += 1
        cur += timedelta(days=1)
    return count


def format_history_entry(h: dict) -> dict:
    """Format history entry converting internal BSON datetimes to epoch milliseconds."""
    changes = {}
    for k, v in h.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            from_val = to_epoch_ms(from_val)
            to_val = to_epoch_ms(to_val)
        changes[k] = {"from": from_val, "to": to_val}
    return {
        "at": to_epoch_ms(h.get("at")),
        "by": h.get("by"),
        "reason": h.get("reason"),
        "changes": changes,
    }


def format_attendance_doc(doc: dict) -> dict:
    """Format attendance document for API response matching AttendanceRecord schema."""
    history = [format_history_entry(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def format_employee_doc(doc: dict) -> dict:
    """Format employee document for API response matching Employee schema."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Pydantic Request / Response Models
# --------------------------------------------------------------------------- #
class StatusEnum(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatusEnum(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def validate_shift(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatusEnum = PresenceStatusEnum.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[StatusEnum] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe: Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"MongoDB unavailable: {str(e)}",
        )


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforced unique by MongoDB index on emp_code."""
    now_utc = datetime.now(UTC).replace(microsecond=0)
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_utc,
    }
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Record punch-in. Race-safe unique insert on (emp_code, date)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = to_utc_dt(body.punched_at)
    else:
        punch_in_dt = datetime.now(UTC).replace(microsecond=0)

    attendance_date = compute_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """Record punch-out. Closes most recent open record whose punch_in <= punched_at."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = to_utc_dt(body.punched_at)
    else:
        punch_out_dt = datetime.now(UTC).replace(microsecond=0)

    # Find the most recent record whose punch_in <= punch_out_dt
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", -1)],
    )
    if not rec:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="that record is already punched out",
        )

    punch_in_dt = rec["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)

    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be strictly after punch_in",
        )
    if diff_sec > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punched_at cannot be more than 24 hours after punch_in",
        )

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_minutes = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], rec["date"])
    half_day = is_half_day(work_hours)

    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated_doc is None:
        raise HTTPException(
            status_code=409,
            detail="that record is already punched out",
        )

    return format_attendance_doc(updated_doc)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusEnum] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date descending, then emp_code ascending."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be <= date_to",
        )

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_q = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q
    if status:
        q["status"] = status.value

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(..., description="Employee code"),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="Attendance date YYYY-MM-DD"),
    body: RegularizeRequest = ...,
):
    """Regularize (manually correct) one day's record and append audit trail."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    target_status = body.status.value if body.status is not None else rec["status"]

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out must not be supplied when setting status to ABSENT or LEAVE",
            )
        target_punch_in = None
        target_punch_out = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        # Presence status
        if body.punch_in is not None:
            target_punch_in = to_utc_dt(body.punch_in)
        else:
            target_punch_in = rec.get("punch_in")

        if target_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires punch_in",
            )

        if target_punch_in.tzinfo is None:
            target_punch_in = target_punch_in.replace(tzinfo=UTC)

        calc_date = compute_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in falls on attendance date {calc_date}, does not match record date {date}",
            )

        if body.punch_out is not None:
            target_punch_out = to_utc_dt(body.punch_out)
        else:
            target_punch_out = rec.get("punch_out")

        if target_punch_out is not None and target_punch_out.tzinfo is None:
            target_punch_out = target_punch_out.replace(tzinfo=UTC)

        if target_punch_out is not None:
            diff_sec = (target_punch_out - target_punch_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 24 * 3600:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and within 24 hours",
                )
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime_minutes = compute_overtime(
                target_punch_out, emp["shift_start"], emp["shift_end"], date
            )
            target_half_day = is_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

        target_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date)

    # Compare old and new fields
    old_punch_in = rec.get("punch_in")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=UTC)

    old_punch_out = rec.get("punch_out")
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=UTC)

    changes = {}
    if rec["status"] != target_status:
        changes["status"] = {"from": rec["status"], "to": target_status}
    if old_punch_in != target_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": target_punch_in}
    if old_punch_out != target_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": target_punch_out}
    if rec.get("work_hours") != target_work_hours:
        changes["work_hours"] = {"from": rec.get("work_hours"), "to": target_work_hours}
    if rec.get("late_minutes", 0) != target_late_minutes:
        changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": target_late_minutes}
    if rec.get("overtime_minutes", 0) != target_overtime_minutes:
        changes["overtime_minutes"] = {
            "from": rec.get("overtime_minutes", 0),
            "to": target_overtime_minutes,
        }
    if rec.get("half_day", False) != target_half_day:
        changes["half_day"] = {"from": rec.get("half_day", False), "to": target_half_day}

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="A request that changes nothing is rejected",
        )

    now_utc = datetime.now(UTC).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomic update ensuring audit history is appended safely
    history_len = len(rec.get("history", []))
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], f"history.{history_len}": {"$exists": False}},
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated_doc is None:
        raise HTTPException(
            status_code=409,
            detail="Concurrent modification conflict on attendance history",
        )

    return format_attendance_doc(updated_doc)


# --------------------------------------------------------------------------- #
# Analytics Pipelines & Endpoints
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month_start: str, month_end: str) -> list:
    """Build aggregation pipeline for GET /analytics/employees/{emp_code}/monthly."""
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$addFields": {
                "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(..., description="Employee code"),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month YYYY-MM"),
):
    """Monthly attendance summary for one employee backed by MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    # Working days: Mon-Fri from max(month_start, joined_on) to month_end
    start_working = max(month_start, emp["joined_on"])
    working_days = count_working_days(start_working, month_end)

    pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        res = results[0]
        present_days = round_half_up(res.get("present_days", 0.0), 2)
        leave_days = res.get("leave_days", 0)
        late_count = res.get("late_count", 0)
        total_late_minutes = res.get("total_late_minutes", 0)
        total_overtime_minutes = res.get("total_overtime_minutes", 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100.0, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month_start: str, month_end: str, department: Optional[str] = None) -> list:
    """Build aggregation pipeline for GET /analytics/departments/summary across employees and attendance_logs."""
    match_stage = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "department": {"$first": "$department"},
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$addFields": {
                "dayOfWeek": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        0,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "department": {"$first": "$department"},
                "headcount": {"$first": "$headcount"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$type": "$logs.work_hours"}, "missing"]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$type": "$logs.work_hours"}, "missing"]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month YYYY-MM"),
    department: Optional[str] = Query(None, description="Restrict to one department (exact match)"),
):
    """Department summary for a month backed by MongoDB aggregation across both collections."""
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = build_department_summary_pipeline(month_start, month_end, department)
    raw_items = list(db.employees.aggregate(pipeline))

    items = []
    for row in raw_items:
        c = row.get("work_hours_count", 0)
        s = row.get("work_hours_sum", 0.0)
        avg_wh = round_half_up(s / c, 2) if c > 0 else None

        items.append({
            "department": row["department"],
            "headcount": row["headcount"],
            "present_days": round_half_up(row.get("present_days", 0.0), 2),
            "avg_work_hours": avg_wh,
            "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0),
            "leave_count": row.get("leave_count", 0),
            "on_duty_count": row.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


def build_leaderboard_pipeline(month_start: str, month_end: str, limit: int, department: Optional[str] = None) -> list:
    """Build aggregation pipeline for GET /analytics/leaderboard/late using window ranking."""
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "emp_code": {"$first": "$emp_code"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month YYYY-MM"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None, description="Filter by department"),
):
    """Late-comers leaderboard for a month with competition ranking (1, 2, 2, 4) in MongoDB."""
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline = build_leaderboard_pipeline(month_start, month_end, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list:
    """Build gap-filled daily trend aggregation pipeline with moving average."""
    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=UTC)
    to_dt = datetime.fromisoformat(to_date).replace(tzinfo=UTC)

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "date_obj": {"$literal": from_dt},
            }
        },
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt + timedelta(days=1)],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"current_date": "$date"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$current_date"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {"$unwind": "$emp"},
                    {"$match": {"emp.department": department}},
                ],
                "as": "day_logs",
            }
        },
        {
            "$addFields": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$day_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"current_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$current_date"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "active_employees",
            }
        },
        {
            "$addFields": {
                "headcount": {"$size": "$active_employees"}
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(..., description="Department name"),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend with gap filling and 7-day moving average."""
    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if d_to < d_from:
        raise HTTPException(
            status_code=422,
            detail="'to' date must be >= 'from' date",
        )

    if (d_to - d_from).days > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    raw_items = list(db.employees.aggregate(pipeline))

    items = []
    for r in raw_items:
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": round_half_up(r.get("present_count", 0.0), 2),
            "late_count": r.get("late_count", 0),
            "attendance_rate": round_half_up(r.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(r.get("moving_avg_7d"), 4),
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
class ExplainEndpointEnum(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: ExplainEndpointEnum = Path(..., description="Endpoint to explain"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[StatusEnum] = Query(None),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Run explain with executionStats on the specified endpoint's main query or pipeline."""
    if endpoint == ExplainEndpointEnum.attendance_list:
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from > date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            date_q = {}
            if date_from:
                date_q["$gte"] = date_from
            if date_to:
                date_q["$lte"] = date_to
            q["date"] = date_q
        if status:
            q["status"] = status.value

        skip = (page - 1) * page_size
        explain_doc = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint.value,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(explain_doc),
        }

    elif endpoint == ExplainEndpointEnum.employee_monthly:
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code required")
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"
        pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)

        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint.value,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(explain_doc),
        }

    elif endpoint == ExplainEndpointEnum.department_summary:
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"
        pipeline = build_department_summary_pipeline(month_start, month_end, department)

        explain_doc = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint.value,
            "collection": "employees",
            "explain": jsonable_encoder(explain_doc),
        }

    elif endpoint == ExplainEndpointEnum.late_leaderboard:
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"
        pipeline = build_leaderboard_pipeline(month_start, month_end, limit, department)

        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint.value,
            "collection": "attendance_logs",
            "explain": jsonable_encoder(explain_doc),
        }

    elif endpoint == ExplainEndpointEnum.department_trend:
        if not department:
            raise HTTPException(status_code=422, detail="department required")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="from and to dates required")
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="to must be >= from")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain_doc = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint.value,
            "collection": "employees",
            "explain": jsonable_encoder(explain_doc),
        }
