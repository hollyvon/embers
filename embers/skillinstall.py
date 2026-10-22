"""`embers install-skill`: copy the bundled Claude Code skill to ~/.claude/skills/embers/."""
from __future__ import annotations

import os
import shutil
from typing import Optional

SKILL_FILE = "SKILL.md"


def default_dest() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "skills", "embers")


def bundled_skill_path() -> Optional[str]:
    """The SKILL.md shipped with the package (wheel: embers/skill/), else the checkout's skills/embers/."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "skill", SKILL_FILE),
                 os.path.join(os.path.dirname(here), "skills", "embers", SKILL_FILE)):
        if os.path.isfile(cand):
            return cand
    return None


def install(dest: Optional[str] = None, force: bool = False) -> str:
    """Copy the skill into `dest`. Raises FileExistsError without `force` if SKILL.md is already there,
    FileNotFoundError if the skill is not bundled. Returns the installed file's path."""
    src = bundled_skill_path()
    if src is None:
        raise FileNotFoundError("the bundled skill (skills/embers/SKILL.md) was not found")
    dest = dest or default_dest()
    target = os.path.join(dest, SKILL_FILE)
    if os.path.exists(target) and not force:
        raise FileExistsError(target)
    os.makedirs(dest, exist_ok=True)
    shutil.copyfile(src, target)
    return target
