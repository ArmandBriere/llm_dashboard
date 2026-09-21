# Usage insights: what the numbers mean

The **usage** views (Models, Activity, Projects, Sessions, Skills, Plugins,
Tools, 5-hour windows) are computed from the transcripts Claude Code writes
under `~/.claude*/projects/<project>/<session>.jsonl`. This page explains where
each figure comes from so a label is never a guess.

## Source

Every assistant turn in a transcript carries the model and the `usage` block
the API returned: input tokens, cache-creation tokens, cache-read tokens,
output tokens, thinking tokens. That is far richer than the quota endpoint,
which only reports percentages. The scanner reads new lines incrementally into
three tables:

| Table                     | One row per                                          |
| ------------------------- | ---------------------------------------------------- |
| `usage_turns`             | assistant turn                                       |
| `usage_tool_calls`        | tool the turn called (builtin, MCP, skill, subagent) |
| `usage_skill_invocations` | `Skill` tool call, with its plugin provenance        |

A cold scan of about 1,250 transcripts takes roughly 10 seconds; after that,
only new bytes are read.

## Account attribution

A transcript belongs to the Claude home it was written under. `~/.claude` maps
to the default keychain entry and any other `~/.claude_*` directory maps to the
entry whose suffix is `sha256(path)[:8]`, the same rule Claude Code uses. The
collector stores that keychain service on the subscription row, which is how a
turn is attributed to an account.

## Cost estimate

Subscriptions are flat-rate, so the dollar figures are **not what you pay**.
They apply Anthropic's public per-token prices (`backend/pricing.py`) to your
token counts: cache writes at 1.25× input, cache reads at 0.1× input, output
at its own rate, bigger models at higher rates. The result is a weight that
turns raw token counts into something closer to how the quota window is
consumed. Unknown models fall back to the Opus tier.

## 5-hour windows

Every reset window the poller observed, with each model's share of the
window's **peak** utilisation. The share is either share-of-estimated-cost or
share-of-raw-tokens, multiplied by the peak percentage the window reached.

## How skills are measured

A skill has no token usage of its own, so three different numbers are reported
and they mean different things:

- **Runs** counts `Skill` tool calls. A run that comes back `Unknown skill` is
  counted and flagged, but attributed no work.
- **Context** is the instruction text the skill injects each time it loads,
  estimated at four characters per token. The transcript records no token count
  for it, and it is the one cost a skill imposes directly. The bundled
  `claude-api` skill injects about 142k tokens; most plugin skills inject 1k–7k.
- **Turns / tokens / cost** are the work done _after_ the skill loaded. Each
  turn counts for the skill most recently loaded in its own transcript, so the
  rows partition the window instead of double-counting sessions that load
  several skills. The walk follows the transcript rather than the session
  because a subagent writes its own file under the parent's session id.

Plugin provenance (marketplace, plugin, version) is read from the directory the
skill was loaded from, which is the only place the transcript records it.

## Tools

The tool-call leaderboard splits calls into **builtin** (Read, Edit, Bash…),
**MCP** (`mcp__<server>__<tool>`, grouped by server), **skill** (`Skill`) and
**subagent** (`Agent`/`Task`). A plugin's row adds the calls made to the MCP
servers that plugin ships.

## Sessions

Title, project, branch, duration, turns, tools, tokens, models, the skills the
session invoked, and cost. A session's project is derived from its working
directory; git worktrees are folded back onto the repository they belong to.
