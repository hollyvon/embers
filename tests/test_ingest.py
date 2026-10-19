"""Ingester behaviour against the generated corpus and its truth.json."""
import hashlib
import json
import os
import shutil

from memarchive.ingest import Ingester, discover, open_db


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def q1(conn, sql, *p):
    if len(p) == 1 and isinstance(p[0], tuple):
        p = p[0]
    return conn.execute(sql, p).fetchone()[0]


# --------------------------------------------------------------------------- stitching (truth.json)

def test_threads_match_truth_components(db, corpus):
    sid_of = {rel: info["session_id"] for rel, info in corpus.truth["files"].items() if not info["is_subagent"]}
    got = {}
    for sid, tid in db.execute("SELECT session_id, thread_id FROM sessions WHERE is_subagent = 0"):
        got.setdefault(tid, set()).add(sid)
    expected = sorted(sorted(sid_of[r] for r in c) for c in corpus.truth["components"])
    assert sorted(sorted(v) for v in got.values()) == expected
    # thread id is deterministic: the smallest member session id
    for tid, members in got.items():
        assert tid == min(members)
    assert q1(db, "SELECT COUNT(*) FROM threads WHERE n_sessions > 1") == sum(
        1 for c in corpus.truth["components"] if len(c) > 1)


def test_canonical_owner_is_earliest_file(db, corpus):
    sid_of = {rel: info["session_id"] for rel, info in corpus.truth["files"].items()}
    wrong = []
    for u, rel in corpus.truth["canonical_owner"].items():
        row = db.execute("SELECT session_id, n_owners FROM messages WHERE uuid = ?", (u,)).fetchone()
        if row is None or row["session_id"] != sid_of[rel] or row["n_owners"] < 2:
            wrong.append((u, rel, tuple(row) if row else None))
    assert not wrong, wrong[:5]
    assert q1(db, "SELECT COUNT(*) FROM messages WHERE n_owners > 1") == corpus.truth["shared_uuid_count"]
    # every uuid is stored exactly once, every owner is recorded
    assert q1(db, "SELECT COUNT(*) FROM messages") == q1(db, "SELECT COUNT(DISTINCT uuid) FROM message_owners")
    for u, rel in list(corpus.truth["canonical_owner"].items())[:50]:
        owners = {r[0] for r in db.execute("SELECT session_id FROM message_owners WHERE uuid = ?", (u,))}
        assert sid_of[rel] in owners and len(owners) >= 2


def test_file_view_keeps_copied_prefix_and_thread_dedupes(db, corpus):
    a, b, c = corpus.truth["chain"]
    sa, sb, sc = (corpus.sid(x) for x in (a, b, c))
    n_a = q1(db, "SELECT COUNT(*) FROM message_owners WHERE session_id = ?", (sa,))
    n_b = q1(db, "SELECT COUNT(*) FROM message_owners WHERE session_id = ?", (sb,))
    n_c = q1(db, "SELECT COUNT(*) FROM message_owners WHERE session_id = ?", (sc,))
    assert n_c > n_b > 0 and n_a > 0
    tid = q1(db, "SELECT thread_id FROM sessions WHERE session_id = ?", (sa,))
    assert {sa, sb, sc} <= {r[0] for r in db.execute("SELECT session_id FROM sessions WHERE thread_id = ?", (tid,))}
    canonical = q1(db, "SELECT COUNT(*) FROM messages WHERE thread_id = ? AND is_sidechain = 0", (tid,))
    all_owned = q1(db, """SELECT COUNT(*) FROM message_owners o JOIN sessions s ON s.session_id = o.session_id
                          WHERE s.thread_id = ? AND s.is_subagent = 0""", (tid,))
    assert canonical < all_owned  # the thread view dedupes copies


