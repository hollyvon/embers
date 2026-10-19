"""Incremental ingester: JSONL transcript roots -> SQLite (+FTS5).

Tables: files, sessions, threads, messages, message_owners, blocks, blocks_fts,
session_meta, windows, meta.  See docs in README.md.

Guarantees:
  * sources are opened read-only and never modified;
  * incremental: a file whose (mtime, size) is unchanged is skipped; a file that
    only grew is parsed from the byte offset after its last complete line;
    anything else is re-parsed in full (its old rows are replaced);
  * tolerant: unknown record types/keys are kept or counted, unparseable lines are
    counted and skipped, a partial last line is retried on the next run;
  * stitching (P0 §d): logical thread = connected component of files sharing message
    uuids; messages deduped by uuid with the canonical owner = the owner file with the
    earliest first timestamp (ties: earliest last timestamp, then path);
    `logicalParentUuid` is stored so chains can be followed across compact_boundary.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .records import (META_LAST_WINS, Block, agent_id_from_tool_use_result, extract_blocks, is_human_prompt,
                      opener_kind, record_text_of_user_content)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    root TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    projdir TEXT,
    session_id TEXT NOT NULL,
    is_subagent INTEGER NOT NULL DEFAULT 0,
    parent_session_id TEXT,
    agent_id TEXT,
    mtime REAL,
    size INTEGER,
    parsed_bytes INTEGER NOT NULL DEFAULT 0,
    line_count INTEGER NOT NULL DEFAULT 0,
    bad_lines INTEGER NOT NULL DEFAULT 0,
    dup_lines INTEGER NOT NULL DEFAULT 0,
    partial_tail INTEGER NOT NULL DEFAULT 0,
    first_ts TEXT,
    last_ts TEXT,
    type_counts TEXT,
    ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    file_id INTEGER,
    root TEXT,
    projdir TEXT,
    rel_path TEXT,
    is_subagent INTEGER NOT NULL DEFAULT 0,
    parent_session_id TEXT,
    agent_id TEXT,
    thread_id TEXT,
    title TEXT,
    cwd TEXT,
    git_branch TEXT,
    version TEXT,
    slug TEXT,
    first_ts TEXT,
    last_ts TEXT,
    opener TEXT,
    n_messages INTEGER NOT NULL DEFAULT 0,
    n_human INTEGER NOT NULL DEFAULT 0,
    n_compactions INTEGER NOT NULL DEFAULT 0,
    n_lines INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    n_sessions INTEGER NOT NULL DEFAULT 0,
    n_messages INTEGER NOT NULL DEFAULT 0,
    first_ts TEXT,
    last_ts TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    uuid TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT,
    line_no INTEGER NOT NULL,
    type TEXT,
    role TEXT,
    ts TEXT,
    parent_uuid TEXT,
    logical_parent_uuid TEXT,
    message_id TEXT,
    request_id TEXT,
    model TEXT,
    stop_reason TEXT,
    subtype TEXT,
    is_sidechain INTEGER NOT NULL DEFAULT 0,
    agent_id TEXT,
    is_meta INTEGER NOT NULL DEFAULT 0,
    is_compact_summary INTEGER NOT NULL DEFAULT 0,
    origin_kind TEXT,
    origin_subkind TEXT,
    is_human_prompt INTEGER NOT NULL DEFAULT 0,
    source_tool_assistant_uuid TEXT,
    record_session_id TEXT,
    version TEXT,
    n_owners INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS messages_session_order ON messages(session_id, ts, line_no);
CREATE INDEX IF NOT EXISTS messages_thread_order ON messages(thread_id, ts, line_no);
CREATE INDEX IF NOT EXISTS messages_message_id ON messages(message_id);
CREATE TABLE IF NOT EXISTS message_owners (
    uuid TEXT NOT NULL,
    session_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    PRIMARY KEY (uuid, session_id)
);
CREATE INDEX IF NOT EXISTS message_owners_session ON message_owners(session_id);
CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL,
    session_id TEXT NOT NULL,
    thread_id TEXT,
    block_index INTEGER NOT NULL,
    class TEXT NOT NULL,
    block_type TEXT,
    ts TEXT,
    line_no INTEGER,
    text TEXT NOT NULL DEFAULT '',
    nbytes INTEGER NOT NULL DEFAULT 0,
    tool_name TEXT,
    tool_use_id TEXT,
    is_error INTEGER,
    skipped_kind TEXT,
    skipped_bytes INTEGER NOT NULL DEFAULT 0,
    pointer TEXT
);
CREATE INDEX IF NOT EXISTS blocks_uuid ON blocks(uuid);
CREATE INDEX IF NOT EXISTS blocks_session_order ON blocks(session_id, ts, line_no, block_index);
CREATE INDEX IF NOT EXISTS blocks_class ON blocks(class);
CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(text, content='blocks', content_rowid='id', tokenize='unicode61');
CREATE TABLE IF NOT EXISTS session_meta (
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (session_id, key)
);
CREATE TABLE IF NOT EXISTS windows (
    session_id TEXT PRIMARY KEY,
    window INTEGER,
    title TEXT,
    created_at TEXT,
    cwd TEXT,
    index_path TEXT
);
"""

