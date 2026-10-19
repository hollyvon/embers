"""Build orchestration: node -> prompt pack -> summarizer -> flinch detector -> accepted / flagged / manual.

Idempotent: only pending (or interrupted-flagged) nodes are built; a re-run after a re-ingest rebuilds
only what the new data invalidated. Every node is committed on its own, so an interrupted or
budget-stopped build resumes where it left off.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import dag
from .dag import LEVELS, READY, PROSE_CLASSES
from .flinch import Baseline, FlinchReport, baseline_for_level, evaluate, extract_proper_nouns
from .ingest import now_iso
from .summarize import FALLBACK_TIER, BudgetExceeded, PromptPack, SummaryResult, Summarizer, PRICES, UNKNOWN_PRICE

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
    kind = meta.get("kind")
    kind_note = {"hearth": "   KIND: hearth (live conversations)",
                 "scheduled": "   KIND: scheduled (unattended automated runs)"}.get(kind, "")
    lines = ["LEVEL: %s   SPAN: %s   PARTS: %d%s" % (node["level"], _span(node["first_ts"], node["last_ts"]),
                                                     len(children), kind_note),
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
    workers: int = 1           # concurrent summarizer calls within a level (network-bound; SQLite stays single-writer)


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


def set_manual_summary(conn: sqlite3.Connection, node: sqlite3.Row, text: str) -> None:
    """Store an operator-written summary: backend `manual`, model `human`, status `ok`, indexed in nodes_fts.
    The metrics (report, earlier history) are kept and a {"tier": "manual"} entry is appended. Ancestors built
    over this node's placeholder are rebuilt by the next `mem build` (their child signature changes)."""
    text = text.strip()
    if not text:
        raise ValueError("empty summary")
    meta = json.loads(node["metrics_json"] or "{}")
    nbytes = len(text.encode("utf-8"))
    meta["history"] = (meta.get("history") or []) + [{"tier": "manual", "model": "human", "summary_bytes": nbytes,
                                                      "failed": [], "detail": {}, "evasion_hits": None}]
    meta["manual"] = True
    conn.execute("""UPDATE nodes SET summary=?, summary_bytes=?, model='human', backend='manual', status='ok',
                    metrics_json=?, created_at=? WHERE id=?""", (text, nbytes, json.dumps(meta), now_iso(), node["id"]))
    conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (node["id"],))
    conn.execute("INSERT INTO nodes_fts(node_id, summary) VALUES (?, ?)", (node["id"], text))


@dataclasses.dataclass
class _Job:
    """One node moving through its attempts. Owned by the main thread; workers only see snapshots."""
    node: sqlite3.Row
    pack: PromptPack
    kids: Optional[List[sqlite3.Row]]
    failures: int
    history: List[Dict[str, Any]]


