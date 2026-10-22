"""Summarizer backends. The Anthropic backend is exercised ONLY with a mocked client: no network."""
import os
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from embers.build import BuildOptions, run_build
from embers.summarize import (AnthropicSummarizer, BudgetExceeded, PromptPack, StubSummarizer, make_summarizer)
from conftest import SMALL_THREAD, fresh_conn

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeClient:
    """Records calls; returns a fixed answer with configurable token usage."""

    def __init__(self, text="- Zorblax met Quibble.", tin=None, tout=200, stop_reason="end_turn"):
        self.calls = []
        self.messages = self
        self.text, self.tin, self.tout, self.stop_reason = text, tin, tout, stop_reason

    def create(self, **kw):
        self.calls.append(kw)
        tin = self.tin if self.tin is not None else (len(kw["system"]) + len(kw["messages"][0]["content"].encode("utf-8"))) // 3
        content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=self.text)]
        return SimpleNamespace(content=content, stop_reason=self.stop_reason,
                               stop_details=SimpleNamespace(category="general_harms") if self.stop_reason == "refusal" else None,
                               usage=SimpleNamespace(input_tokens=tin, output_tokens=self.tout))


def pack(level="leaf", n=2000):
    return PromptPack(level=level, text="[human] hello Zorblax\n" * (n // 20), source_bytes=n, proper_nouns=["Zorblax"],
                      segments=[("human", "hello Zorblax")])


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*a, **k):
        raise AssertionError("network access attempted in a test")
    monkeypatch.setattr(socket.socket, "connect", deny)


def test_tiers_pick_models_and_request_shape(monkeypatch):
    monkeypatch.delenv("MEM_MODEL_SMALL", raising=False)
    monkeypatch.delenv("MEM_MODEL_MID", raising=False)
    fc = FakeClient()
    s = AnthropicSummarizer(budget_usd=5, client=fc)
    r1 = s.summarize(pack(), "small")
    r2 = s.summarize(pack("session"), "mid")
    assert [c["model"] for c in fc.calls] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert r1.text == "- Zorblax met Quibble." and r1.backend == "anthropic" and r1.model == "claude-haiku-5-5"
    assert r2.model == "claude-sonnet-5-5"
    c = fc.calls[0]
    assert "temperature" not in c and "budget_tokens" not in str(c) and "Never refuse" in c["system"]
    assert c["messages"][0]["role"] == "user" and "Zorblax" in c["messages"][0]["content"]
    assert s.spent_usd == pytest.approx(r1.cost_usd + r2.cost_usd) and s.spent_usd > 0


def test_models_are_configurable_by_env_and_argument(monkeypatch):
    monkeypatch.setenv("MEM_MODEL_SMALL", "model-a")
    s = AnthropicSummarizer(budget_usd=1, client=FakeClient(), models={"mid": "model-b"})
    assert s.models == {"small": "model-a", "mid": "model-b"}


def test_budget_stops_before_a_call_that_could_exceed_it():
    fc = FakeClient(tout=4000)                        # ~110k input tokens (~$0.011) + 4k output (~$0.002) per call
    s = AnthropicSummarizer(budget_usd=0.03, client=fc, max_tokens=4096)   # the arithmetic below assumes a 4k cap
    done = 0
    with pytest.raises(BudgetExceeded):
        for _ in range(50):
            s.summarize(pack(n=300_000), "small")
            done += 1
    assert done == 2 and len(fc.calls) == done and s.spent_usd <= 0.03


def test_zero_or_missing_budget_is_refused():
    with pytest.raises(ValueError):
        AnthropicSummarizer(budget_usd=0, client=FakeClient())
    with pytest.raises(SystemExit):
        make_summarizer("anthropic", budget_usd=None)
    assert isinstance(make_summarizer(None), StubSummarizer)
    with pytest.raises(SystemExit):
        make_summarizer("nonsense")


def test_refusal_is_reported_as_an_empty_refused_summary():
    r = AnthropicSummarizer(budget_usd=1, client=FakeClient(stop_reason="refusal")).summarize(pack(), "small")
    assert r.text == "" and r.refused and r.detail["refusal_category"] == "general_harms"


def test_unknown_model_is_priced_conservatively():
    assert AnthropicSummarizer.cost("mystery", 1_000_000, 0) == 10.0


def test_build_stops_cleanly_on_budget_and_resumes_later(corpus, tmp_path):
    conn = fresh_conn(corpus, tmp_path)
    tid = conn.execute("SELECT thread_id FROM threads WHERE thread_id LIKE ?", (SMALL_THREAD + "%",)).fetchone()[0]
    fc = FakeClient(text="- Zorblax and Quibble met. " * 20, tout=4000)
    s = AnthropicSummarizer(budget_usd=0.01, client=fc)
    r = run_build(conn, s, BuildOptions(thread_ids=[tid]))
    assert r["stopped"] == "budget" and "budget" in r["stop_reason"]
    assert s.spent_usd <= 0.01 and r["spent_usd"] == pytest.approx(s.spent_usd, abs=1e-6)
    n_calls = len(fc.calls)
    assert 1 <= n_calls < 10
    left = conn.execute("SELECT COUNT(*) FROM nodes WHERE status IN ('pending','flagged')").fetchone()[0]
    assert left > 0
    # a later run with the stub finishes the job; nothing already done is redone
    done_before = conn.execute("SELECT COUNT(*) FROM nodes WHERE status='ok'").fetchone()[0]
    r2 = run_build(conn, StubSummarizer(), BuildOptions(thread_ids=[tid]))
    assert r2["stopped"] is None
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE status IN ('pending','flagged') AND level != 'month'").fetchone()[0] == 0
    assert r2["built"] >= left - 1 and done_before >= 0


def test_anthropic_package_is_optional_and_never_imported_by_the_stub_path():
    code = ("import sys; sys.path.insert(0, %r); import embers.cli, embers.build; from embers.summarize import make_summarizer; "
            "make_summarizer('stub'); assert 'anthropic' not in sys.modules") % REPO
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_stub_is_deterministic_and_flinch_flag_changes_output():
    p = pack(n=4000)
    assert StubSummarizer().summarize(p).text == StubSummarizer().summarize(p).text
    bad = StubSummarizer(flinch=True).summarize(p).text
    assert bad != StubSummarizer().summarize(p).text and len(bad) < 100
    assert StubSummarizer(flinch=True, flinch_tiers=("small",)).summarize(p, "mid").text == StubSummarizer().summarize(p).text
