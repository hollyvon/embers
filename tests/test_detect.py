"""`embers detect` and bare `embers ingest`: roots found by name only, nothing ingested by detect."""
import json
import os

import pytest

from embers import detect as det
from embers.cli import main


def _fake_tree(base):
    """A tiny synthetic ~/.claude/projects: two project dirs, three sessions, one subagent file."""
    proj = base / ".claude" / "projects"
    (proj / "-synthetic-a").mkdir(parents=True)
    (proj / "-synthetic-b" / "sess3" / "subagents").mkdir(parents=True)
    for rel in ("-synthetic-a/s1.jsonl", "-synthetic-a/s2.jsonl", "-synthetic-b/s3.jsonl",
                "-synthetic-b/sess3/subagents/agent-abc.jsonl"):
        (proj / rel).write_text("")
    return proj


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MEM_DB", raising=False)
    return tmp_path


def test_known_roots_is_a_list_of_name_and_glob():
    assert det.KNOWN_ROOTS and all(len(r) == 2 for r in det.KNOWN_ROOTS)
    assert det.KNOWN_ROOTS[0] == ("Claude Code", "~/.claude/projects")


def test_detect_counts_sessions(home):
    proj = _fake_tree(home)
    found = det.detect()
    assert found == [{"name": "Claude Code", "path": str(proj), "projects": 2, "sessions": 3, "subagents": 1}]


def test_detect_accepts_extra_roots(home):
    other = home / "elsewhere" / "x"
    (other / "p").mkdir(parents=True)
    (other / "p" / "a.jsonl").write_text("")
    found = det.detect([("Other", str(home / "elsewhere" / "*"))])
    assert [(e["name"], e["sessions"]) for e in found] == [("Other", 1)]


def test_detect_nothing_found(home, capsys):
    assert det.detect() == []
    assert main(["detect"]) == 1
    assert "no transcript roots found" in capsys.readouterr().out


def test_detect_cli_reports_and_does_not_ingest(home, capsys):
    _fake_tree(home)
    db = home / "never.db"
    assert main(["--db", str(db), "detect"]) == 0
    out = capsys.readouterr().out
    assert "Claude Code" in out and "~3 session(s)" in out and "nothing was read or ingested" in out
    assert not db.exists()


def test_detect_cli_json(home, capsys):
    _fake_tree(home)
    assert main(["--json", "detect"]) == 0
    row = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert row["sessions"] == 3 and row["subagents"] == 1


def test_bare_ingest_uses_detected_root(home, corpus, capsys):
    (home / ".claude").mkdir()
    os.symlink(corpus.roots[0], str(home / ".claude" / "projects"))
    db = home / "bare.db"
    assert main(["--db", str(db), "ingest"]) == 0
    assert "ingested" in capsys.readouterr().out and db.exists()


def test_bare_ingest_without_roots_fails_cleanly(home, capsys):
    assert main(["--db", str(home / "x.db"), "ingest"]) == 2
    assert "embers detect" in capsys.readouterr().err
