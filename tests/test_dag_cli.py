"""mem build / describe / expand / grep / flags end to end on the fixture corpus (stub backend)."""
import json
import os
import re
import subprocess

import pytest

from embers.cli import main
from conftest import SMALL_THREAD

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def jl(text):
    return [json.loads(l) for l in text.splitlines() if l.strip()]


@pytest.fixture(scope="module")
def dbpath(corpus, tmp_path_factory):
    p = str(tmp_path_factory.mktemp("daglcli") / "mem.db")
    assert main(["--db", p, "ingest", "--index", corpus.index] + corpus.roots) == 0
    return p


def test_commands_need_a_build_first(capsys, dbpath):
    for cmd in (["describe", "node", "x"], ["expand", "x"], ["grep", "foo"], ["flags"]):
        code, out, err = run(capsys, "--db", dbpath, *cmd)
        assert code == 1 and "embers build" in err


def test_build_dry_run_then_real_build(capsys, dbpath):
    code, out, _ = run(capsys, "--db", dbpath, "build", "--dry-run")
    assert code == 0 and "nothing written" in out and re.search(r"leaf\s+\d+\s+\d+", out)
    code, out, _ = run(capsys, "--db", dbpath, "--json", "build", "--dry-run", "--level", "leaf")
    d = jl(out)[0]
    assert d["dry_run"] is True and d["plan"]["leaf"]["pending"] > 0 and d["plan"]["leaf"]["est_input_tokens"] > 0
    code, out, _ = run(capsys, "--db", dbpath, "build", "--all", "--backend", "stub")
    assert code == 0 and "0 manual" in out
    code, out, _ = run(capsys, "--db", dbpath, "--json", "build", "--level", "all")
    assert jl(out)[0]["built"] == 0                                           # idempotent


def test_build_budget_flag_required_for_anthropic_backend(capsys, dbpath, monkeypatch):
    monkeypatch.delenv("MEM_BUDGET_USD", raising=False)
    with pytest.raises(SystemExit) as e:
        main(["--db", dbpath, "build", "--backend", "anthropic", "--dry-run"])
    assert "budget" in str(e.value)


def test_describe_window_session_thread_and_node(capsys, dbpath):
    code, out, _ = run(capsys, "--db", dbpath, "describe", "window", "10")
    assert code == 0 and "session" in out and "w10" in out and "children:" in out
    code, out, _ = run(capsys, "--db", dbpath, "describe", "10")                   # bare number = window
    assert code == 0 and "w10" in out
    code, out, _ = run(capsys, "--db", dbpath, "--json", "describe", "window", "15")
    rows = jl(out)
    node = rows[0]["node"]
    assert node["level"] == "session" and node["summary"] and rows[0]["window"] == 15 and all("child" in r for r in rows[1:])
    leaf_id = rows[1]["child"]["id"]
    code, out, _ = run(capsys, "--db", dbpath, "describe", leaf_id[:8])            # node id prefix
    assert code == 0 and leaf_id in out and "prompts in this leaf" in out
    code, out, _ = run(capsys, "--db", dbpath, "describe", "thread", SMALL_THREAD)
    assert code == 0 and re.search(r"\bthread\b", out.splitlines()[0]) and "children:" in out
    code, out, _ = run(capsys, "--db", dbpath, "describe", SMALL_THREAD)           # auto: session or thread prefix
    assert code == 0
    with pytest.raises(SystemExit):
        main(["--db", dbpath, "describe", "nonexistent-thing"])


