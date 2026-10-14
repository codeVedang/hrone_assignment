"""Compare actual database pipelines with independently calculated expected results."""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import json
import random
import time

import pytest

from app import main


def rounded(value, places=2):
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def seed_log(code, day, status="PRESENT", hours=8, late=0, overtime=0, half=False, legacy=False):
    doc = {"emp_code": code, "date": day, "status": status, "work_hours": hours,
           "late_minutes": late, "overtime_minutes": overtime, "punch_in": None, "punch_out": None}
    if not legacy:
        doc |= {"half_day": half, "history": []}
    main.db.attendance_logs.insert_one(doc)
    return doc


def test_analytics_no_logs_and_joiners(api, make_employee):
    make_employee(joined_on="2026-07-15")
    make_employee("EMP0002", department="Empty")
    make_employee("EMP0003", department="Future", joined_on="2026-08-01")
    monthly = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert monthly == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 13,
                       "present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0,
                       "total_overtime_minutes": 0, "attendance_pct": 0}
    future = api.get("/analytics/employees/EMP0003/monthly", params={"month": "2026-07"}).json()
    assert future["working_days"] == 0 and future["attendance_pct"] is None
    summary = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    assert [r["department"] for r in summary] == ["Empty", "Engineering"]
    assert all(r["headcount"] == 1 and r["avg_work_hours"] is None for r in summary)
    for department in ["Empty", "Future"]:
        response = api.get(f"/analytics/departments/{department}/trend", params={"from": "2026-07-03", "to": "2026-07-08"})
        assert response.status_code == 200, response.text
        rows = response.json()["items"]
        assert len(rows) == 6
        assert rows[1]["attendance_rate"] is None and rows[2]["attendance_rate"] is None
        if department == "Future":
            assert all(r["headcount"] == 0 and r["moving_avg_7d"] is None for r in rows)
        else:
            assert all(r["moving_avg_7d"] == 0 for r in rows)


def test_weekends_authoritative_fields_and_weighted_average(api, make_employee):
    make_employee()
    make_employee("EMP0002")
    seed_log("EMP0001", "2026-07-06", hours=2.675, half=True, late=10, overtime=30)
    seed_log("EMP0001", "2026-07-07", status="WFH", hours=None, legacy=True)
    seed_log("EMP0001", "2026-07-11", status="ON_DUTY", hours=8, late=20, overtime=40)
    seed_log("EMP0001", "2026-07-12", status="LEAVE", hours=None)
    seed_log("EMP0002", "2026-07-06", status="ABSENT", hours=None)
    monthly = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert monthly["working_days"] == 23
    assert monthly["present_days"] == 1.5
    assert monthly["leave_days"] == 1
    assert monthly["total_late_minutes"] == 30 and monthly["total_overtime_minutes"] == 70
    assert monthly["attendance_pct"] == 6.52
    summary = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"][0]
    assert summary["headcount"] == 2 and summary["present_days"] == 1.5
    assert summary["avg_work_hours"] == 5.34
    assert summary["on_duty_count"] == 1 and summary["leave_count"] == 1
    trend = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-12"}).json()["items"]
    assert trend[0]["attendance_rate"] == 0.25
    assert trend[1]["moving_avg_7d"] == 0.375
    assert trend[5]["present_count"] == 1 and trend[5]["attendance_rate"] is None
    assert trend[-1]["moving_avg_7d"] == 0.15


def test_average_is_over_records_and_half_up(api, make_employee):
    make_employee()
    make_employee("EMP0002")
    for d in ["2026-07-06", "2026-07-07", "2026-07-08"]:
        seed_log("EMP0001", d, hours=2)
    seed_log("EMP0002", "2026-07-06", hours=10)
    result = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"][0]
    assert result["avg_work_hours"] == 4
    main.db.attendance_logs.delete_many({})
    seed_log("EMP0001", "2026-07-06", hours=2.675)
    result = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"][0]
    assert result["avg_work_hours"] == 2.68


