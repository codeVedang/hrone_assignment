"""Attendance contract v2: UTC storage, IST calendars, atomic writes and Mongo analytics."""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
Millis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]


def valid_date(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    Date.fromisoformat(value)
    return value


def valid_month(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("month must be YYYY-MM")
    Date.fromisoformat(value + "-01")
    return value


DateString = Annotated[str, AfterValidator(valid_date)]
MonthString = Annotated[str, AfterValidator(valid_month)]
ShiftTime = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                     tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=3000,
                     connectTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def create_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True, name="employee_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="department_code")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="joined_department")
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)],
                                    unique=True, name="employee_day")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_employee")
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)],
                                    name="status_date_employee")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="employee_punch")


@asynccontextmanager
async def lifespan(app):
    # A disconnected service can still start and return an honest 503 readiness probe.
    # On recovery health() retries this idempotent setup before declaring readiness.
    app.state.indexes_ready = False
    try:
        create_indexes()
        app.state.indexes_ready = True
    except PyMongoError:
        pass
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_error(request, exc):
    return JSONResponse(status_code=503, content={"detail": "MongoDB unavailable"})


class Input(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(Input):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: DateString

    @model_validator(mode="after")
    def unequal_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutIn(Input):
    emp_code: str
    punched_at: Millis = Field(default_factory=lambda: epoch_ms(now()))


class PunchInIn(PunchOutIn):
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class RegularizeIn(Input):
    # Defaults represent omission only. Explicit null is rejected by the field type.
    status: Status = None
    punch_in: Millis = None
    punch_out: Millis = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


def now():
    return datetime.now(UTC).replace(microsecond=0)


def instant(ms):
    return EPOCH + timedelta(seconds=ms // 1000)


def epoch_ms(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def json_value(value):
    """Convert database dates and decimals, including dates nested in history."""
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_value(v) for v in value]
    return value


DEFAULTS = {"punch_in": None, "punch_out": None, "work_hours": None,
            "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
FIELDS = ["status", "punch_in", "punch_out", "work_hours", "late_minutes",
          "overtime_minutes", "half_day"]


def attendance_json(doc):
    record = {}
    for field in ["emp_code", "date", *FIELDS, "history"]:
        record[field] = doc.get(field, DEFAULTS.get(field))
    return json_value(record)


def employee_json(doc):
    record = {field: doc[field] for field in EmployeeIn.model_fields}
    record["created_at"] = doc["created_at"]
    return json_value(record)


def employee(code):
    result = db.employees.find_one({"emp_code": code})
    if result is None:
        raise HTTPException(404, "unknown employee")
    return result


def attendance_day(ts, emp):
    local = ts.astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.time() < time.fromisoformat(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(emp, day):
    start = datetime.combine(Date.fromisoformat(day), time.fromisoformat(emp["shift_start"]), IST)
    end = datetime.combine(Date.fromisoformat(day), time.fromisoformat(emp["shift_end"]), IST)
    if emp["shift_end"] <= emp["shift_start"]:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in, start):
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in, punch_out):
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out, end):
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def derived(doc, emp):
    if doc["status"] not in PRESENCE:
        return {k: v for k, v in DEFAULTS.items() if k != "history"}
    punch_in = doc.get("punch_in")
    punch_out = doc.get("punch_out")
    if punch_in is None:
        raise HTTPException(422, "presence requires punch_in")
    if attendance_day(punch_in, emp) != doc["date"]:
        raise HTTPException(422, "punch_in must stay on the attendance date")
    start, end = shift_bounds(emp, doc["date"])
    work_hours = None
    overtime_minutes = 0
    half_day = False
    if punch_out is not None:
        if not punch_in < punch_out <= punch_in + timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
        work_hours = compute_work_hours(punch_in, punch_out)
        overtime_minutes = compute_overtime(punch_out, end)
        half_day = work_hours < 4.5
    return {
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": work_hours,
        "late_minutes": compute_late_minutes(punch_in, start),
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }


def snapshot_filter(doc):
    """Only update a record if it still matches the version we read."""
    query = {"_id": doc["_id"]}
    for field in [*FIELDS, "history"]:
        if field in doc:
            query[field] = {"$eq": doc[field]}
        else:
            # Legacy records may omit fields. A concurrent edit adding one
            # must also cause this update to fail rather than overwrite it.
            query[field] = {"$exists": False}
    return query


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not getattr(app.state, "indexes_ready", False):
            create_indexes()
            app.state.indexes_ready = True
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable or indexes not ready")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump() | {"created_at": now()}
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_json(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    q = {"department": department} if department is not None else {}
    hint = "department_code" if department is not None else "employee_code"
    docs = db.employees.find(q).hint(hint).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_json(d) for d in docs], "total": db.employees.count_documents(q, hint=hint),
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employee(body.emp_code)
    ts = instant(body.punched_at)
    doc = {"emp_code": body.emp_code, "date": attendance_day(ts, emp),
           "status": body.status, "punch_in": ts, "punch_out": None, "history": []}
    doc.update(derived(doc, emp))
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_json(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employee(body.emp_code)
    ts = instant(body.punched_at)
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts, "$ne": None}},
                                      sort=[("punch_in", -1)], hint="employee_punch")
    if doc is None:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    updated = derived(doc | {"punch_out": ts}, emp)
    result = db.attendance_logs.find_one_and_update(snapshot_filter(doc), {"$set": updated},
                                                   return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(409, "record changed concurrently; retry")
    return attendance_json(result)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateString, body: RegularizeIn):
    emp = employee(emp_code)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if old is None:
        raise HTTPException(404, "no attendance record for this date")
    supplied = body.model_dump(exclude_unset=True)
    status = supplied.get("status", old["status"])
    if status not in PRESENCE and ("punch_in" in supplied or "punch_out" in supplied):
        raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
    updated = {k: old.get(k, DEFAULTS.get(k)) for k in FIELDS}
    updated["status"] = status
    for k in ["punch_in", "punch_out"]:
        if k in supplied:
            updated[k] = instant(supplied[k])
    updated.update(derived(updated | {"date": date}, emp))
    changes = {}
    for field in FIELDS:
        before = old.get(field, DEFAULTS.get(field))
        after = updated[field]
        if before != after:
            changes[field] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(422, "correction changes nothing")
    entry = {"at": now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    result = db.attendance_logs.find_one_and_update(snapshot_filter(old),
              {"$set": updated, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(409, "record changed concurrently; retry")
    return attendance_json(result)


def checked_range(start, end, max_days=None):
    if start is None or end is None:
        return
    if start > end:
        raise HTTPException(422, "invalid date range")
    days = (Date.fromisoformat(end) - Date.fromisoformat(start)).days + 1
    if max_days is not None and days > max_days:
        raise HTTPException(422, "invalid date range")


def attendance_query(emp_code, date_from, date_to, status):
    checked_range(date_from, date_to)
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        q["status"] = status
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    hint = "employee_day" if emp_code is not None else (
        "status_date_employee" if status is not None else "date_employee")
    return q, hint


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: DateString | None = None,
                    date_to: DateString | None = None, status: Status | None = None,
                    page: Page = 1, page_size: PageSize = 20):
    q, hint = attendance_query(emp_code, date_from, date_to, status)
    docs = db.attendance_logs.find(q).hint(hint).sort([("date", -1), ("emp_code", 1)])
    docs = docs.skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_json(d) for d in docs],
            "total": db.attendance_logs.count_documents(q, hint=hint), "page": page, "page_size": page_size}


