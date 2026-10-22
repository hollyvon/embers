"""`mem` CLI end to end (in-process via main(), plus one subprocess run of ./mem)."""
import json
import os
import subprocess
import sys

import pytest

from embers.cli import main

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def jl(text):
    return [json.loads(l) for l in text.splitlines() if l.strip()]


@pytest.fixture(scope="module")
def dbpath(corpus, tmp_path_factory):
    p = str(tmp_path_factory.mktemp("clidb") / "mem.db")
    code = main(["--db", p, "ingest", "--index", corpus.index] + corpus.roots)
    assert code == 0
    return p


def planted(corpus, cls):
    return next(p for p in corpus.truth["planted"] if p["class"] == cls)


def test_ingest_output_and_incremental(capsys, dbpath, corpus):
    code, out, _ = run(capsys, "--db", dbpath, "ingest", *corpus.roots)
    assert code == 0 and "0 new" in out and "unchanged" in out
    code, out, _ = run(capsys, "--db", dbpath, "--json", "ingest", *corpus.roots)
    d = jl(out)[0]
    assert d["messages_new"] == 0 and d["files_seen"] == len(corpus.truth["files"])


def test_search_planted_each_class(capsys, dbpath, corpus):
    for p in corpus.truth["planted"]:
        code, out, _ = run(capsys, "--db", dbpath, "search", p["phrase"])
        assert code == 0
        assert "hit 1" in out and p["class"] in out and "snippet:" in out
        code, out, _ = run(capsys, "--db", dbpath, "--json", "search", p["phrase"])
        hits = jl(out)
        assert len(hits) == 1 and hits[0]["class"] == p["class"] and p["phrase"] in hits[0]["text"]
        assert "before" in hits[0] and "after" in hits[0] and len(hits[0]["after"]) <= 3
        assert hits[0]["pointer"].endswith(hits[0]["block_type"] and ("]" if "[" in hits[0]["pointer"] else hits[0]["pointer"][-1]))


def test_search_class_filter_and_context(capsys, dbpath, corpus):
    p = planted(corpus, "thinking")
    code, out, _ = run(capsys, "--db", dbpath, "--json", "search", p["phrase"], "--class", "human,assistant")
    assert code == 1 and jl(out) == []
    code, out, _ = run(capsys, "--db", dbpath, "--json", "search", p["phrase"], "--class", "thinking", "--context", "1")
    hits = jl(out)
    assert len(hits) == 1 and len(hits[0]["before"]) <= 1 and len(hits[0]["after"]) <= 1
    with pytest.raises(SystemExit):
        run(capsys, "--db", dbpath, "search", "x", "--class", "bogus")


def test_search_ranked_hits_limit_and_fallback(capsys, dbpath):
    code, out, _ = run(capsys, "--db", dbpath, "--json", "search", "quokka", "--limit", "5")
    hits = jl(out)
    assert len(hits) == 5 and [h["rank"] for h in hits] == [1, 2, 3, 4, 5]
    assert all("quokka" in h["text"].lower() for h in hits)
    assert hits[0]["score"] <= hits[-1]["score"]  # bm25: lower is better
    # FTS syntax works ...
    code, out, _ = run(capsys, "--db", dbpath, "--json", "search", "quokka AND lantern", "--limit", "3")
    assert all("quokka" in h["text"].lower() and "lantern" in h["text"].lower() for h in jl(out))
    # ... and a non-FTS query (hyphens read as column filters) falls back to a phrase instead of crashing
    code, out, _ = run(capsys, "--db", dbpath, "--json", "search", "passphrase-stale-quokka-999")
    assert code == 1 and jl(out) == []
    code, out, _ = run(capsys, "--db", dbpath, "search", "zzzznothing")
    assert code == 1 and "no hits" in out


