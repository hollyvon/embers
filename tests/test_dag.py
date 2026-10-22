"""Layer D: chunking, DAG coverage, idempotence, staleness after re-ingest, escalation, sensitivity."""
import json
import os
import shutil

import pytest

from embers import dag
from embers.build import BuildOptions, children_of, make_pack, run_build
from embers.dag import Msg, chunk_session, plan_chunks
from embers.ingest import Ingester, open_db
from embers.summarize import StubSummarizer
from conftest import SMALL_THREAD, fresh_conn


def msg(i, human=False, prose=100, aprose=None, subtype=None, thinking=False, ts=None):
    if aprose is None:
        aprose = 0 if human else prose
    return Msg(("u%d" % i, "s", ts or "2026-03-01T00:%02d:00Z" % i, i, int(human), subtype, prose, aprose, int(thinking)))


# ---- chunking (pure) ---------------------------------------------------------------------------------
def test_prompt_stays_with_its_answer_when_budget_is_tight():
    # human(300) + assistant(300): together 600 > budget 400, but must not be separated
    msgs = [msg(0, human=True, prose=300), msg(1, prose=300), msg(2, human=True, prose=300), msg(3, prose=300)]
    leaves = chunk_session(msgs, 400)
    assert [[m.uuid for m in lf] for lf in leaves] == [["u0", "u1"], ["u2", "u3"]]


def test_tool_noise_between_prompt_and_answer_does_not_split_them():
    msgs = [msg(0, human=True, prose=300), msg(1, prose=0, aprose=0), msg(2, prose=0, aprose=0), msg(3, prose=300)]
    assert len(chunk_session(msgs, 100)) == 1


def test_oversized_turn_splits_only_after_the_answer_atom():
    msgs = [msg(0, human=True, prose=50)] + [msg(i, prose=300) for i in range(1, 6)]
    leaves = chunk_session(msgs, 400)
    assert [m.uuid for m in leaves[0]][:2] == ["u0", "u1"]
    assert len(leaves) > 1 and sum(len(lf) for lf in leaves) == 6


def test_compact_boundary_always_starts_a_new_leaf():
    msgs = [msg(0, human=True, prose=10), msg(1, prose=10), msg(2, prose=0, aprose=0, subtype="compact_boundary"),
            msg(3, prose=10), msg(4, human=True, prose=10), msg(5, prose=10)]
    leaves = chunk_session(msgs, 10 ** 6)
    assert [[m.uuid for m in lf] for lf in leaves] == [["u0", "u1"], ["u2", "u3", "u4", "u5"]]


def test_every_message_lands_in_exactly_one_leaf_synthetic():
    msgs = [msg(i, human=(i % 3 == 0), prose=70 + i) for i in range(40)]
    leaves = chunk_session(msgs, 500)
    flat = [m.uuid for lf in leaves for m in lf]
    assert flat == [m.uuid for m in msgs]


# ---- chunking on the fixture corpus -----------------------------------------------------------------
@pytest.mark.parametrize("budget", [1500, dag.DEFAULT_CHUNK_BYTES])
def test_fixture_chunks_respect_pairing_and_compaction(db, budget):
    chunks = plan_chunks(db.conn, budget)
    where = {u: c for c in chunks for u in c.uuids}
    rows = db.conn.execute(
        """SELECT m.uuid, m.session_id, m.is_human_prompt, m.subtype,
                  COALESCE((SELECT SUM(nbytes) FROM blocks b WHERE b.uuid = m.uuid AND b.class = 'assistant'), 0)
           FROM messages m ORDER BY m.session_id, COALESCE(m.ts, ''), m.line_no""").fetchall()
    pending_human = None
    seen_boundary_first = 0
    for uuid, sid, human, subtype, aprose in rows:
        if human:
            pending_human = uuid
        elif pending_human is not None and aprose > 0:
            assert where[pending_human] is where[uuid], "prompt separated from its answer"
            pending_human = None
        if subtype == "compact_boundary":
            assert where[uuid].uuids[0] == uuid
            seen_boundary_first += 1
    assert seen_boundary_first == db.conn.execute("SELECT COUNT(*) FROM messages WHERE subtype='compact_boundary'").fetchone()[0] > 0


