"""Runtime settings read from environment variables.

Every knob the dashboard exposes is an ``LLM_DASHBOARD_*`` variable so the
LaunchAgent plist, ``start.sh`` and a plain shell all configure it the same
way. Invalid values fall back to the default with a warning rather than
stopping the service.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 300
# The Anthropic usage endpoint rate-limits aggressively (429). Polling more
# often than this mostly produces cached readings, so the floor protects the
# data rather than the machine.
MIN_POLL_SECONDS = 30

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "llm_dashboard.db"


def poll_interval_seconds(env: dict[str, str] | None = None) -> int:
    """Seconds between two collection passes, from ``LLM_DASHBOARD_POLL_SECONDS``.

    Accepts a positive integer. Anything unparsable falls back to the default;
    anything below ``MIN_POLL_SECONDS`` is raised to the floor.
    """
    source = os.environ if env is None else env
    raw = source.get("LLM_DASHBOARD_POLL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_POLL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "LLM_DASHBOARD_POLL_SECONDS=%r is not an integer; using the default of %ds.",
            raw,
            DEFAULT_POLL_SECONDS,
        )
        return DEFAULT_POLL_SECONDS
    if value < MIN_POLL_SECONDS:
        logger.warning(
            "LLM_DASHBOARD_POLL_SECONDS=%d is below the %ds floor; using %ds.",
            value,
            MIN_POLL_SECONDS,
            MIN_POLL_SECONDS,
        )
        return MIN_POLL_SECONDS
    return value


def db_path(env: dict[str, str] | None = None) -> Path:
    """Where the SQLite file lives, from ``LLM_DASHBOARD_DB_PATH``."""
    source = os.environ if env is None else env
    return Path(source.get("LLM_DASHBOARD_DB_PATH") or DEFAULT_DB_PATH)
