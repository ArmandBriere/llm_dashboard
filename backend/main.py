"""FastAPI Web Application for LLM Quota Tracker."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.collector import collector
from backend.database import get_events, get_snapshots, get_subscriptions, init_db
from backend.skills import get_skill_timeline, get_skill_usage, get_tool_usage
from backend.usage import (
    get_five_hour_windows,
    get_usage_by_model,
    get_usage_heatmap,
    get_usage_projects,
    get_usage_sessions,
    get_usage_summary,
    get_usage_timeline,
    init_usage_schema,
    scan_transcripts,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("llm_dashboard")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for database init and background collector startup."""
    logger.info("Initializing database...")
    init_db()
    init_usage_schema()
    # Run initial collection
    logger.info("Running initial quota collection...")
    await collector.collect_once()
    # Start 5-minute background collector. The pass above just ran, so don't
    # immediately poll again.
    collector.start(skip_first=True)
    yield
    collector.stop()


app = FastAPI(title="LLM Quota Tracker", version="1.0.0", lifespan=lifespan)

# Enforce no-cache for local dev and live dashboard
@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# Allow CORS for development convenience
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/subscriptions")
def api_subscriptions() -> list[dict[str, Any]]:
    """List all tracked subscriptions and their latest live snapshot."""
    return get_subscriptions()


@app.get("/api/snapshots")
def api_snapshots(
    subscription_ids: str | None = Query(None, description="Comma-separated subscription IDs"),
    date: str | None = Query(None, description="Date in YYYY-MM-DD format"),
    start_hour: int | None = Query(None, ge=0, le=23, description="Starting hour of day (0-23)"),
    end_hour: int | None = Query(None, ge=0, le=23, description="Ending hour of day (0-23)"),
) -> list[dict[str, Any]]:
    """Query time-series quota snapshots with flexible date and hour window filtering."""
    sub_id_list = None
    if subscription_ids:
        sub_id_list = [int(s.strip()) for s in subscription_ids.split(",") if s.strip().isdigit()]

    return get_snapshots(
        subscription_ids=sub_id_list,
        date_str=date,
        start_hour=start_hour,
        end_hour=end_hour,
    )


