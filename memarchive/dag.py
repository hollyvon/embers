"""Layer D storage: the node DAG tables, leaf chunking, and structure sync.

Tables (additive; created on demand by `ensure_schema`, never by plain ingest):
    nodes        one row per summary node; level in leaf|session|thread|month,
                 status in pending|ok|flagged|manual
    node_sources the messages a LEAF covers (upper nodes cover children via parent_id)
    nodes_fts    FTS5 over the summaries of accepted (status=ok) nodes

Shape of the DAG:
    leaf     a contiguous run of one session's messages, bounded by a byte budget over the
             prose classes (human/assistant/thinking). Never splits a human prompt from the
             first assistant reply that answers it; always breaks at a compact_boundary and at
             a session boundary (a leaf belongs to exactly one session node).
    session  all leaves of one session file (subagent sessions get their own node).
    thread   all session nodes of a logical thread. A thread with ONE session has no node of
             its own: its session node doubles as the thread node.
    month    all thread-level nodes whose first_ts falls in one calendar month (UTC).

`sync_structure` (re)creates the node rows from the messages table, detecting stale nodes after
a re-ingest; it never calls a summarizer and never commits (the caller owns the transaction).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .ingest import now_iso

LEVELS = ("leaf", "session", "thread", "month")
STATUSES = ("pending", "ok", "flagged", "manual")
PROSE_CLASSES = ("human", "assistant", "thinking")
DEFAULT_CHUNK_BYTES = 24 * 1024
READY = ("ok", "manual")

DAG_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS nodes (
        id TEXT PRIMARY KEY,
        level TEXT NOT NULL CHECK (level IN ('leaf','session','thread','month')),
        parent_id TEXT,
        thread_id TEXT,
        session_id TEXT,
        first_uuid TEXT,
        last_uuid TEXT,
        first_ts TEXT,
        last_ts TEXT,
        n_sources INTEGER NOT NULL DEFAULT 0,
        source_bytes INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '',
        summary_bytes INTEGER NOT NULL DEFAULT 0,
        model TEXT,
        backend TEXT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','ok','flagged','manual')),
        attempts INTEGER NOT NULL DEFAULT 0,
        metrics_json TEXT,
        created_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS nodes_level_status ON nodes(level, status)",
    "CREATE INDEX IF NOT EXISTS nodes_parent ON nodes(parent_id)",
    "CREATE INDEX IF NOT EXISTS nodes_thread ON nodes(thread_id)",
    "CREATE INDEX IF NOT EXISTS nodes_session ON nodes(session_id)",
    """CREATE TABLE IF NOT EXISTS node_sources (
        node_id TEXT NOT NULL,
        uuid TEXT NOT NULL,
        PRIMARY KEY (node_id, uuid)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS node_sources_uuid ON node_sources(uuid)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(node_id UNINDEXED, summary, tokenize='unicode61')",
]


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the Layer D tables if absent. Statement by statement (not executescript) so a caller
    can wrap it in a transaction it later rolls back (`mem build --dry-run`)."""
    for stmt in DAG_STATEMENTS:
        conn.execute(stmt)
    cols = set(r[1] for r in conn.execute("PRAGMA table_info(threads)"))
    if cols and "kind" not in cols:          # (no threads table at all = a bare Layer-D test DB)
        conn.execute("ALTER TABLE threads ADD COLUMN kind TEXT")   # 'hearth' | 'scheduled' (see thread_kinds)


THREAD_KINDS = ("hearth", "scheduled")


def thread_kinds(conn: sqlite3.Connection) -> Dict[str, str]:
    """thread_id -> 'scheduled' when the thread's earliest non-subagent session was opened by a
    scheduled-task trigger (an unattended automated run), else 'hearth' (a live conversation). The
    distinction keeps automated runs from swamping the month view: each month gets one node per kind."""
    rows = conn.execute("""SELECT thread_id, opener FROM sessions WHERE is_subagent = 0 AND thread_id IS NOT NULL
                           ORDER BY thread_id, first_ts""").fetchall()
    kinds: Dict[str, str] = {}
    for tid, opener in rows:
        if tid not in kinds:
            kinds[tid] = "scheduled" if opener == "scheduled" else "hearth"
    return kinds


def _set_kind(conn: sqlite3.Connection, nid: str, kind: str) -> None:
    row = conn.execute("SELECT metrics_json FROM nodes WHERE id = ?", (nid,)).fetchone()
    meta = json.loads((row[0] if row else None) or "{}")
    if meta.get("kind") != kind:
        meta["kind"] = kind
        conn.execute("UPDATE nodes SET metrics_json = ? WHERE id = ?", (json.dumps(meta), nid))


def node_id(prefix: str, key: str) -> str:
    return prefix + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def month_node_id(mon: str, kind: str = "hearth") -> str:
    """'M-2026-10' for the hearth (live conversations), 'M-2026-10-scheduled' for unattended runs."""
    return "M-" + mon + ("" if kind == "hearth" else "-" + kind)


def month_of(ts: Optional[str]) -> str:
    return ts[:7] if ts and len(ts) >= 7 else "undated"


# ----------------------------------------------------------------------------- chunking

class Msg:
    __slots__ = ("uuid", "session_id", "ts", "line_no", "is_human", "subtype", "prose", "aprose", "thinking")

    def __init__(self, row: Sequence[Any]):
        (self.uuid, self.session_id, self.ts, self.line_no, human, self.subtype,
         self.prose, self.aprose, thinking) = row
        self.is_human = bool(human)
        self.thinking = bool(thinking)
        self.prose = self.prose or 0
        self.aprose = self.aprose or 0

    @property
    def order(self) -> Tuple[str, int]:
        return (self.ts or "", self.line_no)


class Chunk:
    """A planned leaf."""
    __slots__ = ("thread_id", "session_id", "uuids", "first_ts", "last_ts", "prose_bytes", "thinking", "id")

    def __init__(self, thread_id: str, session_id: str, msgs: List[Msg]):
        self.thread_id = thread_id
        self.session_id = session_id
        self.uuids = [m.uuid for m in msgs]
        stamps = [m.ts for m in msgs if m.ts]
        self.first_ts = min(stamps) if stamps else None
        self.last_ts = max(stamps) if stamps else None
        self.prose_bytes = sum(m.prose for m in msgs)
        self.thinking = any(m.thinking for m in msgs)
        self.id = node_id("L", self.uuids[0])


def _turns(segment: List[Msg]) -> List[List[List[Msg]]]:
    """Split one compaction-free segment into turns; each turn is a list of atoms (lists of messages).
    A turn opens at a human prompt. Its first atom runs from the prompt through the first assistant
    message that carries prose, so a prompt is never separated from the reply that answers it;
    every later message is its own atom (only matters when a turn alone exceeds the budget)."""
    raw: List[List[Msg]] = []
    for m in segment:
        if m.is_human or not raw:
            raw.append([m])
        else:
            raw[-1].append(m)
    turns = []
    for t in raw:
        if not t[0].is_human:
            turns.append([[m] for m in t])
            continue
        cut = len(t)
        for i, m in enumerate(t[1:], start=1):
            if m.aprose > 0:
                cut = i + 1
                break
        turns.append([t[:cut]] + [[m] for m in t[cut:]])
    return turns


def chunk_session(msgs: List[Msg], budget: int) -> List[List[Msg]]:
    """Group one session's messages (already in canonical order) into leaves."""
    segments: List[List[Msg]] = []
    for m in msgs:
        if m.subtype == "compact_boundary" or not segments:
            segments.append([m])
        else:
            segments[-1].append(m)
    leaves: List[List[Msg]] = []
    for seg in segments:
        cur: List[Msg] = []
        curb = 0
        for turn in _turns(seg):
            tb = sum(m.prose for atom in turn for m in atom)
            if curb + tb <= budget:
                cur += [m for atom in turn for m in atom]
                curb += tb
            elif tb <= budget:
                leaves.append(cur)
                cur = [m for atom in turn for m in atom]
                curb = tb
            else:  # one turn alone exceeds the budget: pack its atoms, never splitting an atom
                for atom in turn:
                    ab = sum(m.prose for m in atom)
                    if cur and curb + ab > budget:
                        leaves.append(cur)
                        cur, curb = [], 0
                    cur += atom
                    curb += ab
        if cur:
            leaves.append(cur)
    return [lf for lf in leaves if lf]