def test_leaf_budget_is_honoured_except_for_single_atoms(db):
    budget = 3000
    for c in plan_chunks(db.conn, budget):
        if c.prose_bytes > budget:
            assert len(c.uuids) >= 1      # oversized only when one prompt+answer atom (or one message) is itself too big


# ---- the built DAG -----------------------------------------------------------------------------------
def test_stub_build_accepts_everything_on_the_fixture(built):
    st = {r[0]: r[1] for r in built.conn.execute("SELECT status, COUNT(*) FROM nodes GROUP BY status")}
    assert st == {"ok": st.get("ok")} and st["ok"] > 100, st
    assert built.stats["manual"] == 0 and built.stats["stopped"] is None


def test_coverage_every_message_in_exactly_one_leaf(built):
    c = built.conn
    n_msgs = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    rows = c.execute("SELECT uuid FROM node_sources").fetchall()
    assert len(rows) == n_msgs == len(set(r[0] for r in rows))
    assert c.execute("""SELECT COUNT(*) FROM messages m WHERE NOT EXISTS
                        (SELECT 1 FROM node_sources s WHERE s.uuid = m.uuid)""").fetchone()[0] == 0
    # n_sources matches the join, and a leaf's messages all belong to its one session
    for lf in c.execute("SELECT id, n_sources, session_id FROM nodes WHERE level='leaf'").fetchall():
        sess = c.execute("""SELECT COUNT(*), COUNT(DISTINCT m.session_id), MIN(m.session_id) FROM node_sources s
                            JOIN messages m ON m.uuid = s.uuid WHERE s.node_id = ?""", (lf["id"],)).fetchone()
        assert sess[0] == lf["n_sources"] and sess[1] == 1 and sess[2] == lf["session_id"]


def test_coverage_leaf_session_thread_month_tree(built):
    c = built.conn
    nodes = {r["id"]: r for r in c.execute("SELECT * FROM nodes")}
    for n in nodes.values():
        if n["level"] == "month":
            assert n["parent_id"] is None
            continue
        parent = nodes[n["parent_id"]]
        want = {"leaf": ("session",), "session": ("thread", "month"), "thread": ("month",)}[n["level"]]
        assert parent["level"] in want
    # each leaf under exactly one session node of its own session
    for lf in (n for n in nodes.values() if n["level"] == "leaf"):
        assert nodes[lf["parent_id"]]["session_id"] == lf["session_id"]
    # every session file with messages has a session node; its n_sources counts its leaves
    for s in nodes.values():
        if s["level"] == "session":
            assert s["n_sources"] == len(children_of(c, s["id"])) > 0
    n_sessions = c.execute("SELECT COUNT(DISTINCT session_id) FROM messages").fetchone()[0]
    assert sum(1 for n in nodes.values() if n["level"] == "session") == n_sessions
    # thread level: one node per thread, multi-session threads get their own, single-session ones double
    threads = c.execute("SELECT thread_id, n_sessions FROM threads WHERE n_messages > 0").fetchall()   # an empty file has no nodes
    for t in threads:
        sess = [n for n in nodes.values() if n["level"] == "session" and n["thread_id"] == t["thread_id"]]
        tn = dag.thread_level_node(c, t["thread_id"])
        assert tn is not None
        if len(sess) == 1:
            assert tn["level"] == "session" and json.loads(tn["metrics_json"]).get("thread_double") is True
        else:
            assert tn["level"] == "thread" and all(s["parent_id"] == tn["id"] for s in sess)
    # months: thread-level nodes grouped by (first_ts month, thread kind); the kind is on the thread row and the node
    kinds = {r[0]: r[1] for r in c.execute("SELECT thread_id, kind FROM threads")}
    for n in nodes.values():
        if n["level"] in ("thread",) or (n["level"] == "session" and json.loads(n["metrics_json"]).get("thread_double")):
            kind = kinds[n["thread_id"]]
            assert kind in dag.THREAD_KINDS and json.loads(n["metrics_json"]).get("kind") == kind
            assert nodes[n["parent_id"]]["id"] == dag.month_node_id(dag.month_of(n["first_ts"]), kind)
    # the fixtures open some sessions with scheduled-task triggers, so both kinds of month node exist
    month_kinds = {json.loads(n["metrics_json"]).get("kind") for n in nodes.values() if n["level"] == "month"}
    assert month_kinds == {"hearth", "scheduled"}
    sched = c.execute("SELECT COUNT(*) FROM threads WHERE kind = 'scheduled'").fetchone()[0]
    opened = c.execute("""SELECT COUNT(DISTINCT thread_id) FROM sessions WHERE is_subagent = 0 AND opener = 'scheduled'""").fetchone()[0]
    assert 0 < sched <= opened