def test_leaderboard_ties_rank_cutoff_and_orphans(api, make_employee):
    for i, total in enumerate([50, 30, 30, 20, 0], 1):
        code = f"EMP{i:04d}"
        make_employee(code, department="Other" if i == 1 else "Engineering")
        seed_log(code, "2026-07-06", late=total)
    seed_log("EMP9999", "2026-07-06", late=999)
    endpoint = "/analytics/leaderboard/late"
    params = {"month": "2026-07", "limit": 2}
    items = api.get(endpoint, params=params).json()["items"]
    assert [r["rank"] for r in items] == [1, 2, 2]
    assert [r["emp_code"] for r in items] == ["EMP0001", "EMP0002", "EMP0003"]
    items = api.get(endpoint, params=params | {"department": "Engineering", "limit": 1}).json()["items"]
    assert [r["rank"] for r in items] == [1, 1]
    all_rows = api.get(endpoint, params=params | {"limit": 10}).json()["items"]
    assert [r["rank"] for r in all_rows] == [1, 2, 2, 4]


def test_trend_joiner_short_windows_and_92_day_boundary(api, make_employee):
    make_employee(joined_on="2026-07-06")
    make_employee("EMP0002", joined_on="2026-07-08")
    seed_log("EMP0001", "2026-07-06")
    endpoint = "/analytics/departments/Engineering/trend"
    items = api.get(endpoint, params={"from": "2026-07-04", "to": "2026-07-13"}).json()["items"]
    assert len(items) == 10
    assert [r["headcount"] for r in items] == [0, 0, 1, 1, 2, 2, 2, 2, 2, 2]
    assert items[0]["moving_avg_7d"] is None
    assert items[2]["moving_avg_7d"] == 1
    assert items[3]["moving_avg_7d"] == 0.5
    assert items[8]["moving_avg_7d"] == 0.2
    assert items[9]["moving_avg_7d"] == 0
    assert api.get(endpoint, params={"from": "2026-07-01", "to": "2026-09-30"}).status_code == 200
    assert api.get(endpoint, params={"from": "2026-07-01", "to": "2026-10-01"}).status_code == 422


@pytest.mark.parametrize("path,params", [
    ("/analytics/employees/EMP0001/monthly", {}),
    ("/analytics/departments/summary", {"month": "2026-13"}),
    ("/analytics/departments/summary", {"month": "0000-01"}),
    ("/analytics/leaderboard/late", {"month": "2026-07", "limit": 0}),
    ("/analytics/leaderboard/late", {"month": "2026-07", "limit": 51}),
    ("/analytics/departments/A/trend", {"from": "2026-07-08", "to": "2026-07-06"}),
    ("/admin/explain/employee_monthly", {"month": "2026-07"}),
    ("/admin/explain/department_summary", {}),
    ("/admin/explain/department_trend", {}),
    ("/admin/explain/nonsense", {})])
def test_analytics_validation(api, path, params):
    assert api.get(path, params=params).status_code == 422


def test_unknown_resources(api):
    assert api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).status_code == 404
    assert api.get("/analytics/departments/A/trend", params={"from": "2026-07-01", "to": "2026-07-01"}).status_code == 404


def test_explain_replica_set_metadata_is_json_serializable(api, monkeypatch):
    from bson import Binary, Timestamp
    def command(*args, **kwargs):
        return {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}},
                "operationTime": Timestamp(1783311000, 1),
                "$clusterTime": {"clusterTime": Timestamp(1783311000, 1),
                                 "signature": {"hash": Binary(b"\xff\x00"), "keyId": 1}}}
    with monkeypatch.context() as patch:
        patch.setattr(main.db, "command", command)
        result = api.get("/admin/explain/attendance_list")
    assert result.status_code == 200
    assert result.json()["explain"]["operationTime"]["$timestamp"]["t"] == 1783311000


