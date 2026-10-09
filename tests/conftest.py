import os
import pathlib
import pytest
from bson import json_util
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import app, init_db

@pytest.fixture(scope="session", autouse=True)
def setup_indexes():
    init_db()

@pytest.fixture(autouse=True)
def reset_database():
    """Reset database to sample dataset before each test."""
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    mongo_db = os.getenv("MONGO_DB", "attendance_db")
    client = MongoClient(mongo_uri, tz_aware=True)
    db = client[mongo_db]

    sample_dir = pathlib.Path(__file__).parent.parent / "sample_data"
    opts = json_util.JSONOptions(tz_aware=True)

    for name in ("employees", "attendance_logs"):
        file_path = sample_dir / f"{name}.json"
        if file_path.exists():
            docs = json_util.loads(file_path.read_text(), json_options=opts)
            db[name].delete_many({})
            if docs:
                db[name].insert_many(docs)
        else:
            db[name].delete_many({})

    yield db

@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