def test_expand_descends_one_level_and_bottoms_out_in_raw_messages(capsys, dbpath):
    code, out, _ = run(capsys, "--db", dbpath, "--json", "describe", "thread", SMALL_THREAD)
    rows = jl(out)
    top = rows[0]["node"]
    assert top["level"] == "thread" and top["n_sources"] == 2
    code, out, _ = run(capsys, "--db", dbpath, "expand", top["id"])
    assert code == 0 and "2 children" in out
    session = [r["child"] for r in rows[1:]][0]
    code, out, _ = run(capsys, "--db", dbpath, "--json", "expand", session["id"])
    leaves = jl(out)
    assert leaves and all(l["level"] == "leaf" for l in leaves)
    code, out, _ = run(capsys, "--db", dbpath, "expand", leaves[0]["id"])
    assert code == 0 and re.search(r"^ +#\d+ +\d\d:\d\d:\d\d L\d+ +(human|assistant|thinking|tool_use)", out, re.M)
    code, out, _ = run(capsys, "--db", dbpath, "--json", "expand", leaves[0]["id"])
    objs = jl(out)
    assert objs[0]["node"]["id"] == leaves[0]["id"] and len(objs) > 1 and {"class", "text", "pointer"} <= set(objs[1])
    n_msgs = len({o["uuid"] for o in objs[1:]})
    assert n_msgs == leaves[0]["n_sources"]


def test_grep_finds_summary_text_with_node_level_span_and_snippet(capsys, dbpath):
    code, out, _ = run(capsys, "--db", dbpath, "--json", "describe", "window", "10")
    summary = jl(out)[0]["node"]["summary"]
    word = next(w for w in re.findall(r"[a-z]{7,}", summary) if w not in ("because",))
    code, out, _ = run(capsys, "--db", dbpath, "grep", word, "--limit", "5")
    assert code == 0 and re.search(r"^[LSTM][-\w]+ +(leaf|session|thread|month) +\d{4}-\d\d-\d\d", out, re.M) and "[" in out
    code, out, _ = run(capsys, "--db", dbpath, "--json", "grep", word, "--level", "session")
    hits = jl(out)
    assert hits and all(h["level"] == "session" and h["snippet"] and h["span"] for h in hits)
    code, out, _ = run(capsys, "--db", dbpath, "grep", "zzzzqqqq")
    assert code == 1 and "no hits" in out
    with pytest.raises(SystemExit):
        main(["--db", dbpath, "grep", word, "--level", "decade"])


def test_flags_empty_after_a_clean_stub_build(capsys, dbpath):
    code, out, _ = run(capsys, "--db", dbpath, "flags")
    assert code == 0 and "no flagged or manual nodes" in out
    code, out, _ = run(capsys, "--db", dbpath, "--json", "flags")
    assert jl(out) == []


def test_flags_lists_manual_nodes_with_metric_breakdown(capsys, corpus, tmp_path, monkeypatch):
    p = str(tmp_path / "flinch.db")
    assert main(["--db", p, "ingest", "--index", corpus.index] + corpus.roots) == 0
    capsys.readouterr()
    # make the stub flinch through the CLI path
    import embers.summarize as sm
    monkeypatch.setattr(sm, "make_summarizer", lambda *a, **k: sm.StubSummarizer(flinch=True))
    monkeypatch.setattr("embers.cli.make_summarizer", lambda *a, **k: sm.StubSummarizer(flinch=True))
    code, out, _ = run(capsys, "--db", p, "build", "--level", "leaf", "--thread", SMALL_THREAD)
    assert code == 0 and "need manual attention" in out
    code, out, _ = run(capsys, "--db", p, "flags", "--status", "manual")
    assert "ratio" in out and "evasion markers" in out and "coverage" in out and "attempts=2" in out
    code, out, _ = run(capsys, "--db", p, "--json", "flags")
    rows = jl(out)
    assert rows and all(r["status"] == "manual" and "evasion" in r["failed"] for r in rows)
    assert any("ratio" in r["failed"] and "coverage" in r["failed"] for r in rows)
    assert run(capsys, "--db", p, "flags", "--status", "flagged")[1].strip() == "no flagged or manual nodes"


def test_subprocess_entry_point_and_phase1_commands_still_work(dbpath):
    r = subprocess.run([os.path.join(REPO, "mem"), "--db", dbpath, "stats"], capture_output=True, text=True)
    assert r.returncode == 0 and "messages:" in r.stdout
    r = subprocess.run([os.path.join(REPO, "mem"), "--db", dbpath, "describe", "window", "10"], capture_output=True, text=True)
    assert r.returncode == 0 and "children:" in r.stdout