def test_search_since_until_window_session(capsys, dbpath, corpus):
    code, out, _ = run(capsys, "--db", dbpath, "--json", "search", "lantern", "--since", "2026-03-15", "--limit", "50")
    hits = jl(out)
    assert hits and all(h["ts"] >= "2026-03-15" for h in hits)
    code, out, _ = run(capsys, "--db", dbpath, "--json", "search", "lantern", "--until", "2026-03-05", "--limit", "50")
    hits = jl(out)
    assert hits and all(h["ts"] < "2026-03-06" for h in hits)
    sid, w = next((s, w["window"]) for s, w in corpus.truth["windows"].items() if w["window"])
    code, out, _ = run(capsys, "--db", dbpath, "--json", "search", "the", "--window", str(w), "--limit", "50")
    hits = jl(out)
    assert hits and all(h["window"] == w for h in hits)
    code, out, _ = run(capsys, "--db", dbpath, "--json", "search", "the", "--session", sid[:8], "--limit", "10")
    assert all(h["session_id"] == sid for h in jl(out))
    with pytest.raises(SystemExit):
        run(capsys, "--db", dbpath, "search", "x", "--since", "yesterday")


def test_session_views(capsys, dbpath, corpus):
    a, b, c = corpus.truth["chain"]
    sb = corpus.sid(b)
    code, out, _ = run(capsys, "--db", dbpath, "session", sb[:8])
    assert code == 0 and sb in out and "thread members" in out and "L1 " in out
    code, out, _ = run(capsys, "--db", dbpath, "--json", "session", sb, "--limit", "5")
    objs = jl(out)
    head = objs[0]["session"]
    assert head["session_id"] == sb and len(head["thread_members"]) >= 3
    assert head["n_messages_in_file"] > head["n_messages"]  # copied prefix shows in the file view
    assert len(objs) == 6 and objs[1]["line_no"] == 1
    code, out, _ = run(capsys, "--db", dbpath, "--json", "session", sb, "--thread", "--class", "human", "--limit", "3")
    objs = jl(out)
    assert all(o["class"] == "human" for o in objs[1:]) and len(objs) == 4
    # by window number
    sid, w = next((s, w["window"]) for s, w in corpus.truth["windows"].items() if w["window"])
    code, out, _ = run(capsys, "--db", dbpath, "--json", "session", str(w), "--limit", "1")
    assert jl(out)[0]["session"]["session_id"] == sid and jl(out)[0]["session"]["window"] == w
    with pytest.raises(SystemExit):
        run(capsys, "--db", dbpath, "session", "no-such-session")


def test_session_around_hit(capsys, dbpath, corpus):
    p = planted(corpus, "assistant")
    code, out, _ = run(capsys, "--db", dbpath, "--json", "search", p["phrase"])
    hit = jl(out)[0]
    code, out, _ = run(capsys, "--db", dbpath, "session", hit["session_id"], "--around", str(hit["id"]), "--context", "2")
    lines = [l for l in out.splitlines() if l.startswith(">") or l.startswith("  #")]
    assert any(l.startswith("> #%d" % hit["id"]) for l in lines)
    assert 1 <= len(lines) <= 5
    code, out, _ = run(capsys, "--db", dbpath, "--json", "session", hit["session_id"], "--around", str(hit["id"]), "--full")
    objs = jl(out)
    assert any(o.get("id") == hit["id"] and p["phrase"] in o["text"] for o in objs[1:])
    with pytest.raises(SystemExit):
        run(capsys, "--db", dbpath, "session", hit["session_id"], "--around", "999999999")


def test_subagent_session(capsys, dbpath, corpus):
    rel, info = next(iter(corpus.truth["subagents"].items()))
    sid = "agent-" + info["agent_id"]
    code, out, _ = run(capsys, "--db", dbpath, "session", sid)
    assert "SUBAGENT of " + info["parent_session_id"] in out
    code, out, _ = run(capsys, "--db", dbpath, "session", info["parent_session_id"], "--limit", "1")
    assert "subagents: " in out and sid in out


def test_sessions_listing(capsys, dbpath, corpus):
    code, out, _ = run(capsys, "--db", dbpath, "--json", "sessions")
    rows = jl(out)
    tops = [i for i in corpus.truth["files"].values() if not i["is_subagent"]]
    assert len(rows) == len(tops)
    code, out, _ = run(capsys, "--db", dbpath, "--json", "sessions", "--subagents")
    assert len(jl(out)) == len(corpus.truth["files"])
    code, out, _ = run(capsys, "--db", dbpath, "--json", "sessions", "--since", "2026-03-15", "--limit", "3")
    rows = jl(out)
    assert len(rows) == 3 and all((r["last_ts"] or "") >= "2026-03-15" for r in rows)
    code, out, _ = run(capsys, "--db", dbpath, "sessions", "--limit", "2")
    assert len(out.splitlines()) == 2


