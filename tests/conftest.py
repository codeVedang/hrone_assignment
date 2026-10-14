"""Every test uses a fresh disposable database; production MONGO_DB is never touched."""
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main


@pytest.fixture
def api(monkeypatch):
    mongo = MongoClient(os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017"),
                        tz_aware=True, tzinfo=main.UTC, serverSelectionTimeoutMS=3000)
    mongo.admin.command("ping")  # Fail rather than silently skip integration coverage.
    name = "attendance_test_" + uuid.uuid4().hex
    monkeypatch.setattr(main, "client", mongo)
    monkeypatch.setattr(main, "db", mongo[name])
    try:
        with TestClient(main.app) as client:
            yield client
    finally:
        # TestClient's lifespan closes its client; cleanup uses a new connection.
        cleanup = MongoClient(os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017"))
        cleanup.drop_database(name)
        cleanup.close()


@pytest.fixture
def make_employee(api):
    def create(code="EMP0001", **overrides):
        body = {"emp_code": code, "name": code, "email": f"{code}@example.com",
                "department": "Engineering", "joined_on": "2026-01-01"} | overrides
        response = api.post("/employees", json=body)
        assert response.status_code == 201, response.text
        return response.json()
    return create
