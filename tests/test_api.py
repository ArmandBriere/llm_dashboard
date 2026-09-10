"""Integration tests for FastAPI endpoints."""

import pytest
from fastapi.testclient import TestClient
from backend.main import app
from backend.database import init_db, upsert_subscription, insert_snapshot


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as test_client:
        yield test_client


def test_api_status(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "interval_seconds" in data
    assert data["interval_seconds"] == 300


def test_api_subscriptions(client):
    resp = client.get("/api/subscriptions")
    assert resp.status_code == 200
    subs = resp.json()
    assert isinstance(subs, list)
    # Discovered the 2 local Claude Code subscriptions
    assert len(subs) >= 2


def test_api_snapshots(client):
    resp = client.get("/api/snapshots")
    assert resp.status_code == 200
    snaps = resp.json()
    assert isinstance(snaps, list)
    assert len(snaps) >= 2


def test_api_events(client):
    resp = client.get("/api/events")
    assert resp.status_code == 200
    events = resp.json()
    assert isinstance(events, list)


def test_frontend_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "LLM Quota Tracker" in resp.text