_MSG_SQL = """
SELECT m.uuid, m.session_id, m.ts, m.line_no, m.is_human_prompt, m.subtype,
       SUM(CASE WHEN b.class IN ('human','assistant','thinking') THEN b.nbytes ELSE 0 END),
       SUM(CASE WHEN b.class = 'assistant' THEN b.nbytes ELSE 0 END),
       MAX(CASE WHEN b.class = 'thinking' THEN 1 ELSE 0 END)
FROM messages m LEFT JOIN blocks b ON b.uuid = m.uuid
WHERE m.thread_id = ? GROUP BY m.uuid"""


def plan_chunks(conn: sqlite3.Connection, budget: int = DEFAULT_CHUNK_BYTES,
                thread_ids: Optional[Iterable[str]] = None) -> List[Chunk]:
    """The leaf plan for the given threads (default: all), deterministic for a given DB and budget."""
    if thread_ids is None:
        thread_ids = [r[0] for r in conn.execute("SELECT thread_id FROM threads ORDER BY thread_id")]
    out: List[Chunk] = []
    for tid in thread_ids:
        by_session: Dict[str, List[Msg]] = {}
        for row in conn.execute(_MSG_SQL, (tid,)):
            m = Msg(row)
            by_session.setdefault(m.session_id, []).append(m)
        for sid in sorted(by_session, key=lambda s: (min((m.ts or "~") for m in by_session[s]), s)):
            msgs = sorted(by_session[sid], key=lambda m: m.order)
            for leaf in chunk_session(msgs, budget):
                out.append(Chunk(tid, sid, leaf))
    return out


