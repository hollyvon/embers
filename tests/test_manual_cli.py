"""mem manual, mem flags --export, mem windows, mem describe forms, mem build --workers."""
import io
import json
import os
import sqlite3

import pytest

from embers.cli import _created_date, main
from conftest import SMALL_THREAD


def run(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture()
def dbpath(corpus, tmp_path):
    p = str(tmp_path / "mem.db")
    assert main(["--db", p, "ingest", "--index", corpus.index] + corpus.roots) == 0
    return p


def make_manual(dbpath, capsys, n=2):
    """Build the small thread with an always-flinching stub so its leaves end up `manual`."""
    from embers.build import BuildOptions, run_build
    from embers.ingest import open_db
    from embers.summarize import StubSummarizer
    conn = open_db(dbpath)
    tid = conn.execute("SELECT thread_id FROM threads WHERE thread_id LIKE ?", (SMALL_THREAD + "%",)).fetchone()[0]
    run_build(conn, StubSummarizer(flinch=True), BuildOptions(thread_ids=[tid]))
    ids = [r[0] for r in conn.execute("SELECT id FROM nodes WHERE status='manual' ORDER BY id")]
    conn.close()
    assert len(ids) >= n
    return ids


# ---- mem manual ----------------------------------------------------------------------------------------
def test_manual_text_sets_summary_backend_model_status_and_index(capsys, dbpath):
    nid = make_manual(dbpath, capsys)[0]
    code, out, _ = run(capsys, "--db", dbpath, "manual", nid, "--text", "  Quokkaville was resolved by hand.  ")
    assert code == 0 and nid in out
    c = sqlite3.connect(dbpath)
    c.row_factory = sqlite3.Row
    n = c.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
    assert (n["status"], n["backend"], n["model"]) == ("ok", "manual", "human")
    assert n["summary"] == "Quokkaville was resolved by hand." and n["summary_bytes"] == len(n["summary"])
    meta = json.loads(n["metrics_json"])
    assert meta["history"][-1]["tier"] == "manual" and len(meta["history"]) >= 3 and "report" in meta       # history and metrics kept
    assert c.execute("SELECT node_id FROM nodes_fts WHERE nodes_fts MATCH 'Quokkaville'").fetchall()[0][0] == nid
    code, out, _ = run(capsys, "--db", dbpath, "grep", "Quokkaville")
    assert nid in out
    code, out, _ = run(capsys, "--db", dbpath, "flags", "--status", "manual")
    assert nid not in out


def test_manual_file_stdin_and_errors(capsys, dbpath, tmp_path, monkeypatch):
    a, b = make_manual(dbpath, capsys)[:2]
    f = tmp_path / "s.txt"
    f.write_text("From a file.\n", encoding="utf-8")
    assert run(capsys, "--db", dbpath, "manual", a, "--file", str(f))[0] == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("From stdin.\n"))
    assert run(capsys, "--db", dbpath, "manual", b, "--stdin")[0] == 0
    c = sqlite3.connect(dbpath)
    assert c.execute("SELECT summary FROM nodes WHERE id=?", (a,)).fetchone()[0] == "From a file."
    assert c.execute("SELECT summary FROM nodes WHERE id=?", (b,)).fetchone()[0] == "From stdin."
    for bad in (["manual", a], ["manual", a, "--text", "x", "--file", str(f)], ["manual", a, "--text", "  "],
                ["manual", "no-such-node", "--text", "x"], ["manual"]):
        with pytest.raises(SystemExit):
            main(["--db", dbpath] + bad)


def test_manual_works_on_any_node_and_ancestors_rebuild(capsys, dbpath):
    nid = make_manual(dbpath, capsys)[0]
    c = sqlite3.connect(dbpath)
    pid = c.execute("SELECT parent_id FROM nodes WHERE id=?", (nid,)).fetchone()[0]
    assert c.execute("SELECT status FROM nodes WHERE id=?", (pid,)).fetchone()[0] == "manual"      # built over the placeholder
    assert run(capsys, "--db", dbpath, "manual", nid, "--text", "Zorblax and Quibble, written by hand.")[0] == 0
    code, out, _ = run(capsys, "--db", dbpath, "--json", "build", "--backend", "stub")
    assert code == 0 and json.loads(out.splitlines()[0])["built"] >= 1          # the parent chain over the placeholder was rebuilt
    c = sqlite3.connect(dbpath)
    assert c.execute("SELECT status, backend FROM nodes WHERE id=?", (pid,)).fetchone() == ("ok", "stub")


