from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import pytest
from pymongo.errors import ConnectionFailure

from app import main


def ms(day, clock):
    return main.epoch_ms(datetime.fromisoformat(day + "T" + clock).replace(tzinfo=main.IST))


def punch(api, code="EMP0001", day="2026-07-06", clock="09:30:00", **extra):
    return api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(day, clock)} | extra)


def test_health_checks_mongo(api, monkeypatch):
    assert api.get("/health").json() == {"status": "ok"}
    def fail(*args, **kwargs):
        raise ConnectionFailure("offline")
    with monkeypatch.context() as patch:
        patch.setattr(type(main.client.admin), "command", fail)
        assert api.get("/health").status_code == 503


@pytest.mark.parametrize("field,value", [
    ("emp_code", "abc"), ("emp_code", "EMP123"), ("name", ""), ("name", "x"*101),
    ("email", "a@b"), ("department", ""), ("department", "x"*51), ("shift_start", "24:00"),
    ("shift_end", "9:30"), ("joined_on", "2026-02-30"), ("joined_on", "2026-7-1"),
    ("joined_on", "0000-01-01"), ("shift_end", "09:30")])
def test_employee_validation(api, field, value):
    body = {"emp_code": "EMP0001", "name": "One", "email": "one@example.com",
            "department": "Engineering", "joined_on": "2026-01-01", field: value}
    assert api.post("/employees", json=body).status_code == 422


@pytest.mark.parametrize("value", [1783311000, 1783311000000.0, "1783311000000", True, None,
                                    99999999999, 4102444800001, 10**30])
def test_timestamp_validation(api, make_employee, value):
    make_employee()
    for path in ["punch-in", "punch-out"]:
        assert api.post("/attendance/" + path, json={"emp_code": "EMP0001", "punched_at": value}).status_code == 422


@pytest.mark.parametrize("clock,late", [("09:40:00", 0), ("09:40:01", 10), ("10:15:59", 45), ("09:20:00", 0)])
def test_grace_seconds(api, make_employee, clock, late):
    make_employee()
    result = punch(api, clock=clock, late_minutes=999).json()
    assert result["late_minutes"] == late
    assert result["history"] == []
    assert "id" not in result and "_id" not in result
    assert isinstance(result["punch_in"], int)


def test_truncation_and_statuses(api, make_employee):
    make_employee()
    response = punch(api, clock="09:40:00", punched_at=ms("2026-07-06", "09:40:00") + 999, status="WFH")
    assert response.json()["punch_in"] == ms("2026-07-06", "09:40:00")
    assert response.json()["late_minutes"] == 0
    assert punch(api).status_code == 409
    assert punch(api, code="EMP9999").status_code == 404
    assert punch(api, day="2026-07-07", status="ABSENT").status_code == 422
    assert punch(api, day="2026-07-07", status="ON_DUTY").status_code == 201


@pytest.mark.parametrize("out,overtime", [("18:59:59", 0), ("19:00:00", 30), ("19:10:59", 40)])
def test_overtime(api, make_employee, out, overtime):
    make_employee()
    punch(api)
    result = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06", out)})
    assert result.status_code == 200, result.text
    assert result.json()["overtime_minutes"] == overtime
    assert result.json()["history"] == []


@pytest.mark.parametrize("seconds,hours,half", [(3618, 1.01, True), (16182, 4.50, False), (16181, 4.49, True)])
def test_decimal_half_up(api, make_employee, seconds, hours, half):
    make_employee()
    record = punch(api).json()
    result = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": record["punch_in"] + seconds*1000})
    assert result.json()["work_hours"] == hours
    assert result.json()["half_day"] is half


def test_overnight_and_early_morning(api, make_employee):
    make_employee(shift_start="22:00", shift_end="06:00")
    record = punch(api, clock="21:55:00").json()
    result = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07", "06:40:00")})
    assert result.status_code == 200, result.text
    assert result.json()["date"] == "2026-07-06"
    assert result.json()["work_hours"] == 8.75
    assert result.json()["overtime_minutes"] == 40
    assert result.json()["late_minutes"] == 0
    assert punch(api, day="2026-07-07", clock="02:00:00").status_code == 409
    new = punch(api, day="2026-07-08", clock="02:00:00").json()
    assert new["date"] == "2026-07-07"
    assert new["late_minutes"] == 240
    at_end = punch(api, day="2026-07-08", clock="06:00:00").json()
    assert at_end["date"] == "2026-07-08"


def test_punchout_missing_and_bounds(api, make_employee):
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    make_employee()
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    pi = punch(api).json()["punch_in"]
    for ts, status in [(pi-1000, 404), (pi, 422), (pi+86401000, 422), (pi+86400000, 200), (pi+86400000, 409)]:
        response = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ts})
        assert response.status_code == status, response.text


def test_punchout_chooses_latest_even_if_closed(api, make_employee):
    make_employee()
    punch(api)
    latest = punch(api, day="2026-07-07").json()
    po = {"emp_code": "EMP0001", "punched_at": latest["punch_in"]+3600000}
    assert api.post("/attendance/punch-out", json=po).status_code == 200
    assert api.post("/attendance/punch-out", json=po).status_code == 409


