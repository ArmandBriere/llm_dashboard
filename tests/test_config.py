"""Tests for the environment-driven settings."""

from __future__ import annotations

from pathlib import Path

from backend.config import DEFAULT_POLL_SECONDS, MIN_POLL_SECONDS, db_path, poll_interval_seconds


def test_poll_interval_defaults_when_unset():
    assert poll_interval_seconds({}) == DEFAULT_POLL_SECONDS
    assert poll_interval_seconds({"LLM_DASHBOARD_POLL_SECONDS": ""}) == DEFAULT_POLL_SECONDS


def test_poll_interval_reads_the_variable():
    assert poll_interval_seconds({"LLM_DASHBOARD_POLL_SECONDS": "120"}) == 120
    assert poll_interval_seconds({"LLM_DASHBOARD_POLL_SECONDS": " 900 "}) == 900


def test_poll_interval_rejects_garbage():
    assert poll_interval_seconds({"LLM_DASHBOARD_POLL_SECONDS": "five minutes"}) == DEFAULT_POLL_SECONDS
    assert poll_interval_seconds({"LLM_DASHBOARD_POLL_SECONDS": "1.5"}) == DEFAULT_POLL_SECONDS


def test_poll_interval_has_a_floor():
    assert poll_interval_seconds({"LLM_DASHBOARD_POLL_SECONDS": "1"}) == MIN_POLL_SECONDS
    assert poll_interval_seconds({"LLM_DASHBOARD_POLL_SECONDS": "-300"}) == MIN_POLL_SECONDS
    assert poll_interval_seconds({"LLM_DASHBOARD_POLL_SECONDS": str(MIN_POLL_SECONDS)}) == MIN_POLL_SECONDS


def test_poll_interval_reads_the_process_environment(monkeypatch):
    monkeypatch.setenv("LLM_DASHBOARD_POLL_SECONDS", "45")
    assert poll_interval_seconds() == 45


def test_db_path_defaults_inside_the_repo():
    assert db_path({}).name == "llm_dashboard.db"
    assert db_path({"LLM_DASHBOARD_DB_PATH": "/tmp/x.db"}) == Path("/tmp/x.db")