# ----------------------------------------------------------------------------- sync

def _drop_node(conn: sqlite3.Connection, nid: str) -> None:
    conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (nid,))
    conn.execute("DELETE FROM node_sources WHERE node_id = ?", (nid,))
    conn.execute("DELETE FROM nodes WHERE id = ?", (nid,))


def reset_pending(conn: sqlite3.Connection, nid: str) -> None:
    conn.execute("""UPDATE nodes SET status='pending', attempts=0, summary='', summary_bytes=0, model=NULL, backend=NULL,
                    metrics_json=NULL WHERE id = ?""", (nid,))
    conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (nid,))


def sync_leaves(conn: sqlite3.Connection, chunks: List[Chunk], scope: Optional[Set[str]] = None) -> Dict[str, int]:
    """Make the leaf rows match the plan. Unchanged leaves keep their summaries; a leaf whose extent
    changed (new messages, different budget, restitch) is reset to pending; leaves no longer in the
    plan are removed. `scope` limits orphan removal to those threads."""
    existing = {r["id"]: r for r in conn.execute("SELECT * FROM nodes WHERE level = 'leaf'")}
    wanted = {c.id: c for c in chunks}
    stats = {"leaf_new": 0, "leaf_changed": 0, "leaf_removed": 0, "leaf_kept": 0}
    for nid, row in existing.items():
        if nid not in wanted and (scope is None or row["thread_id"] in scope):
            _drop_node(conn, nid)
            stats["leaf_removed"] += 1
    redo: List[Chunk] = []
    for nid, c in wanted.items():
        row = existing.get(nid)
        if row is None:
            redo.append(c)
            stats["leaf_new"] += 1
        elif (row["thread_id"], row["session_id"], row["last_uuid"], row["n_sources"], row["source_bytes"]) != (
                c.thread_id, c.session_id, c.uuids[-1], len(c.uuids), c.prose_bytes):
            redo.append(c)
            stats["leaf_changed"] += 1
        else:
            stats["leaf_kept"] += 1
    for c in redo:   # free the uuids first: boundaries can shift between neighbouring leaves
        conn.execute("DELETE FROM node_sources WHERE node_id = ?", (c.id,))
    for c in redo:
        meta = json.dumps({"sensitivity": "high" if c.thinking else "normal"})
        if c.id in existing:
            reset_pending(conn, c.id)
            conn.execute("""UPDATE nodes SET thread_id=?, session_id=?, first_uuid=?, last_uuid=?, first_ts=?, last_ts=?,
                            n_sources=?, source_bytes=?, metrics_json=? WHERE id=?""",
                         (c.thread_id, c.session_id, c.uuids[0], c.uuids[-1], c.first_ts, c.last_ts, len(c.uuids),
                          c.prose_bytes, meta, c.id))
        else:
            conn.execute("""INSERT INTO nodes(id, level, thread_id, session_id, first_uuid, last_uuid, first_ts, last_ts,
                            n_sources, source_bytes, status, attempts, metrics_json, created_at)
                            VALUES (?, 'leaf', ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
                         (c.id, c.thread_id, c.session_id, c.uuids[0], c.uuids[-1], c.first_ts, c.last_ts, len(c.uuids),
                          c.prose_bytes, meta, now_iso()))
        conn.executemany("INSERT INTO node_sources(node_id, uuid) VALUES (?, ?)", [(c.id, u) for u in c.uuids])
    return stats


def children_sig(children: Sequence[sqlite3.Row]) -> str:
    h = hashlib.sha1()
    for ch in children:
        h.update(("%s:%s:%s;" % (ch["id"], ch["status"], hashlib.sha1(ch["summary"].encode("utf-8")).hexdigest()[:10]))
                 .encode("utf-8"))
    return h.hexdigest()


def _ensure_parent(conn: sqlite3.Connection, pid: str, level: str, children: List[sqlite3.Row],
                   thread_id: Optional[str], session_id: Optional[str]) -> None:
    """Create/refresh an upper node over `children` (ordered). Resets it to pending if its child set
    changed, a child is not ready, or a child's summary changed since it was built."""
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (pid,)).fetchone()
    stamps = [c["first_ts"] for c in children if c["first_ts"]]
    ends = [c["last_ts"] for c in children if c["last_ts"]]
    first_ts = min(stamps) if stamps else None
    last_ts = max(ends) if ends else None
    sens = "high" if any(_sensitivity(c) == "high" for c in children) else "normal"
    if row is None:
        conn.execute("""INSERT INTO nodes(id, level, thread_id, session_id, first_ts, last_ts, n_sources, status, attempts,
                        metrics_json, created_at) VALUES (?,?,?,?,?,?,?, 'pending', 0, ?, ?)""",
                     (pid, level, thread_id, session_id, first_ts, last_ts, len(children),
                      json.dumps({"sensitivity": sens}), now_iso()))
    else:
        cur = set(r[0] for r in conn.execute("SELECT id FROM nodes WHERE parent_id = ?", (pid,)))
        new = set(c["id"] for c in children)
        meta = json.loads(row["metrics_json"] or "{}")
        stale = False
        if row["status"] != "pending":
            if cur != new or any(c["status"] not in READY for c in children):
                stale = True
            elif meta.get("children_sig") != children_sig(children):
                stale = True
        if stale:
            reset_pending(conn, pid)
            meta = {}
        meta["sensitivity"] = sens
        conn.execute("""UPDATE nodes SET thread_id=?, session_id=?, first_ts=?, last_ts=?, n_sources=?, metrics_json=?
                        WHERE id=?""", (thread_id, session_id, first_ts, last_ts, len(children), json.dumps(meta), pid))
    conn.execute("UPDATE nodes SET parent_id = NULL WHERE parent_id = ? AND id NOT IN (%s)" % ",".join("?" * len(children)),
                 [pid] + [c["id"] for c in children])
    conn.executemany("UPDATE nodes SET parent_id = ? WHERE id = ?", [(pid, c["id"]) for c in children])