def test_correction_audit_and_clearing(api, make_employee):
    make_employee()
    punch(api, clock="10:05:00")
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06", "18:30:00")})
    path = "/attendance/EMP0001/2026-07-06"
    body = {"reason": "Biometric correction", "regularized_by": "hr.admin", "punch_in": ms("2026-07-06", "09:28:00")}
    result = api.patch(path, json=body)
    assert result.status_code == 200, result.text
    record = result.json()
    assert record["late_minutes"] == 0
    assert record["work_hours"] == 9.03
    entry = record["history"][0]
    assert isinstance(entry["at"], int)
    assert set(entry["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    assert entry["changes"]["punch_in"]["from"] == ms("2026-07-06", "10:05:00")
    assert api.patch(path, json=body).status_code == 422
    for extra in [{"punch_in": ms("2026-07-07", "09:30:00")}, {"punch_out": ms("2026-07-06", "09:00:00")},
                  {"status": "ABSENT", "punch_in": ms("2026-07-06", "09:30:00")}, {"punch_out": None}]:
        assert api.patch(path, json=body | extra).status_code == 422
    absent = api.patch(path, json={"reason": "Approved absence", "regularized_by": "hr", "status": "ABSENT"}).json()
    assert absent["punch_in"] is None and absent["punch_out"] is None
    assert absent["work_hours"] is None and absent["half_day"] is False
    assert len(absent["history"]) == 2
    assert api.patch(path, json={"reason": "Return to work", "regularized_by": "hr", "status": "PRESENT"}).status_code == 422
    restored = api.patch(path, json=body | {"status": "PRESENT"}).json()
    assert restored["work_hours"] is None and len(restored["history"]) == 3
    stored = main.db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert isinstance(stored["history"][0]["changes"]["punch_in"]["from"], datetime)


def test_legacy_and_pagination(api, make_employee):
    for i in [3, 1, 2]:
        make_employee(f"EMP{i:04d}", department="A" if i < 3 else "B")
        punch(api, code=f"EMP{i:04d}")
    main.db.attendance_logs.update_one({"emp_code": "EMP0001"}, {"$unset": {"history": "", "half_day": ""}})
    employees = api.get("/employees", params={"department": "A", "page_size": 1}).json()
    assert employees["total"] == 2 and employees["items"][0]["emp_code"] == "EMP0001"
    assert isinstance(employees["items"][0]["created_at"], int)
    logs = api.get("/attendance", params={"page_size": 2}).json()
    assert [r["emp_code"] for r in logs["items"]] == ["EMP0001", "EMP0002"]
    assert logs["items"][0]["history"] == [] and logs["items"][0]["half_day"] is False
    assert api.get("/attendance", params={"emp_code": "EMP0001"}).json()["total"] == 1
    assert api.get("/attendance", params={"page": 9}).json()["items"] == []
    result = api.patch("/attendance/EMP0001/2026-07-06", json={"status": "WFH", "reason": "Remote work", "regularized_by": "hr"})
    assert result.status_code == 200 and len(result.json()["history"]) == 1
    for params in [{"page": 0}, {"page_size": 101}, {"page_size": 0}]:
        for endpoint in ["/attendance", "/employees"]:
            assert api.get(endpoint, params=params).status_code == 422
    for params in [{"date_from": "2026-02-30"}, {"date_from": "2026-07-07", "date_to": "2026-07-06"}, {"status": "bad"}]:
        assert api.get("/attendance", params=params).status_code == 422


def parallel(request, count=8):
    barrier = Barrier(count)
    def run(_):
        barrier.wait()
        return request()
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


def test_create_and_punch_races(api):
    body = {"emp_code": "EMP0001", "name": "One", "email": "one@example.com", "department": "A", "joined_on": "2026-01-01"}
    results = parallel(lambda: api.post("/employees", json=body))
    assert sorted(r.status_code for r in results) == [201] + [409]*7
    results = parallel(lambda: punch(api))
    assert sorted(r.status_code for r in results) == [201] + [409]*7
    results = parallel(lambda: api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06", "18:30:00")}))
    assert sorted(r.status_code for r in results) == [200] + [409]*7
    assert main.db.attendance_logs.count_documents({}) == 1


def test_correction_race_preserves_history(api, make_employee):
    make_employee()
    punch(api)
    def edit():
        return api.patch("/attendance/EMP0001/2026-07-06", json={"reason": "Remote correction", "regularized_by": "hr", "status": "WFH"})
    results = parallel(edit)
    assert sum(r.status_code == 200 for r in results) == 1
    assert all(r.status_code in [200, 409, 422] for r in results)
    assert len(main.db.attendance_logs.find_one({})["history"]) == 1


def test_distinct_concurrent_edits_use_compare_and_swap(api, make_employee, monkeypatch):
    make_employee()
    punch(api)
    original = main.snapshot_filter
    barrier = Barrier(2)
    def synchronize(doc):
        barrier.wait(timeout=10)
        return original(doc)
    monkeypatch.setattr(main, "snapshot_filter", synchronize)
    def edit(status):
        return api.patch("/attendance/EMP0001/2026-07-06", json={"status": status, "reason": "Correct status", "regularized_by": "hr"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["WFH", "ON_DUTY"]))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert len(main.db.attendance_logs.find_one({})["history"]) == 1