# ---- mem flags --export / mem manual --import -------------------------------------------------------------
def test_export_then_import_roundtrip(capsys, dbpath, tmp_path):
    ids = make_manual(dbpath, capsys)
    d = tmp_path / "ex"
    code, out, _ = run(capsys, "--db", dbpath, "flags", "--status", "manual", "--export", str(d))
    assert code == 0 and "exported %d" % len(ids) in out
    files = sorted(os.listdir(d))
    assert files == sorted(i + ".md" for i in ids)
    text = (d / files[0]).read_text(encoding="utf-8")
    nid = files[0][:-3]
    for must in (nid, "span:", "## Metrics", '"report"', "--- transcript ---", "[human]"):
        assert must in text, must
    for i in ids[:2]:
        (d / (i + ".summary.md")).write_text("Hand-written summary of %s.\n" % i, encoding="utf-8")
    (d / ("zz-ignored.txt")).write_text("not a summary", encoding="utf-8")
    code, out, _ = run(capsys, "--db", dbpath, "manual", "--import", str(d))
    assert code == 0
    c = sqlite3.connect(dbpath)
    for i in ids[:2]:
        assert c.execute("SELECT status, backend, summary FROM nodes WHERE id=?", (i,)).fetchone() == ("ok", "manual", "Hand-written summary of %s." % i)
    still = [i for i in ids[2:] if c.execute("SELECT status FROM nodes WHERE id=?", (i,)).fetchone()[0] == "manual"]
    assert still == ids[2:]
    with pytest.raises(SystemExit):
        main(["--db", dbpath, "manual", "--import", str(d / "nope")])


# ---- mem windows -------------------------------------------------------------------------------------------
def test_created_date_accepts_epoch_ms_seconds_and_iso():
    assert _created_date(1773614400000) == "2026-03-15 22:40:00"
    assert _created_date("1773614400000") == "2026-03-15 22:40:00"
    assert _created_date(1773614400) == "2026-03-15 22:40:00"
    assert _created_date("2026-03-15T22:40:00.123Z") == "2026-03-15 22:40:00"
    assert _created_date("2026-03-15") == "2026-03-15"
    assert _created_date(None) is None and _created_date("garbage") is None


def test_windows_dates_untitled_and_sorting(capsys, dbpath):
    c = sqlite3.connect(dbpath)
    rows = c.execute("SELECT session_id FROM windows ORDER BY session_id LIMIT 3").fetchall()
    c.execute("UPDATE windows SET created_at='1773614400000', title=NULL, window=NULL WHERE session_id=?", (rows[0][0],))
    c.execute("UPDATE windows SET created_at='1000000000000', title='' WHERE session_id=?", (rows[1][0],))
    c.execute("UPDATE windows SET created_at='2030-01-01T00:00:00Z' WHERE session_id=?", (rows[2][0],))
    c.commit()
    c.close()
    capsys.readouterr()
    code, out, _ = run(capsys, "--db", dbpath, "windows")
    lines = out.splitlines()
    assert code == 0 and "(untitled)" in out and not any(l.rstrip().endswith("  -") for l in lines)
    assert "2026-03-15 22:40:00" in out and "1773614400000" not in out
    dates = [l.split()[1] + " " + l.split()[2] for l in lines]
    assert dates == sorted(dates)                                             # sorted by date
    assert lines[0].split()[1] == "2001-09-09"                                # the epoch-ms outlier sorts first
    code, out, _ = run(capsys, "--db", dbpath, "--json", "windows")
    assert all("date" in json.loads(l) for l in out.splitlines())


# ---- mem describe forms ------------------------------------------------------------------------------------
def test_describe_accepts_window_in_all_spellings(capsys, dbpath):
    assert run(capsys, "--db", dbpath, "build", "--backend", "stub", "--workers", "4")[0] == 0
    outs = []
    for args in (["window", "10"], ["window 10"], ["w10"], ["W10"], ["10"], ["window10"], ["Window", "#10"]):
        code, out, _ = run(capsys, "--db", dbpath, "describe", *args)
        assert code == 0, args
        outs.append(out)
    assert len(set(outs)) == 1 and "w10" in outs[0]
    with pytest.raises(SystemExit):
        main(["--db", dbpath, "describe", "w99999"])
    code, out, _ = run(capsys, "--db", dbpath, "session", "w10")                # same spelling for `mem session`
    assert code == 0


# ---- mem build --workers ----------------------------------------------------------------------------------
def test_build_workers_flag_and_env(capsys, dbpath, monkeypatch):
    code, out, _ = run(capsys, "--db", dbpath, "build", "--all", "--workers", "8")
    assert code == 0 and "8 workers" in out and "0 manual" in out
    monkeypatch.setenv("MEM_WORKERS", "3")
    code, out, _ = run(capsys, "--db", dbpath, "--json", "build", "--all")
    assert json.loads(out.splitlines()[0])["workers"] == 3
    with pytest.raises(SystemExit):
        main(["--db", dbpath, "build", "--workers", "0"])
