"""Tests for skill / plugin / tool extraction and the attribution model."""

from __future__ import annotations

import json

import pytest

from backend import database
from backend.database import init_db, upsert_subscription
from backend.skills import (
    classify_tool,
    get_skill_usage,
    get_tool_usage,
    parse_skill_base_dir,
    split_skill_id,
    version_key,
)
from backend.usage import get_usage_sessions, keychain_service_for_home, scan_transcripts

PLUGIN_DIR = "/Users/a/.claude/plugins/cache/monad-tools/git/0.14.0/skills/pr"


def _usage(inp=10, cc=100, cr=1000, out=50):
    return {
        "input_tokens": inp,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
        "output_tokens": out,
        "cache_creation": {"ephemeral_1h_input_tokens": cc, "ephemeral_5m_input_tokens": 0},
        "output_tokens_details": {"thinking_tokens": 5},
    }


def _turn(session, ts, msg_id, uuid, tools=(), model="claude-opus-5", sidechain=False, out=50):
    """One assistant record. ``tools`` is a list of (tool_name, input) pairs."""
    content = [{"type": "text", "text": "ok"}]
    for i, (name, payload) in enumerate(tools):
        content.append({"type": "tool_use", "id": f"{msg_id}-{i}", "name": name, "input": payload})
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "isSidechain": sidechain,
            "sessionId": session,
            "timestamp": ts,
            "cwd": "/Users/a/src/proj",
            "gitBranch": "main",
            "requestId": f"req-{msg_id}",
            "message": {
                "id": msg_id,
                "model": model,
                "role": "assistant",
                "content": content,
                "usage": _usage(out=out),
            },
        }
    )


def _launch_result(session, ts, tool_use_id, uuid, skill):
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "sessionId": session,
            "timestamp": ts,
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": f"Launching skill: {skill}"}
                ],
            },
            "toolUseResult": {"success": True, "commandName": skill},
        }
    )


def _error_result(session, ts, tool_use_id, uuid, skill):
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "sessionId": session,
            "timestamp": ts,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "is_error": True,
                        "content": f"<tool_use_error>Unknown skill: {skill}</tool_use_error>",
                    }
                ],
            },
        }
    )


def _payload(session, ts, parent_uuid, uuid, base_dir, body):
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "parentUuid": parent_uuid,
            "sessionId": session,
            "timestamp": ts,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": f"Base directory for this skill: {base_dir}\n\n{body}"}],
            },
        }
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", db)
    monkeypatch.setattr(database, "_initialized", False)
    init_db()
    user_home = tmp_path / "home"
    default_home = user_home / ".claude"
    (default_home / "projects" / "-tmp-proj").mkdir(parents=True)
    sub = upsert_subscription(
        email="a@vooban.com",
        account_uuid="u1",
        organization_name="Vooban",
        keychain_service=keychain_service_for_home(default_home),
        is_active=True,
    )
    return {"home": user_home, "default": default_home, "sub_id": sub["id"]}


def _write(env, session, lines):
    path = env["default"] / "projects" / "-tmp-proj" / f"{session}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "skill,expected",
    [("git:pr", ("git", "pr")), ("dataviz", (None, "dataviz")), ("a:b:c", ("a", "b:c"))],
)
def test_split_skill_id(skill, expected):
    assert split_skill_id(skill) == expected


@pytest.mark.parametrize(
    "base_dir,source,version,plugin",
    [
        (PLUGIN_DIR, "plugin", "0.14.0", "git"),
        ("/private/tmp/claude-502/bundled-skills/2.1.266/abc123/dataviz", "bundled", "2.1.266", None),
        ("/Users/a/.claude/skills/linear-progress", "personal", None, None),
        ("/Users/a/Library/App/rpm/plugin_01VU/skills/figma-design-to-code", "plugin", None, None),
        ("/somewhere/else", "unknown", None, None),
    ],
)
def test_parse_skill_base_dir(base_dir, source, version, plugin):
    info = parse_skill_base_dir(base_dir)
    assert (info["source"], info["version"], info["plugin"]) == (source, version, plugin)


