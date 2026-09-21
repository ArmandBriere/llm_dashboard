"""Where Claude Code keeps its state on this machine.

Claude Code has one *home* per profile: ``~/.claude`` by default, or whatever
``CLAUDE_CONFIG_DIR`` points at (by convention ``~/.claude_<name>``). Each home
holds the session transcripts under ``projects/`` and has a companion
``.claude.json`` with the signed-in account and a cached usage reading. Its
credentials live in the macOS keychain under a service name derived from the
home's path. Both the quota provider and the transcript scanner need that
mapping, so it lives here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

DEFAULT_KEYCHAIN_SERVICE = "Claude Code-credentials"


def keychain_service_for_home(home_dir: Path) -> str:
    """Return the keychain service name Claude Code uses for a config directory.

    ``~/.claude`` uses the bare service name; any other directory gets a suffix
    of the first eight hex digits of the SHA-256 of its absolute path, which is
    the rule Claude Code itself applies.
    """
    if home_dir.name == ".claude":
        return DEFAULT_KEYCHAIN_SERVICE
    digest = hashlib.sha256(str(home_dir).encode("utf-8")).hexdigest()[:8]
    return f"{DEFAULT_KEYCHAIN_SERVICE}-{digest}"


def config_file_for_home(home_dir: Path) -> Path:
    """Return the ``.claude.json`` that belongs to a Claude home.

    The default home keeps it next to itself (``~/.claude.json``); every other
    home keeps it inside the directory.
    """
    if home_dir.name == ".claude":
        return home_dir.parent / ".claude.json"
    return home_dir / ".claude.json"


def discover_claude_homes(user_home: Path | None = None, *, with_transcripts: bool = False) -> list[Path]:
    """Find every ``~/.claude*`` directory on this machine, sorted by path.

    Backup copies (``*.backup``) are skipped. With ``with_transcripts`` only
    homes that have a ``projects/`` directory, and therefore transcripts, are
    returned.
    """
    base = user_home or Path.home()
    homes: list[Path] = []
    for d in sorted(base.glob(".claude*")):
        if not d.is_dir() or d.name.endswith(".backup"):
            continue
        if with_transcripts and not (d / "projects").is_dir():
            continue
        homes.append(d)
    return homes