# Analytics builders are shared verbatim with explain. No log reduction or gap filling happens in Python.
def month_bounds(month):
    year, number = map(int, month.split("-"))
    return month + "-01", f"{month}-{calendar.monthrange(year, number)[1]:02d}"


def mongo_date(value):
    return {"$dateFromString": {"dateString": value, "format": "%Y-%m-%d", "timezone": "+05:30"}}


def date_string(value):
    return {"$dateToString": {"date": value, "format": "%Y-%m-%d", "timezone": "+05:30"}}


def weekday(value):
    return {"$and": [{"$gte": [{"$isoDayOfWeek": {"date": value, "timezone": "+05:30"}}, 1]},
                     {"$lte": [{"$isoDayOfWeek": {"date": value, "timezone": "+05:30"}}, 5]}]}


def decimal_value(value):
    return {"$toDecimal": value}


def half_up(value, places=2):
    """All analytics quantities are nonnegative; Decimal avoids binary midpoint drift."""
    scale = 10 ** places
    scaled_value = {"$multiply": [decimal_value(value), scale]}
    rounded_integer = {"$floor": {"$add": [scaled_value, Decimal128("0.5")]}}
    rounded_value = {"$toDouble": {"$divide": [rounded_integer, scale]}}
    return {"$cond": [{"$eq": [value, None]}, None, rounded_value]}


