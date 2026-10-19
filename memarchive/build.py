"""Build orchestration: node -> prompt pack -> summarizer -> flinch detector -> accepted / flagged / manual.

Idempotent: only pending (or interrupted-flagged) nodes are built; a re-run after a re-ingest rebuilds
only what the new data invalidated. Every node is committed on its own, so an interrupted or
budget-stopped build resumes where it left off.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import dag
from .dag import LEVELS, READY, PROSE_CLASSES
from .flinch import Baseline, FlinchReport, baseline_for_level, evaluate, extract_proper_nouns
from .ingest import now_iso
from .summarize import BudgetExceeded, PromptPack, SummaryResult, Summarizer, PRICES, UNKNOWN_PRICE

NOISE_CLASSES = ("tool_use", "tool_result", "attachment", "system", "summary")
NOISE_PREVIEW = 120
MAX_NOISE_LINES = 60
EST_RATIO = 0.15           # dry-run guess: summary bytes / source bytes


def _preview(text: str, n: int = NOISE_PREVIEW) -> str:
    s = " ".join((text or "").split())
    return s[:n]


def _span(first: Optional[str], last: Optional[str]) -> str:
    return "%s -> %s" % ((first or "?")[:16], (last or "?")[:16])


# ----------------------------------------------------------------------------- packs

def leaf_pack(conn: sqlite3.Connection, node: sqlite3.Row, meta: Dict[str, Any]) -> PromptPack:
    rows = conn.execute(
        """SELECT b.* FROM node_sources ns JOIN blocks b ON b.uuid = ns.uuid WHERE ns.node_id = ?
           ORDER BY COALESCE(b.ts, ''), b.line_no, b.block_index""", (node["id"],)).fetchall()
    segments, noise = [], []
    lines = ["LEVEL: leaf   SPAN: %s   MESSAGES: %d" % (_span(node["first_ts"], node["last_ts"]), node["n_sources"]),
             "--- transcript ---"]
    for b in rows:
        cls = b["class"]
        text = (b["text"] or "").strip()
        if cls in PROSE_CLASSES:
            if text:
                segments.append((cls, text))
                lines.append("[%s] %s" % (cls, text))
        elif cls in NOISE_CLASSES and text:
            if cls == "tool_use":
                name = b["tool_name"] or "?"
                body = text[len(name):] if text.startswith(name) else text
                entry = "tool_use %s: %s" % (name, _preview(body))
            else:
                entry = "%s%s: %s" % (cls, "[%s]" % b["block_type"] if b["block_type"] else "", _preview(text))
            noise.append(entry)
        elif b["skipped_kind"] in ("image", "document"):
            noise.append("%s attached (%d bytes, not shown)" % (b["skipped_kind"], b["skipped_bytes"]))
    if len(noise) > MAX_NOISE_LINES:
        extra = len(noise) - MAX_NOISE_LINES
        noise = noise[:MAX_NOISE_LINES] + ["(+%d more tool/system items)" % extra]
    lines += ["· " + n for n in noise]
    lines.append("--- end ---")
    prose = "\n".join(t for _, t in segments)
    return PromptPack(level="leaf", text="\n".join(lines), source_bytes=node["source_bytes"],
                      proper_nouns=extract_proper_nouns(prose), sensitivity=meta.get("sensitivity", "normal"),
                      segments=segments, noise=noise, span=_span(node["first_ts"], node["last_ts"]),
                      n_sources=node["n_sources"])


def children_of(conn: sqlite3.Connection, node_id: str) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM nodes WHERE parent_id = ? ORDER BY COALESCE(first_ts, ''), id", (node_id,)).fetchall()


def upper_pack(node: sqlite3.Row, children: List[sqlite3.Row], meta: Dict[str, Any]) -> PromptPack:
    segments = []
    lines = ["LEVEL: %s   SPAN: %s   PARTS: %d" % (node["level"], _span(node["first_ts"], node["last_ts"]), len(children)),
             "--- parts ---"]
    for i, ch in enumerate(children, start=1):
        if ch["status"] == "ok":
            body = ch["summary"].strip()
        else:
            body = "[summary unavailable: this part was flagged for manual summarization (%d sources)]" % ch["n_sources"]
        head = "[part %d, %s, %s]" % (i, ch["level"], _span(ch["first_ts"], ch["last_ts"]))
        segments.append(("child", body))
        lines.append(head + "\n" + body)
    lines.append("--- end ---")
    joined = "\n".join(t for _, t in segments)
    return PromptPack(level=node["level"], text="\n".join(lines), source_bytes=len(joined.encode("utf-8")),
                      proper_nouns=extract_proper_nouns(joined), sensitivity=meta.get("sensitivity", "normal"),
                      segments=segments, span=_span(node["first_ts"], node["last_ts"]), n_sources=len(children))


def make_pack(conn: sqlite3.Connection, node: sqlite3.Row) -> Optional[PromptPack]:
    """The prompt pack for a node, or None while any child is not yet accepted/manual."""
    meta = json.loads(node["metrics_json"] or "{}")
    if node["level"] == "leaf":
        return leaf_pack(conn, node, meta)
    kids = children_of(conn, node["id"])
    if not kids or any(k["status"] not in READY for k in kids):
        return None
    return upper_pack(node, kids, meta)


# ----------------------------------------------------------------------------- build

@dataclasses.dataclass
class BuildOptions:
    levels: Sequence[str] = LEVELS
    thread_ids: Optional[List[str]] = None
    chunk_bytes: int = dag.DEFAULT_CHUNK_BYTES
    limit: int = 0
    dry_run: bool = False


def _pending(conn: sqlite3.Connection, level: str, scope: Optional[List[str]]) -> List[sqlite3.Row]:
    rows = conn.execute("""SELECT * FROM nodes WHERE level = ? AND status IN ('pending', 'flagged')
                           ORDER BY COALESCE(first_ts, ''), id""", (level,)).fetchall()
    if scope is not None and level != "month":
        keep = set(scope)
        rows = [r for r in rows if r["thread_id"] in keep]
    return rows


def _store(conn: sqlite3.Connection, node: sqlite3.Row, pack: PromptPack, res: SummaryResult, status: str,
           attempts: int, report: FlinchReport, history: List[Dict[str, Any]], kids: Optional[List[sqlite3.Row]]) -> None:
    meta = json.loads(node["metrics_json"] or "{}")
    meta.update(sensitivity=pack.sensitivity, report=report.as_dict(), history=history[-4:],
                usage={"input_tokens": res.input_tokens, "output_tokens": res.output_tokens, "cost_usd": res.cost_usd})
    if res.refused:
        meta["refused"] = res.detail
    if kids is not None:
        meta["children_sig"] = dag.children_sig(kids)
    conn.execute("""UPDATE nodes SET summary=?, summary_bytes=?, model=?, backend=?, status=?, attempts=?, metrics_json=?,
                    source_bytes=?, created_at=? WHERE id=?""",
                 (res.text, len(res.text.encode("utf-8")), res.model, res.backend, status, attempts, json.dumps(meta),
                  pack.source_bytes, now_iso(), node["id"]))
    conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (node["id"],))
    if status == "ok" and res.text:
        conn.execute("INSERT INTO nodes_fts(node_id, summary) VALUES (?, ?)", (node["id"], res.text))


class Builder:
    def __init__(self, conn: sqlite3.Connection, summarizer: Summarizer, log: Callable[[str], None] = lambda s: None):
        self.conn = conn
        self.summarizer = summarizer
        self.log = log
        self._baselines: Dict[str, Optional[Baseline]] = {}
        self._since: Dict[str, int] = {}

    def baseline(self, level: str) -> Optional[Baseline]:
        n = self._since.get(level, 10 ** 9)
        if level not in self._baselines or n >= 25:
            self._baselines[level] = baseline_for_level(self.conn, level)
            self._since[level] = 0
        return self._baselines[level]

    def process(self, node: sqlite3.Row, pack: PromptPack, kids: Optional[List[sqlite3.Row]]) -> Tuple[str, int]:
        """Summarize one node: first attempt (small tier for leaves, mid above), one rerun on mid if the
        flinch detector rejects it, then manual. Each outcome is committed, so an interruption between
        the attempts leaves the node `flagged` and the next build goes straight to the rerun.
        Returns (final status, number of rejected summaries)."""
        conn = self.conn
        failures = node["attempts"]
        history: List[Dict[str, Any]] = json.loads(node["metrics_json"] or "{}").get("history", []) if failures else []
        for attempt in range(min(failures, 1), 2):
            tier = "small" if (attempt == 0 and node["level"] == "leaf") else "mid"
            p = pack
            if history:
                p = dataclasses.replace(pack, feedback="it failed the quality checks (%s); cover every concrete name, "
                                                       "decision and event, and do not omit or soften anything."
                                                       % ", ".join(history[-1].get("failed") or ["?"]))
            res = self.summarizer.summarize(p, tier)
            report = evaluate(res.text, level=node["level"], source_bytes=pack.source_bytes,
                              proper_nouns=pack.proper_nouns, sensitivity=pack.sensitivity,
                              baseline=self.baseline(node["level"]))
            history.append({"tier": tier, "model": res.model, "summary_bytes": len(res.text.encode("utf-8")),
                            "failed": report.failed, "detail": res.detail,
                            "evasion_hits": next((c.detail.get("hits") for c in report.checks if c.name == "evasion"), None)})
            if report.passed:
                _store(conn, node, pack, res, "ok", failures, report, history, kids)
                self._since[node["level"]] = self._since.get(node["level"], 0) + 1
                conn.commit()
                return "ok", failures
            failures += 1
            status = "manual" if attempt == 1 else "flagged"
            _store(conn, node, pack, res, status, failures, report, history, kids)
            conn.commit()
            self.log("%s %s %s: failed %s" % (node["level"], node["id"], "-> manual" if status == "manual" else
                                              "-> rerun on mid", ",".join(report.failed)))
        return "manual", failures


def run_build(conn: sqlite3.Connection, summarizer: Summarizer, opts: BuildOptions,
              log: Callable[[str], None] = lambda s: None) -> Dict[str, Any]:
    """Sync the node structure, then build pending nodes level by level. Returns a stats dict."""
    if opts.dry_run and not conn.in_transaction:
        conn.execute("BEGIN")
    dag.ensure_schema(conn)
    scope = opts.thread_ids
    sync = dag.sync_structure(conn, opts.chunk_bytes, scope, months=scope is None)
    out: Dict[str, Any] = {"sync": sync, "levels": {}, "built": 0, "flagged_then_ok": 0, "manual": 0,
                           "not_ready": 0, "stopped": None, "dry_run": opts.dry_run}
    if opts.dry_run:
        out["plan"] = plan_stats(conn, opts.levels, scope, summarizer)
        conn.rollback()
        return out
    conn.commit()
    b = Builder(conn, summarizer, log)
    done = 0
    try:
        for level in LEVELS:
            if level not in opts.levels:
                continue
            lv = out["levels"].setdefault(level, {"ok": 0, "manual": 0, "not_ready": 0})
            for node in _pending(conn, level, scope):
                if opts.limit and done >= opts.limit:
                    out["stopped"] = "limit"
                    raise StopIteration
                pack = make_pack(conn, node)
                if pack is None:
                    lv["not_ready"] += 1
                    out["not_ready"] += 1
                    continue
                kids = children_of(conn, node["id"]) if level != "leaf" else None
                status, failures = b.process(node, pack, kids)
                done += 1
                lv[status] += 1
                out["built"] += 1 if status == "ok" else 0
                out["manual"] += 1 if status == "manual" else 0
                if status == "ok" and failures:
                    out["flagged_then_ok"] += 1
    except StopIteration:
        pass
    except BudgetExceeded as e:
        conn.rollback()
        out["stopped"] = "budget"
        out["stop_reason"] = str(e)
    out["spent_usd"] = round(getattr(summarizer, "spent_usd", 0.0), 6)
    return out


# ----------------------------------------------------------------------------- dry-run plan

def plan_stats(conn: sqlite3.Connection, levels: Sequence[str], scope: Optional[List[str]],
               summarizer: Summarizer) -> Dict[str, Any]:
    """What a build would do right now: per level, nodes pending, source bytes and estimated tokens.
    Upper-level sources are estimated from existing summaries or EST_RATIO of their own sources."""
    est: Dict[str, float] = {}     # node id -> estimated summary bytes
    plan: Dict[str, Any] = {}
    keep = set(scope) if scope is not None else None
    for level in LEVELS:
        rows = conn.execute("SELECT * FROM nodes WHERE level = ?", (level,)).fetchall()
        n_pending = 0
        src_total = 0.0
        for r in rows:
            if level == "leaf":
                src = float(r["source_bytes"])
            else:
                src = sum(est.get(k["id"], 0.0) for k in children_of(conn, r["id"]))
            est[r["id"]] = float(r["summary_bytes"]) if r["status"] in READY and r["summary_bytes"] else src * EST_RATIO
            is_pending = r["status"] in ("pending", "flagged")
            in_scope = keep is None or (level != "month" and r["thread_id"] in keep)
            if is_pending and in_scope:
                n_pending += 1
                src_total += src
        if level in levels:
            tokens = int(src_total / 4)
            tier = "small" if level == "leaf" else "mid"
            model = getattr(summarizer, "models", {}).get(tier)
            pin, pout = PRICES.get(model, UNKNOWN_PRICE) if model else (0.0, 0.0)
            plan[level] = {"nodes": len(rows), "pending": n_pending, "source_bytes": int(src_total),
                           "est_input_tokens": tokens,
                           "est_cost_usd": round((tokens * pin + tokens * EST_RATIO * pout) / 1e6, 4)}
    return plan
