# Employee Attendance & Analytics API (HROne Assignment)

A high-performance, race-safe backend API built with FastAPI and MongoDB for employee attendance tracking, manual regularization with audit trails, and reporting analytics.

---

## 1. Technology Stack

- **Python**: 3.11+ (Tested on Python 3.13)
- **Framework**: FastAPI
- **Server**: Uvicorn
- **Database**: MongoDB 6.0+ (Tested on MongoDB 8.2)
- **Driver**: PyMongo 4.7+
- **Validation**: Pydantic v2
- **Testing**: Pytest & HTTPX

---

## 2. Prerequisites

- Python 3.11 or higher installed on your system.
- MongoDB Server 6.0 or newer running locally on `localhost:27017` or accessible via a connection URI.

---

## 3. Installation & Setup (Windows & PowerShell)

### Step 1: Clone the Repository
```powershell
git clone <repository-url>
cd <repository-folder>
```

### Step 2: Create and Activate Virtual Environment
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### Step 3: Install Dependencies
```powershell
pip install -r requirements.txt
pip install pytest httpx
```

### Step 4: Configure Environment Variables
Copy `.env.example` to `.env`:
```powershell
Copy-Item .env.example .env
```

Default configuration in `.env`:
```ini
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### Step 5: Seed Sample Data
```powershell
python sample_seed.py
```

---

## 4. Running the Application

Start the FastAPI application with Uvicorn:
```powershell
uvicorn app.main:app --port 8000 --reload
```

Once started:
- **API Base URL**: `http://localhost:8000`
- **Interactive Swagger Docs**: `http://localhost:8000/docs`
- **OpenAPI Schema**: `http://localhost:8000/openapi.json`
- **Health / Readiness Probe**: `http://localhost:8000/health`

---

## 5. Running Automated Tests

Run the full pytest test suite:
```powershell
pytest -v
```

---

## 6. Endpoint Summary

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/health` | Readiness probe returning `200 OK` if MongoDB answers ping, else `503`. |
| `POST` | `/employees` | Creates an employee with unique `emp_code` and shift timings. |
| `GET` | `/employees` | Lists employees with pagination and optional department filtering. |
| `POST` | `/attendance/punch-in` | Records punch-in; enforces business rules (R1, R2); race-safe. |
| `POST` | `/attendance/punch-out` | Closes most recent open punch-in; computes work hours, overtime, and half day (R3–R5). |
| `GET` | `/attendance` | Paginated listing of attendance logs sorted by `date DESC, emp_code ASC`. |
| `PATCH` | `/attendance/{emp_code}/{date}` | Regularizes records, recomputes derived metrics, and appends immutable audit history. |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly attendance report for a specific employee via MongoDB aggregation. |
| `GET` | `/analytics/departments/summary` | Department-level monthly summary including accurate 0-log headcount. |
| `GET` | `/analytics/leaderboard/late` | Late-comers ranking using competition rank (`1, 2, 2, 4`) with post-ranking limits. |
| `GET` | `/analytics/departments/{department}/trend` | Gap-filled daily trend using `$densify` with 7-day moving averages. |
| `GET` | `/admin/explain/{endpoint}` | Runs `explain(executionStats)` proving queries utilize `IXSCAN` and avoid `COLLSCAN`. |

---

## 7. Submission Verification Checklist

- [x] All application code resides in `app/main.py`.
- [x] Application starts cleanly with `uvicorn app.main:app --port 8000`.
- [x] All database indexes created idempotently at startup.
- [x] `MONGO_URI` and `MONGO_DB` read dynamically from environment variables.
- [x] `REVIEW.md` and `DECISIONS.md` fully completed.
- [x] No `.env`, secrets, or Dockerfile included in Git tracking.
