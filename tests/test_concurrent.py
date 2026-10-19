"""Concurrent builds (--workers), per-level rerun tiers, the refusal fallback. No network: stub and mocked clients."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from memarchive.build import BuildOptions, run_build
from memarchive.summarize import AnthropicSummarizer, BudgetExceeded, StubSummarizer, SummaryResult, make_summarizer
from conftest import SMALL_THREAD, fresh_conn


def snapshot(conn):
    return conn.execute("SELECT id, level, status, attempts, summary, backend, parent_id FROM nodes ORDER BY id").fetchall()


class SlowStub(StubSummarizer):
    """Stub that sleeps a few ms and records how many calls overlapped and on which threads they ran."""

    def __init__(self, delay=0.004, **kw):
        super().__init__(**kw)
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.threads = set()

    def summarize(self, pack, tier="small"):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.threads.add(threading.get_ident())
        try:
            time.sleep(self.delay)
            return super().summarize(pack, tier)
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture(scope="module")
def reference(corpus, tmp_path_factory):
    """The whole corpus built sequentially: what every concurrent run must reproduce."""
    conn = fresh_conn(corpus, tmp_path_factory.mktemp("ref"))
    run_build(conn, StubSummarizer(), BuildOptions())
    yield conn
    conn.close()


def test_workers_8_matches_workers_1(corpus, tmp_path, reference):
    conn = fresh_conn(corpus, tmp_path)
    s = SlowStub()
    r = run_build(conn, s, BuildOptions(workers=8))
    assert r["stopped"] is None and r["manual"] == 0 and r["not_ready"] == 0 and r["workers"] == 8
    assert snapshot(conn) == snapshot(reference)
    assert s.max_active > 1                                   # it really was concurrent
    assert threading.get_ident() not in s.threads             # summarize ran in workers, the main thread did the writing
    assert conn.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0] == conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE status='ok'").fetchone()[0]


def test_workers_1_still_runs_one_at_a_time(corpus, tmp_path):
    conn = fresh_conn(corpus, tmp_path)
    s = SlowStub(delay=0.001)
    run_build(conn, s, BuildOptions(thread_ids=[_tid(conn)], workers=1))
    assert s.max_active == 1


def _tid(conn):
    return conn.execute("SELECT thread_id FROM threads WHERE thread_id LIKE ?", (SMALL_THREAD + "%",)).fetchone()[0]


def test_limit_means_at_most_n_nodes_stored(corpus, tmp_path):
    conn = fresh_conn(corpus, tmp_path)
    r = run_build(conn, SlowStub(), BuildOptions(limit=5, levels=("leaf",), workers=8))
    assert r["stopped"] == "limit" and r["built"] == 5
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE level='leaf' AND status='ok'").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE status='flagged'").fetchone()[0] == 0
    r = run_build(conn, SlowStub(), BuildOptions(limit=5, levels=("leaf",), workers=8))      # continues where it stopped
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE level='leaf' AND status='ok'").fetchone()[0] == 10


def test_upper_levels_wait_for_their_children(corpus, tmp_path):
    conn = fresh_conn(corpus, tmp_path)
    r = run_build(conn, SlowStub(), BuildOptions(limit=3, levels=("leaf",), workers=8))
    r = run_build(conn, SlowStub(), BuildOptions(levels=("session",), workers=8))
    assert r["built"] <= 1 and r["levels"]["session"]["not_ready"] > 0            # only sessions whose leaves are all done
    for sn in conn.execute("SELECT * FROM nodes WHERE level='session' AND status='ok'"):
        kids = conn.execute("SELECT status FROM nodes WHERE parent_id=?", (sn["id"],)).fetchall()
        assert kids and all(k[0] == "ok" for k in kids)


# ---- failure injection ---------------------------------------------------------------------------------
class Bomb(SlowStub):
    def __init__(self, exc, after=10, **kw):
        super().__init__(**kw)
        self.exc, self.after, self.n = exc, after, 0

    def summarize(self, pack, tier="small"):
        with self.lock:
            self.n += 1
            hit = self.n == self.after
        if hit:
            raise self.exc
        return super().summarize(pack, tier)


def _assert_consistent(conn):
    for n in conn.execute("SELECT * FROM nodes"):
        if n["status"] == "ok":
            assert n["summary"] and conn.execute("SELECT 1 FROM nodes_fts WHERE node_id=?", (n["id"],)).fetchone()
        else:
            assert n["status"] == "pending" and n["summary"] == "" and n["attempts"] == 0, n["id"]
            assert not conn.execute("SELECT 1 FROM nodes_fts WHERE node_id=?", (n["id"],)).fetchone()


def test_exception_in_one_worker_corrupts_and_skips_nothing(corpus, tmp_path, reference):
    conn = fresh_conn(corpus, tmp_path)
    with pytest.raises(RuntimeError, match="injected"):
        run_build(conn, Bomb(RuntimeError("injected")), BuildOptions(levels=("leaf",), workers=4))
    _assert_consistent(conn)
    done = conn.execute("SELECT COUNT(*) FROM nodes WHERE level='leaf' AND status='ok'").fetchone()[0]
    pend = conn.execute("SELECT COUNT(*) FROM nodes WHERE level='leaf' AND status='pending'").fetchone()[0]
    assert done >= 9 and pend >= 1                                       # siblings finished and were stored; the failed node is pending
    run_build(conn, StubSummarizer(), BuildOptions(workers=4))           # a healthy rerun completes the rest...
    assert snapshot(conn) == snapshot(reference)                         # ...to exactly the sequential result


def test_ctrl_c_leaves_finished_nodes_stored_and_the_rest_pending(corpus, tmp_path, reference):
    conn = fresh_conn(corpus, tmp_path)
    with pytest.raises(KeyboardInterrupt):
        run_build(conn, Bomb(KeyboardInterrupt(), after=12), BuildOptions(levels=("leaf",), workers=4))
    _assert_consistent(conn)
    assert 0 < conn.execute("SELECT COUNT(*) FROM nodes WHERE status='ok'").fetchone()[0] < 137
    run_build(conn, StubSummarizer(), BuildOptions(workers=4))
    assert snapshot(conn) == snapshot(reference)


# ---- budget under concurrency ---------------------------------------------------------------------------
class SlowClient:
    def __init__(self, text="- Zorblax met Quibble.", delay=0.03, tin=500, tout=100):
        self.messages = self
        self.text, self.delay, self.tin, self.tout = text, delay, tin, tout
        self.calls = 0
        self.lock = threading.Lock()

    def create(self, **kw):
        time.sleep(self.delay)
        with self.lock:
            self.calls += 1
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)], stop_reason="end_turn", stop_details=None,
                               usage=SimpleNamespace(input_tokens=self.tin, output_tokens=self.tout))


def _pack(n=3000):
    from memarchive.summarize import PromptPack
    return PromptPack(level="leaf", text="[human] hello Zorblax\n" * (n // 20), source_bytes=n, proper_nouns=["Zorblax"])


def test_budget_reservation_holds_with_calls_in_flight():
    probe = AnthropicSummarizer(budget_usd=1, client=SlowClient())
    worst = probe.estimate_cost(_pack(), "small")
    fc = SlowClient()
    s = AnthropicSummarizer(budget_usd=worst * 3.5, client=fc)         # room for three worst-case calls at once, not four
    results, errors = [], []

    def go():
        try:
            results.append(s.summarize(_pack(), "small"))
        except BudgetExceeded as e:
            errors.append(e)

    ts = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(results) == 3 and len(errors) == 9 and fc.calls == 3        # reservations, not just completed spend, were counted
    assert s.reserved_usd == pytest.approx(0, abs=1e-12) and s.spent_usd <= s.budget_usd and s.calls == 3


def test_failed_call_releases_its_reservation():
    class Down(SlowClient):
        def create(self, **kw):
            raise ConnectionError("down")
    s = AnthropicSummarizer(budget_usd=1, client=Down())
    with pytest.raises(ConnectionError):
        s.summarize(_pack(), "small")
    assert s.reserved_usd == pytest.approx(0, abs=1e-12) and s.spent_usd == 0


def test_build_budget_cap_holds_with_workers(corpus, tmp_path):
    conn = fresh_conn(corpus, tmp_path)
    probe = AnthropicSummarizer(budget_usd=1, client=SlowClient())
    fc = SlowClient()
    s = AnthropicSummarizer(budget_usd=0.012, client=fc)
    r = run_build(conn, s, BuildOptions(levels=("leaf",), workers=8))
    assert r["stopped"] == "budget" and s.spent_usd <= 0.012 and s.reserved_usd == pytest.approx(0, abs=1e-12)
    assert fc.calls == s.calls and fc.calls >= 1
    for n in conn.execute("SELECT * FROM nodes WHERE status NOT IN ('ok', 'pending', 'flagged', 'manual')"):
        raise AssertionError(n["id"])
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE level='leaf' AND status='pending'").fetchone()[0] > 0   # the rest wait


# ---- per-level rerun tier and the refusal fallback ------------------------------------------------------
class Scripted(StubSummarizer):
    """Behaviour per tier: 'good' | 'bad' (evasive) | 'refuse'. Rerun tier names come from `rerun`."""

    def __init__(self, script, rerun="rerun_leaf", **kw):
        super().__init__(**kw)
        self.script, self.rerun, self.calls = script, rerun, []

    def rerun_tier(self, level):
        return self.rerun if level == "leaf" else "mid"

    def summarize(self, pack, tier="small"):
        self.calls.append((pack.level, tier))
        how = self.script.get(tier, "good") if pack.level == "leaf" else "good"
        if how == "refuse":
            return SummaryResult(text="", model="m-" + tier, backend="stub", refused=True,
                                 detail={"stop_reason": "refusal", "refusal_category": "reasoning_extraction"})
        if how == "bad":
            return SummaryResult(text="I can't go into that.", model="m-" + tier, backend="stub")
        r = super().summarize(pack, "none")
        r.model = "m-" + tier
        return r


def _leaf_history(conn):
    row = conn.execute("SELECT * FROM nodes WHERE level='leaf' ORDER BY id LIMIT 1").fetchone()
    return row, json.loads(row["metrics_json"])["history"]


def test_refused_rerun_gets_one_more_try_on_small_at_medium_effort(corpus, tmp_path):
    conn = fresh_conn(corpus, tmp_path)
    s = Scripted({"small": "bad", "rerun_leaf": "refuse"})
    r = run_build(conn, s, BuildOptions(thread_ids=[_tid(conn)], levels=("leaf",)))
    assert r["manual"] == 0 and r["built"] > 0 and r["flagged_then_ok"] == r["built"]
    row, h = _leaf_history(conn)
    assert row["status"] == "ok" and row["attempts"] == 2
    assert [x["tier"] for x in h] == ["small", "rerun_leaf", "small_medium"]
    assert h[1]["refused"] and h[1]["detail"]["refusal_category"] == "reasoning_extraction" and "small tier" in h[1]["note"]
    assert h[2]["effort"] == "medium" and "rerun refused" in h[2]["reason"]


def test_refusal_then_failed_fallback_is_manual_after_three_attempts(corpus, tmp_path):
    conn = fresh_conn(corpus, tmp_path)
    s = Scripted({"small": "bad", "rerun_leaf": "refuse", "small_medium": "bad"})
    r = run_build(conn, s, BuildOptions(thread_ids=[_tid(conn)], levels=("leaf",)))
    row, h = _leaf_history(conn)
    assert r["built"] == 0 and r["manual"] > 0 and row["status"] == "manual" and row["attempts"] == 3
    assert [x["tier"] for x in h][-3:] == ["small", "rerun_leaf", "small_medium"]


def test_a_rerun_that_is_bad_but_not_refused_has_no_fallback(corpus, tmp_path):
    conn = fresh_conn(corpus, tmp_path)
    s = Scripted({"small": "bad", "rerun_leaf": "bad"})
    run_build(conn, s, BuildOptions(thread_ids=[_tid(conn)], levels=("leaf",)))
    row, h = _leaf_history(conn)
    assert row["status"] == "manual" and row["attempts"] == 2 and "small_medium" not in [c[1] for c in s.calls]


def test_interrupted_after_a_refused_rerun_resumes_at_the_fallback(corpus, tmp_path):
    conn = fresh_conn(corpus, tmp_path)

    class Stop(Exception):
        pass

    class Boom(Scripted):
        def summarize(self, pack, tier="small"):
            if tier == "small_medium":
                raise Stop()
            return super().summarize(pack, tier)

    with pytest.raises(Stop):
        run_build(conn, Boom({"small": "bad", "rerun_leaf": "refuse"}), BuildOptions(thread_ids=[_tid(conn)], levels=("leaf",)))
    row = conn.execute("SELECT * FROM nodes WHERE status='flagged'").fetchone()
    assert row is not None and row["attempts"] == 2
    s = Scripted({})
    run_build(conn, s, BuildOptions(thread_ids=[_tid(conn)], levels=("leaf",)))
    assert s.calls[0] == ("leaf", "small_medium")
    assert conn.execute("SELECT status FROM nodes WHERE id=?", (row["id"],)).fetchone()[0] == "ok"


def test_rerun_models_default_to_mid_and_are_configurable(monkeypatch):
    for k in ("MEM_MODEL_SMALL", "MEM_MODEL_MID", "MEM_MODEL_RERUN_LEAF", "MEM_MODEL_RERUN_UPPER"):
        monkeypatch.delenv(k, raising=False)
    s = AnthropicSummarizer(budget_usd=5, client=SlowClient())
    assert s.rerun_tier("leaf") == "mid" and s.rerun_tier("month") == "mid" and s.model_for("mid") == "claude-sonnet-5-5"
    monkeypatch.setenv("MEM_MODEL_RERUN_LEAF", "claude-opus-5-5")
    s = AnthropicSummarizer(budget_usd=5, client=SlowClient(), rerun_models={"upper": "claude-fable-5-1"})   # argument and env mix
    assert s.rerun_tier("leaf") == "rerun_leaf" and s.model_for("rerun_leaf") == "claude-opus-5-5"
    assert s.rerun_tier("session") == "rerun_upper" and s.model_for("rerun_upper") == "claude-fable-5-1"
    assert s.models == {"small": "claude-haiku-5-5", "mid": "claude-sonnet-5-5"}
    s = make_summarizer("anthropic", budget_usd=1, client=SlowClient(), rerun_models={"leaf": "x-model"})
    assert s.model_for("rerun_leaf") == "x-model"                       # flag beats env
    assert make_summarizer("stub").rerun_tier("leaf") == "mid"


def test_request_shape_of_rerun_and_fallback_tiers():
    class Rec(SlowClient):
        def create(self, **kw):
            self.last = kw
            return super().create(**kw)
    fc = Rec(delay=0)
    s = AnthropicSummarizer(budget_usd=5, client=fc, rerun_models={"leaf": "claude-opus-5-5"})
    s.summarize(_pack(), "rerun_leaf")
    assert fc.last["model"] == "claude-opus-5-5" and fc.last["output_config"] == {"effort": "medium"}
    s.summarize(_pack(), "small_medium")
    assert fc.last["model"] == "claude-haiku-5-5" and fc.last["output_config"] == {"effort": "medium"}
    s.summarize(_pack(), "small")
    assert fc.last["output_config"] == {"effort": "low"}