def test_human_prompts_match_truth(db, corpus):
    for rel, info in corpus.truth["files"].items():
        if info["is_subagent"]:
            continue
        got = {r[0] for r in db.execute(
            """SELECT m.uuid FROM messages m JOIN message_owners o ON o.uuid = m.uuid
               WHERE m.is_human_prompt = 1 AND o.session_id = ?""", (info["session_id"],))}
        assert got == set(info["human_prompt_uuids"]), rel
    assert q1(db, "SELECT COUNT(*) FROM messages WHERE is_human_prompt = 1 AND is_sidechain = 1") == 0


def test_metadata_dedupe_keep_last(db, corpus):
    for rel, info in corpus.truth["files"].items():
        if info["is_subagent"]:
            continue
        sid = info["session_id"]
        assert q1(db, "SELECT title FROM sessions WHERE session_id = ?", (sid,)) == info["resolved_title"], rel
        meta = {k: v for k, v in db.execute("SELECT key, value FROM session_meta WHERE session_id = ?", (sid,))}
        if info.get("final_last_prompt"):
            assert meta["last_prompt"] == info["final_last_prompt"]
        if info.get("final_mode"):
            assert meta["mode"] == info["final_mode"]
        assert "custom_title" not in meta or len(meta["custom_title"]) > 0


def test_compaction_chain_integrity(db, corpus):
    n = 0
    for rel, info in corpus.truth["files"].items():
        for c in info["compactions"]:
            n += 1
            b = db.execute("SELECT * FROM messages WHERE uuid = ?", (c["boundary"],)).fetchone()
            assert b["type"] == "system" and b["subtype"] == "compact_boundary"
            assert b["parent_uuid"] is None and b["logical_parent_uuid"] == c["logicalParentUuid"]
            lp = db.execute("SELECT * FROM messages WHERE uuid = ?", (c["logicalParentUuid"],)).fetchone()
            assert lp is not None and lp["thread_id"] == b["thread_id"]
            # the logical parent precedes the boundary in the canonical order
            assert (lp["ts"], lp["line_no"]) < (b["ts"], b["line_no"])
            s = db.execute("SELECT * FROM messages WHERE uuid = ?", (c["summary"],)).fetchone()
            assert s["is_compact_summary"] == 1 and s["parent_uuid"] == c["boundary"] and s["is_human_prompt"] == 0
            cls = db.execute("SELECT class FROM blocks WHERE uuid = ?", (c["summary"],)).fetchone()[0]
            assert cls == "summary"
    assert n >= 4
    assert q1(db, "SELECT COUNT(*) FROM messages WHERE subtype = 'compact_boundary'") >= n
    # walking logicalParentUuid from every boundary reaches the file's first root
    for rel, info in corpus.truth["files"].items():
        if not info["compactions"]:
            continue
        sid = info["session_id"]
        for c in info["compactions"]:
            cur = c["boundary"]
            hops = 0
            while True:
                row = db.execute("SELECT parent_uuid, logical_parent_uuid FROM messages WHERE uuid = ?", (cur,)).fetchone()
                nxt = row["parent_uuid"] or row["logical_parent_uuid"]
                if nxt is None:
                    break
                cur = nxt
                hops += 1
                assert hops < 100000
            assert q1(db, "SELECT COUNT(*) FROM message_owners WHERE uuid = ? AND session_id = ?", (cur, sid)) == 1 or \
                q1(db, "SELECT n_owners FROM messages WHERE uuid = ?", (cur,)) > 1


