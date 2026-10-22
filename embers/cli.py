"""`embers` (alias `mem`) -- bash-friendly CLI over the archive.

    embers ingest <root>... [--index DIR] [--rebuild]
    embers search "query" [--class human,assistant,thinking] [--window N] [--since DATE] [--until DATE] [--limit N]
    embers session <id> [--around <hit>] [--context N] [--thread] [--class ...] [--full]
    embers sessions [--since DATE] [--limit N]
    embers windows [--index DIR]
    embers stats
    embers build [--level leaf|session|thread|month|all] [--thread ID] [--backend stub|anthropic] [--budget-usd N]
              [--dry-run] [--limit N] [--chunk-bytes N] [--workers N]
              [--rerun-model-leaf M] [--rerun-model-upper M]
    embers describe <session-id | window N | wN | N | thread ID | node ID>
    embers expand <node ID> [--full]
    embers grep "query" [--level ...] [--limit N]
    embers flags [--status flagged|manual] [--export DIR]
    embers manual <node ID> (--text "..." | --file PATH | --stdin)      |   embers manual --import DIR

Global: --db PATH (or $MEM_DB; default ~/.memory-archive/mem.db), --json (machine output, one JSON per line).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from . import dag
from .build import BuildOptions, children_of, make_pack, run_build, set_manual_summary
from .ingest import Ingester, default_db_path, open_db
from .summarize import make_summarizer

CLASSES = ("human", "assistant", "thinking", "tool_use", "tool_result", "attachment", "system", "summary")


# ----------------------------------------------------------------------------- helpers

def _one_line(text: str, width: int) -> str:
    s = " ".join((text or "").split())
    if len(s) > width:
        return s[: width - 1] + "…"
    return s


def _short(session_id: str) -> str:
    return session_id if session_id.startswith("agent-") else session_id[:8]


def _date(ts: Optional[str], full: bool = False) -> str:
    if not ts:
        return "-"
    return ts[:19].replace("T", " ") if full else ts[:10]


def _norm_date(s: Optional[str], end: bool = False) -> Optional[str]:
    """YYYY-MM-DD[THH:MM[:SS]] -> ISO prefix comparable against stored timestamps."""
    if not s:
        return None
    s = s.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s + ("T23:59:59.999Z" if end else "T00:00:00.000Z")
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?Z?$", s):
        return s
    raise SystemExit("bad date %r (use YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS])" % s)


def _classes(arg: Optional[str]) -> Optional[List[str]]:
    if not arg:
        return None
    out = []
    for c in arg.split(","):
        c = c.strip()
        if not c:
            continue
        if c not in CLASSES:
            raise SystemExit("unknown class %r (choose from %s)" % (c, ", ".join(CLASSES)))
        out.append(c)
    return out or None


def _emit(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")


def _fts_query(conn: sqlite3.Connection, sql: str, query: str, params: Sequence[Any]) -> List[sqlite3.Row]:
    """Run an FTS5 MATCH; if the raw query is not valid FTS syntax, retry it as a phrase."""
    try:
        return conn.execute(sql, (query,) + tuple(params)).fetchall()
    except sqlite3.OperationalError:
        # e.g. "fts5: syntax error" or "no such column: x" (a hyphenated word reads as a column filter)
        phrase = '"' + query.replace('"', '""') + '"'
        return conn.execute(sql, (phrase,) + tuple(params)).fetchall()


WINDOW_REF_RE = re.compile(r"\s*(?:window\s*#?\s*|w\s*)(\d+)\s*", re.I)


def _resolve_session(conn: sqlite3.Connection, ident: str) -> Optional[sqlite3.Row]:
    """Accept a full/prefix session id, a window number ('32', 'w32', 'window 32'), or 'agent-<id>'."""
    m = WINDOW_REF_RE.fullmatch(ident)
    if m:
        ident = m.group(1)
    if ident.isdigit():
        row = conn.execute("SELECT session_id FROM windows WHERE window = ?", (int(ident),)).fetchone()
        if row is None:
            return None
        ident = row[0]
    rows = conn.execute("SELECT * FROM sessions WHERE session_id = ? OR session_id LIKE ? ORDER BY session_id",
                        (ident, ident + "%")).fetchall()
    if not rows:
        return None
    exact = [r for r in rows if r["session_id"] == ident]
    if exact:
        return exact[0]
    if len(rows) > 1:
        raise SystemExit("ambiguous session id %r: %s" % (ident, ", ".join(r["session_id"] for r in rows[:8])))
    return rows[0]


def _window_of(conn: sqlite3.Connection, session_id: str) -> Optional[int]:
    row = conn.execute(
        """SELECT COALESCE(w.window, wp.window) FROM sessions s
           LEFT JOIN windows w ON w.session_id = s.session_id
           LEFT JOIN windows wp ON wp.session_id = s.parent_session_id
           WHERE s.session_id = ?""", (session_id,)).fetchone()
    return row[0] if row else None


def _context(conn: sqlite3.Connection, hit: sqlite3.Row, n: int, classes: Optional[List[str]] = None) -> Dict[str, List[sqlite3.Row]]:
    """n blocks before and after `hit` within the same session, in reading order."""
    cls_sql = ""
    cls_params: List[Any] = []
    if classes:
        cls_sql = " AND class IN (%s)" % ",".join("?" * len(classes))
        cls_params = list(classes)
    key = (hit["session_id"], hit["ts"] or "", hit["line_no"], hit["block_index"])
    before = conn.execute(
        """SELECT * FROM blocks WHERE session_id = ? AND (COALESCE(ts,''), line_no, block_index) < (?, ?, ?)%s
           ORDER BY COALESCE(ts,'') DESC, line_no DESC, block_index DESC LIMIT ?""" % cls_sql,
        key + tuple(cls_params) + (n,)).fetchall()
    after = conn.execute(
        """SELECT * FROM blocks WHERE session_id = ? AND (COALESCE(ts,''), line_no, block_index) > (?, ?, ?)%s
           ORDER BY COALESCE(ts,''), line_no, block_index LIMIT ?""" % cls_sql,
        key + tuple(cls_params) + (n,)).fetchall()
    return {"before": list(reversed(before)), "after": after}


def _block_dict(b: sqlite3.Row, full: bool = False, width: int = 300) -> Dict[str, Any]:
    text = b["text"] if full else _one_line(b["text"], width)
    d = {"id": b["id"], "uuid": b["uuid"], "session_id": b["session_id"], "thread_id": b["thread_id"],
         "class": b["class"], "block_type": b["block_type"], "ts": b["ts"], "line_no": b["line_no"],
         "block_index": b["block_index"], "text": text, "pointer": b["pointer"]}
    if b["tool_name"]:
        d["tool_name"] = b["tool_name"]
    if b["is_error"]:
        d["is_error"] = True
    if b["skipped_kind"]:
        d["skipped_kind"] = b["skipped_kind"]
        d["skipped_bytes"] = b["skipped_bytes"]
    return d


def _fmt_block(b: sqlite3.Row, marker: str = " ", width: int = 110, full: bool = False) -> str:
    cls = b["class"]
    tag = cls if cls != "tool_use" else "tool_use:%s" % (b["tool_name"] or "?")
    if b["skipped_kind"] and not b["text"]:
        body = "[skipped %s, %d bytes -> %s]" % (b["skipped_kind"], b["skipped_bytes"], b["pointer"])
    elif full:
        body = b["text"]
    else:
        body = _one_line(b["text"], width)
    if full and "\n" in body:
        body = ("\n" + " " * 4).join(body.split("\n"))
    return "%s %-12s %s" % (marker, tag, body)


def _block_line(b: sqlite3.Row, marker: str = " ", full: bool = False) -> str:
    """One rendered block line, as `embers session` prints it (also used by `embers expand` on a leaf)."""
    stamp = _date(b["ts"], True)[11:] if b["ts"] else "        "
    return "%s #%-7d %s L%-5d %s" % (marker, b["id"], stamp, b["line_no"], _fmt_block(b, marker="", full=full).lstrip())


# ----------------------------------------------------------------------------- commands

def cmd_ingest(conn: sqlite3.Connection, a: argparse.Namespace) -> int:
    log = (lambda s: sys.stderr.write(s + "\n")) if a.verbose else None
    stats = Ingester(conn, verbose=a.verbose, log=log).ingest(a.roots, index_dir=a.index, rebuild=a.rebuild)
    if a.json:
        _emit(dict(stats))
    else:
        print("ingested %d file(s): %d new, %d appended, %d unchanged; %d lines, %d bad; "
              "%d new messages (%d duplicates), %d new blocks; %d sessions in %d threads; %.2fs" % (
                  stats.get("files_seen", 0), stats.get("files_full", 0), stats.get("files_append", 0),
                  stats.get("files_unchanged", 0), stats.get("lines_parsed", 0), stats.get("bad_lines", 0),
                  stats.get("messages_new", 0), stats.get("messages_dup", 0), stats.get("blocks_new", 0),
                  stats.get("sessions", 0), stats.get("threads", 0), stats.get("seconds", 0)))
    return 0


def cmd_search(conn: sqlite3.Connection, a: argparse.Namespace) -> int:
    classes = _classes(a.cls)
    where = []
    params: List[Any] = []
    if classes:
        where.append("b.class IN (%s)" % ",".join("?" * len(classes)))
        params += classes
    if a.window is not None:
        where.append("COALESCE(w.window, wp.window) = ?")
        params.append(a.window)
    since = _norm_date(a.since)
    until = _norm_date(a.until, end=True)
    if since:
        where.append("b.ts >= ?")
        params.append(since)
    if until:
        where.append("b.ts <= ?")
        params.append(until)
    if a.session:
        where.append("(b.session_id = ? OR b.session_id LIKE ?)")
        params += [a.session, a.session + "%"]
    sql = """SELECT b.*, s.title AS title, COALESCE(w.window, wp.window) AS window, s.is_subagent AS is_subagent,
                    snippet(blocks_fts, 0, '[', ']', '…', 14) AS snippet, bm25(blocks_fts) AS rank
             FROM blocks_fts JOIN blocks b ON b.id = blocks_fts.rowid
             JOIN sessions s ON s.session_id = b.session_id
             LEFT JOIN windows w ON w.session_id = s.session_id
             LEFT JOIN windows wp ON wp.session_id = s.parent_session_id
             WHERE blocks_fts MATCH ?%s
             ORDER BY rank LIMIT ?""" % ("".join(" AND " + c for c in where))
    rows = _fts_query(conn, sql, a.query, params + [a.limit])
    for i, h in enumerate(rows, start=1):
        ctx = _context(conn, h, a.context)
        if a.json:
            d = _block_dict(h, full=True)
            d.update({"rank": i, "score": h["rank"], "snippet": h["snippet"], "window": h["window"],
                      "title": h["title"],
                      "before": [_block_dict(b) for b in ctx["before"]],
                      "after": [_block_dict(b) for b in ctx["after"]]})
            _emit(d)
            continue
        win = ("w%s" % h["window"]) if h["window"] is not None else "w-"
        head = "#%d  hit %d  %s  %s  %s  %s%s" % (h["id"], i, _date(h["ts"], full=True), win, h["session_id"][:8],
                                                 h["class"], "  (subagent)" if h["is_subagent"] else "")
        if h["title"]:
            head += "  “%s”" % _one_line(h["title"], 60)
        print(head)
        for b in ctx["before"]:
            print("   " + _fmt_block(b))
        print("   " + _fmt_block(h, marker=">"))
        print("      snippet: %s" % _one_line(h["snippet"], 160))
        for b in ctx["after"]:
            print("   " + _fmt_block(b))
        print()
    if not rows and not a.json:
        print("no hits")
    return 0 if rows else 1


def cmd_session(conn: sqlite3.Connection, a: argparse.Namespace) -> int:
    s = _resolve_session(conn, a.id)
    if s is None:
        raise SystemExit("no such session: %s" % a.id)
    classes = _classes(a.cls)
    scope_col = "thread_id" if a.thread else "session_id"
    scope_val = s["thread_id"] if a.thread else s["session_id"]
    members = [r["session_id"] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE thread_id = ? ORDER BY COALESCE(first_ts,''), session_id",
        (s["thread_id"],))]
    subagents = [r["session_id"] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE parent_session_id = ? ORDER BY session_id", (s["session_id"],))]
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM session_meta WHERE session_id = ?",
                                                        (s["session_id"],))}
    n_in_file = conn.execute("SELECT COUNT(*) FROM message_owners WHERE session_id = ?", (s["session_id"],)).fetchone()[0]
    header = {"session_id": s["session_id"], "thread_id": s["thread_id"], "title": s["title"],
              "window": _window_of(conn, s["session_id"]), "first_ts": s["first_ts"], "last_ts": s["last_ts"],
              "cwd": s["cwd"], "root": s["root"], "rel_path": s["rel_path"], "opener": s["opener"],
              "is_subagent": bool(s["is_subagent"]), "parent_session_id": s["parent_session_id"],
              "n_messages": s["n_messages"], "n_messages_in_file": n_in_file, "n_human": s["n_human"],
              "n_compactions": s["n_compactions"],
              "n_lines": s["n_lines"], "thread_members": members, "subagents": subagents, "meta": meta}
    where = ["b.%s = ?" % scope_col]
    params: List[Any] = [scope_val]
    if classes:
        where.append("b.class IN (%s)" % ",".join("?" * len(classes)))
        params += classes
    if a.around is not None:
        hit = conn.execute("SELECT * FROM blocks WHERE id = ?", (a.around,)).fetchone()
        if hit is None:
            raise SystemExit("no such hit/block id: %s" % a.around)
        ctx = _context(conn, hit, a.context, classes)
        rows = ctx["before"] + [hit] + ctx["after"]
    elif a.thread:
        # the logical thread: every canonical (deduped) message of the component, in time order
        sql = """SELECT b.* FROM blocks b WHERE %s
                 ORDER BY COALESCE(b.ts,''), b.line_no, b.block_index""" % " AND ".join(where)
        if a.limit:
            sql += " LIMIT %d" % a.limit
        rows = conn.execute(sql, params).fetchall()
    else:
        # the file as written: includes a resumed prefix even though its canonical owner is another file
        where[0] = "o.session_id = ?"
        sql = """SELECT b.*, o.line_no AS line_no FROM message_owners o JOIN blocks b ON b.uuid = o.uuid
                 WHERE %s ORDER BY o.line_no, b.block_index""" % " AND ".join(where)
        if a.limit:
            sql += " LIMIT %d" % a.limit
        rows = conn.execute(sql, params).fetchall()
    if a.json:
        _emit({"session": header})
        for b in rows:
            _emit(_block_dict(b, full=True))
        return 0
    win = header["window"]
    print("session %s%s  %s → %s  thread %s (%d file%s)%s" % (
        s["session_id"], "  [w%s]" % win if win is not None else "", _date(s["first_ts"], True),
        _date(s["last_ts"], True), (s["thread_id"] or "-")[:8], len(members), "" if len(members) == 1 else "s",
        "  SUBAGENT of %s" % s["parent_session_id"] if s["is_subagent"] else ""))
    print("  title: %s" % (s["title"] or "-"))
    print("  path:  %s/%s" % (s["root"], s["rel_path"]))
    print("  cwd: %s  opener: %s  messages: %d in file (%d first seen here)  human prompts: %d  compactions: %d" % (
        s["cwd"] or "-", s["opener"], n_in_file, s["n_messages"], s["n_human"], s["n_compactions"]))
    if len(members) > 1:
        print("  thread members: %s" % ", ".join(_short(m) for m in members))
    if subagents:
        print("  subagents: %s" % ", ".join(subagents))
    print()
    for b in rows:
        print(_block_line(b, marker=">" if a.around is not None and b["id"] == a.around else " ", full=a.full))
    return 0


def cmd_sessions(conn: sqlite3.Connection, a: argparse.Namespace) -> int:
    where = ["1=1"]
    params: List[Any] = []
    since = _norm_date(a.since)
    if since:
        where.append("COALESCE(s.last_ts, s.first_ts) >= ?")
        params.append(since)
    if not a.subagents:
        where.append("s.is_subagent = 0")
    sql = """SELECT s.*, COALESCE(w.window, wp.window) AS window FROM sessions s
             LEFT JOIN windows w ON w.session_id = s.session_id
             LEFT JOIN windows wp ON wp.session_id = s.parent_session_id
             WHERE %s ORDER BY COALESCE(s.first_ts,''), s.session_id""" % " AND ".join(where)
    if a.limit:
        sql += " LIMIT %d" % a.limit
    rows = conn.execute(sql, params).fetchall()
    for s in rows:
        if a.json:
            _emit({k: s[k] for k in s.keys()})
            continue
        print("%-10s %s  %-4s %s  %4d msg  %s  %s" % (
            _date(s["first_ts"]), s["session_id"][:8], ("w%s" % s["window"]) if s["window"] is not None else "-",
            (s["thread_id"] or "-")[:8], s["n_messages"], _one_line(s["title"] or "-", 50), s["cwd"] or ""))
    return 0


def _created_date(v: Any) -> Optional[str]:
    """createdAt as 'YYYY-MM-DD HH:MM:SS' (UTC): accepts epoch milliseconds or seconds (number or digit
    string) and ISO-8601 strings; None if it is neither."""
    if v is None or v == "":
        return None
    t = str(v).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", t):
        x = float(t)
        if abs(x) >= 1e11:          # milliseconds (seconds stay below 1e11 until the year 5138)
            x /= 1000.0
        try:
            return _dt.datetime.fromtimestamp(x, _dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return None
    if re.match(r"\d{4}-\d{2}-\d{2}", t):
        return t[:19].replace("T", " ")
    return None


def cmd_windows(conn: sqlite3.Connection, a: argparse.Namespace) -> int:
    if a.index:
        n = Ingester(conn).load_index(a.index)
        if not a.json:
            sys.stderr.write("loaded %d index entries from %s\n" % (n, a.index))
    rows = conn.execute(
        """SELECT w.*, s.n_messages AS n_messages, s.first_ts AS first_ts, s.last_ts AS last_ts, s.thread_id AS thread_id
           FROM windows w LEFT JOIN sessions s ON s.session_id = w.session_id""").fetchall()
    if not rows and not a.json:
        print("no window map: run `embers ingest --index DIR` or `embers windows --index DIR`")
        return 1
    # sort by date (createdAt, else the session's first message), then window number
    dated = []
    for w in rows:
        d = _created_date(w["created_at"]) or (_date(w["first_ts"], True) if w["first_ts"] else None)
        dated.append((d or "9999", w["window"] if w["window"] is not None else 10 ** 9, w["session_id"], d, w))
    dated.sort(key=lambda x: x[:3])
    for _, _, _, d, w in dated:
        if a.json:
            r = {k: w[k] for k in w.keys()}
            r["date"] = d
            _emit(r)
            continue
        print("%-5s %s  %s  %s  %s" % (
            ("w%d" % w["window"]) if w["window"] is not None else "-", (d or "-").ljust(19),
            w["session_id"][:8], "%4s msg" % (w["n_messages"] if w["n_messages"] is not None else "?"),
            _one_line(w["title"] or "(untitled)", 70)))
    return 0


def cmd_stats(conn: sqlite3.Connection, a: argparse.Namespace) -> int:
    q = lambda sql, *p: conn.execute(sql, p).fetchone()[0]  # noqa: E731
    d: Dict[str, Any] = {
        "db": a.db,
        "files": q("SELECT COUNT(*) FROM files"),
        "files_bytes": q("SELECT COALESCE(SUM(size),0) FROM files"),
        "files_with_bad_lines": q("SELECT COUNT(*) FROM files WHERE bad_lines > 0"),
        "files_with_partial_tail": q("SELECT COUNT(*) FROM files WHERE partial_tail = 1"),
        "bad_lines": q("SELECT COALESCE(SUM(bad_lines),0) FROM files"),
        "dup_lines_in_file": q("SELECT COALESCE(SUM(dup_lines),0) FROM files"),
        "sessions": q("SELECT COUNT(*) FROM sessions WHERE is_subagent = 0"),
        "subagent_sessions": q("SELECT COUNT(*) FROM sessions WHERE is_subagent = 1"),
        "threads": q("SELECT COUNT(*) FROM threads"),
        "multi_file_threads": q("SELECT COUNT(*) FROM threads WHERE n_sessions > 1"),
        "messages": q("SELECT COUNT(*) FROM messages"),
        "messages_shared": q("SELECT COUNT(*) FROM messages WHERE n_owners > 1"),
        "human_prompts": q("SELECT COUNT(*) FROM messages WHERE is_human_prompt = 1"),
        "compaction_boundaries": q("SELECT COUNT(*) FROM messages WHERE subtype = 'compact_boundary'"),
        "blocks": q("SELECT COUNT(*) FROM blocks"),
        "text_bytes": q("SELECT COALESCE(SUM(nbytes),0) FROM blocks"),
        "skipped_bytes": q("SELECT COALESCE(SUM(skipped_bytes),0) FROM blocks"),
        "windows": q("SELECT COUNT(*) FROM windows WHERE window IS NOT NULL"),
        "first_ts": q("SELECT MIN(first_ts) FROM sessions"),
        "last_ts": q("SELECT MAX(last_ts) FROM sessions"),
        "roots": [r[0] for r in conn.execute("SELECT DISTINCT root FROM files ORDER BY root")],
        "by_class": {r[0]: {"blocks": r[1], "bytes": r[2]} for r in conn.execute(
            "SELECT class, COUNT(*), SUM(nbytes) FROM blocks GROUP BY class ORDER BY class")},
        "skipped_by_kind": {r[0]: {"blocks": r[1], "bytes": r[2]} for r in conn.execute(
            "SELECT skipped_kind, COUNT(*), SUM(skipped_bytes) FROM blocks WHERE skipped_kind IS NOT NULL GROUP BY skipped_kind ORDER BY skipped_kind")},
        "record_types": {},
    }
    rt: Dict[str, int] = {}
    for (tc,) in conn.execute("SELECT type_counts FROM files WHERE type_counts IS NOT NULL"):
        for k, v in json.loads(tc).items():
            rt[k] = rt.get(k, 0) + v
    d["record_types"] = dict(sorted(rt.items(), key=lambda kv: -kv[1]))
    if a.json:
        _emit(d)
        return 0
    print("db: %s" % d["db"])
    print("roots: %s" % (", ".join(d["roots"]) or "-"))
    print("files: %d (%.1f MB), bad lines: %d, partial tails: %d, in-file duplicate uuids: %d" % (
        d["files"], d["files_bytes"] / 1e6, d["bad_lines"], d["files_with_partial_tail"], d["dup_lines_in_file"]))
    print("sessions: %d (+%d subagent) in %d threads (%d spanning several files); windows mapped: %d" % (
        d["sessions"], d["subagent_sessions"], d["threads"], d["multi_file_threads"], d["windows"]))
    print("messages: %d (%d shared across files), human prompts: %d, compaction boundaries: %d" % (
        d["messages"], d["messages_shared"], d["human_prompts"], d["compaction_boundaries"]))
    print("blocks: %d, indexed text: %.1f MB, skipped payloads: %.1f MB" % (
        d["blocks"], d["text_bytes"] / 1e6, d["skipped_bytes"] / 1e6))
    print("span: %s → %s" % (_date(d["first_ts"]), _date(d["last_ts"])))
    print("by class:")
    for c, v in d["by_class"].items():
        print("  %-12s %7d blocks %10d bytes" % (c, v["blocks"], v["bytes"]))
    if d["skipped_by_kind"]:
        print("skipped (pointer only):")
        for c, v in d["skipped_by_kind"].items():
            print("  %-16s %7d blocks %10d bytes" % (c, v["blocks"], v["bytes"]))
    print("record types:")
    for c, v in d["record_types"].items():
        print("  %-22s %7d" % (c, v))
    return 0


# ----------------------------------------------------------------------------- Layer D commands

def _need_nodes(conn: sqlite3.Connection) -> bool:
    ok = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'nodes'").fetchone() is not None
    if ok:
        ok = conn.execute("SELECT 1 FROM nodes LIMIT 1").fetchone() is not None
    if not ok:
        sys.stderr.write("no summaries yet: run `embers build` first\n")
    return ok


def _node_dict(n: sqlite3.Row, summary: bool = True) -> Dict[str, Any]:
    d = {k: n[k] for k in n.keys() if k != "metrics_json"}
    if not summary:
        d.pop("summary", None)
    try:
        d["metrics"] = json.loads(n["metrics_json"]) if n["metrics_json"] else {}
    except ValueError:
        d["metrics"] = {}
    return d


def _span(n: sqlite3.Row) -> str:
    a, b = _date(n["first_ts"]), _date(n["last_ts"])
    return a if a == b else "%s..%s" % (a, b)


def _node_line(n: sqlite3.Row, width: int = 90) -> str:
    return "%-14s %-8s %-23s %-8s %4d src  %s" % (n["id"], n["level"], _span(n), n["status"], n["n_sources"],
                                                _one_line(n["summary"], width))


def _resolve_node(conn: sqlite3.Connection, words: List[str]) -> sqlite3.Row:
    words = " ".join(words).split()              # "window 32" given as one quoted token works too
    m = WINDOW_REF_RE.fullmatch(" ".join(words))
    if m:
        words = ["window", m.group(1)]
    if not words:
        raise SystemExit("usage: embers describe <session-id | window N | thread ID | node ID>")
    kind, val = (None, words[0]) if len(words) == 1 else (words[0], words[1])
    if len(words) > 2 or (kind is not None and kind not in ("window", "session", "thread", "node")):
        raise SystemExit("usage: embers describe <session-id | window N | thread ID | node ID>")

    def by_node() -> Optional[sqlite3.Row]:
        rows = conn.execute("SELECT * FROM nodes WHERE id = ? OR id LIKE ? ORDER BY id", (val, val + "%")).fetchall()
        exact = [r for r in rows if r["id"] == val]
        if exact or len(rows) == 1:
            return (exact or rows)[0]
        if rows:
            raise SystemExit("ambiguous node id %r: %s" % (val, ", ".join(r["id"] for r in rows[:8])))
        return None

    def by_session() -> Optional[sqlite3.Row]:
        s = _resolve_session(conn, val)
        return conn.execute("SELECT * FROM nodes WHERE id = ?", (dag.node_id("S", s["session_id"]),)).fetchone() if s else None

    def by_thread() -> Optional[sqlite3.Row]:
        rows = conn.execute("SELECT thread_id FROM threads WHERE thread_id = ? OR thread_id LIKE ?", (val, val + "%")).fetchall()
        if len(rows) > 1 and not any(r[0] == val for r in rows):
            raise SystemExit("ambiguous thread id %r" % val)
        return dag.thread_level_node(conn, val if any(r[0] == val for r in rows) else rows[0][0]) if rows else None

    if kind == "node":
        order = [by_node]
    elif kind in ("window", "session"):
        order = [by_session]
    elif kind == "thread":
        order = [by_thread]
    else:
        order = [by_node, by_session, by_thread] if not val.isdigit() else [by_session, by_node]
    for fn in order:
        n = fn()
        if n is not None:
            return n
    raise SystemExit("no summary node for %s (run `embers build`?)" % " ".join(words))


def cmd_describe(conn: sqlite3.Connection, a: argparse.Namespace) -> int:
    if not _need_nodes(conn):
        return 1
    n = _resolve_node(conn, a.target)
    kids = children_of(conn, n["id"])
    meta = json.loads(n["metrics_json"] or "{}")
    session = n["session_id"]
    win = _window_of(conn, session) if session else None
    leaf_msgs: List[sqlite3.Row] = []
    if n["level"] == "leaf":
        leaf_msgs = conn.execute(
            """SELECT m.uuid, m.ts, b.text FROM node_sources ns JOIN messages m ON m.uuid = ns.uuid
               JOIN blocks b ON b.uuid = m.uuid AND b.class = 'human' AND m.is_human_prompt = 1
               WHERE ns.node_id = ? ORDER BY COALESCE(m.ts, ''), m.line_no, b.block_index LIMIT 10""", (n["id"],)).fetchall()
    if a.json:
        _emit({"node": _node_dict(n), "window": win})
        for k in kids:
            _emit({"child": _node_dict(k, summary=True)})
        for m in leaf_msgs:
            _emit({"prompt": {"uuid": m["uuid"], "ts": m["ts"], "text": _one_line(m["text"], 200)}})
        return 0
    print("%s  %s  %s  %s  %d source%s (%d bytes)  model=%s backend=%s%s" % (
        n["id"], n["level"], _date(n["first_ts"], True) + " -> " + _date(n["last_ts"], True), n["status"],
        n["n_sources"], "" if n["n_sources"] == 1 else "s", n["source_bytes"], n["model"] or "-", n["backend"] or "-",
        ("  [thread: this session is the whole thread]" if meta.get("thread_double") else "")
        + ("  kind=%s" % meta["kind"] if meta.get("kind") else "")))
    print("  thread %s%s%s" % ((n["thread_id"] or "-")[:8], "  session %s" % session[:8] if session else "",
                              "  window w%s" % win if win is not None else ""))
    print()
    print(n["summary"] if n["summary"] else "(no summary yet: status %s)" % n["status"])
    print()
    if kids:
        print("children:")
        for k in kids:
            print("  " + _node_line(k))
    elif leaf_msgs:
        print("prompts in this leaf (embers expand %s for the raw messages):" % n["id"])
        for m in leaf_msgs:
            print("  %s  %s" % (_date(m["ts"], True)[11:], _one_line(m["text"], 100)))
    return 0


def cmd_expand(conn: sqlite3.Connection, a: argparse.Namespace) -> int:
    if not _need_nodes(conn):
        return 1
    n = _resolve_node(conn, ["node", a.node])
    if n["level"] != "leaf":
        kids = children_of(conn, n["id"])
        if a.json:
            for k in kids:
                _emit(_node_dict(k))
            return 0
        print("%s (%s) -> %d child%s" % (n["id"], n["level"], len(kids), "" if len(kids) == 1 else "ren"))
        for k in kids:
            print()
            print(_node_line(k, width=60))
            body = k["summary"] if k["summary"] else "(no summary yet)"
            print("\n".join("    " + ln for ln in body.splitlines()) if a.full else "    " + _one_line(body, 300))
        return 0
    rows = conn.execute(
        """SELECT b.* FROM node_sources ns JOIN blocks b ON b.uuid = ns.uuid WHERE ns.node_id = ?
           ORDER BY COALESCE(b.ts, ''), b.line_no, b.block_index""", (n["id"],)).fetchall()
    if a.json:
        _emit({"node": _node_dict(n, summary=False)})
        for b in rows:
            _emit(_block_dict(b, full=True))
        return 0
    print("leaf %s  %s -> %s  %d messages, %d blocks" % (n["id"], _date(n["first_ts"], True), _date(n["last_ts"], True),
                                                         n["n_sources"], len(rows)))
    print()
    for b in rows:
        print(_block_line(b, full=a.full))
    return 0


def cmd_grep(conn: sqlite3.Connection, a: argparse.Namespace) -> int:
    if not _need_nodes(conn):
        return 1
    where = ""
    params: List[Any] = []
    if a.level:
        levels = [x.strip() for x in a.level.split(",") if x.strip()]
        for lv in levels:
            if lv not in dag.LEVELS:
                raise SystemExit("unknown level %r (choose from %s)" % (lv, ", ".join(dag.LEVELS)))
        where = " AND n.level IN (%s)" % ",".join("?" * len(levels))
        params += levels
    sql = """SELECT n.*, snippet(nodes_fts, 1, '[', ']', '…', 16) AS snippet, bm25(nodes_fts) AS score
             FROM nodes_fts JOIN nodes n ON n.id = nodes_fts.node_id
             WHERE nodes_fts MATCH ? AND n.status = 'ok'%s ORDER BY score LIMIT ?""" % where
    rows = _fts_query(conn, sql, a.query, params + [a.limit])
    for i, h in enumerate(rows, start=1):
        if a.json:
            d = _node_dict(h, summary=False)
            d.update(rank=i, score=h["score"], snippet=h["snippet"], span=_span(h))
            _emit(d)
        else:
            print("%-14s %-8s %-23s %s" % (h["id"], h["level"], _span(h), _one_line(h["snippet"], 150)))
    if not rows and not a.json:
        print("no hits")
    return 0 if rows else 1


def _explain(report: Dict[str, Any]) -> List[str]:
    out = []
    for name, c in (report.get("checks") or {}).items():
        d = c.get("detail", {})
        if c.get("passed"):
            continue
        if name == "ratio":
            out.append("ratio %.3f outside [%s, %s] (%s, %s)" % (
                c["score"], "%.3f" % d["low"] if "low" in d else "-", "%.3f" % d["high"] if "high" in d else "-",
                d.get("reason", "?"), d.get("source", "")))
        elif name == "evasion":
            out.append("evasion markers: " + ", ".join(repr(h) for h in d.get("hits", [])))
        elif name == "coverage":
            out.append("coverage %.2f < %.2f of %d nouns, missing: %s" % (
                c["score"], d.get("threshold", 0), d.get("probed", 0), ", ".join(d.get("missing", [])[:8])))
    return out


def cmd_flags(conn: sqlite3.Connection, a: argparse.Namespace) -> int:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'nodes'").fetchone() is None:
        sys.stderr.write("no summaries yet: run `embers build` first\n")
        return 1
    statuses = [a.status] if a.status else ["flagged", "manual"]
    rows = conn.execute("SELECT * FROM nodes WHERE status IN (%s) ORDER BY status, level, COALESCE(first_ts, ''), id" %
                        ",".join("?" * len(statuses)), statuses).fetchall()
    if a.export:
        return _export_flags(conn, rows, a)
    for n in rows:
        meta = json.loads(n["metrics_json"] or "{}")
        report = meta.get("report") or {}
        if a.json:
            d = _node_dict(n, summary=False)
            d["failed"] = report.get("failed", [])
            _emit(d)
            continue
        print("%-14s %-8s %-8s attempts=%d  %s  sensitivity=%s  thresholds=%s" % (
            n["id"], n["level"], n["status"], n["attempts"], _span(n), meta.get("sensitivity", "normal"),
            report.get("thresholds", "-")))
        for line in _explain(report) or ["(no metrics recorded)"]:
            print("    " + line)
        if n["summary"]:
            print("    last output: " + _one_line(n["summary"], 120))
    if not rows and not a.json:
        print("no flagged or manual nodes")
    return 0


def _export_flags(conn: sqlite3.Connection, rows: List[sqlite3.Row], a: argparse.Namespace) -> int:
    """One <node-id>.md per node: span, metrics and the raw pack text, so a summary can be written with the
    source in front of you. Write the summary to <node-id>.summary.md and load it with `embers manual --import DIR`."""
    os.makedirs(a.export, exist_ok=True)
    for n in rows:
        meta = json.loads(n["metrics_json"] or "{}")
        report = meta.get("report") or {}
        pack = make_pack(conn, n)
        lines = ["# %s  (%s, %s)" % (n["id"], n["level"], n["status"]), "",
                 "- span: %s" % _span(n), "- thread: %s  session: %s" % (n["thread_id"] or "-", n["session_id"] or "-"),
                 "- sources: %d (%d bytes)  attempts: %d  sensitivity: %s" % (
                     n["n_sources"], n["source_bytes"], n["attempts"], meta.get("sensitivity", "normal")), "",
                 "## Why it was flagged", ""]
        lines += ["- " + l for l in (_explain(report) or ["(no metrics recorded)"])]
        lines += ["", "## Metrics", "", "```json",
                  json.dumps({"report": report, "history": meta.get("history", []), "usage": meta.get("usage")},
                             indent=2, sort_keys=True), "```", "", "## Last output (rejected)", "",
                  n["summary"] or "(none)", "", "## Source (what the summarizer was shown)", "", "```text",
                  pack.text if pack is not None else "(parts not built yet: " + ", ".join(
                      "%s %s" % (k["id"], k["status"]) for k in children_of(conn, n["id"])) + ")", "```", "",
                 "Write the summary to %s.summary.md in this directory, then run: embers manual --import %s" % (n["id"], a.export), ""]
        with open(os.path.join(a.export, n["id"] + ".md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    if a.json:
        _emit({"exported": len(rows), "dir": a.export})
    else:
        print("exported %d node(s) to %s" % (len(rows), a.export))
    return 0


def _find_node(conn: sqlite3.Connection, ident: str) -> sqlite3.Row:
    rows = conn.execute("SELECT * FROM nodes WHERE id = ? OR id LIKE ? ORDER BY id", (ident, ident + "%")).fetchall()
    exact = [r for r in rows if r["id"] == ident]
    if exact:
        return exact[0]
    if len(rows) == 1:
        return rows[0]
    if rows:
        raise SystemExit("ambiguous node id %r: %s" % (ident, ", ".join(r["id"] for r in rows[:8])))
    raise SystemExit("no such node: %s" % ident)


def cmd_manual(conn: sqlite3.Connection, a: argparse.Namespace) -> int:
    """Enter an operator-written summary for a node (usually a `manual` one)."""
    if not _need_nodes(conn):
        return 1
    jobs = []     # (node, text)
    if a.import_dir:
        if a.node or a.text is not None or a.file or a.stdin:
            raise SystemExit("--import DIR takes no node id and no --text/--file/--stdin")
        if not os.path.isdir(a.import_dir):
            raise SystemExit("not a directory: %s" % a.import_dir)
        for fn in sorted(os.listdir(a.import_dir)):
            if not fn.endswith(".summary.md"):
                continue
            with open(os.path.join(a.import_dir, fn), "r", encoding="utf-8") as f:
                text = f.read().strip()
            if not text:
                sys.stderr.write("skipped %s: empty\n" % fn)
                continue
            jobs.append((_find_node(conn, fn[: -len(".summary.md")]), text))
    else:
        if not a.node:
            raise SystemExit("usage: embers manual <node ID> (--text TEXT | --file PATH | --stdin)   or   embers manual --import DIR")
        given = [x for x in (a.text is not None, bool(a.file), a.stdin) if x]
        if len(given) != 1:
            raise SystemExit("give exactly one of --text, --file, --stdin")
        if a.text is not None:
            text = a.text
        elif a.file:
            with open(a.file, "r", encoding="utf-8") as f:
                text = f.read()
        else:
            text = sys.stdin.read()
        if not text.strip():
            raise SystemExit("the summary is empty")
        jobs.append((_find_node(conn, a.node), text))
    for node, text in jobs:
        set_manual_summary(conn, node, text)
        if a.json:
            _emit({"node": node["id"], "status": "ok", "backend": "manual", "model": "human"})
        else:
            print("%s: stored manual summary (%d bytes); was %s" % (node["id"], len(text.strip().encode("utf-8")), node["status"]))
    conn.commit()
    if jobs and not a.json:
        print("run `embers build` to rebuild the nodes above these that were built over a placeholder")
    return 0


def cmd_build(conn: sqlite3.Connection, a: argparse.Namespace) -> int:
    levels = list(dag.LEVELS) if a.all or a.level == "all" else [a.level]
    budget = a.budget_usd if a.budget_usd is not None else (float(os.environ["MEM_BUDGET_USD"]) if os.environ.get("MEM_BUDGET_USD") else None)
    backend = a.backend or os.environ.get("MEM_BACKEND") or "stub"
    rerun = {k: v for k, v in (("leaf", a.rerun_model_leaf), ("upper", a.rerun_model_upper)) if v}
    summarizer = make_summarizer(backend, budget_usd=budget, rerun_models=rerun or None)
    workers = a.workers if a.workers is not None else int(os.environ.get("MEM_WORKERS") or 1)
    if workers < 1:
        raise SystemExit("--workers must be at least 1")
    if a.reset_backend:
        ids = [r[0] for r in conn.execute("SELECT id FROM nodes WHERE backend = ?", (a.reset_backend,))]
        if not a.dry_run:
            for nid in ids:
                dag.reset_pending(conn, nid)
            conn.commit()
        sys.stderr.write("reset %d node(s) built by backend %r to pending%s\n" % (len(ids), a.reset_backend,
                         " (dry run: not written)" if a.dry_run else ""))
    scope = None
    if a.thread:
        rows = conn.execute("SELECT thread_id FROM threads WHERE thread_id = ? OR thread_id LIKE ?", (a.thread, a.thread + "%")).fetchall()
        if not rows:
            raise SystemExit("no such thread: %s" % a.thread)
        scope = [r[0] for r in rows]
    chunk = a.chunk_bytes or int(os.environ.get("MEM_CHUNK_BYTES", dag.DEFAULT_CHUNK_BYTES))
    log = (lambda m: sys.stderr.write(m + "\n")) if a.verbose else (lambda m: None)
    res = run_build(conn, summarizer, BuildOptions(levels=levels, thread_ids=scope, chunk_bytes=chunk, limit=a.limit,
                                                   dry_run=a.dry_run, workers=workers), log=log)
    if a.json:
        _emit(res)
    elif a.dry_run:
        print("dry run (backend %s): nothing written" % backend)
        if scope is not None and "month" in levels:
            print("  (month level skipped when --thread is given)")
        print("%-8s %6s %8s %12s %12s %10s" % ("level", "nodes", "pending", "source bytes", "est. tokens", "est. $"))
        for lv, p in res["plan"].items():
            print("%-8s %6d %8d %12d %12d %10s" % (lv, p["nodes"], p["pending"], p["source_bytes"], p["est_input_tokens"],
                                                  ("%.4f" % p["est_cost_usd"]) if backend == "anthropic" else "-"))
    else:
        print("build (backend %s, %d worker%s): %d accepted, %d manual%s" % (
            backend, workers, "" if workers == 1 else "s", res["built"], res["manual"],
                                                              ", %d accepted after a rerun" % res["flagged_then_ok"] if res["flagged_then_ok"] else ""))
        for lv, c in res["levels"].items():
            print("  %-8s ok %d  manual %d  not ready %d" % (lv, c["ok"], c["manual"], c["not_ready"]))
        if res["manual"]:
            print("%d node(s) need manual attention: see `embers flags`" % res["manual"])
        if res["stopped"] == "budget":
            print("STOPPED: %s" % res.get("stop_reason"))
        elif res["stopped"] == "limit":
            print("stopped at --limit; run again to continue")
        if backend == "anthropic":
            print("spent $%.4f" % res["spent_usd"])
    return 3 if res["stopped"] == "budget" else 0


# ----------------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="embers", description="Embers: lossless memory for Claude Code. Search Claude Code transcripts locally.")
    ap.add_argument("--db", default=None, help="SQLite path (default $MEM_DB or ~/.memory-archive/mem.db)")
    ap.add_argument("--json", action="store_true", help="machine-readable output, one JSON object per line")
    ap.add_argument("--version", action="version", version="embers " + __version__)
    sub = ap.add_subparsers(dest="cmd", metavar="command")
    sub.required = True

    p = sub.add_parser("ingest", help="ingest one or more transcript roots (incremental)")
    p.add_argument("roots", nargs="+", metavar="root")
    p.add_argument("--index", help="sessions-index directory (per-session metadata JSONs) for the window map")
    p.add_argument("--rebuild", action="store_true", help="re-parse every file even if unchanged")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("search", help="full-text search (FTS5 syntax; falls back to a phrase)")
    p.add_argument("query")
    p.add_argument("--class", dest="cls", help="comma-separated: %s" % ",".join(CLASSES))
    p.add_argument("--window", type=int, help="only this context-window number")
    p.add_argument("--since", help="YYYY-MM-DD (UTC)")
    p.add_argument("--until", help="YYYY-MM-DD (UTC)")
    p.add_argument("--session", help="only this session id (or prefix)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--context", type=int, default=3, help="lines of context before/after each hit")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("session", help="show a session (id or prefix, window number, or agent-<id>)")
    p.add_argument("id")
    p.add_argument("--around", type=int, metavar="HIT", help="block id from `embers search`; show context around it")
    p.add_argument("--context", type=int, default=20, help="blocks before/after with --around")
    p.add_argument("--thread", action="store_true", help="show the whole logical thread (all stitched files)")
    p.add_argument("--class", dest="cls")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--full", action="store_true", help="print full block text instead of one line each")
    p.set_defaults(fn=cmd_session)

    p = sub.add_parser("sessions", help="list sessions")
    p.add_argument("--since")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--subagents", action="store_true", help="include subagent sessions")
    p.set_defaults(fn=cmd_sessions)

    p = sub.add_parser("windows", help="human-numbered window map from the sessions index")
    p.add_argument("--index", help="sessions-index directory to (re)load first")
    p.set_defaults(fn=cmd_windows)

    p = sub.add_parser("stats", help="archive statistics")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("build", help="build the summary DAG (Layer D); idempotent")
    p.add_argument("--reset-backend", metavar="NAME",
                   help="first reset every node built by this backend (e.g. stub) to pending so this run replaces it")
    p.add_argument("--level", choices=list(dag.LEVELS) + ["all"], default="all")
    p.add_argument("--all", action="store_true", help="same as --level all")
    p.add_argument("--thread", help="only this thread id (or prefix); skips the month level")
    p.add_argument("--backend", choices=["stub", "anthropic"], help="default $MEM_BACKEND or stub")
    p.add_argument("--budget-usd", type=float, help="hard spend cap for the anthropic backend (or $MEM_BUDGET_USD)")
    p.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    p.add_argument("--limit", type=int, default=0, help="build at most N nodes this run")
    p.add_argument("--chunk-bytes", type=int, help="leaf prose budget in bytes (default 24576 or $MEM_CHUNK_BYTES)")
    p.add_argument("--workers", type=int, help="summarizer calls in flight at once within a level (default $MEM_WORKERS or 1)")
    p.add_argument("--rerun-model-leaf", metavar="MODEL", help="model for a leaf rerun (default $MEM_MODEL_RERUN_LEAF or the mid model)")
    p.add_argument("--rerun-model-upper", metavar="MODEL", help="model for a session/thread/month rerun (default $MEM_MODEL_RERUN_UPPER or the mid model)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("describe", help="show a node's summary and its children (session id, window N, thread ID, node ID)")
    p.add_argument("target", nargs="+", metavar="target")
    p.set_defaults(fn=cmd_describe)

    p = sub.add_parser("expand", help="descend one level from a node (raw messages at a leaf)")
    p.add_argument("node")
    p.add_argument("--full", action="store_true")
    p.set_defaults(fn=cmd_expand)

    p = sub.add_parser("grep", help="full-text search over accepted summaries")
    p.add_argument("query")
    p.add_argument("--level", help="comma-separated: %s" % ",".join(dag.LEVELS))
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_grep)

    p = sub.add_parser("flags", help="list flagged / manual nodes with their detector metrics")
    p.add_argument("--status", choices=["flagged", "manual"])
    p.add_argument("--export", metavar="DIR", help="write one <node-id>.md per node (span, metrics, raw pack text) into DIR")
    p.set_defaults(fn=cmd_flags)

    p = sub.add_parser("manual", help="store an operator-written summary for a node (or --import DIR of <node-id>.summary.md)")
    p.add_argument("node", nargs="?", help="node id (or unique prefix)")
    p.add_argument("--text", help="the summary text")
    p.add_argument("--file", help="read the summary from this file")
    p.add_argument("--stdin", action="store_true", help="read the summary from standard input")
    p.add_argument("--import", dest="import_dir", metavar="DIR", help="import every <node-id>.summary.md in DIR")
    p.set_defaults(fn=cmd_manual)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    a.db = a.db or default_db_path()
    if a.cmd != "ingest" and not os.path.exists(a.db) and a.db != ":memory:":
        sys.stderr.write("no archive at %s (run `embers ingest <root>...` first, or set --db/$MEM_DB)\n" % a.db)
        return 2
    conn = open_db(a.db)
    try:
        return a.fn(conn, a)
    except BrokenPipeError:
        # `embers … | head` closed the pipe: exit quietly like a well-behaved unix tool
        try:
            sys.stdout.close()
        except Exception:
            pass
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
