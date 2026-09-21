"""Tests for the local Claude Code transcript scanner and its aggregates."""

from __future__ import annotations

import hashlib
import json

import pytest

from backend import database
from backend.database import init_db, insert_snapshot, upsert_subscription
from backend.usage import (
    get_five_hour_windows,
    get_usage_by_model,
    get_usage_sessions,
    get_usage_summary,
    keychain_service_for_home,
    scan_transcripts,
)


def _assistant_line(session, ts, model, msg_id, req_id, usage, tools=0, sidechain=False, cwd="/Users/a/src/proj"):
    content = [{"type": "text", "text": "hi"}] + [
        {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}} for i in range(tools)
    ]
    return json.dumps(
        {
            "parentUuid": None,
            "isSidechain": sidechain,
            "type": "assistant",
            "sessionId": session,
            "timestamp": ts,
            "cwd": cwd,
            "gitBranch": "main",
            "version": "2.1.0",
            "requestId": req_id,
            "message": {"id": msg_id, "model": model, "role": "assistant", "content": content, "usage": usage},
        }
    )


def _usage(inp=10, cc=100, cr=1000, out=50):
    return {
        "input_tokens": inp,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
        "output_tokens": out,
        "cache_creation": {"ephemeral_1h_input_tokens": cc, "ephemeral_5m_input_tokens": 0},
        "output_tokens_details": {"thinking_tokens": 5},
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    monkeypatch.setattr(database, "_initialized", False)
    init_db()

    user_home = tmp_path / "home"
    default_home = user_home / ".claude"
    labs_home = user_home / ".claude_labs"
    (default_home / "projects" / "-tmp-proj").mkdir(parents=True)
    (labs_home / "projects" / "-tmp-proj").mkdir(parents=True)

    vooban = upsert_subscription(
        email="a@vooban.com",
        account_uuid="u2",
        organization_name="Vooban",
        organization_uuid="o2",
        keychain_service="Claude Code-credentials",
        is_active=True,
    )
    labs = upsert_subscription(
        email="a@labs.com",
        account_uuid="u1",
        organization_name="Labs",
        organization_uuid="o1",
        keychain_service=keychain_service_for_home(labs_home),
        is_active=False,
    )
    return {
        "home": user_home,
        "default": default_home,
        "labs": labs_home,
        "vooban_id": vooban["id"],
        "labs_id": labs["id"],
    }


def test_keychain_service_hash_matches_claude_code_convention(tmp_path):
    home = tmp_path / ".claude_x"
    digest = hashlib.sha256(str(home).encode()).hexdigest()[:8]
    assert keychain_service_for_home(home) == f"Claude Code-credentials-{digest}"
    assert keychain_service_for_home(tmp_path / ".claude") == "Claude Code-credentials"


def test_scan_dedupes_streamed_lines_and_maps_accounts(env):
    sess = "11111111-1111-1111-1111-111111111111"
    f = env["default"] / "projects" / "-tmp-proj" / f"{sess}.jsonl"
    lines = [
        json.dumps({"type": "ai-title", "aiTitle": "Fix the widget", "sessionId": sess}),
        json.dumps(
            {
                "type": "user",
                "isSidechain": False,
                "sessionId": sess,
                "timestamp": "2026-09-09T10:00:00.000Z",
                "message": {"role": "user", "content": "Please fix the widget"},
            }
        ),
        # Same message streamed as two lines: partial usage first, then final; one tool call each.
        _assistant_line(
            sess,
            "2026-09-09T10:00:05.000Z",
            "claude-opus-5",
            "m1",
            "r1",
            {"input_tokens": 10, "output_tokens": 0},
            tools=1,
        ),
        _assistant_line(sess, "2026-09-09T10:00:06.000Z", "claude-opus-5", "m1", "r1", _usage(out=80), tools=1),
        _assistant_line(sess, "2026-09-09T10:01:00.000Z", "claude-sonnet-5", "m2", "r2", _usage(out=20)),
        # Synthetic error rows carry no real usage and must be ignored.
        _assistant_line(sess, "2026-09-09T10:02:00.000Z", "<synthetic>", "m3", "r3", _usage(0, 0, 0, 0)),
    ]
    f.write_text("\n".join(lines) + "\n")

    # Subagent transcript nested under the session directory, for the other account.
    sess2 = "22222222-2222-2222-2222-222222222222"
    nested = env["labs"] / "projects" / "-tmp-proj" / sess2 / "subagents"
    nested.mkdir(parents=True)
    (nested / "agent-abc.jsonl").write_text(
        _assistant_line(
            sess2, "2026-09-09T11:00:00.000Z", "claude-fable-5-1", "m9", "r9", _usage(out=300), sidechain=True
        )
        + "\n"
    )

    result = scan_transcripts(user_home=env["home"])
    assert result["files_scanned"] == 2
    assert result["turns_written"] == 3

    summary = get_usage_summary()
    assert summary["turns"] == 3
    assert summary["sessions"] == 2
    assert summary["output_tokens"] == 80 + 20 + 300
    assert summary["tool_calls"] == 2  # both streamed lines of m1 contributed one tool_use each
    assert summary["sidechain_turns"] == 1
    per_account = {a["subscription_id"]: a for a in summary["per_account"]}
    assert per_account[env["vooban_id"]]["turns"] == 2
    assert per_account[env["labs_id"]]["turns"] == 1

    models = {m["model"]: m for m in get_usage_by_model()}
    assert set(models) == {"claude-opus-5", "claude-sonnet-5", "claude-fable-5-1"}
    assert models["claude-fable-5-1"]["model_label"] == "Fable 5.1"
    assert abs(sum(m["share_cost"] for m in models.values()) - 1.0) < 1e-9

    sessions = {s["session_id"]: s for s in get_usage_sessions()}
    assert sessions[sess]["title"] == "Fix the widget"
    assert sessions[sess]["first_prompt"] == "Please fix the widget"
    assert sessions[sess]["project_name"] == "proj"

    # A second pass with nothing new touches no files and writes no turns.
    again = scan_transcripts(user_home=env["home"])
    assert again["files_scanned"] == 0 and again["turns_written"] == 0

    # Appending a line is picked up incrementally without duplicating older turns.
    with f.open("a") as fh:
        fh.write(_assistant_line(sess, "2026-09-09T10:05:00.000Z", "claude-opus-5", "m4", "r4", _usage(out=1)) + "\n")
    third = scan_transcripts(user_home=env["home"])
    assert third["files_scanned"] == 1 and third["turns_written"] == 1
    assert get_usage_summary()["turns"] == 4


def test_five_hour_windows_attribute_models_by_share(env):
    sess = "33333333-3333-3333-3333-333333333333"
    f = env["default"] / "projects" / "-tmp-proj" / f"{sess}.jsonl"
    # Two turns inside the window, one outside it.
    f.write_text(
        "\n".join(
            [
                _assistant_line(sess, "2026-09-09T13:00:00.000Z", "claude-opus-5", "a", "a", _usage(0, 0, 0, 1000)),
                _assistant_line(sess, "2026-09-09T14:00:00.000Z", "claude-sonnet-5", "b", "b", _usage(0, 0, 0, 1000)),
                _assistant_line(sess, "2026-09-09T08:00:00.000Z", "claude-opus-5", "c", "c", _usage(0, 0, 0, 5000)),
            ]
        )
        + "\n"
    )
    scan_transcripts(user_home=env["home"])

    sub = env["vooban_id"]
    # Deadline 17:00Z, so the window is 12:00Z-17:00Z. The API jitters the
    # deadline by a few hundred ms between polls; those are one window.
    insert_snapshot(
        subscription_id=sub,
        five_hour_pct=10,
        five_hour_resets_at="2026-09-09T17:00:00.100+00:00",
        seven_day_pct=5,
        seven_day_resets_at=None,
        timestamp="2026-09-09T12:30:00+00:00",
    )
    insert_snapshot(
        subscription_id=sub,
        five_hour_pct=40,
        five_hour_resets_at="2026-09-09T16:59:59.900+00:00",
        seven_day_pct=5,
        seven_day_resets_at=None,
        timestamp="2026-09-09T14:30:00+00:00",
    )

    windows = get_five_hour_windows(subscription_ids=[sub])
    assert len(windows) == 1
    w = windows[0]
    assert w["readings"] == 2
    assert w["peak_pct"] == 40
    assert w["turns"] == 2
    by_model = {m["model"]: m for m in w["models"]}
    # Opus output costs 25/MTok vs Sonnet 15/MTok, so opus takes 62.5% of the cost share.
    assert by_model["claude-opus-5"]["window_pct_by_cost"] == pytest.approx(40 * 25 / 40)
    assert by_model["claude-sonnet-5"]["window_pct_by_cost"] == pytest.approx(40 * 15 / 40)
    assert by_model["claude-opus-5"]["window_pct_by_tokens"] == pytest.approx(20)


@pytest.mark.parametrize(
    "cwd,expected",
    [
        ("/Users/a/.t3/worktrees/morphe/t3code-38ca7f52", "morphe"),
        ("/Users/a/.t3/worktrees/morphe/t3code-475b66d8/frontend/morphe", "morphe"),
        ("/Users/a/src/morphe/.claude/worktrees/cool-payne-dc62ab", "morphe"),
        ("/Users/a/src/morphe/.claude/worktrees/x/frontend/morphe", "morphe"),
        ("/Users/a/.t3/worktrees/morphe/t3code-1/.claude/worktrees/ci-phase2", "morphe"),
        ("/Users/a/src/morphe", "morphe"),
        ("/Users/a/src/llm_dashboard/backend", "llm_dashboard"),
        ("/Users/a", "(home)"),
        ("/private/tmp", "(scratch)"),
        ("/private/tmp/loading-project", "(scratch)"),
        ("/private/var/folders/lj/x/T/t3code-claude-title-yQdZFk", "T3 Code helpers"),
        ("/Users/a/.claude/projects/-Users-a-src-morphe/memory", "(claude memory)"),
        (None, "(unknown)"),
    ],
)
def test_canonical_project_collapses_worktrees(cwd, expected):
    from backend.usage import canonical_project

    assert canonical_project(cwd) == expected