def test_subagents_are_flagged_children(db, corpus):
    for rel, info in corpus.truth["subagents"].items():
        sid = "agent-" + info["agent_id"]
        s = db.execute("SELECT * FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        assert s is not None and s["is_subagent"] == 1
        assert s["parent_session_id"] == info["parent_session_id"] and s["agent_id"] == info["agent_id"]
        parent = db.execute("SELECT thread_id FROM sessions WHERE session_id = ?", (info["parent_session_id"],)).fetchone()
        assert s["thread_id"] == parent["thread_id"]
        assert s["opener"] == "subagent"
        assert q1(db, "SELECT COUNT(*) FROM messages WHERE session_id = ? AND is_sidechain = 0", (sid,)) == 0
        assert q1(db, "SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)) == \
            len([r for r in corpus.records(rel) if r and "uuid" in r])
    assert q1(db, "SELECT COUNT(*) FROM sessions WHERE is_subagent = 1") == len(corpus.truth["subagents"])


def test_openers(db, corpus):
    for rel, info in corpus.truth["files"].items():
        if info["is_subagent"] or info["complete_lines"] == 0:
            continue
        op = q1(db, "SELECT opener FROM sessions WHERE session_id = ?", (info["session_id"],))
        assert (op == "scheduled") == info["scheduled"], rel


# --------------------------------------------------------------------------- robustness

def test_truncated_line_survival(db, corpus):
    rels = corpus.truth["edge_cases"]["truncated_last_line"]
    assert rels
    for rel in rels:
        f = db.execute("SELECT * FROM files WHERE rel_path = ?", (rel.split("/", 1)[1],)).fetchone()
        assert f["partial_tail"] == 1 and f["bad_lines"] == 0
        assert f["line_count"] == corpus.truth["files"][rel]["complete_lines"]
        assert f["parsed_bytes"] < f["size"]
        n_msgs = len([r for r in corpus.records(rel) if r and "uuid" in r])
        assert q1(db, "SELECT COUNT(*) FROM message_owners WHERE session_id = ?", (corpus.sid(rel),)) == n_msgs
    assert db.stats["bad_lines"] == 0


def test_zero_length_file(db, corpus):
    rel = corpus.truth["edge_cases"]["zero_length"][0]
    f = db.execute("SELECT * FROM files WHERE rel_path = ?", (rel.split("/", 1)[1],)).fetchone()
    assert f is not None and f["size"] == 0 and f["line_count"] == 0
    s = db.execute("SELECT * FROM sessions WHERE session_id = ?", (corpus.sid(rel),)).fetchone()
    assert s is not None and s["n_messages"] == 0 and s["opener"] == "unknown"


def test_huge_line_and_payload_skipping(db, corpus):
    rel = corpus.truth["edge_cases"]["huge_line"][0]
    sid = corpus.sid(rel)
    big = db.execute("""SELECT * FROM blocks WHERE session_id = ? AND skipped_kind = 'image'
                        ORDER BY skipped_bytes DESC LIMIT 1""", (sid,)).fetchone()
    assert big["skipped_bytes"] >= 5 * 1024 * 1024 and big["text"] == ""
    assert big["pointer"].endswith("message.content[1]")
    # the text next to the image on that same line is indexed
    sib = db.execute("SELECT text FROM blocks WHERE uuid = ? AND block_index = 0", (big["uuid"],)).fetchone()
    assert sib["text"]
    # nothing base64-sized is stored as text
    assert q1(db, "SELECT MAX(nbytes) FROM blocks") < 200000
    assert q1(db, "SELECT COUNT(*) FROM blocks WHERE skipped_kind = 'toolUseResult'") > 100
    assert q1(db, "SELECT COUNT(*) FROM blocks WHERE skipped_kind = 'toolUseResult' AND text != ''") == 0
    assert q1(db, "SELECT COUNT(*) FROM blocks WHERE skipped_kind = 'document'") > 0


def test_duplicate_uuid_within_file_counted_once(db, corpus):
    rel = corpus.truth["edge_cases"]["duplicate_uuid_in_file"][0]
    f = db.execute("SELECT * FROM files WHERE rel_path = ?", (rel.split("/", 1)[1],)).fetchone()
    assert f["dup_lines"] == 1
    sid = corpus.sid(rel)
    recs = [r for r in corpus.records(rel) if r and "uuid" in r]
    assert q1(db, "SELECT COUNT(*) FROM message_owners WHERE session_id = ?", (sid,)) == len({r["uuid"] for r in recs})


def test_thinking_blocks_never_dropped(db, corpus):
    expected = 0
    for rel in corpus.truth["files"]:
        for r in corpus.records(rel):
            if r and r.get("type") == "assistant":
                for b in r["message"]["content"]:
                    if b["type"] == "thinking":
                        expected += 1
    got_dups = 0
    for rel, info in corpus.truth["files"].items():
        pass
    # counted per distinct uuid: shared (copied) thinking lines count once
    distinct = set()
    for rel in corpus.truth["files"]:
        for r in corpus.records(rel):
            if r and r.get("type") == "assistant" and r["message"]["content"][0]["type"] == "thinking":
                distinct.add(r["uuid"])
    assert q1(db, "SELECT COUNT(*) FROM blocks WHERE class = 'thinking'") == len(distinct)
    assert expected >= len(distinct)
    assert q1(db, "SELECT COUNT(*) FROM blocks WHERE class = 'thinking' AND text = ''") == 0
    assert q1(db, "SELECT COUNT(*) FROM blocks WHERE skipped_kind = 'signature'") > 0


def test_all_classes_present_and_fts_finds_planted(db, corpus):
    classes = {r[0] for r in db.execute("SELECT DISTINCT class FROM blocks")}
    assert classes == {"human", "assistant", "thinking", "tool_use", "tool_result", "attachment", "system", "summary"}
    for p in corpus.truth["planted"]:
        rows = db.execute("""SELECT b.class, b.session_id FROM blocks_fts f JOIN blocks b ON b.id = f.rowid
                             WHERE blocks_fts MATCH ?""", ('"%s"' % p["phrase"],)).fetchall()
        assert len(rows) == 1, p
        assert rows[0]["class"] == p["class"]
        owners = {r[0] for r in db.execute("SELECT session_id FROM message_owners WHERE uuid = (SELECT uuid FROM blocks WHERE id = (SELECT rowid FROM blocks_fts WHERE blocks_fts MATCH ?))", ('"%s"' % p["phrase"],))}
        assert p["session_id"] in owners
    # unicode survives
    assert q1(db, "SELECT COUNT(*) FROM blocks_fts WHERE blocks_fts MATCH ?", ("日本語のテキスト",)) > 0


def test_record_type_counts_and_windows(db, corpus):
    total = {}
    for (tc,) in db.execute("SELECT type_counts FROM files"):
        for k, v in json.loads(tc).items():
            total[k] = total.get(k, 0) + v
    expected = {}
    for info in corpus.truth["files"].values():
        for k, v in info["type_counts"].items():
            expected[k] = expected.get(k, 0) + v
    assert total == expected
    for sid, w in corpus.truth["windows"].items():
        row = db.execute("SELECT window, title FROM windows WHERE session_id = ?", (sid,)).fetchone()
        assert row["title"] == w["title"] and row["window"] == w["window"]


def test_sources_untouched(corpus, tmp_path):
    files = [f.path for f in discover(corpus.roots)]
    before = {p: (sha(p), os.stat(p).st_mtime_ns) for p in files}
    conn = open_db(str(tmp_path / "x.db"))
    Ingester(conn).ingest(corpus.roots)
    conn.close()
    assert {p: (sha(p), os.stat(p).st_mtime_ns) for p in files} == before


# --------------------------------------------------------------------------- incremental

def snapshot(conn):
    return {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            for t in ("files", "sessions", "threads", "messages", "message_owners", "blocks", "blocks_fts", "session_meta")}


def test_reingest_is_idempotent(db, corpus):
    before = snapshot(db)
    canon_before = db.execute("SELECT uuid, session_id, line_no, thread_id FROM messages ORDER BY uuid").fetchall()
    stats = Ingester(db.conn).ingest(corpus.roots, index_dir=corpus.index)
    assert stats["messages_new"] == 0 and stats["blocks_new"] == 0 and stats["lines_parsed"] == 0
    assert stats["files_unchanged"] + stats.get("files_tail_retry", 0) == stats["files_seen"]
    assert stats.get("files_full", 0) == 0 and stats.get("files_append", 0) == 0
    # only partial-tail files are re-opened (to retry the tail)
    assert stats.get("files_tail_retry", 0) == len(corpus.truth["edge_cases"]["truncated_last_line"])
    assert snapshot(db) == before
    assert db.execute("SELECT uuid, session_id, line_no, thread_id FROM messages ORDER BY uuid").fetchall() == canon_before


def test_rebuild_matches_incremental(db, corpus, tmp_path):
    conn = open_db(str(tmp_path / "r.db"))
    Ingester(conn).ingest(corpus.roots, index_dir=corpus.index)
    Ingester(conn).ingest(corpus.roots, rebuild=True)
    assert snapshot(conn) == snapshot(db)
    a = db.execute("SELECT uuid, session_id, thread_id, is_human_prompt FROM messages ORDER BY uuid").fetchall()
    b = conn.execute("SELECT uuid, session_id, thread_id, is_human_prompt FROM messages ORDER BY uuid").fetchall()
    assert [tuple(r) for r in a] == [tuple(r) for r in b]
    conn.close()


def test_append_and_partial_tail_recovery(corpus, tmp_path):
    """A file grows while we watch it: new complete lines are picked up from the watermark,
    a partial last line is retried once it is completed, and nothing is parsed twice."""
    work = tmp_path / "copy"
    shutil.copytree(corpus.out, str(work))
    roots = [str(work / "root1"), str(work / "root2")]
    conn = open_db(str(tmp_path / "a.db"))
    s1 = Ingester(conn).ingest(roots)
    assert s1["files_full"] == s1["files_seen"]
    # pick an ordinary complete file and append: one complete record, then a partial one
    rel = next(rel for rel, i in corpus.truth["files"].items()
               if not i["is_subagent"] and not i["truncated_tail"] and 50 < i["complete_lines"] < 400
               and rel not in corpus.truth["edge_cases"]["huge_line"])
    sid = corpus.sid(rel)
    path = str(work / rel)
    last = [r for r in corpus.records(rel) if r and "uuid" in r][-1]
    new1 = {"type": "user", "uuid": "11111111-1111-4111-8111-111111111111", "parentUuid": last["uuid"],
            "sessionId": sid, "timestamp": "2026-12-31T00:00:00.000Z", "cwd": "/x", "version": "9.9.9",
            "entrypoint": "claude-desktop", "userType": "external", "isSidechain": False, "origin": {"kind": "human"},
            "message": {"role": "user", "content": "appended-marker-phrase one"}}
    new2 = {"type": "assistant", "uuid": "22222222-2222-4222-8222-222222222222", "parentUuid": new1["uuid"],
            "sessionId": sid, "timestamp": "2026-12-31T00:00:01.000Z", "cwd": "/x", "version": "9.9.9",
            "entrypoint": "claude-desktop", "userType": "external", "isSidechain": False,
            "message": {"id": "msg_x", "role": "assistant", "model": "m", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "appended-marker-phrase two"}]}}
    line2 = json.dumps(new2)
    before = snapshot(conn)
    line_count = conn.execute("SELECT line_count FROM files WHERE session_id = ?", (sid,)).fetchone()[0]
    os.utime(path, None)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(new1) + "\n")
        f.write(line2[: len(line2) // 2])
    s2 = Ingester(conn).ingest(roots)
    assert s2.get("files_append") == 1 and s2.get("files_full", 0) == 0 and s2["messages_new"] == 1
    f = conn.execute("SELECT * FROM files WHERE session_id = ?", (sid,)).fetchone()
    assert f["partial_tail"] == 1 and f["line_count"] == line_count + 1
    assert conn.execute("SELECT is_human_prompt FROM messages WHERE uuid = ?", (new1["uuid"],)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM blocks_fts WHERE blocks_fts MATCH ?", ('"appended-marker-phrase"',)).fetchone()[0] == 1
    # complete the partial line
    with open(path, "a", encoding="utf-8") as f:
        f.write(line2[len(line2) // 2:] + "\n")
    s3 = Ingester(conn).ingest(roots)
    assert s3.get("files_append") == 1 and s3["messages_new"] == 1
    f = conn.execute("SELECT * FROM files WHERE session_id = ?", (sid,)).fetchone()
    assert f["partial_tail"] == 0 and f["line_count"] == line_count + 2 and f["parsed_bytes"] == os.path.getsize(path)
    assert conn.execute("SELECT COUNT(*) FROM blocks_fts WHERE blocks_fts MATCH ?", ('"appended-marker-phrase"',)).fetchone()[0] == 2
    after = snapshot(conn)
    assert after["messages"] == before["messages"] + 2 and after["files"] == before["files"]
    assert conn.execute("SELECT last_ts FROM sessions WHERE session_id = ?", (sid,)).fetchone()[0] == new2["timestamp"]
    # a rewritten (shrunk) file is re-parsed in full and its old rows replaced
    with open(path, "rb") as fh:
        head = fh.read().split(b"\n")[:10]
    with open(path, "wb") as fh:
        fh.write(b"\n".join(head) + b"\n")
    s4 = Ingester(conn).ingest(roots)
    assert s4.get("files_full") == 1
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE uuid IN (?, ?)", (new1["uuid"], new2["uuid"])).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM blocks_fts WHERE blocks_fts MATCH ?", ('"appended-marker-phrase"',)).fetchone()[0] == 0
    conn.close()


def test_legacy_summary_tolerance(corpus_legacy, tmp_path):
    conn = open_db(str(tmp_path / "l.db"))
    stats = Ingester(conn).ingest(corpus_legacy.roots)
    assert stats["bad_lines"] == 0
    rels = corpus_legacy.truth["edge_cases"]["legacy_summary"]
    assert len(rels) == 3
    assert q1(conn, "SELECT COUNT(*) FROM blocks WHERE block_type = 'legacy_summary'") == 3
    assert q1(conn, "SELECT COUNT(*) FROM blocks WHERE class = 'summary' AND block_type = 'legacy_summary' AND text LIKE 'Legacy invented summary%'") == 3
    for rel in rels:
        sid = corpus_legacy.sid(rel)
        f = conn.execute("SELECT * FROM files WHERE session_id = ?", (sid,)).fetchone()
        assert json.loads(f["type_counts"]).get("summary") == 1
        # the rest of the file ingested normally: same message count as the non-legacy corpus
        n = q1(conn, "SELECT COUNT(*) FROM message_owners WHERE session_id = ? AND uuid NOT LIKE '%:L%'", (sid,))
        assert n == len({r["uuid"] for r in corpus_legacy.records(rel) if r and "uuid" in r})
    conn.close()


def test_unknown_record_type_and_garbage_lines(tmp_path):
    root = tmp_path / "root"
    proj = root / "-proj"
    proj.mkdir(parents=True)
    lines = [
        json.dumps({"type": "future-record", "sessionId": "s", "payload": {"x": [1, 2]}}),
        "this is not json",
        json.dumps([1, 2, 3]),
        json.dumps({"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": "s",
                    "timestamp": "2026-01-01T00:00:00.000Z", "extraTopLevel": True,
                    "message": {"role": "user", "content": "hello world", "unknownMsgKey": 1}}),
        json.dumps({"type": "user", "uuid": "u2", "parentUuid": "u1", "sessionId": "s", "message": None}),
        json.dumps({"type": "assistant", "uuid": "u3", "parentUuid": "u2", "sessionId": "s",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "reply"}, "junk", 7]}}),
        "",
        json.dumps({"type": "custom-title", "customTitle": 12345, "sessionId": "s"}),
    ]
    (proj / "s.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    conn = open_db(":memory:")
    stats = Ingester(conn).ingest([str(root)])
    assert stats["bad_lines"] == 3  # garbage text, a JSON array, an empty line
    assert stats["messages_new"] == 3
    assert q1(conn, "SELECT COUNT(*) FROM blocks") == 2
    assert q1(conn, "SELECT title FROM sessions WHERE session_id = 's'") == "12345"
    f = conn.execute("SELECT * FROM files").fetchone()
    assert json.loads(f["type_counts"]) == {"future-record": 1, "user": 2, "assistant": 1, "custom-title": 1}
    assert f["line_count"] == 8 and f["partial_tail"] == 0