def test_parse_skill_base_dir_keeps_the_marketplace():
    assert parse_skill_base_dir(PLUGIN_DIR)["marketplace"] == "monad-tools"


def test_version_key_orders_numerically_not_lexically():
    assert sorted(["0.7.0", "0.14.0", "0.5.1"], key=version_key) == ["0.5.1", "0.7.0", "0.14.0"]


@pytest.mark.parametrize(
    "name,kind,server,plugin",
    [
        ("Bash", "builtin", None, None),
        ("Skill", "skill", None, None),
        ("Agent", "agent", None, None),
        ("mcp__claude_ai_Linear__list_issues", "mcp", "claude_ai_Linear", None),
        ("mcp__plugin_code-quality_playwright__browser_navigate", "mcp", "playwright", "code-quality"),
    ],
)
def test_classify_tool(name, kind, server, plugin):
    assert classify_tool(name, {"code-quality", "git"}) == {"tool_kind": kind, "server": server, "plugin": plugin}


def test_classify_tool_falls_back_when_the_plugin_is_unknown():
    # Without a known plugin to match, the split is a guess at the last underscore.
    assert classify_tool("mcp__plugin_foo_bar__baz") == {"tool_kind": "mcp", "server": "bar", "plugin": "foo"}


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def test_scan_records_invocation_with_plugin_provenance_and_payload(env):
    sess = "11111111-1111-1111-1111-111111111111"
    _write(
        env,
        sess,
        [
            _turn(
                sess, "2026-09-09T10:00:00.000Z", "m1", "u1", tools=[("Skill", {"skill": "git:pr", "args": "draft"})]
            ),
            _launch_result(sess, "2026-09-09T10:00:01.000Z", "m1-0", "u2", "git:pr"),
            _payload(sess, "2026-09-09T10:00:02.000Z", "u2", "u3", PLUGIN_DIR, "x" * 396),
        ],
    )
    scan_transcripts(user_home=env["home"])

    skills = get_skill_usage()["skills"]
    assert len(skills) == 1
    s = skills[0]
    assert (s["skill"], s["plugin"], s["skill_name"]) == ("git:pr", "git", "pr")
    assert (s["source"], s["marketplace"], s["latest_version"]) == ("plugin", "monad-tools", "0.14.0")
    assert s["invocations"] == 1 and s["errors"] == 0
    # "Base directory for this skill: <dir>\n\n" + 396 chars, at 4 chars per token.
    assert s["payload_tokens_each"] == round((len(PLUGIN_DIR) + 32 + 396) / 4)


def test_failed_invocation_is_counted_but_attributed_nothing(env):
    sess = "22222222-2222-2222-2222-222222222222"
    _write(
        env,
        sess,
        [
            _turn(sess, "2026-09-09T10:00:00.000Z", "m1", "u1", tools=[("Skill", {"skill": "ghost:skill"})]),
            _error_result(sess, "2026-09-09T10:00:01.000Z", "m1-0", "u2", "ghost:skill"),
            _turn(sess, "2026-09-09T10:01:00.000Z", "m2", "u3", out=500),
        ],
    )
    scan_transcripts(user_home=env["home"])

    data = get_skill_usage()
    s = data["skills"][0]
    assert (s["invocations"], s["errors"], s["ok"]) == (1, 1, 0)
    assert s["attributed_turns"] == 0
    # The skill never loaded, so the turn after it belongs to nobody.
    assert data["totals"]["unattributed"]["turns"] == 2
    assert s["payload_tokens"] == 0


