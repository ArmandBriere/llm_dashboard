"""Base interface for LLM quota providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseProvider(ABC):
    """Abstract provider for querying LLM accounts and usage quotas."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Unique identifier for this provider (e.g. 'claude_code')."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human readable name (e.g. 'Claude Code')."""
        pass

    @abstractmethod
    async def discover_and_fetch_all(self) -> list[dict[str, Any]]:
        """Discover all configured accounts and fetch their live quota status.

        Returns a list of dicts with shape:
        {
            "account_uuid": str,
            "email": str,
            "organization_name": str,
            "organization_uuid": str,
            "keychain_service": str | None,
            "is_active": bool,
            "quota": {
                "five_hour_pct": float | None,
                "five_hour_resets_at": str | None,
                "seven_day_pct": float | None,
                "seven_day_resets_at": str | None,
                "scoped_model": str | None,
                "scoped_pct": float | None,
                "spend_used": float | None,
                "spend_limit": float | None,
                "spend_currency": str | None,
                "raw": dict,
            }
        }
        """
        pass