def test_describe_summary_text_is_stored_and_indexed(built):
    c = built.conn
    assert c.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0] == c.execute("SELECT COUNT(*) FROM nodes WHERE status='ok'").fetchone()[0]
    assert c.execute("SELECT COUNT(*) FROM nodes WHERE status='ok' AND summary_bytes = 0").fetchone()[0] == 0


# ---- idempotence / schema / staleness ------------------------------------------------------------
def snapshot(conn):
    return [tuple(r) for r in conn.execute("SELECT id, status, summary, attempts, model, created_at FROM nodes ORDER BY id")]


def test_build_is_idempotent(corpus, tmp_path):
    conn = fresh_conn(corpus, tmp_path)
    r1 = run_build(conn, StubSummarizer(), BuildOptions(thread_ids=None, levels=("leaf", "session", "thread", "month")))
    s1 = snapshot(conn)
    r2 = run_build(conn, StubSummarizer(), BuildOptions())
    assert r1["built"] > 0 and r2["built"] == 0 and r2["manual"] == 0
    assert snapshot(conn) == s1


def test_dry_run_writes_nothing_even_the_tables(corpus, tmp_path):
    conn = fresh_conn(corpus, tmp_path)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='nodes'").fetchone() is None
    r = run_build(conn, StubSummarizer(), BuildOptions(dry_run=True))
    assert r["plan"]["leaf"]["pending"] > 0 and r["plan"]["month"]["pending"] >= 1   # one per (month, kind)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='nodes'").fetchone() is None


def test_limit_and_level_and_thread_scoping(corpus, tmp_path):
    conn = fresh_conn(corpus, tmp_path)
    r = run_build(conn, StubSummarizer(), BuildOptions(limit=2, levels=("leaf",)))
    assert r["built"] == 2 and r["stopped"] == "limit"
    r = run_build(conn, StubSummarizer(), BuildOptions(levels=("session",)))      # leaves not ready: nothing builds
    assert r["built"] == 0 and r["levels"]["session"]["not_ready"] > 0
    tid = conn.execute("SELECT thread_id FROM threads WHERE thread_id LIKE ?", (SMALL_THREAD + "%",)).fetchone()[0]
    r = run_build(conn, StubSummarizer(), BuildOptions(thread_ids=[tid]))
    assert r["built"] > 0
    ok_threads = {x[0] for x in conn.execute("SELECT DISTINCT thread_id FROM nodes WHERE status='ok' AND level='session'")}
    assert ok_threads == {tid}
    assert conn.execute("SELECT status FROM nodes WHERE level='month'").fetchone()[0] == "pending"   # months skipped when scoped


def test_ingest_keeps_working_on_a_db_with_layer_d_tables(corpus, tmp_path):
    conn = fresh_conn(corpus, tmp_path)
    run_build(conn, StubSummarizer(), BuildOptions(limit=3, levels=("leaf",)))
    stats = Ingester(conn).ingest(corpus.roots, index_dir=corpus.index)
    assert stats["messages_new"] == 0 and stats["blocks_new"] == 0