def ratio(numerator, denominator, places=2, multiplier=1):
    return {"$cond": [{"$gt": [denominator, 0]},
        half_up({"$divide": [{"$multiply": [decimal_value(numerator), multiplier]}, denominator]}, places), None]}


def calendar_days(start, count):
    return {"$map": {"input": {"$range": [0, count]}, "as": "offset",
           "in": {"$dateAdd": {"startDate": mongo_date(start), "unit": "day", "amount": "$$offset"}}}}


def working_days(start, end):
    first = {"$cond": [{"$gt": ["$joined_on", start]}, "$joined_on", start]}
    count = {"$max": [0, {"$add": [{"$dateDiff": {"startDate": mongo_date(first),
                           "endDate": mongo_date(end), "unit": "day"}}, 1]}]}
    return {"$size": {"$filter": {"input": calendar_days(first, count), "as": "day",
                                 "cond": weekday("$$day")}}}


def log_statistics():
    present = {"$in": ["$status", PRESENCE]}
    weight = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}
    has_hours = {"$and": [present, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    late = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
    return {"$group": {"_id": None,
        "present_days": {"$sum": {"$cond": [{"$and": [present, weekday(mongo_date("$date"))]}, weight, 0]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [late, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "hours_sum": {"$sum": {"$cond": [has_hours, decimal_value("$work_hours"), 0]}},
        "hours_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}}


STAT_FIELDS = ["present_days", "leave_days", "late_count", "total_late_minutes",
               "total_overtime_minutes", "hours_sum", "hours_count", "on_duty_count"]


def monthly_lookup(start, end):
    return [{"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
             "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}, log_statistics()], "as": "stats"}},
            {"$set": {"stats": {"$arrayElemAt": ["$stats", 0]}}},
            {"$set": {k: {"$ifNull": ["$stats." + k, 0]} for k in STAT_FIELDS}}]


def monthly_pipeline(code, month):
    start, end = month_bounds(month)
    pipeline = [{"$match": {"emp_code": code}}, *monthly_lookup(start, end),
                {"$set": {"working_days": working_days(start, end)}},
                {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1,
                  **{k: 1 for k in STAT_FIELDS[:5]},
                  "attendance_pct": ratio("$present_days", "$working_days", multiplier=100)}}]
    return "employees", pipeline, "employee_code"


def summary_pipeline(month, department):
    start, end = month_bounds(month)
    match = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    pipeline = [{"$match": match}, *monthly_lookup(start, end),
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                    **{k: {"$sum": "$" + k} for k in STAT_FIELDS}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
          "avg_work_hours": ratio("$hours_sum", "$hours_count"), "late_count": 1, "total_late_minutes": 1,
          "leave_count": "$leave_days", "on_duty_count": 1}}, {"$sort": {"department": 1}}]
    return "employees", pipeline, "department_code" if department is not None else "joined_department"


def leaderboard_pipeline(month, department, limit):
    start, end = month_bounds(month)
    pipeline = [{"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
          "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code",
                     "pipeline": [{"$project": {"_id": 0, "name": 1, "department": 1}}], "as": "employee"}},
        {"$unwind": "$employee"}]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
                 {"$match": {"rank": {"$lte": limit}}},
                 {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name",
                    "department": "$employee.department", "total_late_minutes": 1, "late_count": 1, "rank": 1}},
                 {"$sort": {"total_late_minutes": -1, "emp_code": 1}}]
    return "attendance_logs", pipeline, "date_employee"


def trend_pipeline(department, start, end):
    checked_range(start, end, 92)
    count = (Date.fromisoformat(end) - Date.fromisoformat(start)).days + 1
    # Generate a bounded calendar in MongoDB from department employees. Unlike densify on
    # an empty log stream, this produces rows even when the whole period has no logs.
    pipeline = [{"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {"code": "$emp_code", "joined": "$joined_on"}}}},
        {"$set": {"day": calendar_days(start, count)}}, {"$unwind": "$day"},
        {"$set": {"date": date_string("$day"), "is_working_day": weekday("$day")}},
        {"$set": {"eligible": {"$filter": {"input": "$employees", "as": "e",
                                            "cond": {"$lte": ["$$e.joined", "$date"]}}}}},
        {"$set": {"headcount": {"$size": "$eligible"}, "codes": "$eligible.code"}},
        {"$lookup": {"from": "attendance_logs", "localField": "codes", "foreignField": "emp_code",
          "let": {"day": "$date"}, "pipeline": [
              {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
              {"$group": {"_id": None,
                 "present_count": {"$sum": {"$cond": [{"$in": ["$status", PRESENCE]},
                     {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                 "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}],
          "as": "stats"}},
        {"$set": {"stats": {"$arrayElemAt": ["$stats", 0]}}},
        {"$set": {"present_count": {"$ifNull": ["$stats.present_count", 0]},
                  "late_count": {"$ifNull": ["$stats.late_count", 0]}}},
        {"$set": {"attendance_rate": {"$cond": ["$is_working_day", ratio("$present_count", "$headcount", 4), None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
             "moving_avg_7d": {"$avg": {"$toDecimal": "$attendance_rate"}, "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": half_up("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}}]
    return "employees", pipeline, "department_code"


def aggregate(spec):
    collection, pipeline, hint = spec
    return json_value(list(db[collection].aggregate(pipeline, hint=hint, allowDiskUse=True)))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthString):
    items = aggregate(monthly_pipeline(emp_code, month))
    if not items:
        raise HTTPException(404, "unknown employee")
    return items[0]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthString, department: str | None = None):
    return {"month": month, "items": aggregate(summary_pipeline(month, department))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthString, department: str | None = None, limit: Limit = 10):
    return {"month": month, "items": aggregate(leaderboard_pipeline(month, department, limit))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[DateString, Query(alias="from")],
                     to: DateString):
    spec = trend_pipeline(department, from_, to)
    items = aggregate(spec)
    if not items:
        raise HTTPException(404, "unknown department")
    return {"department": department, "items": items}


ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: ExplainEndpoint, emp_code: str | None = None, month: MonthString | None = None,
                     department: str | None = None, limit: Limit = 10,
                     date_from: DateString | None = None, date_to: DateString | None = None,
                     status: Status | None = None, from_: Annotated[DateString | None, Query(alias="from")] = None,
                     to: DateString | None = None, page: Page = 1, page_size: PageSize = 20):
    if endpoint == "attendance_list":
        q, hint = attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {"find": collection, "filter": q, "sort": {"date": -1, "emp_code": 1},
                   "skip": (page - 1) * page_size, "limit": page_size, "hint": hint}
    else:
        if endpoint == "department_trend":
            if department is None or from_ is None or to is None:
                raise HTTPException(422, "department, from and to are required")
            spec = trend_pipeline(department, from_, to)
        else:
            if month is None:
                raise HTTPException(422, "month is required")
            if endpoint == "employee_monthly":
                if emp_code is None:
                    raise HTTPException(422, "emp_code is required")
                spec = monthly_pipeline(emp_code, month)
            elif endpoint == "department_summary":
                spec = summary_pipeline(month, department)
            else:
                spec = leaderboard_pipeline(month, department, limit)
        collection, pipeline, hint = spec
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint, "allowDiskUse": True}
    output = db.command("explain", command, verbosity="executionStats")
    # Replica sets / Atlas also return BSON cluster timestamps and binary signatures.
    # Extended JSON preserves those raw values without assuming standalone metadata.
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(output))}
