"""Direct native Claude Code quota provider.

Communicates directly with macOS Keychain and Anthropic's OAuth API.
Does NOT use or depend on claude-swap.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from backend.providers.base import BaseProvider

logger = logging.getLogger(__name__)

OAUTH_BETA_HEADER = "oauth-2025-04-20"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"


class ClaudeCodeProvider(BaseProvider):
    """Provider for Claude Code subscriptions via native Keychain & Anthropic API."""

    def discover_keychain_services(self) -> list[str]:
        """Find all Claude Code credential service entries in macOS Keychain."""
        services: set[str] = set()

        # 1. Primary default service
        services.add("Claude Code-credentials")

        # 2. Check known hashes from ~/.claude* directories
        home = Path.home()
        for d in home.glob(".claude*"):
            if d.is_dir() and not d.name.endswith(".backup"):
                path_str = str(d)
                digest = hashlib.sha256(path_str.encode("utf-8")).hexdigest()[:8]
                services.add(f"Claude Code-credentials-{digest}")

        # 3. Discover from `security dump-keychain`
        try:
            res = subprocess.run(
                ["/usr/bin/security", "dump-keychain"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0:
                matches = re.findall(r'"svce"<blob>="(Claude Code-credentials[^"]*)"', res.stdout)
                for m in matches:
                    services.add(m)
        except Exception as e:
            logger.debug("Failed to dump keychain: %s", e)

        return sorted(list(services))

    def read_credentials_from_keychain(self, service_name: str) -> dict[str, Any] | None:
        """Read and parse credentials JSON from macOS Keychain."""
        try:
            cmd = ["/usr/bin/security", "find-generic-password", "-s", service_name, "-a", self.username, "-w"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if res.returncode == 0 and res.stdout.strip():
                return json.loads(res.stdout.strip())
        except Exception as e:
            logger.warning("Error reading keychain service %s: %s", service_name, e)
        return None

    def update_keychain_credentials(self, service_name: str, creds: dict[str, Any]) -> bool:
        """Update keychain entry with new credentials (e.g. after token refresh)."""
        try:
            data = json.dumps(creds)
            # Use security add-generic-password -U to update
            cmd = [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-s",
                service_name,
                "-a",
                self.username,
                "-w",
                data,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            return res.returncode == 0
        except Exception as e:
            logger.warning("Failed to update keychain for %s: %s", service_name, e)
            return False

    async def ensure_valid_token(self, service_name: str, creds: dict[str, Any]) -> str | None:
        """Verify token expiration and refresh via OAuth endpoint if needed."""
        oauth_data = creds.get("claudeAiOauth")
        if not oauth_data:
            return None

        access_token = oauth_data.get("accessToken")
        refresh_token = oauth_data.get("refreshToken")
        expires_at_ms = oauth_data.get("expiresAt")

        now_ms = int(time.time() * 1000)
        # Refresh if expires in less than 5 minutes or already expired
        needs_refresh = expires_at_ms and (now_ms + 300_000 >= int(expires_at_ms))

        if not needs_refresh and access_token:
            return access_token

        if not refresh_token:
            logger.warning("Token expired for %s and no refreshToken present", service_name)
            return access_token  # try anyway

        logger.info("Refreshing OAuth token for %s...", service_name)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    OAUTH_TOKEN_URL,
                    json={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": OAUTH_CLIENT_ID,
                    },
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code == 200:
                    token_data = resp.json()
                    new_access_token = token_data.get("access_token")
                    new_refresh_token = token_data.get("refresh_token", refresh_token)
                    expires_in = token_data.get("expires_in", 3600)

                    oauth_data["accessToken"] = new_access_token
                    oauth_data["refreshToken"] = new_refresh_token
                    oauth_data["expiresAt"] = int(time.time() * 1000) + (expires_in * 1000)
                    creds["claudeAiOauth"] = oauth_data

                    self.update_keychain_credentials(service_name, creds)
                    return new_access_token
                else:
                    logger.warning("Token refresh failed (status %d): %s", resp.status_code, resp.text)
        except Exception as e:
            logger.error("Exception during token refresh for %s: %s", service_name, e)

        return access_token

    async def fetch_profile(self, access_token: str) -> dict[str, Any] | None:
        """Call Anthropic OAuth profile endpoint."""
        try:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "anthropic-beta": OAUTH_BETA_HEADER,
                "User-Agent": "Claude-Code/2.1.266",
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(ANTHROPIC_PROFILE_URL, headers=headers)
                if resp.status_code == 200:
                    return resp.json()
                logger.warning("Profile fetch returned %d: %s", resp.status_code, resp.text)
        except Exception as e:
            logger.warning("Failed to fetch profile: %s", e)
        return None

    def __init__(self, username: str | None = None):
        self.username = username or os.environ.get("USER") or "armand.briere"
        self._last_known_usage: dict[str, dict[str, Any]] = {}

    @property
    def provider_id(self) -> str:
        return "claude_code"

    @property
    def display_name(self) -> str:
        return "Claude Code"

    def _get_fallback_account_info(self, svc: str) -> dict[str, Any] | None:
        """Derive account info from local .claude.json files based on service name or scan."""
        home = Path.home()
        dirs = [home, home / ".claude_vooban", home / ".claude_9router"]
        for d in dirs:
            json_file = d / ".claude.json" if d != home else home / ".claude.json"
            if json_file.exists():
                try:
                    with open(json_file) as f:
                        data = json.load(f)
                        oauth = data.get("oauthAccount")
                        if oauth and oauth.get("accountUuid"):
                            path_str = str(d)
                            digest = hashlib.sha256(path_str.encode("utf-8")).hexdigest()[:8]
                            # If service matches or it's default
                            if svc == f"Claude Code-credentials-{digest}" or (svc == "Claude Code-credentials" and d == home):
                                return {
                                    "account_uuid": oauth.get("accountUuid"),
                                    "email": oauth.get("emailAddress"),
                                    "organization_name": oauth.get("organizationName"),
                                    "organization_uuid": oauth.get("organizationUuid"),
                                }
                except Exception:
                    pass
        return None

    def _read_local_cached_usage(self, account_uuid: str) -> dict[str, Any] | None:
        """Search ~/.claude* json files for cachedUsageUtilization matching account_uuid."""
        home = Path.home()
        for p in [home / ".claude.json", home / ".claude_vooban" / ".claude.json", home / ".claude_9router" / ".claude.json"]:
            if p.exists():
                try:
                    with open(p) as f:
                        d = json.load(f)
                        if d.get("oauthAccount", {}).get("accountUuid") == account_uuid:
                            cached = d.get("cachedUsageUtilization", {})
                            if cached and "utilization" in cached:
                                return cached["utilization"]
                except Exception:
                    pass
        return None

    @staticmethod
    def _parse_window(usage: dict[str, Any], key: str) -> tuple[float | None, str | None]:
        """Read one quota window's utilization and reset time.

        The 5-hour quota is a ROLLING window: it opens on the first request of
        a session and expires five hours later. Between sessions no window
        exists, and the API reports {"utilization": 0.0, "resets_at": null}.
        That is a real measurement -- genuinely nothing used -- not missing
        data, so it is recorded as 0% with no reset time.
        """
        window = usage.get(key) or {}
        return window.get("utilization"), window.get("resets_at")

    @staticmethod
    def _has_expired_window(usage: dict[str, Any]) -> bool:
        """True when a snapshot's own resets_at has already passed.

        A cached reading whose reset moment is in the past describes a quota
        window that no longer exists — the real utilization has since reset to
        something lower. Serving it would report a stale 100% as if it were
        current, so such a snapshot must never be used as a fallback.
        """
        for key in ("five_hour", "seven_day"):
            resets_at = (usage.get(key) or {}).get("resets_at")
            if not resets_at:
                continue
            try:
                dt = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt <= datetime.now(timezone.utc):
                return True
        return False

    async def fetch_usage(self, access_token: str, account_uuid: str | None = None) -> dict[str, Any] | None:
        """Call Anthropic OAuth usage API, falling back to cached utilization.

        Fallback readings are tagged with `_stale: True` so callers can tell a
        live measurement from a remembered one. A cached snapshot whose quota
        window has already reset is discarded rather than reported, because its
        utilization no longer describes the current window.
        """
        try:
            headers = {
                "Authorization": f"Bearer {access_token}",
                "anthropic-beta": OAUTH_BETA_HEADER,
                "User-Agent": "Claude-Code/2.1.266",
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(ANTHROPIC_USAGE_URL, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    data["_stale"] = False
                    if account_uuid:
                        self._last_known_usage[account_uuid] = data
                    return data
                elif resp.status_code == 429:
                    logger.warning("Anthropic usage API rate-limited (429). Falling back to cached utilization.")
                else:
                    logger.warning("Usage fetch returned %d: %s", resp.status_code, resp.text)
        except Exception as e:
            logger.warning("Failed to fetch usage from Anthropic API: %s", e)

        # Fallback to in-memory or disk cache, newest first.
        if account_uuid:
            for source, cached in (
                ("memory", self._last_known_usage.get(account_uuid)),
                ("disk", self._read_local_cached_usage(account_uuid)),
            ):
                if not cached:
                    continue
                if self._has_expired_window(cached):
                    logger.warning(
                        "Discarding %s cache for %s: its quota window already reset, "
                        "so the cached utilization is no longer valid.",
                        source,
                        account_uuid,
                    )
                    continue
                stale = dict(cached)
                stale["_stale"] = True
                return stale

        return None

    def get_active_account_uuid(self) -> str | None:
        """Determine which account is currently active in the main ~/.claude.json."""
        try:
            main_json = Path.home() / ".claude.json"
            if main_json.exists():
                with open(main_json) as f:
                    data = json.load(f)
                    return data.get("oauthAccount", {}).get("accountUuid")
        except Exception:
            pass
        return None

    async def discover_and_fetch_all(self) -> list[dict[str, Any]]:
        """Discover accounts from Keychain, fetch identity and usage directly."""
        services = self.discover_keychain_services()
        active_uuid = self.get_active_account_uuid()

        # Keyed by account_uuid so several keychain entries pointing at the same
        # account collapse into one result instead of one entry each.
        accounts: list[dict[str, Any]] = []
        by_uuid: dict[str, dict[str, Any]] = {}

        for svc in services:
            creds = self.read_credentials_from_keychain(svc)
            if not creds or "claudeAiOauth" not in creds:
                continue

            access_token = await self.ensure_valid_token(svc, creds)
            if not access_token:
                continue

            # Fetch authoritative profile (with local fallback if network/429 occurs)
            profile = await self.fetch_profile(access_token)
            account_info = profile.get("account", {}) if profile else {}
            org_info = profile.get("organization", {}) if profile else {}

            account_uuid = account_info.get("uuid")
            email = account_info.get("email")
            org_name = org_info.get("name")
            org_uuid = org_info.get("uuid")

            # Fallback to local config files if profile endpoint is unavailable
            if not account_uuid or not email:
                fallback_info = self._get_fallback_account_info(svc)
                if fallback_info:
                    account_uuid = account_uuid or fallback_info.get("account_uuid")
                    email = email or fallback_info.get("email")
                    org_name = org_name or fallback_info.get("organization_name")
                    org_uuid = org_uuid or fallback_info.get("organization_uuid")

            # Several keychain entries can point at the same account (e.g.
            # ~/.claude and ~/.claude_9router). Skip the duplicate call once we
            # already hold a LIVE reading — but if the earlier entry only
            # produced cached data (typically a 429), try this one, since it
            # carries a different token that may not be rate-limited.
            existing = by_uuid.get(account_uuid) if account_uuid else None
            if existing is not None and not existing["quota"]["is_stale"]:
                logger.debug("Skipping duplicate keychain entry %s for account %s", svc, email)
                continue

            # Fetch live usage quota
            raw_usage = await self.fetch_usage(access_token, account_uuid=account_uuid)

            # Parse quota details
            five_hour_pct = None
            five_hour_resets_at = None
            seven_day_pct = None
            seven_day_resets_at = None
            scoped_model = None
            scoped_pct = None
            scoped_resets_at = None
            spend_used = None
            spend_limit = None
            spend_currency = None

            if raw_usage:
                five_hour_pct, five_hour_resets_at = self._parse_window(raw_usage, "five_hour")
                seven_day_pct, seven_day_resets_at = self._parse_window(raw_usage, "seven_day")

                # Scoped model limit (e.g. Fable)
                for lim in raw_usage.get("limits", []):
                    if lim.get("kind") == "weekly_scoped":
                        model_name = lim.get("scope", {}).get("model", {}).get("display_name")
                        scoped_model = model_name
                        scoped_pct = lim.get("percent")
                        scoped_resets_at = lim.get("resets_at")
                        break

                # Spend
                spend = raw_usage.get("spend") or {}
                if spend.get("used"):
                    used_minor = spend["used"].get("amount_minor", 0)
                    exp = spend["used"].get("exponent", 2)
                    spend_used = used_minor / (10**exp)
                    spend_currency = spend["used"].get("currency")
                if spend.get("limit"):
                    lim_minor = spend["limit"].get("amount_minor", 0)
                    exp = spend["limit"].get("exponent", 2)
                    spend_limit = lim_minor / (10**exp)

            is_active = bool(account_uuid and account_uuid == active_uuid)

            record = {
                "account_uuid": account_uuid,
                "email": email or "unknown@claude.ai",
                "organization_name": org_name or "Unknown Org",
                "organization_uuid": org_uuid,
                "keychain_service": svc,
                # Any credential for the active account marks it active.
                "is_active": is_active or bool(existing and existing["is_active"]),
                "quota": {
                    "five_hour_pct": five_hour_pct,
                    "five_hour_resets_at": five_hour_resets_at,
                    "seven_day_pct": seven_day_pct,
                    "seven_day_resets_at": seven_day_resets_at,
                    "scoped_model": scoped_model,
                    "scoped_pct": scoped_pct,
                    "scoped_resets_at": scoped_resets_at,
                    "spend_used": spend_used,
                    "spend_limit": spend_limit,
                    "spend_currency": spend_currency,
                    "is_stale": bool((raw_usage or {}).get("_stale")),
                    "raw": raw_usage,
                },
            }

            if existing is None:
                accounts.append(record)
                if account_uuid:
                    by_uuid[account_uuid] = record
            elif raw_usage is not None:
                # This retry beat the earlier stale/empty attempt — replace it.
                accounts[accounts.index(existing)] = record
                by_uuid[account_uuid] = record

        return accounts