def test_reingest_after_growth_rebuilds_only_the_affected_branch(corpus, tmp_path):
    root = tmp_path / "corpus"
    shutil.copytree(corpus.out, root)
    roots = [str(root / "root1"), str(root / "root2")]
    conn = open_db(str(tmp_path / "grow.db"))
    Ingester(conn).ingest(roots, index_dir=str(root / "sessions-index"))
    run_build(conn, StubSummarizer(), BuildOptions())
    before = {r["id"]: (r["status"], r["summary"]) for r in conn.execute("SELECT * FROM nodes")}
    # append one human prompt + one answer to the last session of the small multi-session thread
    sid = conn.execute("SELECT session_id, rel_path, root FROM sessions s WHERE thread_id LIKE ? AND is_subagent=0 "
                       "ORDER BY last_ts DESC LIMIT 1", (SMALL_THREAD + "%",)).fetchone()
    path = os.path.join(sid["root"], sid["rel_path"])
    tip = conn.execute("SELECT uuid FROM messages WHERE session_id = ? ORDER BY COALESCE(ts,'') DESC, line_no DESC LIMIT 1",
                       (sid["session_id"],)).fetchone()[0]
    recs = [{"type": "user", "uuid": "grow-u1", "parentUuid": tip, "sessionId": sid["session_id"], "isSidechain": False,
             "timestamp": "2026-04-02T10:00:00.000Z", "origin": {"kind": "human"},
             "message": {"role": "user", "content": "Does Zorblax survive the new quarter?"}},
            {"type": "assistant", "uuid": "grow-a1", "parentUuid": "grow-u1", "sessionId": sid["session_id"],
             "isSidechain": False, "timestamp": "2026-04-02T10:00:05.000Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "Zorblax is stable."}]}}]
    with open(path, "ab") as f:
        for r in recs:
            f.write((json.dumps(r) + "\n").encode())
    Ingester(conn).ingest(roots, index_dir=str(root / "sessions-index"))
    r = run_build(conn, StubSummarizer(), BuildOptions())
    after = {x["id"]: (x["status"], x["summary"]) for x in conn.execute("SELECT * FROM nodes")}
    assert r["sync"]["leaf_changed"] + r["sync"]["leaf_new"] >= 1
    changed = [i for i in after if i not in before or after[i] != before[i]]
    levels = conn.execute("SELECT level FROM nodes WHERE id IN (%s)" % ",".join("?" * len(changed)), changed).fetchall()
    assert 1 <= len(changed) <= 8, changed         # the leaf, its session, thread, the March month (plus a new April month)
    assert {"leaf", "session"} <= {x[0] for x in levels} and "month" in {x[0] for x in levels}
    assert r["built"] == len(changed)
    assert all(v[0] == "ok" for v in after.values())
    assert conn.execute("SELECT COUNT(*) FROM node_sources").fetchone()[0] == conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


# ---- escalation small -> mid -> manual ------------------------------------------------------------
def small_thread_conn(corpus, tmp_path):
    conn = fresh_conn(corpus, tmp_path)
    tid = conn.execute("SELECT thread_id FROM threads WHERE thread_id LIKE ?", (SMALL_THREAD + "%",)).fetchone()[0]
    return conn, tid


class Recorder(StubSummarizer):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = []

    def summarize(self, pack, tier="small"):
        self.calls.append((pack.level, tier, pack.feedback))
        return super().summarize(pack, tier)


def test_flagged_leaf_is_rerun_on_mid_and_accepted(corpus, tmp_path):
    conn, tid = small_thread_conn(corpus, tmp_path)
    s = Recorder(flinch=True, flinch_tiers=("small",))
    r = run_build(conn, s, BuildOptions(thread_ids=[tid], levels=("leaf",)))
    assert r["built"] == r["flagged_then_ok"] > 0 and r["manual"] == 0
    leaf_calls = [c for c in s.calls if c[0] == "leaf"]
    assert [c[1] for c in leaf_calls[:2]] == ["small", "mid"] and leaf_calls[1][2] and "quality checks" in leaf_calls[1][2]
    row = conn.execute("SELECT * FROM nodes WHERE level='leaf' LIMIT 1").fetchone()
    meta = json.loads(row["metrics_json"])
    assert row["status"] == "ok" and row["attempts"] == 1 and [h["tier"] for h in meta["history"]] == ["small", "mid"]
    assert meta["history"][0]["failed"] == ["ratio", "evasion", "coverage"] or meta["history"][0]["failed"]


def test_second_failure_goes_manual_and_the_build_moves_on(corpus, tmp_path):
    conn, tid = small_thread_conn(corpus, tmp_path)
    s = Recorder(flinch=True)
    r = run_build(conn, s, BuildOptions(thread_ids=[tid]))
    assert r["manual"] > 0 and r["built"] == 0
    leaf = conn.execute("SELECT * FROM nodes WHERE level='leaf' LIMIT 1").fetchone()
    assert leaf["status"] == "manual" and leaf["attempts"] == 2 and "report" in json.loads(leaf["metrics_json"])
    # upper levels were still built (over placeholders), not left pending
    ses = conn.execute("SELECT * FROM nodes WHERE level='session'").fetchall()
    assert ses and all(x["status"] in ("ok", "manual") for x in ses)
    pack = make_pack(conn, conn.execute("SELECT * FROM nodes WHERE level='session' LIMIT 1").fetchone())
    assert "unavailable" in pack.text
    assert not conn.execute("SELECT 1 FROM nodes_fts WHERE nodes_fts MATCH 'cannot'").fetchone()   # flinched text is never indexed


def test_interrupted_between_attempts_resumes_at_the_rerun(corpus, tmp_path):
    conn, tid = small_thread_conn(corpus, tmp_path)

    class Stop(Exception):
        pass

    class Boom(StubSummarizer):
        def summarize(self, pack, tier="small"):
            if tier == "mid":
                raise Stop()
            return super().summarize(pack, tier)

    boom = Boom(flinch=True)
    with pytest.raises(Stop):
        run_build(conn, boom, BuildOptions(thread_ids=[tid], levels=("leaf",)))
    row = conn.execute("SELECT * FROM nodes WHERE status='flagged'").fetchone()
    assert row is not None and row["attempts"] == 1
    s = Recorder()
    run_build(conn, s, BuildOptions(thread_ids=[tid], levels=("leaf",)))
    assert s.calls[0][1] == "mid"          # went straight to the rerun
    assert conn.execute("SELECT status, attempts FROM nodes WHERE id=?", (row["id"],)).fetchone()[:] == ("ok", 1)


# ---- sensitivity ---------------------------------------------------------------------------------
def test_high_sensitivity_engages_when_a_thinking_block_is_in_the_source(built):
    c = built.conn
    hi = c.execute("SELECT * FROM nodes WHERE level='leaf' AND metrics_json LIKE '%\"high\"%' LIMIT 1").fetchone()
    lo = c.execute("SELECT * FROM nodes WHERE level='leaf' AND metrics_json LIKE '%\"normal\"%' LIMIT 1").fetchone()
    for lf, want in ((hi, "high"), (lo, "normal")):
        has_thinking = c.execute("""SELECT COUNT(*) FROM node_sources s JOIN blocks b ON b.uuid = s.uuid
                                    WHERE s.node_id = ? AND b.class = 'thinking'""", (lf["id"],)).fetchone()[0] > 0
        assert has_thinking == (want == "high")
        assert make_pack(c, lf).sensitivity == want
        assert json.loads(lf["metrics_json"])["report"]["thresholds"] == want
    # sensitivity propagates upward
    parent = c.execute("SELECT * FROM nodes WHERE id = ?", (hi["parent_id"],)).fetchone()
    assert json.loads(parent["metrics_json"])["sensitivity"] == "high"


def test_stub_passes_the_detector_on_another_corpus_seed(tmp_path):
    """Seed 99 has leaves that are mostly thinking text; the stub must not trip the guard on them."""
    from fixtures.gen import generate
    out = str(tmp_path / "fx99")
    generate(out, seed=99)
    conn = open_db(str(tmp_path / "s99.db"))
    Ingester(conn).ingest([os.path.join(out, "root1"), os.path.join(out, "root2")])
    r = run_build(conn, StubSummarizer(), BuildOptions())
    assert r["manual"] == 0 and r["stopped"] is None
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE status != 'ok'").fetchone()[0] == 0
