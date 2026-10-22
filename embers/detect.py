"""`embers detect`: find known transcript roots and count sessions, without ingesting.

Only file names are looked at; no transcript is opened or read. To support another harness later,
add a `(name, path_glob)` pair to KNOWN_ROOTS. This module does no adapter work.
"""
from __future__ import annotations

import glob
import os
from typing import Dict, List, Sequence, Tuple

# (name, path_glob). Globs are expanded with ~ and may match several directories.
KNOWN_ROOTS: Sequence[Tuple[str, str]] = (
    ("Claude Code", "~/.claude/projects"),
)


def _count_sessions(root: str) -> Dict[str, int]:
    """Count by file name only, using the `<root>/<projdir>/<session>.jsonl` layout."""
    sessions = 0
    subagents = 0
    projects = 0
    try:
        projdirs = sorted((e for e in os.scandir(root) if e.is_dir()), key=lambda e: e.name)
    except OSError:
        return {"projects": 0, "sessions": 0, "subagents": 0}
    for pd in projdirs:
        projects += 1
        for f in glob.glob(os.path.join(glob.escape(pd.path), "*.jsonl")):
            if os.path.isfile(f):
                sessions += 1
        subagents += len(glob.glob(os.path.join(glob.escape(pd.path), "*", "subagents", "agent-*.jsonl")))
    return {"projects": projects, "sessions": sessions, "subagents": subagents}


def detect(known: Sequence[Tuple[str, str]] = None) -> List[Dict[str, object]]:
    """Return one entry per existing directory matched by a known root glob."""
    found: List[Dict[str, object]] = []
    for name, pattern in (KNOWN_ROOTS if known is None else known):
        for path in sorted(glob.glob(os.path.expanduser(pattern))):
            if os.path.isdir(path):
                entry: Dict[str, object] = {"name": name, "path": path}
                entry.update(_count_sessions(path))
                found.append(entry)
    return found


def detected_paths() -> List[str]:
    return [str(e["path"]) for e in detect()]