class Builder:
    """Escalation per node: small tier (leaf) / mid (upper) -> rerun tier -> (only if the rerun was refused)
    small tier at effort medium -> manual. Every outcome is committed by the MAIN thread; worker threads
    only call `summarizer.summarize()` and `flinch.evaluate()`, so SQLite has a single writer and an
    interruption leaves finished nodes stored and unfinished ones untouched."""

    def __init__(self, conn: sqlite3.Connection, summarizer: Summarizer, log: Callable[[str], None] = lambda s: None,
                 workers: int = 1):
        self.conn = conn
        self.summarizer = summarizer
        self.log = log
        self.workers = max(1, int(workers or 1))
        self._baselines: Dict[str, Optional[Baseline]] = {}
        self._since: Dict[str, int] = {}

    def baseline(self, level: str) -> Optional[Baseline]:
        """Read from the DB (main thread) once per batch of 25 accepted nodes, not per node."""
        n = self._since.get(level, 10 ** 9)
        if level not in self._baselines or n >= 25:
            self._baselines[level] = baseline_for_level(self.conn, level)
            self._since[level] = 0
        return self._baselines[level]

    # ---- the pieces
    def _tier(self, level: str, step: int) -> str:
        if step <= 0:
            return "small" if level == "leaf" else "mid"
        if step == 1:
            return self.summarizer.rerun_tier(level)
        return FALLBACK_TIER

    def _attempt(self, job_pack: PromptPack, node_level: str, tier: str,
                 baseline: Optional[Baseline]) -> Tuple[SummaryResult, FlinchReport]:
        """WORKER THREAD. No database access."""
        res = self.summarizer.summarize(job_pack, tier)
        report = evaluate(res.text, level=node_level, source_bytes=job_pack.source_bytes,
                          proper_nouns=job_pack.proper_nouns, sensitivity=job_pack.sensitivity,
                          baseline=baseline, source_text=job_pack.text)
        return res, report

    def _submit(self, ex: ThreadPoolExecutor, inflight: Dict[Any, Tuple[_Job, int, str]], job: _Job) -> None:
        step = min(job.failures, 2)
        tier = self._tier(job.node["level"], step)
        p = job.pack
        if job.history:
            p = dataclasses.replace(p, feedback="it failed the quality checks (%s); cover every concrete name, "
                                                "decision and event, and do not omit or soften anything."
                                                % ", ".join(job.history[-1].get("failed") or ["?"]))
        fut = ex.submit(self._attempt, p, job.node["level"], tier, self.baseline(job.node["level"]))
        inflight[fut] = (job, step, tier)

    def _apply(self, job: _Job, step: int, tier: str, res: SummaryResult, report: FlinchReport) -> str:
        """MAIN THREAD. Record the attempt, store and commit. Returns ok | flagged (rerun pending) | manual."""
        node, conn = job.node, self.conn
        entry: Dict[str, Any] = {
            "tier": tier, "model": res.model, "summary_bytes": len(res.text.encode("utf-8")),
            "failed": report.failed, "detail": res.detail,
            "evasion_hits": next((c.detail.get("hits") for c in report.checks if c.name == "evasion"), None)}
        if step >= 2:
            entry.update(effort="medium", reason="rerun refused; one more try on the small tier at effort medium")
        if res.refused:
            entry["refused"] = True
        job.history.append(entry)
        if report.passed:
            _store(conn, node, job.pack, res, "ok", job.failures, report, job.history, job.kids)
            self._since[node["level"]] = self._since.get(node["level"], 0) + 1
            conn.commit()
            return "ok"
        job.failures += 1
        if step == 0:
            status = "flagged"
        elif step == 1 and res.refused:
            status = "flagged"
            entry["note"] = "refused; next try: small tier at effort medium"
        else:
            status = "manual"
        _store(conn, node, job.pack, res, status, job.failures, report, job.history, job.kids)
        conn.commit()
        self.log("%s %s %s: failed %s" % (node["level"], node["id"],
                 "-> manual" if status == "manual" else "-> rerun on " + self._tier(node["level"], job.failures),
                 ",".join(report.failed)))
        return status

    # ---- one level
    def run_level(self, level: str, pending: List[sqlite3.Row], remaining: Optional[int],
                  lv: Dict[str, int], out: Dict[str, Any]) -> int:
        """Build `pending` with up to `self.workers` calls in flight. Returns the number of nodes started.
        Sets out['stopped'] to 'limit' or 'budget' when it stops early. A worker exception stops dispatch,
        lets the calls already in flight finish and be stored, then re-raises (the failed node stays as it was)."""
        conn = self.conn
        nodes = iter(pending)
        inflight: Dict[Any, Tuple[_Job, int, str]] = {}
        started = 0
        exhausted = False
        first_exc: Optional[BaseException] = None
        ex = ThreadPoolExecutor(max_workers=self.workers)
        try:
            while True:
                while not out["stopped"] and first_exc is None and not exhausted and len(inflight) < self.workers:
                    node = next(nodes, None)
                    if node is None:
                        exhausted = True
                        break
                    if remaining is not None and started >= remaining:
                        out["stopped"] = "limit"
                        break
                    pack = make_pack(conn, node)
                    if pack is None:
                        lv["not_ready"] += 1
                        out["not_ready"] += 1
                        continue
                    kids = children_of(conn, node["id"]) if level != "leaf" else None
                    failures = node["attempts"]
                    history = json.loads(node["metrics_json"] or "{}").get("history", []) if failures else []
                    self._submit(ex, inflight, _Job(node, pack, kids, failures, history))
                    started += 1
                if not inflight:
                    break
                done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                for fut in done:
                    job, step, tier = inflight.pop(fut)
                    try:
                        res, report = fut.result()
                    except BudgetExceeded as e:
                        out["stopped"], out["stop_reason"] = "budget", str(e)      # node untouched: still pending/flagged
                        continue
                    except Exception as e:                                          # noqa: BLE001
                        first_exc = first_exc or e
                        continue
                    status = self._apply(job, step, tier, res, report)
                    if status == "flagged":
                        if out["stopped"] != "budget" and first_exc is None:
                            self._submit(ex, inflight, job)
                        continue                                                    # else left flagged: next build resumes it
                    lv[status] += 1
                    out["built"] += 1 if status == "ok" else 0
                    out["manual"] += 1 if status == "manual" else 0
                    if status == "ok" and job.failures:
                        out["flagged_then_ok"] += 1
        except BaseException:
            # Ctrl-C or an unexpected error in the main thread: stop everything not yet running, keep what is stored.
            ex.shutdown(wait=False, cancel_futures=True)
            conn.rollback()
            raise
        ex.shutdown(wait=True)
        if first_exc is not None:
            raise first_exc
        return started


def run_build(conn: sqlite3.Connection, summarizer: Summarizer, opts: BuildOptions,
              log: Callable[[str], None] = lambda s: None) -> Dict[str, Any]:
    """Sync the node structure, then build pending nodes level by level. Returns a stats dict."""
    if opts.dry_run and not conn.in_transaction:
        conn.execute("BEGIN")
    dag.ensure_schema(conn)
    scope = opts.thread_ids
    sync = dag.sync_structure(conn, opts.chunk_bytes, scope, months=scope is None)
    out: Dict[str, Any] = {"sync": sync, "levels": {}, "built": 0, "flagged_then_ok": 0, "manual": 0,
                           "not_ready": 0, "stopped": None, "dry_run": opts.dry_run, "workers": max(1, opts.workers)}
    if opts.dry_run:
        out["plan"] = plan_stats(conn, opts.levels, scope, summarizer)
        conn.rollback()
        return out
    conn.commit()
    b = Builder(conn, summarizer, log, workers=opts.workers)
    done = 0
    for level in LEVELS:                      # a level starts only when every node of the one below has finished
        if level not in opts.levels:
            continue
        lv = out["levels"].setdefault(level, {"ok": 0, "manual": 0, "not_ready": 0})
        done += b.run_level(level, _pending(conn, level, scope), (opts.limit - done) if opts.limit else None, lv, out)
        if out["stopped"]:
            break
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