def test_turns_are_attributed_to_the_most_recently_loaded_skill(env):
    sess = "33333333-3333-3333-3333-333333333333"
    _write(
        env,
        sess,
        [
            # One turn before any skill, then two skills back to back.
            _turn(sess, "2026-09-09T10:00:00.000Z", "m0", "a0", out=10),
            _turn(sess, "2026-09-09T10:01:00.000Z", "m1", "a1", tools=[("Skill", {"skill": "git:pr"})]),
            _launch_result(sess, "2026-09-09T10:01:01.000Z", "m1-0", "a2", "git:pr"),
            _payload(sess, "2026-09-09T10:01:02.000Z", "a2", "a3", PLUGIN_DIR, "pr instructions"),
            _turn(sess, "2026-09-09T10:02:00.000Z", "m2", "a4", out=100),
            _turn(sess, "2026-09-09T10:03:00.000Z", "m3", "a5", tools=[("Skill", {"skill": "docs:notion"})]),
            _launch_result(sess, "2026-09-09T10:03:01.000Z", "m3-0", "a6", "docs:notion"),
            _turn(sess, "2026-09-09T10:04:00.000Z", "m4", "a7", out=200),
        ],
    )
    scan_transcripts(user_home=env["home"])

    data = get_skill_usage()
    by_skill = {s["skill"]: s for s in data["skills"]}
    # m1 (the invoking turn) and m2 belong to git:pr; m3 and m4 to docs:notion.
    assert by_skill["git:pr"]["attributed_turns"] == 2
    assert by_skill["docs:notion"]["attributed_turns"] == 2
    assert data["totals"]["unattributed"]["turns"] == 1
    # The split is a partition: no turn is counted twice.
    assert data["totals"]["attributed"]["turns"] + data["totals"]["unattributed"]["turns"] == 5


def test_a_subagent_skill_does_not_capture_the_main_thread(env):
    """Subagents share the parent's session id but write their own transcript."""
    sess = "44444444-4444-4444-4444-444444444444"
    _write(
        env,
        sess,
        [
            _turn(sess, "2026-09-09T10:00:00.000Z", "m1", "b1", out=10),
            _turn(sess, "2026-09-09T10:05:00.000Z", "m2", "b2", out=20),
        ],
    )
    nested = env["default"] / "projects" / "-tmp-proj" / sess / "subagents"
    nested.mkdir(parents=True)
    (nested / "agent-x.jsonl").write_text(
        "\n".join(
            [
                _turn(
                    sess, "2026-09-09T10:01:00.000Z", "s1", "c1", tools=[("Skill", {"skill": "git:pr"})], sidechain=True
                ),
                _launch_result(sess, "2026-09-09T10:01:01.000Z", "s1-0", "c2", "git:pr"),
                _turn(sess, "2026-09-09T10:02:00.000Z", "s2", "c3", sidechain=True, out=30),
            ]
        )
        + "\n"
    )
    scan_transcripts(user_home=env["home"])

    data = get_skill_usage()
    # Only the subagent's own two turns, not the main thread's 10:05 turn.
    assert data["skills"][0]["attributed_turns"] == 2
    assert data["totals"]["unattributed"]["turns"] == 2


def test_plugins_roll_up_skills_and_their_mcp_tools(env):
    sess = "55555555-5555-5555-5555-555555555555"
    ts_dir = "/Users/a/.claude/plugins/cache/monad-tools/code-quality/0.20.0/skills/typescript"
    _write(
        env,
        sess,
        [
            _turn(
                sess, "2026-09-09T10:00:00.000Z", "m1", "d1", tools=[("Skill", {"skill": "code-quality:typescript"})]
            ),
            _launch_result(sess, "2026-09-09T10:00:01.000Z", "m1-0", "d2", "code-quality:typescript"),
            _payload(sess, "2026-09-09T10:00:02.000Z", "d2", "d3", ts_dir, "ts rules"),
            _turn(
                sess,
                "2026-09-09T10:01:00.000Z",
                "m2",
                "d4",
                tools=[
                    ("mcp__plugin_code-quality_playwright__browser_navigate", {}),
                    ("mcp__plugin_code-quality_playwright__browser_take_screenshot", {}),
                    ("Bash", {}),
                ],
            ),
        ],
    )
    scan_transcripts(user_home=env["home"])

    plugin = get_skill_usage()["plugins"][0]
    assert plugin["plugin"] == "code-quality"
    assert plugin["invocations"] == 1 and plugin["skill_count"] == 1
    assert plugin["mcp_calls"] == 2
    assert plugin["mcp_servers"] == [{"server": "playwright", "calls": 2, "tools": 2}]

    tools = get_tool_usage()
    assert tools["total_calls"] == 4  # 1 Skill + 2 MCP + 1 Bash
    assert {k["tool_kind"]: k["calls"] for k in tools["kinds"]} == {"skill": 1, "mcp": 2, "builtin": 1}