def _sensitivity(row: sqlite3.Row) -> str:
    try:
        return json.loads(row["metrics_json"] or "{}").get("sensitivity", "normal")
    except ValueError:
        return "normal"


def _rows(conn: sqlite3.Connection, ids: Iterable[str]) -> Dict[str, sqlite3.Row]:
    ids = list(ids)
    out: Dict[str, sqlite3.Row] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in conn.execute("SELECT * FROM nodes WHERE id IN (%s)" % ",".join("?" * len(part)), part):
            out[r["id"]] = r
    return out


def sync_upper(conn: sqlite3.Connection, months: bool = True) -> Dict[str, int]:
    """(Re)create session, thread and month nodes from the leaf rows. Bottom-up so each level reads the
    freshly reset state of the one below."""
    stats = {"session": 0, "thread": 0, "month": 0, "removed": 0}
    order = lambda r: (r["first_ts"] or "", r["id"])  # noqa: E731
    # sessions
    leaves = conn.execute("SELECT * FROM nodes WHERE level = 'leaf'").fetchall()
    by_session: Dict[str, List[sqlite3.Row]] = {}
    for lf in leaves:
        by_session.setdefault(lf["session_id"], []).append(lf)
    session_nodes: Dict[str, str] = {}
    for sid, lfs in by_session.items():
        lfs.sort(key=order)
        pid = node_id("S", sid)
        _ensure_parent(conn, pid, "session", lfs, lfs[0]["thread_id"], sid)
        session_nodes[sid] = pid
        stats["session"] += 1
    for r in conn.execute("SELECT id FROM nodes WHERE level = 'session'").fetchall():
        if r[0] not in set(session_nodes.values()):
            conn.execute("UPDATE nodes SET parent_id = NULL WHERE parent_id = ?", (r[0],))
            _drop_node(conn, r[0])
            stats["removed"] += 1
    # threads
    snodes = _rows(conn, session_nodes.values())
    by_thread: Dict[str, List[sqlite3.Row]] = {}
    for pid in session_nodes.values():
        by_thread.setdefault(snodes[pid]["thread_id"], []).append(snodes[pid])
    thread_level: Dict[str, str] = {}   # thread_id -> node id acting as the thread node
    thread_nodes: Set[str] = set()
    for tid, sns in by_thread.items():
        sns.sort(key=order)
        if len(sns) == 1:
            thread_level[tid] = sns[0]["id"]
            continue
        pid = node_id("T", tid)
        _ensure_parent(conn, pid, "thread", sns, tid, None)
        thread_level[tid] = pid
        thread_nodes.add(pid)
        stats["thread"] += 1
    for r in conn.execute("SELECT id FROM nodes WHERE level = 'thread'").fetchall():
        if r[0] not in thread_nodes:
            conn.execute("UPDATE nodes SET parent_id = NULL WHERE parent_id = ?", (r[0],))
            _drop_node(conn, r[0])
            stats["removed"] += 1
    # a session node that doubles as its thread node is flagged so; others lose a stale flag
    kinds = thread_kinds(conn)
    for tid, pid in thread_level.items():
        for sn in by_thread[tid]:
            _flag_double(conn, sn["id"], pid == sn["id"])
        kind = kinds.get(tid, "hearth")
        conn.execute("UPDATE threads SET kind = ? WHERE thread_id = ?", (kind, tid))
        _set_kind(conn, pid, kind)
    # months: one node per (month, kind) so unattended runs never swamp the hearth's month
    if months:
        tl = _rows(conn, thread_level.values())
        by_month: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
        for tid, pid in thread_level.items():
            by_month.setdefault((month_of(tl[pid]["first_ts"]), kinds.get(tid, "hearth")), []).append(tl[pid])
        month_nodes = set()
        for (mon, kind), rows in by_month.items():
            rows.sort(key=order)
            pid = month_node_id(mon, kind)
            _ensure_parent(conn, pid, "month", rows, None, None)
            _set_kind(conn, pid, kind)
            month_nodes.add(pid)
            stats["month"] += 1
        for r in conn.execute("SELECT id FROM nodes WHERE level = 'month'").fetchall():
            if r[0] not in month_nodes:
                conn.execute("UPDATE nodes SET parent_id = NULL WHERE parent_id = ?", (r[0],))
                _drop_node(conn, r[0])
                stats["removed"] += 1
    return stats