def test_windows(capsys, dbpath, corpus, tmp_path):
    code, out, _ = run(capsys, "--db", dbpath, "--json", "windows")
    rows = jl(out)
    expected = corpus.truth["windows"]
    assert {r["session_id"] for r in rows} == set(expected)
    nums = [r["window"] for r in rows if r["window"] is not None]
    assert nums == sorted(nums) and len(nums) == sum(1 for w in expected.values() if w["window"])
    dates = [r["date"] or "9999" for r in rows]           # `mem windows` now sorts by date (unnumbered windows included)
    assert dates == sorted(dates)
    for r in rows:
        assert r["title"] == expected[r["session_id"]]["title"] and r["window"] == expected[r["session_id"]]["window"]
        assert r["created_at"] == expected[r["session_id"]]["createdAt"]
    code, out, _ = run(capsys, "--db", dbpath, "windows")
    assert out.splitlines()[0].startswith("w1 ")
    # windows --index on a DB that never saw the index
    p = str(tmp_path / "w.db")
    main(["--db", p, "ingest", corpus.roots[0]])
    code, out, _ = run(capsys, "--db", p, "windows")
    assert code == 1 and "no window map" in out
    code, out, _ = run(capsys, "--db", p, "--json", "windows", "--index", corpus.index)
    assert code == 0 and len(jl(out)) == len(expected)


def test_stats(capsys, dbpath, corpus):
    code, out, _ = run(capsys, "--db", dbpath, "--json", "stats")
    d = jl(out)[0]
    assert d["files"] == len(corpus.truth["files"])
    assert d["subagent_sessions"] == len(corpus.truth["subagents"])
    assert d["multi_file_threads"] == sum(1 for c in corpus.truth["components"] if len(c) > 1)
    assert d["messages_shared"] == corpus.truth["shared_uuid_count"]
    assert set(d["by_class"]) == {"human", "assistant", "thinking", "tool_use", "tool_result", "attachment", "system", "summary"}
    assert d["skipped_by_kind"]["image"]["bytes"] > 5 * 1024 * 1024
    assert d["files_with_partial_tail"] == len(corpus.truth["edge_cases"]["truncated_last_line"])
    assert set(d["record_types"]) >= {"assistant", "user", "cost-state", "file-history-delta"}
    code, out, _ = run(capsys, "--db", dbpath, "stats")
    assert "by class:" in out and "thinking" in out


def test_missing_db_message(capsys, tmp_path):
    code, out, err = run(capsys, "--db", str(tmp_path / "nope.db"), "stats")
    assert code == 2 and "embers ingest" in err


def test_script_entry_point_end_to_end(corpus, tmp_path):
    """The documented one-liner, via the ./mem script and a subprocess."""
    env = dict(os.environ, MEM_DB=str(tmp_path / "e2e.db"))
    mem = os.path.join(REPO, "mem")
    r = subprocess.run([sys.executable, mem, "ingest"] + corpus.roots + ["--index", corpus.index],
                       capture_output=True, text=True, env=env, cwd=REPO)
    assert r.returncode == 0, r.stderr
    p = planted(corpus, "human")
    r = subprocess.run([sys.executable, mem, "search", p["phrase"]], capture_output=True, text=True, env=env, cwd=REPO)
    assert r.returncode == 0 and p["phrase"] in r.stdout and "hit 1" in r.stdout
    r = subprocess.run([sys.executable, mem, "windows"], capture_output=True, text=True, env=env, cwd=REPO)
    assert r.returncode == 0 and r.stdout.startswith("w1 ")
    r = subprocess.run([sys.executable, mem, "--version"], capture_output=True, text=True, env=env, cwd=REPO)
    assert r.returncode == 0 and r.stdout.startswith("embers ")


def test_version_flag_on_both_names(capsys):
    """`embers --version` prints the package version; the `mem` alias shares the same entry point."""
    import embers
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == "embers " + embers.__version__
