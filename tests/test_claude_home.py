"""Tests for Claude home discovery and the provider's offline fallbacks."""

from __future__ import annotations

import hashlib
import json

from backend.claude_home import (
    DEFAULT_KEYCHAIN_SERVICE,
    config_file_for_home,
    discover_claude_homes,
    keychain_service_for_home,
)
from backend.providers.claude_code import ClaudeCodeProvider


def _make_home(base, name, *, account=None, cached=None, projects=True):
    home = base / name
    home.mkdir()
    if projects:
        (home / "projects").mkdir()
    config = {}
    if account:
        config["oauthAccount"] = account
    if cached is not None:
        config["cachedUsageUtilization"] = {"utilization": cached}
    if config:
        config_file_for_home(home).write_text(json.dumps(config))
    return home


def test_default_home_uses_bare_service_and_sibling_config(tmp_path):
    home = tmp_path / ".claude"
    assert keychain_service_for_home(home) == DEFAULT_KEYCHAIN_SERVICE
    assert config_file_for_home(home) == tmp_path / ".claude.json"


def test_other_homes_hash_their_path_like_claude_code(tmp_path):
    home = tmp_path / ".claude_work"
    digest = hashlib.sha256(str(home).encode()).hexdigest()[:8]
    assert keychain_service_for_home(home) == f"{DEFAULT_KEYCHAIN_SERVICE}-{digest}"
    assert config_file_for_home(home) == home / ".claude.json"


def test_discover_homes_skips_backups_files_and_optionally_empty_homes(tmp_path):
    _make_home(tmp_path, ".claude")
    _make_home(tmp_path, ".claude_work", projects=False)
    (tmp_path / ".claude_old.backup").mkdir()
    (tmp_path / ".claude.json").write_text("{}")

    assert [h.name for h in discover_claude_homes(tmp_path)] == [".claude", ".claude_work"]
    assert [h.name for h in discover_claude_homes(tmp_path, with_transcripts=True)] == [".claude"]


def test_provider_discovers_a_keychain_service_per_home(tmp_path, monkeypatch):
    _make_home(tmp_path, ".claude")
    work = _make_home(tmp_path, ".claude_work")
    provider = ClaudeCodeProvider(username="someone", user_home=tmp_path)
    # Keep the test off the real keychain.
    monkeypatch.setattr("backend.providers.claude_code.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(OSError))

    assert provider.discover_keychain_services() == sorted([DEFAULT_KEYCHAIN_SERVICE, keychain_service_for_home(work)])


def test_provider_falls_back_to_the_matching_local_config(tmp_path):
    _make_home(
        tmp_path,
        ".claude",
        account={"accountUuid": "u-default", "emailAddress": "a@example.com", "organizationName": "Default"},
        cached={"five_hour": {"utilization": 12.0, "resets_at": None}},
    )
    work = _make_home(
        tmp_path,
        ".claude_work",
        account={"accountUuid": "u-work", "emailAddress": "b@example.com", "organizationName": "Work"},
        cached={"five_hour": {"utilization": 99.0, "resets_at": None}},
    )
    provider = ClaudeCodeProvider(username="someone", user_home=tmp_path)

    default = provider._get_fallback_account_info(DEFAULT_KEYCHAIN_SERVICE)
    assert default and default["email"] == "a@example.com"
    assert default["organization_name"] == "Default"

    from_work = provider._get_fallback_account_info(keychain_service_for_home(work))
    assert from_work and from_work["account_uuid"] == "u-work"

    assert provider._get_fallback_account_info("Claude Code-credentials-deadbeef") is None
    assert provider._read_local_cached_usage("u-work")["five_hour"]["utilization"] == 99.0
    assert provider._read_local_cached_usage("nobody") is None
    assert provider.get_active_account_uuid() == "u-default"


def test_provider_ignores_unreadable_configs(tmp_path):
    home = _make_home(tmp_path, ".claude_broken")
    config_file_for_home(home).write_text("{not json")
    provider = ClaudeCodeProvider(username="someone", user_home=tmp_path)

    assert provider._get_fallback_account_info(keychain_service_for_home(home)) is None
    assert provider.get_active_account_uuid() is None