@app.get("/api/events")
def api_events(
    subscription_ids: str | None = Query(None, description="Comma-separated subscription IDs"),
    date: str | None = Query(None, description="Date in YYYY-MM-DD format"),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Retrieve detected quota reset and refresh events."""
    sub_id_list = None
    if subscription_ids:
        sub_id_list = [int(s.strip()) for s in subscription_ids.split(",") if s.strip().isdigit()]

    return get_events(subscription_ids=sub_id_list, date_str=date, limit=limit)


@app.post("/api/refresh")
async def api_refresh() -> dict[str, Any]:
    """Force an immediate collection tick and return updated subscriptions."""
    result = await collector.collect_once()
    subs = get_subscriptions()
    return {"status": "ok", "summary": result, "subscriptions": subs}


def _parse_ids(subscription_ids: str | None) -> list[int] | None:
    if not subscription_ids:
        return None
    return [int(s.strip()) for s in subscription_ids.split(",") if s.strip().isdigit()]


def _usage_filters(subscription_ids, date, start_hour, end_hour) -> dict[str, Any]:
    return {
        "subscription_ids": _parse_ids(subscription_ids),
        "date_str": date,
        "start_hour": start_hour,
        "end_hour": end_hour,
    }


UsageIds = Query(None, description="Comma-separated subscription IDs")
UsageDate = Query(None, description="Local date YYYY-MM-DD; omit for all time")
UsageStart = Query(None, ge=0, le=23)
UsageEnd = Query(None, ge=0, le=23)


@app.get("/api/usage/summary")
def api_usage_summary(
    subscription_ids: str | None = UsageIds,
    date: str | None = UsageDate,
    start_hour: int | None = UsageStart,
    end_hour: int | None = UsageEnd,
) -> dict[str, Any]:
    """Token, cost, session and cache totals from local Claude Code transcripts."""
    return get_usage_summary(**_usage_filters(subscription_ids, date, start_hour, end_hour))


@app.get("/api/usage/models")
def api_usage_models(
    subscription_ids: str | None = UsageIds,
    date: str | None = UsageDate,
    start_hour: int | None = UsageStart,
    end_hour: int | None = UsageEnd,
) -> list[dict[str, Any]]:
    """Per-model share of turns, tokens and estimated cost."""
    return get_usage_by_model(**_usage_filters(subscription_ids, date, start_hour, end_hour))


@app.get("/api/usage/sessions")
def api_usage_sessions(
    subscription_ids: str | None = UsageIds,
    date: str | None = UsageDate,
    start_hour: int | None = UsageStart,
    end_hour: int | None = UsageEnd,
    limit: int = Query(60, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Sessions active in the window with per-session totals and models."""
    return get_usage_sessions(**_usage_filters(subscription_ids, date, start_hour, end_hour), limit=limit)


@app.get("/api/usage/projects")
def api_usage_projects(
    subscription_ids: str | None = UsageIds,
    date: str | None = UsageDate,
    start_hour: int | None = UsageStart,
    end_hour: int | None = UsageEnd,
    limit: int = Query(15, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Per-project totals."""
    return get_usage_projects(**_usage_filters(subscription_ids, date, start_hour, end_hour), limit=limit)


@app.get("/api/usage/heatmap")
def api_usage_heatmap(
    subscription_ids: str | None = UsageIds,
    date: str | None = UsageDate,
    start_hour: int | None = UsageStart,
    end_hour: int | None = UsageEnd,
) -> list[dict[str, Any]]:
    """Weekday x hour activity matrix in local time."""
    return get_usage_heatmap(**_usage_filters(subscription_ids, date, start_hour, end_hour))


@app.get("/api/usage/timeline")
def api_usage_timeline(
    subscription_ids: str | None = UsageIds,
    date: str | None = UsageDate,
    start_hour: int | None = UsageStart,
    end_hour: int | None = UsageEnd,
    bucket: str = Query("hour", pattern="^(hour|day)$"),
) -> list[dict[str, Any]]:
    """Tokens and cost per model per hour or day."""
    return get_usage_timeline(**_usage_filters(subscription_ids, date, start_hour, end_hour), bucket=bucket)


@app.get("/api/usage/windows")
def api_usage_windows(
    subscription_ids: str | None = UsageIds,
    date: str | None = UsageDate,
    limit: int = Query(12, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Observed 5-hour quota windows with each model's share of the window."""
    return get_five_hour_windows(subscription_ids=_parse_ids(subscription_ids), date_str=date, limit=limit)


@app.get("/api/usage/skills")
def api_usage_skills(
    subscription_ids: str | None = UsageIds,
    date: str | None = UsageDate,
    start_hour: int | None = UsageStart,
    end_hour: int | None = UsageEnd,
) -> dict[str, Any]:
    """Per-skill and per-plugin invocation counts, injected context and attributed tokens."""
    return get_skill_usage(**_usage_filters(subscription_ids, date, start_hour, end_hour))


@app.get("/api/usage/tools")
def api_usage_tools(
    subscription_ids: str | None = UsageIds,
    date: str | None = UsageDate,
    start_hour: int | None = UsageStart,
    end_hour: int | None = UsageEnd,
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    """Tool-call leaderboard with totals per tool kind and MCP server."""
    return get_tool_usage(**_usage_filters(subscription_ids, date, start_hour, end_hour), limit=limit)


@app.get("/api/usage/skill-timeline")
def api_usage_skill_timeline(
    subscription_ids: str | None = UsageIds,
    date: str | None = UsageDate,
    start_hour: int | None = UsageStart,
    end_hour: int | None = UsageEnd,
    bucket: str = Query("day", pattern="^(hour|day)$"),
) -> list[dict[str, Any]]:
    """Skill invocations per hour or day."""
    return get_skill_timeline(**_usage_filters(subscription_ids, date, start_hour, end_hour), bucket=bucket)


@app.post("/api/usage/rescan")
async def api_usage_rescan() -> dict[str, Any]:
    """Index new transcript lines right now."""
    import asyncio

    return await asyncio.to_thread(scan_transcripts)


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    """Return collector status and schedule info."""
    return {
        "status": collector.last_run_status,
        "last_run_time": collector.last_run_time,
        "interval_seconds": collector.interval_seconds,
        "usage_scan": collector.last_usage_scan,
    }


# Mount static assets if frontend directory exists
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
def serve_index():
    """Serve frontend dashboard."""
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"message": "Frontend not yet initialized"}
