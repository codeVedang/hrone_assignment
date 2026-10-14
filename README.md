# Employee Attendance & Analytics API

FastAPI and MongoDB implementation of the HROne trainee assignment. All application code is in `app/main.py`. All contracted endpoints are implemented; no known functionality is missing.

Requires Python 3.11+ and MongoDB 6.0+. From the repository root:

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# Set MONGO_URI and MONGO_DB in your shell, then:
uvicorn app.main:app --port 8000
```

Defaults are `mongodb://localhost:27017` and database `attendance_db`. PowerShell configuration example:

```powershell
$env:MONGO_URI = 'mongodb://localhost:27017'
$env:MONGO_DB = 'attendance_db'
```

`GET /health` pings MongoDB. Indexes are created automatically without altering seeded records. Interactive API documentation is at `http://localhost:8000/docs`.

Punch timestamps are strict integer epoch milliseconds, truncated to seconds. MongoDB stores aware UTC instants; attendance calendars use IST. Unique indexes protect inserts, and conditional updates protect punch-outs and append-only correction history. Analytics read stored derived fields, use record-weighted averages and competition ranks, and generate missing trend days in MongoDB. Explain uses the same query builders, filters and index hints as the corresponding endpoints.

Run the tests against a disposable database on a real MongoDB server (no production database is cleared):

```bash
pip install -r requirements-dev.txt
python -m pytest -q -m "not scale"
python -m pytest -q -m scale -s
```

Set `TEST_MONGO_URI` to override the default local test server. The scale test generates 100,000 logs and checks actual responses plus executionStats plans for all five explain targets. Other tests cover business-rule boundaries, overnight shifts, concurrency, legacy records, joiners, missing logs, ties and randomized comparisons against an independent Python oracle. Local validation: **59 tests passed on MongoDB 7.0.16**; all five explain targets had IXSCAN and no COLLSCAN. The slowest measured endpoint on the synthetic 100,000-log dataset was the department summary at about 1.01 seconds; this is a local measurement, not a latency guarantee.

Design choices and the starter defect review are in `DECISIONS.md` and `REVIEW.md`. This implementation was prepared with AI assistance; review and understand the code and tests before the live walkthrough.