WINDOW_RE = re.compile(r"^\s*(?:\[(\d+)\]|Context window #(\d+))", re.I)


def parse_window(title: Optional[str]) -> Optional[int]:
    """`[28] …` or `Context window #28 …` -> 28; anything else -> None."""
    if not title:
        return None
    m = WINDOW_RE.match(title)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def default_db_path() -> str:
    return os.environ.get("MEM_DB") or os.path.join(os.path.expanduser("~"), ".memory-archive", "mem.db")


def open_db(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


# ----------------------------------------------------------------------------- file discovery

class FileInfo:
    __slots__ = ("path", "root", "rel_path", "projdir", "session_id", "is_subagent", "parent_session_id", "agent_id")

    def __init__(self, path: str, root: str):
        self.path = path
        self.root = root
        rel = os.path.relpath(path, root)
        self.rel_path = rel.replace(os.sep, "/")
        parts = self.rel_path.split("/")
        stem = parts[-1][:-len(".jsonl")]
        self.projdir = parts[0] if len(parts) > 1 else ""
        if len(parts) >= 4 and parts[-2] == "subagents":
            self.is_subagent = True
            self.parent_session_id = parts[-3]
            self.agent_id = stem[len("agent-"):] if stem.startswith("agent-") else stem
            self.session_id = "agent-" + self.agent_id
        else:
            self.is_subagent = False
            self.parent_session_id = None
            self.agent_id = None
            self.session_id = stem


def discover(roots: Iterable[str]) -> List[FileInfo]:
    out: List[FileInfo] = []
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            raise FileNotFoundError("root is not a directory: %s" % root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for fn in sorted(filenames):
                if fn.endswith(".jsonl"):
                    out.append(FileInfo(os.path.join(dirpath, fn), root))
    return out


# ----------------------------------------------------------------------------- ingester

class Stats(dict):
    def bump(self, key: str, n: int = 1) -> None:
        self[key] = self.get(key, 0) + n


class Ingester:
    def __init__(self, conn: sqlite3.Connection, verbose: bool = False, log=None):
        self.conn = conn
        self.verbose = verbose
        self.log = log or (lambda s: None)

    # ---- public ---------------------------------------------------------------------
    def ingest(self, roots: Iterable[str], index_dir: Optional[str] = None, rebuild: bool = False) -> Stats:
        stats = Stats()
        t0 = time.time()
        files = discover(roots)
        stats["files_seen"] = len(files)
        for fi in files:
            self._ingest_file(fi, stats, rebuild)
        if index_dir:
            stats["index_entries"] = self.load_index(index_dir)
        self.stitch(stats)
        stats["seconds"] = round(time.time() - t0, 3)
        return stats

    def load_index(self, index_dir: str) -> int:
        n = 0
        for fn in sorted(os.listdir(index_dir)):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(index_dir, fn)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(d, dict):
                continue
            sid = d.get("cliSessionId") or fn[:-5]
            title = d.get("title") if isinstance(d.get("title"), str) else None
            self.conn.execute(
                "INSERT OR REPLACE INTO windows(session_id, window, title, created_at, cwd, index_path) VALUES (?,?,?,?,?,?)",
                (sid, parse_window(title), title, d.get("createdAt"), d.get("cwd"), p))
            n += 1
        self.conn.commit()
        return n

    # ---- per file ------------------------------------------------------------------
    def _ingest_file(self, fi: FileInfo, stats: Stats, rebuild: bool) -> None:
        conn = self.conn
        st = os.stat(fi.path)
        row = conn.execute("SELECT * FROM files WHERE path = ?", (fi.path,)).fetchone()
        mode = "full"
        start = 0
        line_no = 0
        prev = None
        if row is not None and not rebuild:
            if row["mtime"] == st.st_mtime and row["size"] == st.st_size and not row["partial_tail"]:
                stats.bump("files_unchanged")
                return
            if st.st_size >= row["size"] and row["parsed_bytes"] <= st.st_size and self._prefix_intact(fi.path, row):
                # unchanged size but a partial tail last time: just retry the tail
                mode = "tail_retry" if (st.st_size == row["size"] and row["partial_tail"]) else "append"
                start = row["parsed_bytes"]
                line_no = row["line_count"]
                prev = row
        if mode == "full" and row is not None:
            self._purge_file(fi, row)
        stats.bump("files_" + mode)
        self.log("%s %s" % (mode, fi.rel_path))
        conn.execute("BEGIN")
        try:
            r = self._parse(fi, start, line_no, prev, stats)
            type_counts = r["type_counts"]
            if prev is not None and prev["type_counts"]:
                old = json.loads(prev["type_counts"])
                for k, v in old.items():
                    type_counts[k] = type_counts.get(k, 0) + v
            first_ts = r["first_ts"] if prev is None or not prev["first_ts"] else prev["first_ts"]
            last_ts = r["last_ts"] or (prev["last_ts"] if prev is not None else None)
            conn.execute(
                """INSERT INTO files(path, root, rel_path, projdir, session_id, is_subagent, parent_session_id, agent_id,
                       mtime, size, parsed_bytes, line_count, bad_lines, dup_lines, partial_tail, first_ts, last_ts,
                       type_counts, ingested_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size,
                       parsed_bytes=excluded.parsed_bytes, line_count=excluded.line_count,
                       bad_lines=excluded.bad_lines, dup_lines=excluded.dup_lines, partial_tail=excluded.partial_tail,
                       first_ts=excluded.first_ts, last_ts=excluded.last_ts, type_counts=excluded.type_counts,
                       ingested_at=excluded.ingested_at""",
                (fi.path, fi.root, fi.rel_path, fi.projdir, fi.session_id, int(fi.is_subagent), fi.parent_session_id,
                 fi.agent_id, st.st_mtime, st.st_size, r["parsed_bytes"], r["line_count"],
                 r["bad_lines"] + (prev["bad_lines"] if prev is not None else 0),
                 r["dup_lines"] + (prev["dup_lines"] if prev is not None else 0), int(r["partial_tail"]),
                 first_ts, last_ts, json.dumps(type_counts, sort_keys=True), now_iso()))
            file_id = conn.execute("SELECT id FROM files WHERE path = ?", (fi.path,)).fetchone()[0]
            self._upsert_session(fi, file_id, r, first_ts, last_ts, prev)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        stats.bump("lines_parsed", r["line_count"] - line_no)
        stats.bump("bad_lines", r["bad_lines"])
        stats.bump("messages_new", r["messages_new"])
        stats.bump("messages_dup", r["messages_dup"])
        stats.bump("blocks_new", r["blocks_new"])

    @staticmethod
    def _prefix_intact(path: str, row: sqlite3.Row) -> bool:
        """Cheap append-only check: the byte before the watermark is still a newline."""
        pb = row["parsed_bytes"]
        if pb == 0:
            return True
        try:
            with open(path, "rb") as f:
                f.seek(pb - 1)
                return f.read(1) == b"\n"
        except OSError:
            return False

    def _purge_file(self, fi: FileInfo, row: sqlite3.Row) -> None:
        conn = self.conn
        sid = fi.session_id
        conn.execute("BEGIN")
        uuids = [r[0] for r in conn.execute("SELECT uuid FROM message_owners WHERE session_id = ?", (sid,))]
        conn.execute("DELETE FROM message_owners WHERE session_id = ?", (sid,))
        for u in uuids:
            others = conn.execute("SELECT session_id, line_no FROM message_owners WHERE uuid = ? ORDER BY session_id",
                                  (u,)).fetchall()
            if not others:
                for b in conn.execute("SELECT id, text FROM blocks WHERE uuid = ?", (u,)).fetchall():
                    conn.execute("INSERT INTO blocks_fts(blocks_fts, rowid, text) VALUES ('delete', ?, ?)",
                                 (b["id"], b["text"]))
                conn.execute("DELETE FROM blocks WHERE uuid = ?", (u,))
                conn.execute("DELETE FROM messages WHERE uuid = ?", (u,))
            else:
                m = conn.execute("SELECT session_id FROM messages WHERE uuid = ?", (u,)).fetchone()
                if m is not None and m[0] == sid:
                    conn.execute("UPDATE messages SET session_id = ?, line_no = ?, n_owners = ? WHERE uuid = ?",
                                 (others[0][0], others[0][1], len(others), u))
                    conn.execute("UPDATE blocks SET session_id = ?, line_no = ? WHERE uuid = ?",
                                 (others[0][0], others[0][1], u))
                else:
                    conn.execute("UPDATE messages SET n_owners = ? WHERE uuid = ?", (len(others), u))
        conn.execute("DELETE FROM session_meta WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM files WHERE id = ?", (row["id"],))
        conn.execute("COMMIT")

    def _parse(self, fi: FileInfo, start: int, line_no: int, prev: Optional[sqlite3.Row], stats: Stats) -> Dict[str, Any]:
        conn = self.conn
        r: Dict[str, Any] = {"type_counts": {}, "first_ts": None, "last_ts": None, "bad_lines": 0, "dup_lines": 0,
                             "partial_tail": False, "messages_new": 0, "messages_dup": 0, "blocks_new": 0,
                             "line_count": line_no, "parsed_bytes": start, "env": {}, "first_user_text": None,
                             "n_human": 0, "n_compact": 0, "tool_agent_ids": []}
        seen_in_file = set(x[0] for x in conn.execute(
            "SELECT uuid FROM message_owners WHERE session_id = ?", (fi.session_id,))) if start else set()
        offset = start
        with open(fi.path, "rb") as f:
            f.seek(start)
            for raw in f:
                n = len(raw)
                complete = raw.endswith(b"\n")
                try:
                    rec = json.loads(raw)
                except ValueError:
                    rec = None
                if rec is None or not isinstance(rec, dict):
                    if not complete:
                        # partial last line (file still being written, or truncated): retry next run
                        r["partial_tail"] = True
                        break
                    offset += n
                    r["bad_lines"] += 1
                    r["line_count"] += 1
                    continue
                offset += n
                r["line_count"] += 1
                r["parsed_bytes"] = offset
                self._handle(fi, rec, r["line_count"], r, seen_in_file)
        return r

    def _handle(self, fi: FileInfo, rec: Dict[str, Any], line_no: int, r: Dict[str, Any], seen: set) -> None:
        conn = self.conn
        t = rec.get("type")
        tkey = t if isinstance(t, str) else "?"
        r["type_counts"][tkey] = r["type_counts"].get(tkey, 0) + 1
        ts = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None
        if ts:
            if r["first_ts"] is None or ts < r["first_ts"]:
                r["first_ts"] = ts
            if r["last_ts"] is None or ts > r["last_ts"]:
                r["last_ts"] = ts
        for k in ("cwd", "gitBranch", "version", "slug"):
            if isinstance(rec.get(k), str):
                r["env"][k] = rec[k]
        # metadata records (no uuid): dedupe, keep last
        if tkey in META_LAST_WINS:
            pk, mk = META_LAST_WINS[tkey]
            v = rec.get(pk)
            if v is not None:
                conn.execute("INSERT OR REPLACE INTO session_meta(session_id, key, value) VALUES (?,?,?)",
                             (fi.session_id, mk, v if isinstance(v, str) else json.dumps(v)))
            return
        if tkey == "cost-state":
            conn.execute("INSERT OR REPLACE INTO session_meta(session_id, key, value) VALUES (?,?,?)",
                         (fi.session_id, "cost_usd", str(rec.get("totalCostUSD"))))
            return
        uuid = rec.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            if tkey == "summary":
                uuid = "%s:L%d" % (fi.session_id, line_no)   # legacy summary records carry no uuid
            else:
                return   # queue-operation, file-history-*, bridge-session, unknown: counted only
        if uuid in seen:
            r["dup_lines"] += 1
            return
        seen.add(uuid)
        conn.execute("INSERT OR IGNORE INTO message_owners(uuid, session_id, line_no) VALUES (?,?,?)",
                     (uuid, fi.session_id, line_no))
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        human = is_human_prompt(rec)
        if human:
            r["n_human"] += 1
        if tkey == "user" and r["first_user_text"] is None and not rec.get("isCompactSummary"):
            r["first_user_text"] = record_text_of_user_content(msg.get("content"))
        if rec.get("subtype") == "compact_boundary":
            r["n_compact"] += 1
        aid = agent_id_from_tool_use_result(rec)
        if aid:
            r["tool_agent_ids"].append(aid)
        existing = conn.execute("SELECT session_id FROM messages WHERE uuid = ?", (uuid,)).fetchone()
        if existing is not None:
            r["messages_dup"] += 1
            conn.execute("UPDATE messages SET n_owners = n_owners + 1 WHERE uuid = ?", (uuid,))
            return
        origin = rec.get("origin") if isinstance(rec.get("origin"), dict) else {}
        conn.execute(
            """INSERT INTO messages(uuid, session_id, line_no, type, role, ts, parent_uuid, logical_parent_uuid,
                   message_id, request_id, model, stop_reason, subtype, is_sidechain, agent_id, is_meta,
                   is_compact_summary, origin_kind, origin_subkind, is_human_prompt, source_tool_assistant_uuid,
                   record_session_id, version, n_owners)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
            (uuid, fi.session_id, line_no, tkey, msg.get("role") if isinstance(msg.get("role"), str) else None, ts,
             _str(rec.get("parentUuid")), _str(rec.get("logicalParentUuid")), _str(msg.get("id")),
             _str(rec.get("requestId")), _str(msg.get("model")), _str(msg.get("stop_reason")), _str(rec.get("subtype")),
             int(rec.get("isSidechain") is True), _str(rec.get("agentId")), int(rec.get("isMeta") is True),
             int(rec.get("isCompactSummary") is True), _str(origin.get("kind")), _str(origin.get("subkind")),
             int(human), _str(rec.get("sourceToolAssistantUUID")), _str(rec.get("sessionId")), _str(rec.get("version"))))
        r["messages_new"] += 1
        for i, b in enumerate(extract_blocks(rec)):
            self._insert_block(fi, uuid, line_no, ts, i, b)
            r["blocks_new"] += 1

    def _insert_block(self, fi: FileInfo, uuid: str, line_no: int, ts: Optional[str], i: int, b: Block) -> None:
        pointer = "%s:L%d:%s" % (fi.rel_path, line_no, b.jsonpath)
        cur = self.conn.execute(
            """INSERT INTO blocks(uuid, session_id, block_index, class, block_type, ts, line_no, text, nbytes, tool_name,
                   tool_use_id, is_error, skipped_kind, skipped_bytes, pointer)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (uuid, fi.session_id, i, b.cls, b.block_type, ts, line_no, b.text, len(b.text.encode("utf-8", "replace")),
             b.tool_name, b.tool_use_id, None if b.is_error is None else int(b.is_error), b.skipped_kind,
             b.skipped_bytes, pointer))
        if b.text:
            self.conn.execute("INSERT INTO blocks_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, b.text))

    def _upsert_session(self, fi: FileInfo, file_id: int, r: Dict[str, Any], first_ts: Optional[str],
                        last_ts: Optional[str], prev: Optional[sqlite3.Row]) -> None:
        conn = self.conn
        old = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (fi.session_id,)).fetchone()
        meta = {row["key"]: row["value"] for row in conn.execute(
            "SELECT key, value FROM session_meta WHERE session_id = ?", (fi.session_id,))}
        title = meta.get("custom_title") or meta.get("ai_title")
        env = r["env"]
        opener = old["opener"] if old is not None and old["opener"] not in (None, "unknown") else (
            "subagent" if fi.is_subagent else opener_kind(r["first_user_text"]))
        conn.execute(
            """INSERT INTO sessions(session_id, file_id, root, projdir, rel_path, is_subagent, parent_session_id, agent_id,
                   title, cwd, git_branch, version, slug, first_ts, last_ts, opener, n_human, n_compactions, n_lines)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET file_id=excluded.file_id, title=excluded.title,
                   cwd=COALESCE(excluded.cwd, sessions.cwd), git_branch=COALESCE(excluded.git_branch, sessions.git_branch),
                   version=COALESCE(excluded.version, sessions.version), slug=COALESCE(excluded.slug, sessions.slug),
                   first_ts=excluded.first_ts, last_ts=excluded.last_ts, opener=excluded.opener,
                   n_human=sessions.n_human + excluded.n_human, n_compactions=sessions.n_compactions + excluded.n_compactions,
                   n_lines=excluded.n_lines""",
            (fi.session_id, file_id, fi.root, fi.projdir, fi.rel_path, int(fi.is_subagent), fi.parent_session_id,
             fi.agent_id, title, env.get("cwd"), env.get("gitBranch"), env.get("version"), env.get("slug"),
             first_ts, last_ts, opener, r["n_human"], r["n_compact"], r["line_count"]))

    # ---- stitching ------------------------------------------------------------------
    def stitch(self, stats: Optional[Stats] = None) -> None:
        """Recompute canonical owners, threads and per-session counts over the whole DB."""
        conn = self.conn
        stats = stats if stats is not None else Stats()
        conn.execute("BEGIN")
        files = {row["session_id"]: row for row in conn.execute("SELECT * FROM files")}

        def rank(sid: str) -> Tuple[str, str, str]:
            f = files.get(sid)
            if f is None:
                return ("~", "~", sid)
            return (f["first_ts"] or "~", f["last_ts"] or "~", f["rel_path"])

        # 1. canonical owner per shared uuid (earliest-timestamp owner file)
        shared = conn.execute("SELECT uuid FROM message_owners GROUP BY uuid HAVING COUNT(*) > 1").fetchall()
        changed = 0
        for (u,) in shared:
            owners = conn.execute("SELECT session_id, line_no FROM message_owners WHERE uuid = ?", (u,)).fetchall()
            best = min(owners, key=lambda o: rank(o[0]))
            cur = conn.execute("SELECT session_id, line_no, n_owners FROM messages WHERE uuid = ?", (u,)).fetchone()
            if cur is None:
                continue
            if cur[0] != best[0] or cur[1] != best[1] or cur[2] != len(owners):
                conn.execute("UPDATE messages SET session_id = ?, line_no = ?, n_owners = ? WHERE uuid = ?",
                             (best[0], best[1], len(owners), u))
                conn.execute("UPDATE blocks SET session_id = ?, line_no = ? WHERE uuid = ?", (best[0], best[1], u))
                changed += 1
        stats["canonical_reassigned"] = changed
        # 2. connected components of files sharing uuids -> thread ids
        parent: Dict[str, str] = {}

        def find(x: str) -> str:
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                # root = smallest session id, never a subagent when a real session is in the thread
                key = lambda x: (x.startswith("agent-"), x)  # noqa: E731
                parent[max(ra, rb, key=key)] = min(ra, rb, key=key)

        for row in conn.execute("SELECT session_id, is_subagent, parent_session_id FROM sessions"):
            find(row[0])
            if row[1] and row[2]:
                union(row[0], row[2])
        for (u,) in shared:
            sids = [o[0] for o in conn.execute("SELECT session_id FROM message_owners WHERE uuid = ?", (u,))]
            for s in sids[1:]:
                union(sids[0], s)
        sess_rows = conn.execute("SELECT session_id, thread_id FROM sessions").fetchall()
        for sid, old_tid in sess_rows:
            tid = find(sid)
            if tid != old_tid:
                conn.execute("UPDATE sessions SET thread_id = ? WHERE session_id = ?", (tid, sid))
        conn.execute("""UPDATE messages SET thread_id = (SELECT thread_id FROM sessions WHERE sessions.session_id = messages.session_id)
                        WHERE thread_id IS NOT (SELECT thread_id FROM sessions WHERE sessions.session_id = messages.session_id)""")
        conn.execute("""UPDATE blocks SET thread_id = (SELECT thread_id FROM sessions WHERE sessions.session_id = blocks.session_id)
                        WHERE thread_id IS NOT (SELECT thread_id FROM sessions WHERE sessions.session_id = blocks.session_id)""")
        # 3. counts
        conn.execute("""UPDATE sessions SET n_messages = (SELECT COUNT(*) FROM messages WHERE messages.session_id = sessions.session_id)""")
        conn.execute("DELETE FROM threads")
        conn.execute("""INSERT INTO threads(thread_id, n_sessions, n_messages, first_ts, last_ts)
                        SELECT thread_id, SUM(is_subagent = 0), SUM(n_messages), MIN(first_ts), MAX(last_ts) FROM sessions
                        WHERE thread_id IS NOT NULL GROUP BY thread_id""")
        conn.execute("COMMIT")
        stats["threads"] = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        stats["sessions"] = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        stats["messages"] = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        stats["blocks"] = conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]


def _str(v: Any) -> Optional[str]:
    if v is None:
        return None
    return v if isinstance(v, str) else json.dumps(v, sort_keys=True)


def ingest(roots: Iterable[str], db_path: Optional[str] = None, index_dir: Optional[str] = None,
           rebuild: bool = False, verbose: bool = False, log=None) -> Stats:
    """Convenience wrapper: open (or create) the DB, ingest, close."""
    conn = open_db(db_path or default_db_path())
    try:
        return Ingester(conn, verbose=verbose, log=log).ingest(list(roots), index_dir=index_dir, rebuild=rebuild)
    finally:
        conn.close()