def test_sessions_list_the_skills_they_invoked(env):
    sess = "66666666-6666-6666-6666-666666666666"
    _write(
        env,
        sess,
        [
            _turn(sess, "2026-09-09T10:00:00.000Z", "m1", "e1", tools=[("Skill", {"skill": "git:pr"})]),
            _launch_result(sess, "2026-09-09T10:00:01.000Z", "m1-0", "e2", "git:pr"),
            _turn(sess, "2026-09-09T10:01:00.000Z", "m2", "e3", tools=[("Skill", {"skill": "git:pr"})]),
            _launch_result(sess, "2026-09-09T10:01:01.000Z", "m2-0", "e4", "git:pr"),
        ],
    )
    scan_transcripts(user_home=env["home"])

    session = get_usage_sessions()[0]
    assert session["skills"] == [
        {
            "session_id": sess,
            "skill": "git:pr",
            "plugin": "git",
            "skill_name": "pr",
            "invocations": 2,
            "errors": 0,
            "payload_tokens": 0,
        }
    ]


def test_rescanning_an_appended_transcript_does_not_double_count(env):
    """The launch result and instructions can arrive in a later scan pass."""
    sess = "77777777-7777-7777-7777-777777777777"
    path = _write(
        env,
        sess,
        [
            _turn(sess, "2026-09-09T10:00:00.000Z", "m1", "f1", tools=[("Skill", {"skill": "git:pr"}), ("Bash", {})]),
        ],
    )
    scan_transcripts(user_home=env["home"])
    assert get_skill_usage()["skills"][0]["invocations"] == 1

    with path.open("a") as fh:
        fh.write(_launch_result(sess, "2026-09-09T10:00:01.000Z", "m1-0", "f2", "git:pr") + "\n")
        fh.write(_payload(sess, "2026-09-09T10:00:02.000Z", "f2", "f3", PLUGIN_DIR, "later") + "\n")
    scan_transcripts(user_home=env["home"])

    s = get_skill_usage()["skills"][0]
    assert s["invocations"] == 1
    assert s["source"] == "plugin" and s["latest_version"] == "0.14.0"
    assert s["payload_tokens"] > 0
    assert get_tool_usage()["total_calls"] == 2


def test_filters_narrow_skills_to_the_selected_day_and_account(env):
    sess = "88888888-8888-8888-8888-888888888888"
    _write(
        env,
        sess,
        [
            _turn(sess, "2026-09-08T10:00:00.000Z", "m1", "g1", tools=[("Skill", {"skill": "git:pr"})]),
            _launch_result(sess, "2026-09-08T10:00:01.000Z", "m1-0", "g2", "git:pr"),
            _turn(sess, "2026-09-09T10:00:00.000Z", "m2", "g3", tools=[("Skill", {"skill": "docs:notion"})]),
            _launch_result(sess, "2026-09-09T10:00:01.000Z", "m2-0", "g4", "docs:notion"),
        ],
    )
    scan_transcripts(user_home=env["home"])

    from datetime import datetime

    day = datetime.fromisoformat("2026-09-09T10:00:00+00:00").astimezone().strftime("%Y-%m-%d")
    only_that_day = get_skill_usage(date_str=day)
    assert [s["skill"] for s in only_that_day["skills"]] == ["docs:notion"]
    assert get_skill_usage(subscription_ids=[env["sub_id"] + 99])["skills"] == []
    assert len(get_skill_usage()["skills"]) == 2