def test_randomized_monthly_and_trend_against_oracle(api, make_employee):
    rng = random.Random(20261014)
    employees, logs = [], []
    for i in range(1, 16):
        joined = date(2026, 7, 1) + timedelta(days=rng.randrange(25))
        code = f"EMP{i:04d}"
        make_employee(code, joined_on=joined.isoformat())
        employees.append((code, joined))
        for day in range(joined.day, 32):
            if rng.random() < .35:
                continue
            status = rng.choice([*main.PRESENCE, "ABSENT", "LEAVE"])
            hours = rng.choice([None, 2.675, 4.49, 4.5, 8.0]) if status in main.PRESENCE else None
            logs.append(seed_log(code, f"2026-07-{day:02d}", status, hours,
                                 rng.choice([0, 0, 10, 35]), rng.choice([0, 30, 40]),
                                 half=rng.choice([False, True]), legacy=rng.random() < .2))
    for code, joined in employees:
        own = [l for l in logs if l["emp_code"] == code]
        present = sum(.5 if l.get("half_day", False) else 1 for l in own
                      if l["status"] in main.PRESENCE and date.fromisoformat(l["date"]).weekday() < 5)
        working = sum(date(2026, 7, d).weekday() < 5 for d in range(joined.day, 32))
        actual = api.get(f"/analytics/employees/{code}/monthly", params={"month": "2026-07"}).json()
        assert actual["present_days"] == present and actual["working_days"] == working
        assert actual["attendance_pct"] == rounded(Decimal(str(present))*100/working)
        assert actual["total_late_minutes"] == sum(l["late_minutes"] for l in own)
    actual = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2026-07-31"}).json()["items"]
    rates = []
    for day, row in enumerate(actual, 1):
        current = date(2026, 7, day)
        headcount = sum(joined <= current for _, joined in employees)
        own = [l for l in logs if l["date"] == current.isoformat()]
        present = sum(.5 if l.get("half_day", False) else 1 for l in own if l["status"] in main.PRESENCE)
        rate = rounded(Decimal(str(present))/headcount, 4) if current.weekday() < 5 and headcount else None
        rates.append(rate)
        window = [Decimal(str(r)) for r in rates[-7:] if r is not None]
        average = rounded(sum(window)/len(window), 4) if window else None
        assert row["headcount"] == headcount and row["present_count"] == present
        assert row["attendance_rate"] == rate and row["moving_avg_7d"] == average


@pytest.mark.scale
def test_100k_index_plans_and_endpoints(api, make_employee):
    for i in range(1, 1001):
        make_employee(f"EMP{i:04d}")
    start = date(2026, 6, 1)
    logs = [{"emp_code": f"EMP{i:04d}", "date": (start+timedelta(days=d)).isoformat(),
             "status": "PRESENT", "work_hours": 8, "late_minutes": i % 3 * 10}
             for i in range(1, 1001) for d in range(100)]
    main.db.attendance_logs.insert_many(logs)
    cases = [
        ("attendance_list", {}, "/attendance"),
        ("attendance_list", {"emp_code": "EMP0001", "page": 2, "page_size": 5}, "/attendance"),
        ("attendance_list", {"status": "PRESENT", "date_from": "2026-07-01", "date_to": "2026-07-31"}, "/attendance"),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}, "/analytics/employees/EMP0001/monthly"),
        ("department_summary", {"month": "2026-07"}, "/analytics/departments/summary"),
        ("late_leaderboard", {"month": "2026-07", "limit": 1}, "/analytics/leaderboard/late"),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}, "/analytics/departments/Engineering/trend")]
    timings = {}
    for endpoint, params, path in cases:
        explain = api.get("/admin/explain/" + endpoint, params=params)
        assert explain.status_code == 200, explain.text
        encoded = json.dumps(explain.json()["explain"])
        assert '"IXSCAN"' in encoded, encoded
        assert '"COLLSCAN"' not in encoded, encoded
        before = time.perf_counter()
        response = api.get(path, params=params)
        elapsed = time.perf_counter() - before
        assert response.status_code == 200, response.text
        timings[endpoint] = round(elapsed, 3)
        assert elapsed < 15, (endpoint, elapsed)
    print("100k endpoint seconds:", timings)