def _flag_double(conn: sqlite3.Connection, nid: str, double: bool) -> None:
    row = conn.execute("SELECT metrics_json FROM nodes WHERE id = ?", (nid,)).fetchone()
    meta = json.loads(row[0] or "{}")
    if bool(meta.get("thread_double")) != double:
        if double:
            meta["thread_double"] = True
        else:
            meta.pop("thread_double", None)
        conn.execute("UPDATE nodes SET metrics_json = ? WHERE id = ?", (json.dumps(meta), nid))


def sync_structure(conn: sqlite3.Connection, chunk_bytes: int = DEFAULT_CHUNK_BYTES,
                   thread_ids: Optional[List[str]] = None, months: bool = True) -> Dict[str, int]:
    """Plan leaves and bring every node row in line with the messages table. No commit."""
    chunks = plan_chunks(conn, chunk_bytes, thread_ids)
    stats = sync_leaves(conn, chunks, set(thread_ids) if thread_ids is not None else None)
    stats.update(sync_upper(conn, months=months))
    return stats


def thread_level_node(conn: sqlite3.Connection, thread_id: str) -> Optional[sqlite3.Row]:
    """The node standing for a thread: its own thread node, else its only session node."""
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id("T", thread_id),)).fetchone()
    if row is not None:
        return row
    rows = conn.execute("SELECT * FROM nodes WHERE level = 'session' AND thread_id = ?", (thread_id,)).fetchall()
    return rows[0] if len(rows) == 1 else None
