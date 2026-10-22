"""`embers install-skill`: copies the bundled skill, refuses to overwrite without --force."""
import os
import re

import pytest

from embers import skillinstall
from embers.cli import main

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL = os.path.join(REPO, "skills", "embers", "SKILL.md")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def test_skill_file_format_and_size():
    text = open(SKILL, encoding="utf-8").read()
    m = re.match(r"---\nname: embers\ndescription: (.+)\n---\n", text)
    assert m, "frontmatter must be name: embers + one-paragraph description"
    for phrase in ("what was said or decided", "day, week or month", "re-ingest after a migration", "first time"):
        assert phrase in m.group(1)
    assert len(text.splitlines()) <= 250


def test_skill_mentions_only_real_commands():
    """Every `embers <subcommand>` the skill tells an agent to run must exist in the CLI."""
    from embers.cli import build_parser
    sub = next(a for a in build_parser()._actions if a.dest == "cmd").choices
    text = open(SKILL, encoding="utf-8").read()
    used = set(re.findall(r"`?embers ([a-z][a-z-]+)", text))
    unknown = {u for u in used if u not in sub}
    assert not unknown, unknown


def test_install_to_default_location(home, capsys):
    assert main(["install-skill"]) == 0
    target = home / ".claude" / "skills" / "embers" / "SKILL.md"
    assert target.read_text(encoding="utf-8") == open(SKILL, encoding="utf-8").read()
    assert str(target) in capsys.readouterr().out


def test_refuses_to_overwrite_without_force(home, capsys):
    assert main(["install-skill"]) == 0
    target = home / ".claude" / "skills" / "embers" / "SKILL.md"
    target.write_text("local edit")
    assert main(["install-skill"]) == 1
    assert "--force" in capsys.readouterr().err
    assert target.read_text() == "local edit"
    assert main(["install-skill", "--force"]) == 0
    assert target.read_text(encoding="utf-8") == open(SKILL, encoding="utf-8").read()


def test_dest_override(tmp_path, home):
    dest = tmp_path / "custom"
    assert main(["install-skill", "--dest", str(dest)]) == 0
    assert (dest / "SKILL.md").is_file()


def test_missing_bundle_is_reported(home, monkeypatch, capsys):
    monkeypatch.setattr(skillinstall, "bundled_skill_path", lambda: None)
    assert main(["install-skill"]) == 1
    assert "not found" in capsys.readouterr().err
