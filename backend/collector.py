"""Background quota collector service.

Periodically queries registered providers (every 5 minutes by default)
and persists snapshots and detected events to SQLite.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.database import insert_snapshot, upsert_subscription
from backend.usage import scan_transcripts
from backend.providers.base import BaseProvider
from backend.providers.claude_code import ClaudeCodeProvider

logger = logging.getLogger(__name__)


class QuotaCollector:
    """Manages scheduled and on-demand quota metrics collection."""

    # When a pass falls back to cache (usually a 429), retry well before the
    # normal interval so a stale reading is corrected quickly.
    RETRY_SECONDS = 60

    def __init__(self, interval_seconds: int = 300):
        self.interval_seconds = interval_seconds
        self.providers: list[BaseProvider] = [ClaudeCodeProvider()]
        self._running = False
        self._task: asyncio.Task | None = None
        self.last_run_time: str | None = None
        self.last_run_status: str = "idle"
        self.last_run_had_stale: bool = False
        self.last_usage_scan: dict[str, Any] | None = None

    async def collect_once(self) -> dict[str, Any]:
        """Execute one collection pass across all providers and save to database."""
        logger.info("Starting quota collection pass...")
        self.last_run_status = "running"
        summary: dict[str, Any] = {"collected_accounts": 0, "stale_accounts": 0, "errors": []}

        for provider in self.providers:
            try:
                accounts = await provider.discover_and_fetch_all()
                for acc in accounts:
                    sub_record = upsert_subscription(
                        email=acc["email"],
                        account_uuid=acc.get("account_uuid"),
                        organization_name=acc.get("organization_name"),
                        organization_uuid=acc.get("organization_uuid"),
                        keychain_service=acc.get("keychain_service"),
                        is_active=acc.get("is_active", False),
                        provider=provider.provider_id,
                    )
                    sub_id = sub_record["id"]
                    quota = acc.get("quota") or {}

                    five_hour_pct = quota.get("five_hour_pct")
                    seven_day_pct = quota.get("seven_day_pct")

                    # Do not record completely empty snapshots
                    if five_hour_pct is None and seven_day_pct is None:
                        logger.warning("Skipping snapshot insertion for %s due to missing quota metrics.", acc.get("email"))
                        continue

                    insert_snapshot(
                        subscription_id=sub_id,
                        five_hour_pct=five_hour_pct,
                        five_hour_resets_at=quota.get("five_hour_resets_at"),
                        seven_day_pct=seven_day_pct,
                        seven_day_resets_at=quota.get("seven_day_resets_at"),
                        scoped_model=quota.get("scoped_model"),
                        scoped_pct=quota.get("scoped_pct"),
                        spend_used=quota.get("spend_used"),
                        spend_limit=quota.get("spend_limit"),
                        spend_currency=quota.get("spend_currency"),
                        is_stale=bool(quota.get("is_stale")),
                        raw_data=quota.get("raw"),
                    )
                    summary["collected_accounts"] += 1
                    if quota.get("is_stale"):
                        summary["stale_accounts"] += 1
                        logger.warning(
                            "Recorded a CACHED (not live) reading for %s; will retry shortly.",
                            acc.get("email"),
                        )
            except Exception as e:
                logger.exception("Error collecting from provider %s: %s", provider.provider_id, e)
                summary["errors"].append(str(e))

        # Index any new Claude Code transcript lines. Runs off the event loop
        # because a cold scan of every transcript takes a few seconds.
        try:
            self.last_usage_scan = await asyncio.to_thread(scan_transcripts)
        except Exception as e:
            logger.exception("Transcript scan failed: %s", e)
            summary["errors"].append(f"usage scan: {e}")

        from datetime import datetime, timezone

        self.last_run_time = datetime.now(timezone.utc).isoformat()
        self.last_run_had_stale = summary["stale_accounts"] > 0
        if summary["errors"]:
            self.last_run_status = "error"
        elif self.last_run_had_stale:
            self.last_run_status = "stale"
        else:
            self.last_run_status = "ok"
        logger.info(
            "Collection finished. Recorded %d accounts (%d from cache).",
            summary["collected_accounts"],
            summary["stale_accounts"],
        )
        return summary

    async def _loop(self, skip_first: bool = False) -> None:
        """Internal background polling loop."""
        first = True
        while self._running:
            # Startup already ran a pass; polling again immediately would just
            # double our request rate against a rate-limited endpoint.
            if first and skip_first:
                first = False
            else:
                try:
                    await self.collect_once()
                except Exception as e:
                    logger.error("Unexpected error in collector loop: %s", e)
            # Come back sooner when the last pass only had cached data to show.
            delay = self.RETRY_SECONDS if self.last_run_had_stale else self.interval_seconds
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    def start(self, skip_first: bool = False) -> None:
        """Start the background collector loop.

        Pass skip_first when a collection has just run, so the loop waits out a
        full interval instead of polling again straight away.
        """
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._loop(skip_first=skip_first))
            logger.info("Background QuotaCollector started (every %d seconds).", self.interval_seconds)

    def stop(self) -> None:
        """Stop the background collector loop."""
        if self._running:
            self._running = False
            if self._task:
                self._task.cancel()
                self._task = None
            logger.info("Background QuotaCollector stopped.")


collector = QuotaCollector(interval_seconds=300)
